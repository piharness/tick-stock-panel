"""`docstring 里的数必须能 index 到产物` —— 把 §9.38 的教训做成检查。

## 为什么需要它

`docs/decision-ledger.md` §9.38 的教训：`pwin.py` / `settle.py` 里「iid 会
窄 2.5–3.2 倍」「321 天 ⇒ 44–66」这些**实测值**，任何一份落盘产物里都复算
不出来——不是「数错了 20%」，是这些数从来没有出处，每次重写靠记忆。而本轮
真正值钱的发现（两条序列不能互相代用）之所以能被发现，也是因为加了
`verdict_rows` 落盘：**数有各自的产物，混用才会在 diff 里现形**。

所以规则只有一条：**读数形态的数字，要么指着一份产物，要么被封存**。

## 抓什么

单位是**行**，不是整段 docstring：

1. 先找「断言段落」——含 `实测`/`复算`/`落盘`/`测得` 且带数字的 docstring
   或连续的 `#:` 注释块。没有这些词的段落是设计说明，不是读数。
2. 段落里每一行含**读数形态**数字的，就是一个**断言行**：小数、带量纲的
   （`%`/`bp`/`倍`/`×`）、以及整数区间（`44–49`、`28–62`——`n_eff` 恰好是这类）。
   单独的整数不进来：`250` 天、`20` 日、`90%` 置信度是定义，不是量出来的东西。

粒度选行不选段落，是因为段落里一个错字就会让整段「变了」，而错字不该逼人
重新核数；反过来，行文本就是身份，改数必然换身份，**改字不换身份**。

## 两条检查（都不跑数据）

R1 · 覆盖。每个断言行必须二选一：

- **已锚定**：在 `docs/number-provenance.json` 的 `entries` 里，带产物路径与
  逐项 `claimed` 值；
- **已封存**：在 `frozen` 名单里（存整行归一化文本）。

都没有 ⇒ `UNREGISTERED`（新写的读数没出处）。改了数 ⇒ 旧行进 `STALE`、新行
进 `UNREGISTERED`，两条并排就是一份 diff。**「改数」和「新增数」是同一个
失败模式**，所以两者都红。

R2 · 锚定完整性。每个 `entries` 必须（a）该行仍在文件里（否则 `STALE_ENTRY`），
（b）`claimed` 值出现在该行文本里（否则 registry 与 docstring 已不同步），
（c）产物存在时逐项与产物比对（`VALUE_MISMATCH` 带偏离量），（d）产物欠缺时
记 `SKIP`，不假装通过。

## 它不是万能的

- **抓不到「语义混用」**：把增量序列的 `28–62` 贴到 verdict 上，数值仍在容差
  内就不会红。这一层只能靠产物分块（`summary` / `verdict_rows`）。
- **抓不到没写「实测」的段落**：库常数、闭式推导（`267/400`、`2/3`）不在射程
  内。那类要靠符号测试，不是文本检查。
- **封存名单是债**：82 处历史读数今天仍然没有产物（见登记表 `frozen`），封存只保证它们**不能再
  悄悄变**，不保证它们是对的。数量写进 `docs/decision-ledger.md` §9.39，每锚定
  一处就从名单里删一处（`--rebaseline` 会自动把它移出 frozen）。
- **rebaseline 是逃生门**：它能一键把新数封存。它挡住的是「悄悄改」，不是
  「故意改」；后者只能靠评审。

## 用法

    python scripts/check_number_provenance.py               # 人读的报告
    python scripts/check_number_provenance.py --json out.json
    python scripts/check_number_provenance.py --list        # 列出全部断言行
    python scripts/check_number_provenance.py --rebaseline  # 重封存（打印增删）

退出码非零 ⇔ 有 `UNREGISTERED` / `STALE` / `STALE_ENTRY` / `VALUE_MISMATCH`
/ `REGISTRY_DRIFT`。`SKIP` 不算失败（产物不在时 CI 仍要能跑）。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 段落判据：出现这些词之一 ⇒ 这段文字在声称某件事是量出来的。
#: `落盘` 也算——它本身就是出处声明，漏掉它等于放过最该检查的一句。
CLAIM_KEYWORDS = ("实测", "复算", "落盘", "测得")

#: 读数形态：小数，或带量纲的整数。`250 天`/`20 日` 这类结构常数**不**进来。
#: 裸小数也算：`lag1 0.66–0.87`、`−0.00237` 这类不带量纲的读数很常见，
#: 只认带量纲的会漏掉正好是被引用最多的那类区间。整数（`250`/`20`/`146`）
#: 一律不认——它们是结构常数。
READING = re.compile(r"\d+\.\d+|\d+\s*(?:%|bp|倍|×)|\d+\s*[–—-]\s*\d+")

#: 置信度/显著水平的**定义**，不是读数：一行里只剩这些百分数就不是断言行。
#: 不排除它们，「单侧 95% 下界」「5% 置信水平」这类纯定义会把名单灌满。
STRUCTURAL_PCT = {"0", "1", "5", "50", "90", "95", "99", "100"}

SCOPE_GLOBS = (
    "backend/app/decision/*.py",
    "scripts/probe_*.py",
    "backend/tests/test_decision_*.py",
)


@dataclass
class Line:
    """一条断言行。**归一化文本就是身份**——改数必换身份，错字不换。"""

    file: str
    text: str
    where: str          #: 「哪个 docstring / 注释块」，仅供人读，不参与判定
    lineno: int

    @property
    def key(self) -> str:
        return _sha(self.text)

    def as_dict(self) -> dict:
        return {"file": self.file, "text": self.text, "where": self.where}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_DATE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")    #: `2026-10-08` 不是区间
_SECTION_REF = re.compile(r"§\s*\d+(?:\.\d+)*")   #: `§9.23` 不是读数
_VERSION = re.compile(r"\bv\d+(?:\.\d+)+")          #: `v13c` / `v6.1` 也不是


def readings(text: str) -> list[str]:
    """一行里算「读数」的数字串：小数，或非结构百分数/量纲数。

    先剔掉章节号（`§6.1`）与版本号（`v13c`）——它们长得像小数，但一行的
    主题如果是「见 §6.1」，把它当读数只会往名单里灌噪声。
    """
    text = _VERSION.sub(" ", _SECTION_REF.sub(" ", _DATE.sub(" ", text)))
    out: list[str] = []
    for m in READING.finditer(text):
        tok = m.group(0)
        num = re.match(r"\d+(?:\.\d+)?", tok).group(0)  # type: ignore[union-attr]  # noqa: E501
        if tok.endswith("%") and "." not in num and num in STRUCTURAL_PCT:
            continue
        out.append(tok)
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


def _blocks(path: Path) -> list[tuple[str, int, list[tuple[int, str]]]]:
    """抽出（名称, 起始行, [(行号, 行文本)]）——docstring 与 `#:` 注释块都算。

    `settle.py` 的核偏差常数写在 `#:` 注释里（不是 docstring），只扫 docstring
    会正好漏掉本轮改过的那两处。
    """
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    blocks: list[tuple[str, int, list[tuple[int, str]]]] = []
    seen: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        name = "<module>" if isinstance(node, ast.Module) else node.name
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:  # 同名（嵌套函数 / 参数化）加序号，避免人读时混淆
            name = f"{name}#{seen[name]}"
        start = getattr(node, "lineno", 1)
        blocks.append((name, start, [(start + i, ln) for i, ln in enumerate(doc.splitlines())]))

    run: list[tuple[int, str]] = []
    block_no = 0
    for i, line in enumerate(src.splitlines(), start=1):
        s = line.strip()
        if s.startswith("#:"):
            run.append((i, s[2:].strip()))
            continue
        if run:
            block_no += 1
            blocks.append((f"comment#{block_no}", run[0][0], run))
            run = []
    if run:
        block_no += 1
        blocks.append((f"comment#{block_no}", run[0][0], run))
    return blocks


def scan(root: Path) -> list[Line]:
    """全部断言行。空行与纯文字行不进来。"""
    out: list[Line] = []
    for pat in SCOPE_GLOBS:
        for path in sorted(root.glob(pat)):
            rel = path.relative_to(root).as_posix()
            for name, _start, lines in _blocks(path):
                body = "\n".join(t for _, t in lines)
                if not (any(k in body for k in CLAIM_KEYWORDS)
                        and any(readings(t) for _, t in lines)):
                    continue          # 设计说明段落，不是读数
                for lineno, text in lines:
                    if readings(text):
                        out.append(Line(rel, _norm(text), name, lineno))
    return out


# --------------------------------------------------------------------------- #
# 产物取值：`a.b` / `rows[k=v].field` / `acf.1`，可配 agg
# --------------------------------------------------------------------------- #

class PathError(Exception):
    """产物里没有这个键，或筛选后为空——都当失败，不返回 None。"""


_SEG = re.compile(r"^(?P<name>[\w/%.−-]*)(?:\[(?P<sel>[^\]]*)\])?$")


def _eq(a: object, b: object) -> bool:
    try:
        return float(a) == float(b)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(a) == str(b)


def resolve(doc: object, path: str) -> list[object]:
    """按 `.` 分段取值；`[]` 里是 `键=值` 过滤；返回叶子列表。"""
    def walk(node: object, parts: list[str]) -> list[object]:
        if not parts:
            return [node]
        head, rest = parts[0], parts[1:]
        m = _SEG.match(head)
        if m is None:
            raise PathError(f"段语法非法: {head}")
        name, sel = m.group("name") or "", m.group("sel")
        if name:
            if isinstance(node, list):
                if not name.isdigit():
                    raise PathError(f"列表下标须为数字: {head}")
                node = node[int(name)]
            elif isinstance(node, dict):
                if name not in node:
                    raise PathError(f"缺键 {name}（在 {head}）")
                node = node[name]
            else:
                raise PathError(f"{head} 之上不是容器")
        if sel is None:
            return walk(node, rest)
        if not isinstance(node, list):
            raise PathError(f"{head} 不是列表，不能过滤")
        preds = [p.split("=", 1) for p in sel.split(",") if p.strip()]
        out: list[object] = []
        for item in node:
            if not isinstance(item, dict):
                raise PathError(f"{head} 的元素不是对象")
            if all(k.strip() in item and _eq(item[k.strip()], v) for k, v in preds):
                out.extend(walk(item, rest))
        if not out:
            raise PathError(f"过滤 {sel} 在 {name} 上无匹配")
        return out

    return walk(doc, path.split("."))


def reduce_leaves(leaves: list[object], agg: str | None) -> float:
    if not all(isinstance(x, (int, float)) for x in leaves):
        raise PathError(f"叶子不是数：{leaves[:3]}")
    vals = [float(x) for x in leaves]  # type: ignore[arg-type]
    if agg == "min":
        return min(vals)
    if agg == "max":
        return max(vals)
    if agg == "mean":
        return sum(vals) / len(vals)
    if len(vals) != 1:
        raise PathError(f"多值 {len(vals)} 个但没给 agg")
    return vals[0]


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #

@dataclass
class Report:
    claims: int
    anchored: int
    frozen: int
    failures: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {"claims": self.claims, "anchored": self.anchored,
                "frozen": self.frozen, "failures": self.failures,
                "skipped": self.skipped, "ok": self.ok}


def load_registry(path: Path) -> dict:
    if not path.exists():
        return {"entries": [], "frozen": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _claimed_in_text(text: str, claimed: float) -> bool:
    """`claimed` 必须能在该行找到同写法的数（`2.57` / `44` / `0.817`）。"""
    forms = {f"{claimed:g}"}
    if float(claimed) == int(claimed):
        forms.add(str(int(claimed)))
    return any(f in text for f in forms)


def check(root: Path, registry: dict) -> Report:
    lines = scan(root)
    by_key = {(ln.file, ln.key): ln for ln in lines}
    rep = Report(len(lines), 0, 0)

    entries = {(e["file"], _sha(e["text"])): e for e in registry.get("entries", [])}
    frozen = {(f["file"], _sha(f["text"])): f for f in registry.get("frozen", [])}

    for key, entry in entries.items():
        ln = by_key.get(key)
        if ln is None:
            rep.failures.append({
                "kind": "STALE_ENTRY", "file": key[0], "text": entry["text"],
                "detail": "这一行已不存在——数被改过、或该行被删（registry 要跟着改）"})
            continue
        rep.anchored += 1
        for c in entry.get("checks", []):
            claimed = float(c["claimed"])
            if not _claimed_in_text(ln.text, claimed):
                rep.failures.append({
                    "kind": "REGISTRY_DRIFT", "file": key[0], "text": ln.text,
                    "detail": f"registry 写 {c['claimed']}，该行找不到这个写法"})
            art = entry.get("artifact")
            if not art:
                rep.failures.append({"kind": "REGISTRY_DRIFT", "file": key[0],
                                     "text": ln.text, "detail": "entry 缺 artifact"})
                continue
            apath = root / art
            if not apath.exists():
                rep.skipped.append({"file": key[0], "text": ln.text,
                                    "artifact": art, "path": c["path"]})
                continue
            try:
                got = reduce_leaves(resolve(
                    json.loads(apath.read_text(encoding="utf-8")), c["path"]),
                    c.get("agg"))
            except (PathError, json.JSONDecodeError) as exc:
                rep.failures.append({
                    "kind": "VALUE_MISMATCH", "file": key[0], "text": ln.text,
                    "detail": f"{art}#{c['path']}: {exc}"})
                continue
            tol = float(c.get("tol", 0.005))
            if abs(got - claimed) > tol + 1e-12:
                rep.failures.append({
                    "kind": "VALUE_MISMATCH", "file": key[0], "text": ln.text,
                    "detail": (f"{art}#{c['path']} = {got:.6g}，docstring 写 "
                               f"{c['claimed']}，差 {abs(got - claimed):.6g} > {tol}")})

    for key, f in frozen.items():
        if key not in by_key:
            rep.failures.append({
                "kind": "STALE", "file": key[0], "text": f["text"],
                "detail": "封存的这行读数的文字变了或行没了——改数必须重新核"})
            continue
        rep.frozen += 1

    for ln in lines:
        key = (ln.file, ln.key)
        if key in entries or key in frozen:
            continue
        rep.failures.append({
            "kind": "UNREGISTERED", "file": ln.file, "text": ln.text,
            "where": f"{ln.where}:{ln.lineno}",
            "detail": "读数没有出处，也没进封存名单"})
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--registry", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None, dest="json_out")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rebaseline", action="store_true")
    ap.add_argument("--show", type=int, default=40,
                    help="最多打印多少条失败（0 = 全打）")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    reg_path = args.registry or (root / "docs" / "number-provenance.json")
    registry = load_registry(reg_path)

    if args.list:
        for ln in scan(root):
            print(f"{ln.file}:{ln.lineno}\t{ln.where}\t{ln.text[:90]}")
        return 0

    if args.rebaseline:
        return _rebaseline(root, reg_path, registry)

    rep = check(root, registry)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(rep.as_dict(), ensure_ascii=False,
                                            indent=1), encoding="utf-8")
    print(f"断言行 {rep.claims}：已锚定 {rep.anchored}，已封存 {rep.frozen}，"
          f"失败 {len(rep.failures)}，跳过 {len(rep.skipped)}")
    shown = rep.failures if args.show == 0 else rep.failures[:args.show]
    for f in shown:
        where = f"  [{f['where']}]" if f.get("where") else ""
        print(f"  ✗ {f['kind']:<15} {f['file']}{where}\n      {f['text'][:100]}"
              f"\n      → {f['detail']}")
    if len(shown) < len(rep.failures):
        print(f"  … 还有 {len(rep.failures) - len(shown)} 条，用 --show 0 看全")
    for s in rep.skipped:
        print(f"  · SKIP 产物不在：{s['artifact']}（{s['file']}）")
    print("判定：" + ("✓ 每个读数都有出处或已封存" if rep.ok else "✗ 有没出处的读数"))
    return 0 if rep.ok else 1


def _rebaseline(root: Path, reg_path: Path, registry: dict) -> int:
    """重写封存名单。**只封存没有产物可指的断言行**；已锚定的不动。"""
    lines = scan(root)
    entries = {(e["file"], _sha(e["text"])) for e in registry.get("entries", [])}
    old = {(f["file"], _sha(f["text"])): f for f in registry.get("frozen", [])}
    frozen, added, changed = [], [], []
    for ln in lines:
        key = (ln.file, ln.key)
        if key in entries:
            continue
        if key not in old:
            added.append(f"{ln.file}: {ln.text[:70]}")
        frozen.append({"file": ln.file, "text": ln.text,
                       "where": ln.where, "sha": ln.key})
    new_keys = {(f["file"], _sha(f["text"])) for f in frozen}
    for key, f in old.items():
        if key not in new_keys:
            changed.append(f"{f['file']}: {f['text'][:70]}")
    registry["frozen"] = frozen
    registry["_doc"] = _REGISTRY_DOC
    reg_path.write_text(json.dumps(registry, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(f"封存 {len(frozen)} 条断言行（新增 {len(added)}，消失/改文 {len(changed)}）")
    for name in added:
        print(f"  + {name}")
    for name in changed:
        print(f"  - {name}")
    print("提醒：rebaseline 只写文本，不证明数是对的；能锚定到产物的请进 entries。")
    return 0


_REGISTRY_DOC = (
    "断言行的出处登记。entries = 能 index 到落盘产物的读数（每次运行重算比对）；"
    "frozen = 目前没有产物的历史读数（只保证文本不再变）。"
    "由 scripts/check_number_provenance.py 读取，见 docs/decision-ledger.md §9.39。"
)


if __name__ == "__main__":
    sys.exit(main())
