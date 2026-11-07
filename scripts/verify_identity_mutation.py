"""§9.41 八 那七个数各自怎么来的——把「变异口径」写成可重跑的程序。

## 为什么要有这个脚本

账本 §9.41 八 有一张表，七个「把某个数字改一位」的口径给出七个不同的盲数字：

    正确替换 token  132    前缀重复  135    全域替换  110
    首个替换        125    首位数    132    不取模  132    数值 +1  132

其中只有前两行有过脚本。**表里留五个单源数，等于把「数必须有出处」这条规则
用在了别人身上**。本脚本一次跑完七个口径，输出 `identity_mutation.json`，
并逐项与上表的期望值比对——不一致就非零退出。

## 口径的含义（读者要能照着重新实现）

「盲数字」= 把某个数字 token 改一位之后，`fingerprint()` 没变的那些 token。
分母是断言行里 `\\d+(?:\\.\\d+)?` 的**全部** 315 个 token（含 §章节号、v版本号、
日期分段）。同一个 token 出现两次算两个。

- `correct_target`：在 token **自己**的位置替换，末位 +1（9→0）。这是唯一正确的
  改法，其余六个都是错法，存在的意义是证明 132 不是某个实现的偶然。
- `prefix_dup`：`text[:end-1] + bump + text[end:]`。13n 之前的 bug——token 前缀
  被重复了一遍（`0.50`→`0.50.51`），残渣没有小数点，被 `readings()` 吞掉。
- `replace_all`：`str.replace(token, bumped)`，替换行内**所有**同形子串。
- `replace_first`：同上，`count=1`，只换第一个同形子串。
- `first_digit`：改 token 的**首位**数字（`0.50`→`1.50`），不碰末位。
- `no_mod`：末位 +1 **不取模**（`9`→`10`，token 变长）。
- `value_plus_1`：按数值 +1，保留原小数位数（`0.50`→`1.50`，`2026`→`2027`）。

后三个（首位数 / 不取模 / 数值 +1）**盲集合与 correct 逐 token 相同**——这是
比「都是 132」更强的结论：132 不依赖 bump 怎么写，它就是 `readings()` 看不见
的那批 token（116 裸整数 + 12 章节号 + 3 日期分段 + 1 版本号）。前两个替换口径
则会改错位置：`replace_all` 顺手改坏行内别的数字（22 个翻红），`replace_first`
可能改的是一个更早的同形子串（10 红 3 盲）——两者都在污染计数，不是一个
「更宽松的 correct」。

## 为什么跑在 b4322c8 的树上

账本写的是「b4322c8 的完整树 + 它自己的 `scan()`」。本脚本照做：用
`git archive` 把该提交的扫描范围导出到临时目录，加载**那个提交自带**的
检查器，用它自己的 `scan()` / `fingerprint()` 复算。随后再拿当前工作区的
`scan()` 比对行文本多重集——相等才说明这张表对今天的树仍然成立（v13m/n 只改了
检查器与文档，没碰断言行）。

## 用法

    backend/.venv/Scripts/python.exe scripts/verify_identity_mutation.py
    ... --rev b4322c8 --json data/.scratch/identity_mutation.json
    ... --tree <已导出的目录>      # 跳过 git archive，直接复用

退出码 0 ⇔ 七个口径与 §9.41 八 的表逐位相同。
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REV = "b4322c8"
DEFAULT_OUT = ROOT / "data" / ".scratch" / "identity_mutation.json"
CHECKER = "scripts/check_number_provenance.py"

DIGITS = re.compile(r"\d+(?:\.\d+)?")
SECTION = re.compile(r"§\s*\d+(?:\.\d+)*")
VERSION = re.compile(r"\bv\d+(?:\.\d+)*[a-z]?\b")
DATE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")

#: 账本 §9.41 八 的表。改代码导致这些数变化时，先改表再改这里——两个都得动。
EXPECTED = {
    "correct_target": 132,
    "prefix_dup": 135,
    "replace_all": 110,
    "replace_first": 125,
    "first_digit": 132,
    "no_mod": 132,
    "value_plus_1": 132,
}
#: 只有这两行的「盲行数」进过账本正文，其余行只承诺盲 token 数。
EXPECTED_LINES_BLIND = {"correct_target": 60, "prefix_dup": 62}


def bump_last(tok: str) -> str:
    """末位 +1，9 回 0。"""
    return tok[:-1] + str((int(tok[-1]) + 1) % 10)


def _at(text: str, m: re.Match[str], fn) -> str:
    return text[: m.start()] + fn(m.group(0)) + text[m.end():]


def _plus_one(tok: str) -> str:
    dec = len(tok.split(".")[1]) if "." in tok else 0
    return f"{float(tok) + 1:.{dec}f}"


MODES: list[tuple[str, str, object]] = [
    ("正确替换 token", "correct_target", lambda t, m: _at(t, m, bump_last)),
    ("前缀重复", "prefix_dup",
     lambda t, m: t[: m.end() - 1] + bump_last(m.group(0)) + t[m.end():]),
    ("全域替换", "replace_all", lambda t, m: t.replace(m.group(0), bump_last(m.group(0)))),
    ("首个替换", "replace_first",
     lambda t, m: t.replace(m.group(0), bump_last(m.group(0)), 1)),
    ("首位数", "first_digit",
     lambda t, m: _at(t, m, lambda x: str((int(x[0]) + 1) % 10) + x[1:])),
    ("不取模", "no_mod", lambda t, m: _at(t, m, lambda x: x[:-1] + str(int(x[-1]) + 1))),
    ("数值 +1", "value_plus_1", lambda t, m: _at(t, m, _plus_one)),
]


def _git(args: list[str]) -> bytes:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{p.stderr.decode('utf-8', 'replace')}")
    return p.stdout


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclass 需要模块已注册才能解析 __module__
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def in_scope_paths(rev: str, globs: tuple[str, ...]) -> list[str]:
    """该提交里落在扫描口径内的文件——只导出这些，不搬整棵树。"""
    out = []
    for rel in _git(["ls-tree", "-r", "--name-only", rev]).decode("utf-8").splitlines():
        if Path(rel).match(CHECKER) or any(Path(rel).match(g) for g in globs):
            out.append(rel)
    return out


def export_tree(rev: str, dest: Path, globs: tuple[str, ...]) -> Path:
    paths = in_scope_paths(rev, globs)
    if CHECKER not in paths:
        paths.append(CHECKER)
    blob = _git(["archive", "--format=tar", rev, *paths])
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        try:
            tf.extractall(dest, filter="data")      # 3.12+；旧版没有 filter 参数
        except TypeError:                            # pragma: no cover
            tf.extractall(dest)
    return dest / CHECKER


def classify(text: str, start: int) -> str:
    if any(s.start() <= start < s.end() for s in DATE.finditer(text)):
        return "date"
    if any(s.start() <= start < s.end() for s in SECTION.finditer(text)):
        return "section"
    if any(s.start() <= start < s.end() for s in VERSION.finditer(text)):
        return "version"
    return "digit"


def audit(mod, lines: list[str], fn) -> dict:
    total = blind = lines_with_blind = 0
    by_kind: dict[str, int] = {}
    worst: list[dict] = []
    for text in lines:
        here = 0
        for m in DIGITS.finditer(text):
            total += 1
            if mod.fingerprint(fn(text, m)) == mod.fingerprint(text):
                blind += 1
                here += 1
                kind = classify(text, m.start())
                by_kind[kind] = by_kind.get(kind, 0) + 1
        if here:
            lines_with_blind += 1
            worst.append({"text": text[:120], "blind": here})
    return {"numbers": total, "blind": blind,
            "blind_pct": round(100.0 * blind / total, 1) if total else 0.0,
            "lines_with_blind": lines_with_blind, "by_kind": by_kind,
            "worst_lines": worst}


def blind_points(mod, lines: list[str], fn) -> set[tuple[int, int]]:
    """盲的位置集合，用来做口径间差异集（只比总数会掩盖「换了一批 token」）。"""
    out = set()
    for i, text in enumerate(lines):
        for j, m in enumerate(DIGITS.finditer(text)):
            if mod.fingerprint(fn(text, m)) == mod.fingerprint(text):
                out.add((i, j))
    return out


def diff_report(lines: list[str], base: set[tuple[int, int]],
                got: set[tuple[int, int]], mod) -> dict:
    def rows(points):
        out = []
        for i, j in sorted(points):
            m = list(DIGITS.finditer(lines[i]))[j]
            out.append({"token": m.group(0), "text": lines[i][:110]})
        return out
    return {"to_red": len(base - got), "to_blind": len(got - base),
            "to_red_rows": rows(base - got), "to_blind_rows": rows(got - base)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default=DEFAULT_REV)
    ap.add_argument("--json", type=Path, default=DEFAULT_OUT, dest="json_out")
    ap.add_argument("--tree", type=Path, default=None, help="复用已导出的目录")
    args = ap.parse_args(argv)

    cur = _load(ROOT / CHECKER, "chkcur")
    tmp_holder: tempfile.TemporaryDirectory | None = None
    if args.tree:
        checker_path = args.tree / CHECKER
    else:
        tmp_holder = tempfile.TemporaryDirectory(prefix="b4322c8_")
        checker_path = export_tree(args.rev, Path(tmp_holder.name), cur.SCOPE_GLOBS)

    try:
        old = _load(checker_path, "chkold")
        old_lines = [ln.text for ln in old.scan(checker_path.parents[1])]
        cur_lines = [ln.text for ln in cur.scan(ROOT)]
        same = sorted(old_lines) == sorted(cur_lines)

        print(f"{args.rev}: {len(old_lines)} 行 / "
              f"{sum(1 for t in old_lines for _ in DIGITS.finditer(t))} 个数字")
        print(f"当前工作区: {len(cur_lines)} 行 / "
              f"{sum(1 for t in cur_lines for _ in DIGITS.finditer(t))} 个数字"
              f"｜行文本多重集{'相同' if same else '不同'}")
        if not same:
            for x in old_lines:
                if x not in cur_lines:
                    print(f"  仅旧树: {x[:100]}")

        base = blind_points(old, old_lines, MODES[0][2])
        modes = []
        for label, key, fn in MODES:
            row = audit(old, old_lines, fn)
            row.update({"label": label, "mode": key,
                        "expected_blind": EXPECTED[key],
                        "matches": row["blind"] == EXPECTED[key],
                        "vs_correct": diff_report(old_lines, base,
                                                  blind_points(old, old_lines, fn), old)})
            modes.append(row)
            print(f"  {label:<12} {row['blind']:>4}/{row['numbers']}"
                  f"（{row['blind_pct']:>4}%），{row['lines_with_blind']:>2} 行"
                  f"  期望 {EXPECTED[key]:>4}"
                  f"｜相对 correct：红 {row['vs_correct']['to_red']:>2}"
                  f" / 盲 {row['vs_correct']['to_blind']}")

        residual = [{"kind": classify(old_lines[i], m.start()), "token": m.group(0),
                     "text": old_lines[i][:110]}
                    for i in range(len(old_lines))
                    for j, m in enumerate(DIGITS.finditer(old_lines[i]))
                    if (i, j) in base]
        now = audit(cur, cur_lines, MODES[0][2])  # 只取分母；当前实现的盲集合是另一件事
        now_blind = [{"kind": classify(cur_lines[i], m.start()), "token": m.group(0),
                      "text": cur_lines[i][:110]}
                     for i in range(len(cur_lines))
                     for j, m in enumerate(DIGITS.finditer(cur_lines[i]))
                     if cur.fingerprint(MODES[0][2](cur_lines[i], m)) == cur.fingerprint(cur_lines[i])]

        bad = [m["label"] for m in modes if not m["matches"]]
        for key, want in EXPECTED_LINES_BLIND.items():
            row = next(m for m in modes if m["mode"] == key)
            if row["lines_with_blind"] != want:
                bad.append(f"{row['label']} 盲行数 {row['lines_with_blind']}≠{want}")

        rep = {"rev": args.rev, "lines": len(old_lines),
               "numbers": sum(1 for t in old_lines for _ in DIGITS.finditer(t)),
               "tree_matches_workspace": same,
               "workspace": {"lines": len(cur_lines),
                             "numbers": sum(1 for t in cur_lines for _ in DIGITS.finditer(t))},
               "modes": modes,
               "old_identity_blind": residual,
               "current_identity_blind": now_blind,
               "current_denominator": now["numbers"],
               "matches_ledger": not bad}
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                 encoding="utf-8")

        print(f"\n旧身份（读数向量）哑数字 {len(residual)} 个，"
              f"分类 {audit(old, old_lines, MODES[0][2])['by_kind']}")
        print(f"当前身份（数字+符号）哑数字 {len(now_blind)} 个，"
              f"分类 {sorted({b['kind'] for b in now_blind})}")
        if bad:
            print("✗ 与 §9.41 八 的表不符：" + "、".join(bad))
            return 1
        print("✓ 七个口径与 §9.41 八 的表逐位相同")
        return 0
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()


if __name__ == "__main__":
    sys.exit(main())
