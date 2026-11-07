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

## 身份 = 全部数字，不只是「读数形态」（13m 改正）

§9.39 初版把整行归一化文本当身份，正文却写着「错字不换身份」——**那是假的**：
`sha(_norm(text))` 里包含所有字，加一个「了」就换了身份，于是错字和改数都红，
检查退化成「任何编辑都要重核」，正好是它想避免的摩擦。

13j 把身份换成「读数向量」（`0.42 倍` 这种 token 的有序序列）。方向对了，**词法
错了**：`READING` 是给**成员资格**（哪一行算断言行）写的，它刻意不收裸整数
（`250 天`、`20 日` 是定义，收进来名单就没法读了）。拿它当身份，等于让
「323 个可评估日」改成「324 个」不红。13m 在真实仓库上量了一遍：94 条断言行共
315 个数字，**136 个改了不换身份**（120 个裸整数 + 12 个章节号 + 3 个日期数字
+ 1 个版本号），62/94 行至少有一个——「改任何一个数必换身份」当时是假的，和
§9.39/§9.40 是同一个病：性质写进散文，没写进断言。

根因是把两件事挤进一个正则：

- **成员资格**要**窄**：只认小数 / 带量纲 / 区间，否则名单长到没人读；
- **身份**要**宽**：行里每个数都该承重，否则改数不红。

所以 13m 拆开：

- 成员资格 = `readings()`（不变，所以断言行仍是 94 条）；
- 身份 = `digits()`：剔掉**文档坐标**（`§9.39` 的章节号、`v13c` 的版本号）之后，
  行里**全部**数字，按出现顺序。于是
  - 错字、补标点、改措辞 ⇒ 数字序列不变 ⇒ **不红**（只进 `PROSE_DRIFT` 提示）；
  - 改任何一位数 ⇒ `STALE`/`STALE_ENTRY` + `UNREGISTERED` 并排。
- 豁免只有那两类，且由 `test_only_doc_coordinates_are_exempt` 钉住：**日期不豁免**
  （「2026-11-05 的快照」是对数据新鲜度的声称，改日期该重核）。

**代价要写清楚**：把「偏窄 7/8」改成「偏宽 7/8」这种**只改字、不改数**的语义
翻转，硬检查不会红——它只会在 `PROSE_DRIFT` 里列出来。这一层是留给人和评审的，
不是留给脚本的（§9.40 会写）。

## 两条检查（都不跑数据）

R1 · 覆盖。每个断言行必须二选一：

- **已锚定**：在 `docs/number-provenance.json` 的 `entries` 里，带产物路径与
  逐项 `claimed` 值；
- **已封存**：在 `frozen` 名单里（存整行文本，身份由文本现算）。

都没有 ⇒ `UNREGISTERED`（新写的读数没出处）。改了数 ⇒ 旧行进 `STALE`、新行
进 `UNREGISTERED`，两条并排就是一份 diff。**「改数」和「新增数」是同一个
失败模式**，所以两者都红。

R2 · 锚定完整性。每个 `entries` 必须（a）该行仍在文件里（否则 `STALE_ENTRY`），
（b）`claimed` 值出现在该行文本里（否则 registry 与 docstring 已不同步），
（c）产物存在时逐项与产物比对（`VALUE_MISMATCH` 带偏离量），（d）产物欠缺时
记 `SKIP`，不假装通过。

## 同指纹必须记账

身份只由读数决定，于是「同一文件里两行写着同一组数」会撞键（本仓库实测 6 组，
例如 `pwin.py` 两行都只含 `3 倍`）。撞键不能互相顶替：登记几条就得出现几次。
少一次 ⇒ `STALE`/`STALE_ENTRY`；多一次 ⇒ `UNREGISTERED`。否则删掉其中一行会
静默通过。

撞键也可能是**真的两句话说了同一组数**（`probe_pwin_variance.py` 的模块
docstring 与 `_hac_stats` 都写「7 条偏窄 0.7–17.5%、1 条偏宽 10.1%」），一句
锚定、一句封存是合法的：**计数按指纹合并**，不做「二选一」。核锚定项时按文本
相似度挑回它自己那一行，避免拿隔壁那行过审。

## 它不是万能的

- **抓不到「语义混用」**：把增量序列的 `28–62` 贴到 verdict 上，数值仍在容差
  内就不会红。这一层只能靠产物分块（`summary` / `verdict_rows`）。
- **抓不到只改字的语义翻转**：见上。`PROSE_DRIFT` 只提示，不判定。
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

`root` / `globs` / `claim_keywords` 都可注入（`Config`，命令行上是
`--root` 与 `--globs`），测试用临时仓库验证「它会不会红」，见
`backend/tests/test_decision_number_provenance.py`。

退出码非零 ⇔ 有 `UNREGISTERED` / `STALE` / `STALE_ENTRY` / `VALUE_MISMATCH`
/ `REGISTRY_DRIFT`。`SKIP` 与 `PROSE_DRIFT` 不算失败（前者是产物不在，后者是
数没变）。
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import re
import sys
from collections import Counter
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

_FAILURES = ("UNREGISTERED", "STALE", "STALE_ENTRY", "VALUE_MISMATCH", "REGISTRY_DRIFT")


@dataclass(frozen=True)
class Config:
    """扫描口径。默认值就是本仓库的口径；测试用临时仓库时换 `globs`。

    读数词法与结构性百分数**不**做成旋钮：它们是规则本身，改规则应该改代码
    并重新封存，而不是在测试里调参。
    """

    globs: tuple[str, ...] = SCOPE_GLOBS
    claim_keywords: tuple[str, ...] = CLAIM_KEYWORDS


@dataclass
class Line:
    """一条断言行。**数字向量就是身份**——任何一位数变了就换身份，错字不换。"""

    file: str
    text: str
    where: str          #: 「哪个 docstring / 注释块」，仅供人读，不参与判定
    lineno: int

    @property
    def vector(self) -> tuple[str, ...]:
        return tuple(digits(self.text))

    @property
    def key(self) -> str:
        return fingerprint(self.text)

    def as_dict(self) -> dict:
        return {"file": self.file, "text": self.text, "where": self.where}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_DATE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")    #: `2026-10-08` 不是区间
_SECTION_REF = re.compile(r"§\s*\d+(?:\.\d+)*")   #: `§9.23` 不是读数
#: `v13c` / `v6.1` / `v14` —— 文档坐标，不是读数。`[a-z]?` 是 13m 补的：
#: 初版只认带小数点的 `v6.1`，于是 `v13c` 的 `13` 留在身份里，升版本会假红。
_VERSION = re.compile(r"\bv\d+(?:\.\d+)*[a-z]?\b")


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


#: 身份用**全部**数字，不是「读数形态」那些——见模块文档「身份 = 全部数字」。
#: 全角数字不是另一个数：`０.４２` 与 `0.42` 同一身份（只置换数字与小数点，
#: 不用 NFKC——NFKC 会把 `㎡` 拆成 `m2`，凭空造出一个数字）。
DIGITS = re.compile(r"\d+(?:\.\d+)?")
_FULLWIDTH = str.maketrans("０１２３４５６７８９．", "0123456789.")


def identity_text(text: str) -> str:
    """把「不承重」的片段挖掉：只剩文档坐标被剔，其余每个数字都是身份的一部分。

    日期**不**剔——`2026-11-05 的快照` 是对数据新鲜度的声称，改日期该重核。
    """
    return _VERSION.sub(" ", _SECTION_REF.sub(" ", text.translate(_FULLWIDTH)))


def digits(text: str) -> list[str]:
    """身份向量：行里**全部**数字，按出现顺序。`323` 与 `323.0` 不是同一个。"""
    return DIGITS.findall(identity_text(text))


def fingerprint(text: str) -> str:
    """身份 = 数字序列的哈希。措辞、标点、错字不换身份；任何一位数变了就换。"""
    return hashlib.sha256("|".join(digits(text)).encode("utf-8")).hexdigest()[:16]


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


def scan(root: Path, cfg: Config = Config()) -> list[Line]:
    """全部断言行。空行与纯文字行不进来。"""
    out: list[Line] = []
    for pat in cfg.globs:
        for path in sorted(root.glob(pat)):
            rel = path.relative_to(root).as_posix()
            for name, _start, lines in _blocks(path):
                body = "\n".join(t for _, t in lines)
                if not (any(k in body for k in cfg.claim_keywords)
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
    notes: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def kinds(self) -> set[str]:
        return {f["kind"] for f in self.failures}

    def as_dict(self) -> dict:
        return {"claims": self.claims, "anchored": self.anchored,
                "frozen": self.frozen, "failures": self.failures,
                "skipped": self.skipped, "notes": self.notes, "ok": self.ok}


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


def _group(items: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """按（文件, 数字指纹）归组。同指纹多行 ⇒ 同一组，条数就是要记账的次数。"""
    out: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        out.setdefault((it["file"], fingerprint(it["text"])), []).append(it)
    return out


def check(root: Path, registry: dict, cfg: Config = Config()) -> Report:
    lines = scan(root, cfg)
    by_key: dict[tuple[str, str], list[Line]] = {}
    for ln in lines:
        by_key.setdefault((ln.file, ln.key), []).append(ln)
    repo = Counter({k: len(v) for k, v in by_key.items()})

    entries = _group(registry.get("entries", []))
    frozen = _group(registry.get("frozen", []))
    rep = Report(len(lines), 0, 0)

    # 登记表自身也要自洽：`sha` 是按 `text` 写下的。手改 text 不重签，或身份规则
    # 变了没重签，条目会「看起来还在」——按 text 归组照样能配上行，于是静默通过。
    for item in registry.get("frozen", []):
        sha = item.get("sha")
        if sha and sha != fingerprint(item["text"]):
            rep.failures.append({
                "kind": "REGISTRY_DRIFT", "file": item["file"], "text": item["text"],
                "detail": (f"登记的 sha {sha} 与按 text 现算的 {fingerprint(item['text'])} "
                           "不符——text 被手改过，或身份规则变了没重签（--rebaseline）")})

    registered = Counter()
    for key, group in list(entries.items()) + list(frozen.items()):
        registered[key] += len(group)

    for key, n_reg in registered.items():
        n_repo = repo.get(key, 0)
        n_entry = len(entries.get(key, []))
        n_frozen = n_reg - n_entry
        matched = min(n_reg, n_repo)
        rep.anchored += min(n_entry, matched)   # 行少了时锚定优先占额度
        rep.frozen += matched - min(n_entry, matched)

        if n_repo < n_reg:
            group = entries.get(key) or frozen.get(key) or []
            rep.failures.append({
                "kind": "STALE_ENTRY" if n_entry else "STALE",
                "file": key[0], "text": group[0]["text"] if group else "",
                "detail": (f"登记 {n_reg} 条（锚定 {n_entry} / 封存 {n_frozen}），"
                           f"源码只剩 {n_repo} 条——数被改过，或该行被删")})

        got = by_key.get(key, [])
        if not got:
            continue
        for entry in entries.get(key, []):
            text = _match_text(entry["text"], got)
            rep.failures.extend(_check_entry(root, key[0], text, entry, rep))

    for key, n in repo.items():
        extra = n - registered.get(key, 0)
        if extra <= 0:
            continue
        for ln in by_key[key][registered.get(key, 0):]:
            rep.failures.append({
                "kind": "UNREGISTERED", "file": ln.file, "text": ln.text,
                "where": f"{ln.where}:{ln.lineno}",
                "detail": "读数没有出处，也没进封存名单"})

    rep.notes.extend(_prose_drift(by_key, entries, frozen))
    return rep


def _match_text(entry_text: str, got: list[Line]) -> str:
    """同指纹可能覆盖多行，核数要挑回**它自己那一行**。

    先找完全相同的文本，再退到最相似的一行。不挑的话，锚定条目可能拿隔壁
    那行（读数相同、写法不同）去过审。
    """
    want = _norm(entry_text)
    for ln in got:
        if ln.text == want:
            return ln.text
    return max(got, key=lambda ln: difflib.SequenceMatcher(None, want, ln.text).ratio()).text


def _check_entry(root: Path, file: str, text: str, entry: dict, rep: Report) -> list[dict]:
    out: list[dict] = []
    for c in entry.get("checks", []):
        claimed = float(c["claimed"])
        if not _claimed_in_text(text, claimed):
            out.append({"kind": "REGISTRY_DRIFT", "file": file, "text": text,
                        "detail": f"registry 写 {c['claimed']}，该行找不到这个写法"})
        art = entry.get("artifact")
        if not art:
            out.append({"kind": "REGISTRY_DRIFT", "file": file, "text": text,
                        "detail": "entry 缺 artifact"})
            continue
        apath = root / art
        if not apath.exists():
            rep.skipped.append({"file": file, "text": text,
                                "artifact": art, "path": c["path"]})
            continue
        try:
            got = reduce_leaves(resolve(
                json.loads(apath.read_text(encoding="utf-8")), c["path"]),
                c.get("agg"))
        except (PathError, json.JSONDecodeError) as exc:
            out.append({"kind": "VALUE_MISMATCH", "file": file, "text": text,
                        "detail": f"{art}#{c['path']}: {exc}"})
            continue
        tol = float(c.get("tol", 0.005))
        if abs(got - claimed) > tol + 1e-12:
            out.append({
                "kind": "VALUE_MISMATCH", "file": file, "text": text,
                "detail": (f"{art}#{c['path']} = {got:.6g}，docstring 写 "
                           f"{c['claimed']}，差 {abs(got - claimed):.6g} > {tol}")})
    return out


def _prose_drift(by_key: dict[tuple[str, str], list[Line]],
                 entries: dict, frozen: dict) -> list[dict]:
    """数没变、字变了。**只提示，不判定**——这是身份只看读数付的代价。

    「偏窄 7/8」改成「偏宽 7/8」不该静默：它在这里出现，评审才看得见。
    """
    notes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source, groups in (("entries", entries), ("frozen", frozen)):
        for key, group in groups.items():
            got = by_key.get(key, [])
            if not got:
                continue
            actual = [ln.text for ln in got]
            for item in group:
                old = _norm(item["text"])
                if old in actual or (key, old) in seen:
                    continue
                seen.add((key, old))
                ratio = max(difflib.SequenceMatcher(None, old, a).ratio()
                            for a in actual)
                notes.append({
                    "kind": "PROSE_DRIFT", "source": source, "file": key[0],
                    "text": item["text"], "similarity": round(ratio, 3),
                    "detail": ("数没变、字变了（相似度 "
                               f"{ratio:.2f}）——封存项用 --rebaseline 同步，"
                               "锚定项请手工同步 entries 的 text")})
    return notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--registry", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None, dest="json_out")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rebaseline", action="store_true")
    ap.add_argument("--show", type=int, default=40,
                    help="最多打印多少条失败（0 = 全打）")
    ap.add_argument("--globs", action="append", default=None,
                    help="扫描口径，可重复（默认本仓库的三组 glob）")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    reg_path = args.registry or (root / "docs" / "number-provenance.json")
    registry = load_registry(reg_path)
    cfg = Config(globs=tuple(args.globs)) if args.globs else Config()

    if args.list:
        for ln in scan(root, cfg):
            print(f"{ln.file}:{ln.lineno}\t{ln.where}\t{ln.text[:90]}")
        return 0

    if args.rebaseline:
        return _rebaseline(root, reg_path, registry, cfg)

    rep = check(root, registry, cfg)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(rep.as_dict(), ensure_ascii=False,
                                            indent=1), encoding="utf-8")
    print(f"断言行 {rep.claims}：已锚定 {rep.anchored}，已封存 {rep.frozen}，"
          f"失败 {len(rep.failures)}，跳过 {len(rep.skipped)}，"
          f"文字漂移 {len(rep.notes)}")
    shown = rep.failures if args.show == 0 else rep.failures[:args.show]
    for f in shown:
        where = f"  [{f['where']}]" if f.get("where") else ""
        print(f"  ✗ {f['kind']:<15} {f['file']}{where}\n      {f['text'][:100]}"
              f"\n      → {f['detail']}")
    if len(shown) < len(rep.failures):
        print(f"  … 还有 {len(rep.failures) - len(shown)} 条，用 --show 0 看全")
    for s in rep.skipped:
        print(f"  · SKIP 产物不在：{s['artifact']}（{s['file']}）")
    for n in rep.notes:
        print(f"  · 文字漂移 {n['file']}（{n['source']}，相似度 {n['similarity']}）"
              f"\n      {n['text'][:100]}")
    print("判定：" + ("✓ 每个读数都有出处或已封存" if rep.ok else "✗ 有没出处的读数"))
    return 0 if rep.ok else 1


def _rebaseline(root: Path, reg_path: Path, registry: dict,
                cfg: Config = Config()) -> int:
    """重写封存名单。**只封存没有产物可指的断言行**；已锚定的不动。

    锚定行靠「预留」而不是「先到先得」。同一指纹可能一句锚定、一句封存
    （`probe_pwin_variance.py` 就是），按扫描顺序扣名额会把两行记反：锚定行的文本
    被封存、封存行被当成锚定预留，下次 rebaseline 再换回来——名单在两个状态之间
    来回摆，而两边的 `sha` 都对，检查一直是绿的。所以预留分两轮：先按文本精确
    匹配，再退到最相似的一行（与 `_match_text` 同一条判据）。
    """
    lines = scan(root, cfg)
    entries = registry.get("entries", [])
    by_key: dict[tuple[str, str], list[int]] = {}
    for i, ln in enumerate(lines):
        by_key.setdefault((ln.file, ln.key), []).append(i)

    reserved: set[int] = set()
    matched: set[int] = set()
    for j, entry in enumerate(entries):
        cand = [i for i in by_key.get((entry["file"], fingerprint(entry["text"])), [])
                if i not in reserved]
        want = _norm(entry["text"])
        same = [i for i in cand if lines[i].text == want]
        if same:
            reserved.add(same[0])
            matched.add(j)
    for j, entry in enumerate(entries):
        if j in matched:            # 精确匹配过的不许再占第二行
            continue
        cand = [i for i in by_key.get((entry["file"], fingerprint(entry["text"])), [])
                if i not in reserved]
        if cand:
            reserved.add(max(cand, key=lambda i: difflib.SequenceMatcher(
                None, _norm(entry["text"]), lines[i].text).ratio()))

    frozen = [{"file": ln.file, "text": ln.text, "where": ln.where, "sha": ln.key}
              for i, ln in enumerate(lines) if i not in reserved]

    old = Counter((f["file"], fingerprint(f["text"])) for f in registry.get("frozen", []))
    new = Counter((f["file"], f["sha"]) for f in frozen)
    added, gone = new - old, old - new
    old_text = {(f["file"], fingerprint(f["text"])): f["text"]
                for f in registry.get("frozen", [])}

    registry["frozen"] = frozen
    registry["_doc"] = _REGISTRY_DOC
    reg_path.write_text(json.dumps(registry, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(f"封存 {len(frozen)} 条断言行（登记表原有 {sum(old.values())} 条；"
          f"新增 {sum(added.values())}，消失/改文 {sum(gone.values())}）")
    if added and gone and sum(added.values()) == len(frozen) == sum(gone.values()):
        print("  注意：两边相等 ⇒ 这一次是**身份规则变了**，不是新增债务；"
              "全部 sha 重签了一遍。")
    for key, n in added.items():
        print(f"  + {key[0]}: {next(f['text'] for f in frozen if (f['file'], f['sha']) == key)[:70]}")
    for key, n in gone.items():
        print(f"  - {key[0]}: {old_text.get(key, '')[:70]}")
    print("提醒：rebaseline 只写文本，不证明数是对的；能锚定到产物的请进 entries。")
    return 0


_REGISTRY_DOC = (
    "断言行的出处登记。entries = 能 index 到落盘产物的读数（每次运行重算比对）；"
    "frozen = 目前没有产物的历史读数（只保证读数不再变，措辞可变）。"
    "`sha` 是**数字向量**（行里全部数字，剔掉 §章节号与 v版本号）的指纹，"
    "不是整行文本的哈希：任何一位数变了就换身份，错字不换。"
    "由 scripts/check_number_provenance.py 读取，见 docs/decision-ledger.md §9.39/§9.41。"
)


if __name__ == "__main__":
    sys.exit(main())
