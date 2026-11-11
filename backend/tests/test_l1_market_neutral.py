"""`_diag_l1_market_neutral_result.json` 的不变量：每条都配一份故意错的输入。

为什么这轮要新开一个文件
------------------------
上一轮（D13b）的结论是「权重轴的摆动是市场 beta」。本轮要回答的是它的下半句：
「那把 beta 对冲掉，IR 会不会上去」。这个问题的答案有一半是算术（均值掉多少、
成本付多少），算术错了不会报错，只会让结论看起来很干净。所以四条恒等式各配一份
坏样本：抄成常数、两版 IR 复制成同一条、分档漏天、单位错成百分数。

结论守卫（`test_entry_axis_dominates_hedge`）故意是「会随数据翻转」的那一类：
它钉的是散文里的比较级。若重算后对冲真的抬了 IR，这条会红，正确反应是重写结论，
而不是放宽阈值 —— 与 `test_weighting_probe_recuts_boundaries_in_bootstrap` 同一条纪律。
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
MN = "_diag_l1_market_neutral_result.json"
ENTRY = "_diag_l1_vwap_entry_result.json"


def _load(name: str) -> dict:
    return json.loads((ANALYSIS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 恒等式
def hedge_identity_gap(a: dict) -> float:
    """对冲后均值必须 = 原始 − beta×市场均值 − 成本×beta。"""
    h, u = a["hedge"]["in_sample_ew"], a["unhedged"]
    b = a["beta"]["cohort_vs_ew_market"]
    cost = a["cost"]["hedge_round_bp_per_notional"]
    want = u["mean_bp"] - b["beta"] * b["x_mean_bp"] - cost * b["beta"]
    return abs(h["mean_bp"] - want)


def ir_gap(a: dict, block: dict) -> float:
    """IR 必须 = mean/sd×sqrt(回合数/年)，不能是从别处抄来的。"""
    rounds = a["panel"]["rounds_per_year"]
    want = block["mean_bp"] / block["sd_bp"] * math.sqrt(rounds)
    got = block["ir"] if "ir" in block else block["ir_per_round_annualized"]
    return abs(got - want)


def regime_recon_gap(a: dict) -> float:
    """三档按天数加权必须回到总体超额。"""
    r = a["regime"]
    ter, days = r["prior5d_market_tercile"], r["tercile_days"]
    tot = sum(days.values())
    got = sum(ter[k] * days[k] for k in ter) / tot
    return abs(got - r["excess_mean_bp"])


def breadth_gap(a: dict) -> float:
    b = a["breadth"]
    want = b["single_name_sd_bp"] / math.sqrt(b["names_per_day"])
    return abs(b["idio_floor_sd_bp"] - want)


# ---------------------------------------------------------------- 好样本
def test_cohort_is_the_same_cohort_as_entry() -> None:
    """cohort 必须是 entry 那一批 316 日 / 2,752 只-日、均值 121.5bp。"""
    a, e = _load(MN), _load(ENTRY)
    assert a["panel"]["cohort_days"] == e["cohort"]["n_days"] == 316
    assert a["panel"]["cohort_cells"] == 2752
    assert abs(a["unhedged"]["mean_bp"] - e["daily_bp"]["open_entry"]) <= 0.15


def test_vwap_side_matches_entry_artifact() -> None:
    """入场价轴的 VWAP 那一格与 entry 产物同源：6.43 vs 6.4 只是两位小数。"""
    a, e = _load(MN), _load(ENTRY)
    assert abs(a["entry_axis"]["vwap"]["mean_bp"] - e["daily_bp"]["vwap_entry"]) <= 0.05
    # 均值收益差 115.1 与 entry 的配对差是同一个量；**不是** `entry_price_penalty`
    # 的 108.4（那是 mean(vwap/open−1) 的价格惩罚，两个产地、两个定义，差 6.7bp 不是矛盾）
    assert abs(a["entry_axis"]["return_gap_bp"] - e["daily_bp"]["paired_diff"]["mean_bp"]) <= 0.1


def test_beta_is_a_ratio_not_a_percentage() -> None:
    """单位错了 beta 会变成 1.1 万（本轮真踩过）：落在 0.5–2 之外就是单位错。"""
    a = _load(MN)
    for k in ("cohort_vs_ew_market", "cohort_vs_nonpool", "pool_vs_ew_market"):
        b = a["beta"][k]
        assert 0.5 < b["beta"] < 2.0, (k, b["beta"])
    for k, v in a["beta"]["cohort_vs_index"].items():
        assert 0.3 < v["beta"] < 2.5, (k, v["beta"])
    for k in ("cohort_vs_ew_market", "pool_vs_ew_market"):
        assert 0.0 < a["beta"][k]["r2"] < 1.0


def test_pool_explains_far_more_variance_than_cohort() -> None:
    """同一个市场暴露，654 只/日的池 R² 必须远高于 8.7 只/日的 cohort。

    这正是本轮的核心机制：市场 beta 是**广度**的函数，不是策略的函数。
    """
    a = _load(MN)
    r2c = a["beta"]["cohort_vs_ew_market"]["r2"]
    r2p = a["beta"]["pool_vs_ew_market"]["r2"]
    assert r2p > 3 * r2c, (r2c, r2p)
    assert a["beta"]["cohort_vs_ew_market"]["beta"] == pytest.approx(
        a["beta"]["pool_vs_ew_market"]["beta"], abs=0.15)


def test_hedge_identity_holds() -> None:
    assert hedge_identity_gap(_load(MN)) <= 0.15


def test_ir_recomputes_from_mean_and_sd() -> None:
    a = _load(MN)
    assert ir_gap(a, a["unhedged"]) <= 0.06
    assert ir_gap(a, a["hedge"]["in_sample_ew"]) <= 0.06
    assert ir_gap(a, a["hedge"]["oos_rolling60"]) <= 0.06


def test_cost_sensitivity_is_monotone_and_anchored() -> None:
    """IR 必须随对冲成本单调下降，且基数那一格等于 in_sample_ew 的 IR。"""
    a = _load(MN)
    cs = a["hedge"]["cost_sensitivity"]
    keys = sorted(cs, key=float)
    vals = [cs[k] for k in keys]
    assert all(x > y for x, y in zip(vals, vals[1:])), (keys, vals)
    assert cs["4.34"] == a["hedge"]["in_sample_ew"]["ir"]


def test_ir_delta_agrees_with_the_two_levels() -> None:
    a = _load(MN)
    d = a["hedge"]["ir_delta_median"]
    diff = a["hedge"]["in_sample_ew"]["ir"] - a["unhedged"]["ir_per_round_annualized"]
    assert (d > 0) == (diff > 0), (d, diff)
    lo, hi = a["hedge"]["ir_delta_ci95_block20"]
    assert lo <= d <= hi


def test_entry_axis_dominates_hedge() -> None:
    """散文里的比较级：入场价的 ΔIR 至少是对冲 ΔIR 的 3 倍。翻转则重写结论。"""
    a = _load(MN)
    assert a["entry_axis"]["ir_gap_entry_vs_hedge"] >= 3 * abs(a["hedge"]["ir_delta_median"])
    assert a["entry_axis"]["ir_gap_entry_vs_hedge"] > 3.0


def test_breadth_floor_and_upper_bound() -> None:
    a = _load(MN)
    assert breadth_gap(a) <= 0.3
    b = a["breadth"]
    assert b["idio_floor_sd_bp"] < b["observed_sd_bp"], "地板高于实测 sd —— 广度或 sd 口径错"
    assert b["ir_if_pure_idio"] > a["unhedged"]["ir_per_round_annualized"]


def test_regime_reconstructs_overall_excess() -> None:
    a = _load(MN)
    assert regime_recon_gap(a) <= 0.1
    assert sum(a["regime"]["tercile_days"].values()) == a["regime"]["days_used"]


def test_hedge_cost_is_declared_not_assumed_away() -> None:
    """平今那一笔必须是显式参数且进了总成本，否则「对冲很便宜」会被当成结论。"""
    c = _load(MN)["cost"]
    assert c["hedge_fee_close_today_bp"] > c["hedge_fee_open_bp"] * 10
    assert c["hedge_round_bp_per_notional"] == pytest.approx(
        c["hedge_fee_open_bp"] + c["hedge_fee_close_today_bp"] + c["hedge_slip_bp"], abs=0.01)


# ---------------------------------------------------------------- 坏样本：必须红
def test_hedge_identity_red_when_mean_is_copied() -> None:
    """把对冲后均值抄成原始均值（于是「对冲没代价」）⇒ 恒等式必须报。"""
    a = copy.deepcopy(_load(MN))
    a["hedge"]["in_sample_ew"]["mean_bp"] = a["unhedged"]["mean_bp"]
    assert hedge_identity_gap(a) > 5.0


def test_hedge_identity_red_when_cost_dropped() -> None:
    """把成本那半从对冲均值里拿掉（只削 beta、不付手续费）⇒ 必须报。"""
    a = copy.deepcopy(_load(MN))
    b = a["beta"]["cohort_vs_ew_market"]
    a["hedge"]["in_sample_ew"]["mean_bp"] = round(
        a["unhedged"]["mean_bp"] - b["beta"] * b["x_mean_bp"], 2)
    assert hedge_identity_gap(a) > 3.0


def test_ir_red_when_two_versions_are_copied() -> None:
    """滚动版 IR 直接抄成全样本版 ⇒ 必须报（两版 sd 不同）。"""
    a = copy.deepcopy(_load(MN))
    a["hedge"]["oos_rolling60"]["ir"] = a["hedge"]["in_sample_ew"]["ir"]
    assert ir_gap(a, a["hedge"]["oos_rolling60"]) > 0.1


def test_regime_red_when_a_tercile_drops_a_day() -> None:
    a = copy.deepcopy(_load(MN))
    a["regime"]["tercile_days"]["mid"] -= 30
    assert regime_recon_gap(a) > 0.1


def test_breadth_red_when_floor_uses_pool_breadth() -> None:
    """用 654 只（池）当地板分母 ⇒ 地板被压到 1/8，必须报。"""
    a = copy.deepcopy(_load(MN))
    a["breadth"]["names_per_day"] = 654.0
    assert breadth_gap(a) > 100.0


def test_pool_vs_cohort_r2_order_red_if_cohort_gets_pool_beta() -> None:
    """把池的 R² 抄给 cohort ⇒ 「市场只解释 14%」这条机制句失去依据，必须红。"""
    a = copy.deepcopy(_load(MN))
    a["beta"]["cohort_vs_ew_market"]["r2"] = a["beta"]["pool_vs_ew_market"]["r2"]
    r2c = a["beta"]["cohort_vs_ew_market"]["r2"]
    r2p = a["beta"]["pool_vs_ew_market"]["r2"]
    assert not (r2p > 3 * r2c)
