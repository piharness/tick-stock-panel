"""守门器能不能红：每一条断言配一份**故意错**的输入。

为什么必须写这个：D9 的教训是「护栏自己没过能红检查」。三条不变量里有两条是代数恒等式，
喂毫无关系的随机集合也永远不红 —— 它们和注释的唯一区别是多占了 20 行。
所以本文件对 `_artifact_guard` 的每条规则都给一个坏样本，要求它必须报红；
再给一个等价的好样本，要求它必须不报红（否则规则是「永远红」，同样没用）。

另外把 D9 的**证明**固化成测试：旧不变量 2 在 2,000 组随机集合上 0 次报错。

跑法：backend/.venv/Scripts/python.exe analysis/_test_artifact_guard.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _artifact_guard import violations  # noqa: E402
from _diag_l1_gapset_audit import check_leg_cover  # noqa: E402

# 规则 → (说明, 样本, 是否必须报红)
CASES: list[tuple[str, str, dict, bool]] = [
    # ---- R1：散文必须由字段生成 ----
    ("r1_good", "散文 = 模板渲染结果", {
        "leg_drop_249_only": 0.007,
        "reading_template": "去掉只动 {a:+.3f}bp",
        "reading_fields": {"a": "leg_drop_249_only"},
        "reading": "去掉只动 +0.007bp",
    }, False),
    ("r1_stale_prose", "D7 原形：字段是 0.007/−0.001，散文写 0.008/−0.005", {
        "leg_drop_249_only": 0.007,
        "leg_add_reverse_42": -0.001,
        "reading_template": "去掉只动 {a:+.3f}bp；加回 {b:+.3f}bp",
        "reading_fields": {"a": "leg_drop_249_only", "b": "leg_add_reverse_42"},
        "reading": "去掉只动 +0.008bp；加回 \u22120.005bp",
    }, True),
    ("r1_no_fields", "有模板没有产地声明", {
        "reading_template": "只动 +0.007bp", "reading": "只动 +0.007bp",
    }, True),
    ("r1_undeclared", "模板占位符未在字段表里", {
        "leg_drop_249_only": 0.007,
        "reading_template": "只动 {b:+.3f}bp",
        "reading_fields": {"a": "leg_drop_249_only"},
        "reading": "只动 +0.007bp",
    }, True),
    ("r1_bad_path", "字段表指向不存在的路径", {
        "leg_drop_249_only": 0.007,
        "reading_template": "只动 {a:+.3f}bp",
        "reading_fields": {"a": "sets.nope.cells"},
        "reading": "只动 +0.007bp",
    }, True),
    ("r1_root_path", "$ 从产物根取，跨层引用不需要复制一份数", {
        "sets": {"leg": {"cells": 249}},
        "net": {
            "reading_template": "{n} 个 cell",
            "reading_fields": {"n": "$sets.leg.cells"},
            "reading": "249 个 cell",
        },
    }, False),

    # ---- R2：镜像键必须反号 ----
    ("r2_good", "C − C_eff 与 C_eff − C 互为相反数", {
        "median_C_minus_C_eff": -0.1519, "median_C_eff_minus_C": 0.1519,
    }, False),
    ("r2_same_sign", "D8 原形：两行都写成 median(ce−cs)", {
        "median_C_minus_C_eff": 0.1519, "median_C_eff_minus_C": 0.1519,
    }, True),
    ("r2_not_a_pair", "同层有 minus 但操作数不同 ⇒ 不该判为镜像", {
        "leg_ex_rights": 1.347, "susp_weight_drop_predicted_bp": 0.007,
    }, False),

    # ---- R3：符号必须与 share 一致 ----
    ("r3_good", "64.5% 的日子 C_eff 更高 ⇒ C_eff − C 为正", {
        "share_C_eff_gt_C": 0.645, "median_C_eff_minus_C": 0.1519,
    }, False),
    ("r3_contradiction", "同一个 share 下 C − C_eff 也为正", {
        "share_C_eff_gt_C": 0.645, "median_C_minus_C_eff": 0.1519,
    }, True),
    ("r3_reverse_order", "反序键必须与正序键反号", {
        "share_C_eff_gt_C": 0.645, "median_C_minus_C_eff": -0.1519,
    }, False),
    ("r3_near_half", "share 贴近 0.5 时符号无信息，不判", {
        "share_C_eff_gt_C": 0.52, "median_C_minus_C_eff": 0.01,
    }, False),
]


def _leg_red(**kw) -> bool:
    try:
        check_leg_cover(**kw)
        return False
    except SystemExit:
        return True


def case_leg_cover_good() -> tuple[str, bool, bool]:
    return ("leg_cover_good", False,
            _leg_red(n_aminusc=4100, n_ex=3870, n_non=249, n_both=19))


def case_leg_cover_wrong_leg() -> tuple[str, bool, bool]:
    # 用错腿：把 240 个停牌当成 249 个 ⇒ 必须红
    return ("leg_cover_wrong_leg", True,
            _leg_red(n_aminusc=4119, n_ex=3870, n_non=240, n_both=0))


def case_old_invariant_is_tautology() -> tuple[str, bool, bool]:
    """旧不变量 2：|A\\C|−|C\\A| ≡ |A|−|C|。随机集合上永远不报错 ⇒ 它是恒等式。"""
    rnd = random.Random(20261111)
    bad = 0
    for _ in range(2000):
        a = {i for i in range(200) if rnd.random() < 0.6}
        c = {i for i in range(200) if rnd.random() < 0.4}
        if len(a - c) - len(c - a) != len(a) - len(c):
            bad += 1
    return ("old_invariant2_tautology_2000_random", True, bad == 0)


def case_disk_clean() -> tuple[str, bool, bool]:
    dirty = [p.name for p in sorted(HERE.glob("*_result.json")) if violations(
        json.loads(p.read_text(encoding="utf-8")))]
    if dirty:
        print("     违反产物: " + ", ".join(dirty))
    return ("disk_all_artifacts_clean", False, bool(dirty))


def _check(name: str, desc: str, obj: dict, want_red: bool) -> bool:
    v = violations(obj)
    got_red = bool(v)
    ok = got_red == want_red
    tag = "RED " if got_red else "ok  "
    print(f"{'PASS' if ok else 'FAIL'} {tag} {name:26s} {desc}")
    if got_red:
        for line in v[:2]:
            print("          " + line.replace("\n", " "))
    if not ok:
        print(f"       期望 {'红' if want_red else '不红'}")
    return ok


def main() -> int:
    ok = True
    for name, desc, obj, want in CASES:
        ok &= _check(name, desc, obj, want)

    for fn in (case_leg_cover_good, case_leg_cover_wrong_leg,
               case_old_invariant_is_tautology, case_disk_clean):
        name, want, got = fn()
        ok &= got == want
        print(f"{'PASS' if got == want else 'FAIL'} {name:34s}"
              f" 预期={want} 实测={got}")

    n = len(CASES) + 4
    print(f"\n{n} 例，{'全部符合预期' if ok else '有不符预期的样本'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
