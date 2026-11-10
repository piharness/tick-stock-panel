"""策略声明审计协议（analysis/eval_protocol.py）的守门测试。

为什么要有这一层：协议本身的判据是"缺省即红"，但**它的回归脚本原先只是一次性诊断脚本**，
跑不跑全靠记性。这里把它挂进 pytest，让"某条旁路重新打开"能自动变红。

四类断言，各自在什么条件下会红：

1. `test_protocol_regression_all_pass` —— 任一条端到端/单门/旁路/结构探针失效。
2. `test_four_reported_bypasses_stay_closed` —— 那四条被实名报告的旁路中，
   任一条的拦截理由消失（不是"整体仍 REJECT"这种可以被别的原因掩盖的绿）。
3. `test_positive_control_still_passes` —— 协议退化成恒 REJECT。
4. `test_known_holes_are_declared` —— 有人把未关闭的洞从产物里删掉，
   让 all_pass 看起来像"协议无洞"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
if str(ANALYSIS) not in sys.path:
    sys.path.insert(0, str(ANALYSIS))

import _diag_protocol_regression as regression  # noqa: E402


@pytest.fixture(scope="module")
def report() -> dict:
    return regression.run()


def test_protocol_regression_all_pass(report: dict) -> None:
    failed = []
    for section in ("end_to_end", "unit", "mutation", "structural"):
        for row in report[section]:
            if not row["pass"]:
                failed.append(f"{section}: {row}")
    assert not failed, "协议回归存在失败用例:\n" + "\n".join(failed)
    assert report["all_pass"] is True


def test_case_counts_are_pinned(report: dict) -> None:
    """四类探针的条数本身就是验收标准，必须由产物钉住。

    2026-11-10 的交接把结构门写成 4 条、总数写成 27 条，产物里是 5 条、28 条。
    没有任何测试读过这两个数，所以那句话错了也没人红。条数变了就等于验收标准变了
    ——改这里等于改标准，是设计好的摩擦（同 `SEALED` 的 `.py` 计数）。
    """
    got = {name: len(report[name]) for name in ("end_to_end", "unit", "mutation", "structural")}
    assert got == {
        "end_to_end": 5,
        "unit": 7,
        "mutation": 11,
        "structural": 5,
    }, f"探针条数变了，验收标准的文字要一起改: {got}"


@pytest.mark.parametrize(
    ("bypass", "guard"),
    [
        ("事件类型未登记（原：全部混杂当事件定义）", "unknown_event"),
        ("open_t1→close_t1 自报 1 天", "non_executable_window"),
        ("universe 随便填", "unknown_universe"),
        ("MDE 借别的宇宙", "universe_mismatch"),
        ("exit 未登记", "unknown_exit"),
        ("只填口径不报数", "effect_missing"),
        ("族系阈值 1.96 < Bonferroni 3.66", "threshold_too_loose"),
        ("完全不传 n_tests 与买不进占比", "n_tests_missing"),
        ("entry_unfillable_share 缺省", "unfillable_share_missing"),
        ("报年化但不报容量", "selection_unassessed"),
        ("自报 3 天但配对 2 天", "declared_holding_mismatch"),
    ],
)
def test_each_bypass_is_caught_by_its_own_guard(report: dict, bypass: str, guard: str) -> None:
    row = next((r for r in report["mutation"] if r["bypass"] == bypass), None)
    assert row is not None, f"旁路探针 {bypass!r} 已被删除；删探针等于删门"
    assert row["got_decision"] != "PASS", f"{bypass} 仍能通过"
    assert guard in row["got_reasons"], f"{bypass} 没有被预期的那道门拦住: {row['got_reasons']}"


def test_positive_control_still_passes(report: dict) -> None:
    clean = [r for r in report["end_to_end"] if r["want"] == "PASS"]
    assert clean, "正对照被删除"
    assert all(r["got"] == "PASS" for r in clean), "协议退化成恒 REJECT，判红不再有信息量"


def test_known_holes_are_declared(report: dict) -> None:
    holes = report["known_holes"]
    assert len(holes) >= 4, "未关闭的洞被删减；all_pass 不能代替'无洞'的声明"
    assert all("closed" in h and "closes_with" in h for h in holes)
    assert not any(h["closed"] for h in holes), (
        "有洞被标成已关闭：请同步删掉对应的 closes_with 说明，否则是在自我加冕"
    )


def test_holding_days_comes_from_the_pair_not_from_the_claim() -> None:
    from eval_protocol import holding_days_between

    assert holding_days_between("open_t1", "close_t1") == 0
    assert holding_days_between("open_t1", "close_t3") == 2
    assert holding_days_between("close_t", "open_t1") == 1
    assert holding_days_between("open_t1", "close_t") == -1
    assert holding_days_between("open_t1", "bogus") is None
