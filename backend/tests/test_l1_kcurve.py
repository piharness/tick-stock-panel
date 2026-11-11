"""`_diag_l1_kcurve_result.json` 的不变量：每条关键判据都配一份故意错的输入。

为什么这轮要单开一个文件
------------------------
上一轮的口头结论是「参照类 k 曲线近乎线性走到 0，与 r* 交于 k≈139，容量 5.8–8.2 亿」。
本轮把它落成产物后发现三处结构性错误，每一处都**不报错、只让结论反向**：

① 日线 open 是复权价、amount/volume 是原始价：不除 `raw_close/close`，Δ(240) 从 109
   变成 433，看起来只是「惩罚更大」，实际是拿两个刻度相减；
② 参照类 Δ(k) 不是线性（R²≈0.2、94 段回升），「交于 139」只是某一种归一化的读数，
   换成映射到 cohort 端点就变成 81 ⇒ k* 只有区间，判据卡在边界上；
③ Δ(k) 的日级 se 随 k 放大 2.6 倍，60 天下 19bp 的差只有 1.44σ，而原计划按
   Δ(1) 的 se 推算，得出「3.8σ、60 天够判」。

三条各配坏样本；另外钉三条跨产物的对拍（端点 / entry_price_penalty / 判据常数）。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = ROOT / "analysis"
KC = "_diag_l1_kcurve_result.json"
VE = "_diag_l1_vwap_entry_result.json"
OD = "_diag_l1_open_depth_result.json"
PART = ROOT / "data" / "research" / "l1_open_depth" / "part.parquet"


def _load(name: str) -> dict:
    return json.loads((ANALYSIS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 检查器
def reconcile_gap(a: dict) -> float:
    """Δ(240) 的按日等权必须与 vwap_entry 的 entry_price_penalty 对上。"""
    return abs(a["endpoints"]["d240"]["day_eq_mean_bp"] - a["reconcile"]["published_penalty_bp"])


def adjf_guard_live(a: dict) -> bool:
    """不除复权因子会放大 4 倍；守卫必须能看出这个量级差。"""
    return abs(a["endpoints"]["d240_without_adjf_bp"]) > 2 * abs(
        a["endpoints"]["d240"]["day_eq_mean_bp"]
    )


def criterion_straddled(a: dict) -> bool:
    """两种归一化读数的 c(k*) 落在判据线两侧 ⇒ k* 不可辨。"""
    k = a["kstar"]
    lo, hi = sorted([k["c_at_low"], k["c_at_high"]])
    return lo <= k["c_required"] <= hi


def c_required_recompute(a: dict) -> float:
    od = _load(OD)["coverage"]
    want = 5.0e8 / (od["cohort_cells_per_day"] * 0.10 * od["cohort_day_amt_med_yi"] * 1e8)
    return abs(a["kstar"]["c_required"] - want)


def sigma_recompute(a: dict) -> float:
    """se 在盘上四舍五入到 2 位，反推的容差按这个精度给。"""
    p = a["power"]
    want = 19.0 / p["se_d52_60d_bp"]
    return abs(p["sigma_of_19bp_at_60d"] - want)


def curve_is_monotone_in_c(a: dict) -> bool:
    c = a["reference"]["curve_c"]
    return all(y >= x for x, y in zip(c, c[1:]))


# ---------------------------------------------------------------- 好样本
def test_endpoints_are_recomputed_not_transcribed() -> None:
    """Δ(1) 必须能从 part.parquet 直接复算出来，而不是抄一个数进 JSON。"""
    a = _load(KC)
    p = pl.read_parquet(PART).with_columns(
        ((pl.col("b0_amt") / (pl.col("b0_vol") * 100.0) / pl.col("b0_open") - 1.0) * 1e4).alias("d1")
    )
    assert a["endpoints"]["d1"]["cell_mean_bp"] == pytest.approx(
        float(p["d1"].mean()), abs=0.02
    )
    assert a["endpoints"]["d1"]["cells"] == p.height == 2752
    assert a["endpoints"]["d1"]["days"] == 316


def test_d240_reconciles_with_vwap_entry() -> None:
    """同一个量：本产物按日等权 108.39，vwap_entry 发布 108.4，差必须 ≤1bp。"""
    a = _load(KC)
    assert reconcile_gap(a) <= 1.0
    assert a["reconcile"]["same_caliber"] is True
    assert a["reconcile"]["published_penalty_bp"] == pytest.approx(
        _load(VE)["daily_bp"]["entry_price_penalty"]["mean"], abs=1e-9
    )


def test_adjf_guard_is_live() -> None:
    """不除 raw_close/close 得到的是 4 倍；守卫若失效，这条会红。"""
    a = _load(KC)
    assert adjf_guard_live(a) is True
    assert a["endpoints"]["adjf_multiple"] >= 3.0


def test_shape_is_not_linear() -> None:
    """「近乎线性」是可证伪的：R²<0.8 且存在大量回升。"""
    a = _load(KC)
    assert a["reference"]["ols_r2"] < 0.8
    assert a["reference"]["monotone_violations"] > 10
    assert a["verdict"]["K2_shape_is_not_linear"] is True


def test_c_curve_is_monotone_even_when_delta_is_not() -> None:
    """Δ 可以回升，累积成交额份额不行——这条抓「曲线序列贴错列」。"""
    a = _load(KC)
    assert curve_is_monotone_in_c(a) is True
    assert a["reference"]["curve_c"][-1] == pytest.approx(1.0, abs=0.05)


def test_kstar_is_an_interval_that_straddles_the_gate() -> None:
    """k* 只有区间；若两种读数落到同侧，本文件的前提就该重写。"""
    a = _load(KC)
    assert a["kstar"]["identified"] is False
    assert criterion_straddled(a) is True
    assert a["kstar"]["low_k"] < a["kstar"]["high_k"] < 240


def test_c_required_matches_the_capacity_identity() -> None:
    """判据常数不是手填的：5 亿 /(日均只数 × π × 日成交额中位)。"""
    a = _load(KC)
    assert c_required_recompute(a) < 1e-3
    assert 0.35 < a["kstar"]["c_required"] < 0.45


def test_sixty_days_is_not_enough_and_sigma_is_derived() -> None:
    """σ 必须由 se 反推，且 60 天不足 2σ——原计划「3.8σ」是漏了 se 随 k 放大。"""
    a = _load(KC)
    assert sigma_recompute(a) < 0.01
    assert a["power"]["sigma_of_19bp_at_60d"] < 2.0
    assert a["power"]["sixty_days_enough"] is False
    assert a["power"]["se_ratio_k52_over_k1"] > 2.0


def test_reading_is_the_template_rendered_from_fields() -> None:
    """散文只有模板一个产地：改字段必须让散文跟着变。"""
    import sys

    sys.path.insert(0, str(ANALYSIS))
    from _artifact_guard import render_reading

    a = _load(KC)
    assert a["reading"] == render_reading(a)
    b = copy.deepcopy(a)
    b["kstar"]["low_k"] = 99.9
    assert render_reading(b) != a["reading"]


def test_verdict_all_pass_is_true_today() -> None:
    a = _load(KC)
    assert a["verdict"]["all_pass"] is True
    assert a["verdict"]["online_run_justified"] is True


# ---------------------------------------------------------------- 坏样本
def test_reconcile_red_when_adjf_dropped() -> None:
    """把 Δ(240) 换成没除复权因子的那个值，对拍必须红。"""
    a = _load(KC)
    b = copy.deepcopy(a)
    b["endpoints"]["d240"]["day_eq_mean_bp"] = b["endpoints"]["d240_without_adjf_bp"]
    assert reconcile_gap(b) > 100.0


def test_adjf_guard_red_when_factor_ignored() -> None:
    a = _load(KC)
    b = copy.deepcopy(a)
    b["endpoints"]["d240_without_adjf_bp"] = b["endpoints"]["d240"]["day_eq_mean_bp"]
    assert adjf_guard_live(b) is False


def test_straddle_red_when_both_readings_land_on_one_side() -> None:
    a = _load(KC)
    b = copy.deepcopy(a)
    b["kstar"]["c_at_low"] = 0.60
    b["kstar"]["c_at_high"] = 0.90
    assert criterion_straddled(b) is False


def test_c_required_red_when_book_doubled() -> None:
    a = _load(KC)
    b = copy.deepcopy(a)
    b["kstar"]["c_required"] = b["kstar"]["c_required"] * 2
    assert c_required_recompute(b) > 0.3


def test_c_curve_red_when_delta_column_pasted() -> None:
    """把 Δ（会回升）贴到 c 的位置，单调性检查必须红。"""
    a = _load(KC)
    b = copy.deepcopy(a)
    b["reference"]["curve_c"] = b["reference"]["curve_delta_bp"]
    assert curve_is_monotone_in_c(b) is False
