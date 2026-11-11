"""产物散文 / 镜像键的守门器：把「落盘」变成「被守」。

为什么需要它
------------
同一动作已经失败三次（§9.5.6 的四处 + 本轮的 D7/D8）：数被算对了、搬进产物了，
但产物里的**散文**和**镜像键**不受任何检查。落盘 ≠ 被守。

三条规则，全部只用产物自身的字节判定，不解析自然语言、不读源码：

R1 散文由字段生成
   对每个 `<stem>_template` 键（stem ∈ reading/note/comment），必须有同名字段表
   `<stem>_fields` 或 `reading_fields`：**alias → JSON 路径**的映射，路径可以是
   本层的 `a.b` 或产物根的 `$sets.x.cells`。判定只有一条：
       `<stem> == render(<stem>_template, **{alias: resolve(path)})`
   ⇒ 散文与字段之间只有**一个**产地，写死一个陈旧的数在结构上不可能；
   而「这个数是谁的」在产物里是可读的，不必读源码反推。

   为什么不是「从散文里抽 ±0.000bp 再与同层字段比对」：那个做法对 `0.00bp`
   （引用的是另一个产物里的净值口径）必然假红 —— 本产物里没有值为 0.00 的字段。
   假红会被静音，静音等于没有。

R2 镜像键必须反号
   同一 dict 内两个键，若可写成 `p + u + minus + v + s` 与 `p + v + minus + u + s`
   （snake_case token 级、`u`/`v` 非空、共享前缀 `p` 与等长），则必须逐位互为相反数。
   在盘上 46 个产物里只命中 1 对（就是 D8），无假红。

R3 符号与 share 一致
   同层若有 `share_<X>_gt_<Y>`，且 `|share-0.5| >= 0.05`，则任何读作 `X − Y` 的值必须
   与 `share-0.5` 同号、读作 `Y − X` 的必须反号。D8 被这条同时抓到，且它指出的是
   **哪一个键**错（`median_C_minus_C_eff`），不是笼统报「矛盾」。

用法
----
    from _artifact_guard import assert_clean, render_reading
    nv["reading"] = render_reading(nv, root=out)   # 散文唯一产地
    assert_clean(out)                              # 写盘前：违反则 SystemExit
    python analysis/_artifact_guard.py "analysis/*_result.json"        # 巡盘

守门器自己也要能红：`analysis/_test_artifact_guard.py`（17 例，含每条的坏样本）。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path
from typing import Any, Iterable

TOL = 1e-9

# 已知缺口（不参与通过判定，写在这里防止它被当成已修）：
#   1. R1 只检查模板**渲染**是否等于散文，不检查模板里的字面量数字。
#      跨产物引用（如 pen_slope 的 0.658）仍在散文里写死，本层无字段可绑定。
#      补法：`reading_fields` 支持 `@artifact.json:key.path`，由守门器打开被引产物比对。
#   2. 盘上 46 个产物里只有 2 个有 `reading_template`，其余只受 R2/R3 覆盖。
KNOWN_HOLES = ("cross_artifact_literals", "legacy_artifacts_without_template")


class GuardError(SystemExit):
    """违反不变量。继承 SystemExit 以便脚本在写盘前直接终止。"""


def render(tpl: str, **fields) -> str:
    """散文的唯一产地：模板 + 字段。负号统一成 U+2212，与中文行文一致。

    只替换「前面不是字母/数字、后面是数字」的 `-`，所以 `2025-11-12` 不受影响。
    """
    import re
    return re.sub(r"(?<![\w.])-(?=\d)", "\u2212", tpl.format(**fields))


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, dict]]:
    if isinstance(node, dict):
        yield path or "<root>", node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _numeric(d: dict) -> dict[str, float]:
    return {k: v for k, v in d.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


# ---------------------------------------------------------------- R1
_MISSING = object()


def _resolve(scope: dict, ref: str, root: dict):
    """`a.b.c` 从本层解析；`$a.b` 从产物根解析。返回 _MISSING 表示不存在。"""
    cur = root if ref.startswith("$") else scope
    for part in (ref[1:] if ref.startswith("$") else ref).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def check_prose(d: dict, path: str, root: dict) -> list[str]:
    """对每个 `<stem>_template` 检查同名 `<stem>` 是否就是它的渲染结果。

    stem 可以是 reading / note / comment —— 判定规则只有一条，不认名字。
    """
    bad: list[str] = []
    for key in [k for k in d if k.endswith("_template")]:
        stem = key[:-len("_template")]
        tpl, txt = d[key], d.get(stem)
        if not isinstance(tpl, str) or not isinstance(txt, str):
            bad.append(f"{path}: {key}/{stem} 不是字符串")
            continue
        fields = d.get(f"{stem}_fields") or d.get("reading_fields")
        if isinstance(fields, list):                   # 兼容：alias == 同层键名
            fields = {f: f for f in fields}
        if not isinstance(fields, dict) or not fields:
            bad.append(f"{path}: 有 {key} 但没有字段表（产地未声明）")
            continue
        vals, missing = {}, []
        for alias, ref in fields.items():
            v = _resolve(d, ref, root)
            if v is _MISSING:
                missing.append(f"{alias}->{ref}")
            else:
                vals[alias] = v
        if missing:
            bad.append(f"{path}: 字段指向不存在的路径 {missing}")
            continue
        try:
            placeholders = {f for _, f, _, _ in
                            __import__("string").Formatter().parse(tpl) if f}
        except ValueError as e:
            bad.append(f"{path}: {key} 不是合法格式串（{e}）")
            continue
        undeclared = placeholders - set(vals)
        if undeclared:
            bad.append(f"{path}: 模板占位符 {sorted(undeclared)} 未在字段表声明")
            continue
        expected = render(tpl, **vals)
        if expected != txt:
            bad.append(f"{path}.{stem}: 散文与字段不一致\n"
                       f"    落盘 {txt!r}\n    重算 {expected!r}")
    return bad


# ------------------------------------------------------- R2 / R3 公共
def _toks(k: str) -> list[str]:
    return k.split("_")


def _mirror_split(t1: list[str], t2: list[str]) -> bool:
    """t1 == p+u+minus+v, t2 == p+v+minus+u（u/v 非空）？"""
    if len(t1) != len(t2):
        return False
    for p in range(0, len(t1) - 2):
        if t1[:p] != t2[:p]:
            return False
        r1, r2 = t1[p:], t2[p:]
        if r1.count("minus") != 1 or r2.count("minus") != 1:
            continue
        i = r1.index("minus")
        u, v = r1[:i], r1[i + 1:]
        if u and v and r2 == v + ["minus"] + u:
            return True
    return False


def check_mirrors(d: dict, path: str) -> list[str]:
    nums = _numeric(d)
    cand = [k for k in nums if "minus" in _toks(k)]
    bad: list[str] = []
    for i, k1 in enumerate(cand):
        for k2 in cand[i + 1:]:
            if not _mirror_split(_toks(k1), _toks(k2)):
                continue
            a, b = float(nums[k1]), float(nums[k2])
            if abs(a + b) > TOL:
                bad.append(f"{path}: 镜像键不反号 {k1}={a} / {k2}={b}（和 {a + b:+.4f}）")
    return bad


def _contains(t: list[str], pat: list[str]) -> int:
    n = len(pat)
    for i in range(len(t) - n + 1):
        if t[i:i + n] == pat:
            return i
    return -1


def check_share_sign(d: dict, path: str) -> list[str]:
    bad: list[str] = []
    nums = _numeric(d)
    for sk, sv in nums.items():
        if not (sk.startswith("share_") and "_gt_" in sk):
            continue
        if abs(float(sv) - 0.5) < 0.05:
            continue
        left, right = sk[len("share_"):].split("_gt_", 1)
        L, R = _toks(left), _toks(right)
        want = 1 if float(sv) > 0.5 else -1
        for k, v in nums.items():
            if k == sk or "minus" not in _toks(k):
                continue
            t = _toks(k)
            lr = _contains(t, L + ["minus"] + R) >= 0   # 键读作 X − Y
            rl = _contains(t, R + ["minus"] + L) >= 0   # 键读作 Y − X
            if lr == rl:                # 都命中或都不命中 ⇒ 无法判定，跳过
                continue
            sgn = 1 if float(v) > 0 else (-1 if float(v) < 0 else 0)
            # 键是 Y − X 时，符号要求与 X − Y 相反
            need = want if lr else -want
            if sgn != need:
                order = f"{left} - {right}" if lr else f"{right} - {left}"
                bad.append(
                    f"{path}: 符号与 {sk}={sv} 矛盾 —— {k}={v} 读作 {order}，"
                    f"应{'正' if need > 0 else '负'}（{left} {'>' if want > 0 else '<'} {right}）")
    return bad


def render_reading(d: dict, root: dict | None = None, stem: str = "reading") -> str:
    """按字段表渲染散文。写盘时用它，运行时用它，不许第三条路。"""
    root = root if root is not None else d
    fields = d.get(f"{stem}_fields") or d["reading_fields"]
    if isinstance(fields, list):
        fields = {f: f for f in fields}
    vals = {a: _resolve(d, r, root) for a, r in fields.items()}
    missing = [a for a, v in vals.items() if v is _MISSING]
    if missing:
        raise GuardError(f"字段表指向不存在的路径: {missing}")
    return render(d[f"{stem}_template"], **vals)


def violations(obj: Any) -> list[str]:
    out: list[str] = []
    for path, d in _walk(obj):
        out += check_prose(d, path, obj if isinstance(obj, dict) else {})
        out += check_mirrors(d, path)
        out += check_share_sign(d, path)
    return out


def assert_clean(obj: Any, label: str = "<artifact>") -> None:
    v = violations(obj)
    if v:
        raise GuardError(f"{label}: {len(v)} 处违反\n  " + "\n  ".join(v))


def guard_file(p: Path) -> list[str]:
    return violations(json.loads(Path(p).read_text(encoding="utf-8")))


def main(argv: list[str]) -> int:
    # 默认必须扫全部产物：写成 `_diag_l1_*` 时只覆盖 8/46，打印「0 个违反」看起来像全绿。
    pats = argv[1:] or [str(Path(__file__).resolve().parent / "*_result.json")]
    files: list[Path] = []
    for p in pats:
        files += [Path(x) for x in sorted(glob.glob(p))] if "*" in p else [Path(p)]
    bad = 0
    for f in files:
        v = guard_file(f)
        print(f"{'RED ' if v else 'ok  '} {f.name}")
        for line in v:
            print("     " + line)
        bad += bool(v)
    print(f"\n{len(files)} 个产物，{bad} 个违反")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
