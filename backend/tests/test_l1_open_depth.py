"""`_diag_l1_open_depth_result.json` 的不变量：每条关键恒等式都配一份故意错的输入。

为什么这轮要单开一个文件
------------------------
上一轮（D13c）把决策变量换成了 `r* = (cost − vwap)/gap`，本轮的活是问「r 谁说了算」。
这个问题的答案全落在**分母**上：bar0 成交额。而分母的口径有三处可以悄悄错——
① 把 09:31 之前的残段分区当成全日（bar0 变成下午第 1 分钟）；
② 拿 38 只参照类的份额迁移到 cohort 却不报迁移误差（参照类与 cohort 交集为 0）；
③ 把 bp 当小数代进 r*（本轮上游真踩过：β 一度算成 10,998）。
三处都不报错，只让结论反向，所以各配一份坏样本。

结论守卫（`test_verdict_follows_pi_star`）是「会随数据翻转」的那一类：它钉的是
判据本身，不是结论。若实测后 π* 落到 30% 以上，这条会要求散文改口，而不是放宽阈值。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
OD = "_diag_l1_open_depth_result.json"
MN = "_diag_l1_market_neutral_result.json"
ENTRY = "_diag_l1_vwap_entry_result.json"
PROBE = "_diag_l1_open_probe_result.json"


def _load(name: str) -> dict:
    return json.loads((ANALYSIS / name).read_text(encoding="utf-8"))


def _primary(a: dict) -> dict:
    return a["budget"][a["budget"]["primary_route"]]


# ---------------------------------------------------------------- 恒等式
def pi_req_gap(a: dict, book_yi: float = 5.0, which: str = "med") -> float:
    """π* 必须是 book×r*/(日均只数×bar0成交额)，不能手填。"""
    b = a["budget"]
    p = _primary(a)
    pool = p[f"pool_{which}_yi"] * 1e8
    want = book_yi * 1e8 * b["r_star"] / (a["coverage"]["cohort_cells_per_day"] * pool)
    got = p["pi_req"][f"{book_yi:g}yi"][which]
    return abs(got - want)


def capacity_gap(a: dict, pi: float = 0.10, which: str = "med") -> float:
    """容量必须是 只数×π/r*×bar0成交额，且对 π 线性。"""
    b = a["budget"]
    p = _primary(a)
    pool = p[f"pool_{which}_yi"] * 1e8
    want = a["coverage"]["cohort_cells_per_day"] * pi / b["r_star"] * pool / 1e8
    got = p["capacity"][f"p{int(round(pi * 100)):02d}"][f"{which}_yi"]
    return abs(got - want)


def r_star_gap(a: dict) -> float:
    b = a["budget"]
    return abs(b["r_star"] - (b["cost_bp"] - b["vwap_bp"]) / b["gap_bp"])


# ---------------------------------------------------------------- 好样本
def test_cohort_is_the_entry_cohort() -> None:
    """cohort 必须是 entry 那一批 316 日 / 2,752 只-日，否则分母全漂。"""
    a, e = _load(OD), _load(ENTRY)
    assert a["coverage"]["cohort_days"] == e["cohort"]["n_days"] == 316
    assert a["coverage"]["cohort_cells"] == 2752
    assert a["coverage"]["cohort_cells_per_day"] == pytest.approx(2752 / 316, abs=0.01)


def test_r_star_recomputes_and_sits_in_unit_range() -> None:
    """r* 是小数、gap 是 bp：落在 (0.01, 0.5) 之外就是单位错或口径错。"""
    a = _load(OD)
    assert r_star_gap(a) <= 0.0002
    assert 0.01 < a["budget"]["r_star"] < 0.5
    assert a["verdict"]["R-D4_unit_ok"] is True
    m = _load(MN)
    assert a["budget"]["r_star"] == pytest.approx(
        (m["cost"]["equity_round_bp"] - m["entry_axis"]["vwap"]["mean_bp"])
        / m["entry_axis"]["return_gap_bp"], abs=0.0015)


def test_reference_uses_only_full_partitions() -> None:
    """21 个分区里必须只剩 19 个：残段的首根不是 09:31，份额会被算成下午第 1 分钟。"""
    a = _load(OD)
    c = a["coverage"]
    assert c["partitions"] == 21
    assert c["partitions_used"] + c["partitions_skipped"] == 21
    skipped = [x for x in a["levels"] if not x["used"]]
    assert len(skipped) == c["partitions_skipped"]
    for x in skipped:
        assert x["first_bar"] != "09:31:00" or x["min_bars"] < 200
    assert c["reference_cells"] == 748


def test_reference_share_reconciles_with_open_probe() -> None:
    """同一批分区、同一根 bar 的成交量份额必须与上一轮探针逐位同源。"""
    a, p = _load(OD), _load(PROBE)
    assert a["reference"]["open_probe_vol_med"] == p["pooled"]["median_vol_share"]
    assert a["reference"]["reconcile_with_open_probe_bp"] <= 1.0
    assert a["reference"]["share_vol"]["n"] == a["coverage"]["reference_cells"]


def test_overlap_zero_forces_a_declared_transfer() -> None:
    """参照类与 cohort 交集为 0 ⇒ 要么实测够覆盖，要么必须报迁移误差，不许直接借。"""
    a = _load(OD)
    c = a["coverage"]
    assert c["cohort_minute_overlap"] == 0
    if a["budget"]["primary_route"] == "direct":
        assert c["direct_coverage"] >= 0.8
    else:
        assert a["transfer"]["model"]["lodo_mae_share"] > 0


def test_transfer_reports_two_independent_routes() -> None:
    """参数式与非参式必须各自算，抄成同一条就没有「两条路线取较小」这回事。"""
    t = _load(OD)["transfer"]
    assert 0.0 < t["model"]["r2"] < 1.0
    assert t["model"]["lodo_mae_share"] > 0
    assert t["model"]["n"] == _load(OD)["coverage"]["reference_cells"]
    assert t["m1_med"] != t["m2_med"]
    assert t["used_med"] == pytest.approx(min(t["m1_med"], t["m2_med"]), abs=1e-9)


def test_pi_star_and_capacity_recompute() -> None:
    a = _load(OD)
    assert pi_req_gap(a) <= 1e-4
    for which in ("med", "mean", "p25"):
        assert capacity_gap(a, 0.10, which) <= 5e-3


def test_capacity_is_linear_in_participation() -> None:
    """容量对 π 必须线性：p10 是 p05 的两倍、p20 是 p10 的两倍。"""
    a = _load(OD)
    cap = _primary(a)["capacity"]
    assert cap["p10"]["med_yi"] == pytest.approx(2 * cap["p05"]["med_yi"], rel=0.01)
    assert cap["p20"]["med_yi"] == pytest.approx(2 * cap["p10"]["med_yi"], rel=0.01)
    assert cap["p50"]["med_yi"] > cap["p20"]["med_yi"]


def test_reference_share_rises_with_gap_depth() -> None:
    """cohort 的 gap 中位在 −3%：份额必须随缺口加深而上升，否则迁移方向就反了。"""
    b = _load(OD)["reference"]["by_gap_bin"]
    assert b["le_m3"]["share_med"] > b["m3_m2"]["share_med"] > b["m2_m1"]["share_med"]
    assert b["m2_m1"]["share_med"] > b["m1_0"]["share_med"]
    assert b["le_m3"]["n"] >= 20


def test_verdict_follows_pi_star() -> None:
    """判据只有三档，且必须由 π* 决定；落在 30% 以上时散文必须改口，不许调阈值。"""
    a = _load(OD)
    pi = a["verdict"]["R-D3_pi_star_at_5e8_med"]
    want = "pass" if pi <= 0.10 else ("undecidable" if pi <= 0.30 else "close")
    assert a["verdict"]["R-D3_verdict"] == want
    assert pi == pytest.approx(_primary(a)["pi_at_5e8_med"], abs=1e-4)


def test_reading_is_the_template_rendered_from_fields() -> None:
    """散文与字段只有一个产地；否则改数不会红。"""
    import sys

    sys.path.insert(0, str(ANALYSIS))
    from _artifact_guard import render

    a = _load(OD)
    vals = {}
    for alias, ref in a["reading_fields"].items():
        cur = a
        for part in ref.split("."):
            cur = cur[part]
        vals[alias] = cur
    assert render(a["reading_template"], **vals) == a["reading"]


# ---------------------------------------------------------------- 坏样本：必须红
def test_pi_star_red_when_pool_shifted() -> None:
    """把 bar0 成交额中位放大一倍（等于把分母当成分母的两倍）⇒ 恒等式必须报。"""
    a = copy.deepcopy(_load(OD))
    p = _primary(a)
    p["pool_med_yi"] = round(p["pool_med_yi"] * 2, 4)
    assert pi_req_gap(a) > 1e-3


def test_r_star_red_when_gap_taken_as_fraction() -> None:
    """gap 当小数代进 r* ⇒ 0.118 变 1179。

    注意这份坏样本的恒等式是**自洽的**（分子分母同步错），所以 `r_star_gap` 抓不到它；
    只有值域守卫能抓 —— 这正是「恒等式型检查器实测 0 次报错」那条教训。
    """
    a = copy.deepcopy(_load(OD))
    b = a["budget"]
    b["gap_bp"] = b["gap_bp"] / 1e4
    b["r_star"] = round((b["cost_bp"] - b["vwap_bp"]) / b["gap_bp"], 4)
    assert r_star_gap(a) <= 0.0002
    assert not (0.01 < b["r_star"] < 0.5)


def test_reference_cells_red_if_partial_partition_counted() -> None:
    """把 2026-10-08 那个 13:48 开始的残段也算进全日 ⇒ 份额会被拉低，必须红。"""
    a = copy.deepcopy(_load(OD))
    a["coverage"]["partitions_used"] = 20
    assert a["coverage"]["partitions_used"] + a["coverage"]["partitions_skipped"] != 21


def test_transfer_used_red_when_min_route_copied_from_one() -> None:
    """把「取较小」抄成 m1 一条 ⇒ 两条路线名存实亡，必须红。"""
    a = copy.deepcopy(_load(OD))
    a["transfer"]["used_med"] = a["transfer"]["m1_med"]
    t = a["transfer"]
    assert not (t["used_med"] == pytest.approx(min(t["m1_med"], t["m2_med"]), abs=1e-9))


def test_capacity_red_when_not_linear_in_pi() -> None:
    a = copy.deepcopy(_load(OD))
    p = _primary(a)
    p["capacity"]["p20"]["med_yi"] = p["capacity"]["p10"]["med_yi"] * 1.5
    cap = p["capacity"]
    assert not (cap["p20"]["med_yi"] == pytest.approx(2 * cap["p10"]["med_yi"], rel=0.01))


def test_verdict_red_when_pass_claimed_at_high_pi() -> None:
    """π* 已经 50% 还写 pass ⇒ 判据失效，必须红。"""
    a = copy.deepcopy(_load(OD))
    a["verdict"]["R-D3_pi_star_at_5e8_med"] = 0.50
    pi = a["verdict"]["R-D3_pi_star_at_5e8_med"]
    want = "pass" if pi <= 0.10 else ("undecidable" if pi <= 0.30 else "close")
    assert a["verdict"]["R-D3_verdict"] != want


def test_gap_bin_order_red_when_bins_shuffled() -> None:
    a = copy.deepcopy(_load(OD))
    b = a["reference"]["by_gap_bin"]
    b["le_m3"]["share_med"], b["m1_0"]["share_med"] = b["m1_0"]["share_med"], b["le_m3"]["share_med"]
    assert not (b["le_m3"]["share_med"] > b["m3_m2"]["share_med"] > b["m2_m1"]["share_med"])
