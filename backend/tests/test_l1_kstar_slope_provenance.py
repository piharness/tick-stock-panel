"""`_diag_l1_kstar_slope_provenance_result.json` 的不变量。

为什么这轮要单开一个文件
------------------------
上一轮把「116 天」判死，理由是 `se_c` 太大。但 `se_c = se_Δ · |dc/dΔ|` 的两个因子
（斜率 −0.06949、se 放大 2.60×）**都来自 `kline_minute`**，而那个库与 cohort 的
(date, symbol) 交集是 0。于是「不可辨」测的是斜率产地，不是 k*。

四条各配坏样本：
① 交集必须真的是 0，且参照类规模必须等于 kcurve 发布值；否则"外推"这个前提本身就是错的；
② 临界斜率必须严格落在 [参照类, cohort 端点] 之间 ⇒ 换斜率就换答案；
③ 参照类跨度必须 ≥2 倍窄于 cohort 且有回升段 ⇒ 平坦性是选择效应，不是普遍性质；
④ 水平口径的 2σ 必须被两种形状夹住（t_lo<2≤t_hi）⇒ 加窗口和加广度都修不了；
⑤ `dc/dΔ` 的分子是参照类的 dc/dk、分母是 cohort 割线 ⇒ 必须自报混用；且「翻转」
   的方向取决于两个从未被测的量 ⇒ K11b 显式 False，不能读作方向已知。
⑥ 「切线比割线平 2% 就跨线」是错的（9.0% 才跨）；切/割比的分母怎么量本身跨 2.7 倍；
   参照类 Δ 随 k 下降而 cohort 上升 ⇒ 形状比不可迁移。三条各配一条能红的测试。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = ROOT / "analysis"
SP = "_diag_l1_kstar_slope_provenance_result.json"
KS = "_diag_l1_kstar_condition_result.json"
KC = "_diag_l1_kcurve_result.json"


def _load(name: str) -> dict:
    return json.loads((ANALYSIS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 检查器
def provenance_is_extrapolation(a: dict) -> bool:
    """交集为零才算外推；只要共享一个 (date,symbol)，这条就不是外推而是抽样。"""
    p = a["provenance"]
    return p["cell_overlap"] == 0 and p["ref_cells"] > 0


def slope_ratio(a: dict) -> float:
    return a["inputs"]["coh_endpoint_slope_bp_per_bar"] / abs(a["inputs"]["ref_slope_bp_per_bar"])


def critical_slope(a: dict) -> float:
    i = a["inputs"]
    return i["dc_dk_per_bar"] * i["se_delta_k52_316d_bp"] / (i["gap_between_readings"] / 2.0)


def k10(a: dict) -> bool:
    return slope_ratio(a) >= 2.0


def k11(a: dict) -> bool:
    s = critical_slope(a)
    lo, hi = sorted((abs(a["inputs"]["ref_slope_bp_per_bar"]),
                     a["inputs"]["coh_endpoint_slope_bp_per_bar"]))
    return lo < s < hi


def k12(a: dict) -> bool:
    lb = a["level_bracket"]
    return lb["coh_span_bp"] / lb["ref_span_bp"] >= 2.0 and lb["ref_monotone_violations"] > 0


def k13(a: dict) -> bool:
    lb = a["level_bracket"]
    return lb["t_pessimistic"] < 2.0 <= lb["t_optimistic"]


def se_c_recompute(a: dict, who: str, t_days: float) -> float:
    i = a["inputs"]
    return (i["se_delta_k52_316d_bp"]
            * (i["days_available"] / t_days) ** 0.5
            * a["recompute"][who]["dc_per_bp"])


def level_identity(a: dict, side: str) -> float:
    """Δ + 余量 = 打平线，两端各自成一式。"""
    lb = a["level_bracket"]
    return abs(lb[f"delta_{side}_bp"] + lb[f"margin_{side}_bp"] - a["inputs"]["breakeven_bp"])


def mixes_universes(a: dict) -> bool:
    """dc/dΔ 若分子来自参照类、分母来自 cohort 割线，这条链就是两个宇宙的商。"""
    c = a["recompute"]["coh"]
    return bool(c.get("is_mixed")) and c.get("numerator_from") == "reference"


def flip_direction_is_open(a: dict) -> bool:
    """两种斜率口径给出相反的 K5 ⇒ 翻转方向未定，不可当作「已知」。"""
    s = a["slope_sensitivity"]
    return s["K5_at_secant"] != s["K5_at_migrated_tangent"]


# ---------------------------------------------------------------- 好样本
def test_reference_universe_has_zero_cell_overlap() -> None:
    """本轮全部论证的前提：708/748 格与 2,752 格按 (date,symbol) 无一共享。"""
    a = _load(SP)
    p = a["provenance"]
    assert p["cell_overlap"] == 0
    assert p["symbol_overlap"] <= 2          # 符号层最多撞 2 只，格层必须为 0
    assert provenance_is_extrapolation(a) is True


def test_reference_scale_matches_kcurve() -> None:
    """参照类规模必须仍是 kcurve 发布的 748 格 / 19 天 / 40 只。"""
    a, k = _load(SP), _load(KC)
    p, r = a["provenance"], k["reference"]
    assert p["ref_cells"] == r["cells"]
    assert p["ref_days"] == r["days"]
    assert p["ref_symbols"] == r["symbols"]


def test_critical_slope_sits_between_the_two_sources() -> None:
    """判定边界反解出的临界斜率落在两者之间 ⇒ K5 由产地决定，不由数据决定。"""
    a = _load(SP)
    s = critical_slope(a)
    assert s == pytest.approx(a["slope"]["critical_slope_bp_per_bar"], abs=5e-4)
    assert k11(a) is True
    assert a["verdict"]["K11_verdict_flips_with_slope"] is True
    assert s < a["inputs"]["coh_endpoint_slope_bp_per_bar"]


def test_k5_flips_when_slope_source_changes() -> None:
    """同一份 se_Δ、同一份 gap，只换 dc/dΔ 的产地，K5 从 true 变 false。"""
    a = _load(SP)
    r, c = a["recompute"]["ref"], a["recompute"]["coh"]
    assert r["K5"] is True and c["K5"] is False
    assert r["se_c_at"]["316d"] > a["inputs"]["gap_between_readings"] / 2
    assert c["se_c_at"]["316d"] < a["inputs"]["gap_between_readings"] / 2
    assert r["dc_per_bp"] / c["dc_per_bp"] == pytest.approx(slope_ratio(a), rel=0.02)


def test_days_to_identify_drop_by_the_slope_ratio_squared() -> None:
    """8,482 天 → 262 天：天数是 (dc/dΔ)² 的函数，斜率一变就换量级。"""
    a = _load(SP)
    r, c = a["recompute"]["ref"], a["recompute"]["coh"]
    ratio = r["dc_per_bp"] / c["dc_per_bp"]
    assert r["days_half_gap"] / c["days_half_gap"] == pytest.approx(ratio ** 2, rel=0.03)
    assert c["days_half_gap"] < a["inputs"]["days_available"]


def test_recompute_se_c_is_derived_not_hardcoded() -> None:
    for who in ("ref", "coh"):
        a = _load(SP)
        assert se_c_recompute(a, who, 316) == pytest.approx(
            a["recompute"][who]["se_c_at"]["316d"], abs=5e-3)
        assert se_c_recompute(a, who, 116) == pytest.approx(
            a["recompute"][who]["se_c_at"]["116d"], abs=5e-3)


def test_level_bracket_is_two_identities() -> None:
    """两端各自满足 Δ+余量=打平线；任一被单独改动，这条会红。"""
    a = _load(SP)
    assert level_identity(a, "pessimistic") <= 0.02
    assert level_identity(a, "optimistic") <= 0.02
    assert a["level_bracket"]["margin_optimistic_bp"] > a["level_bracket"]["margin_pessimistic_bp"]


def test_level_bracket_straddles_two_sigma() -> None:
    """14 天 vs 594 天：形状把判决从两侧夹住，加天数修不了。"""
    a = _load(SP)
    lb = a["level_bracket"]
    assert k13(a) is True
    assert lb["t_pessimistic"] < 2.0 < lb["t_optimistic"]
    assert lb["days2s_pessimistic"] > 10 * lb["days2s_optimistic"]
    assert a["verdict"]["K13_level_straddles_2sigma"] is True


def test_days2s_identity() -> None:
    """days2s 必须由 (2·se/margin)² 反推，不是独立填的。"""
    a = _load(SP)
    lb, se = a["level_bracket"], a["inputs"]["se_delta_k52_316d_bp"]
    for side in ("pessimistic", "optimistic"):
        want = a["inputs"]["days_available"] * (2.0 * se / lb[f"margin_{side}_bp"]) ** 2
        assert lb[f"days2s_{side}"] == pytest.approx(want, rel=0.05)


def test_reading_is_the_template_rendered_from_fields() -> None:
    import sys

    sys.path.insert(0, str(ANALYSIS))
    from _artifact_guard import render_reading

    a = _load(SP)
    assert a["reading"] == render_reading(a)
    b = copy.deepcopy(a)
    b["provenance"]["cell_overlap"] = 7
    assert render_reading(b) != a["reading"]


def test_inputs_still_agree_with_upstream_artifacts() -> None:
    """本产物只转述 kstar_condition 的已发布字段，不许自己重算一套。"""
    a, s = _load(SP), _load(KS)
    assert a["inputs"]["ref_slope_bp_per_bar"] == s["slope_main"]["d_delta_dk"]
    assert a["inputs"]["dc_dk_per_bar"] == s["slope_main"]["d_c_dk"]
    assert a["inputs"]["se_delta_k52_316d_bp"] == s["inputs"]["se_delta_k52_316d_bp"]
    assert a["recompute"]["ref"]["dc_per_bp"] == s["position_space"]["dc_per_bp"]
    assert a["level_bracket"]["delta_pessimistic_bp"] == s["level_space"]["delta_at_c_required_bp"]


# ---------------------------------------------------------------- 坏样本
def test_k10_red_when_slope_sources_agree() -> None:
    """若参照类斜率与 cohort 端点同量级，就不存在跨宇宙外推。"""
    a = _load(SP)
    b = copy.deepcopy(a)
    b["inputs"]["coh_endpoint_slope_bp_per_bar"] = abs(a["inputs"]["ref_slope_bp_per_bar"])
    assert k10(b) is False


def test_k11_red_when_gate_is_out_of_range() -> None:
    """把 gap 放大到临界斜率落在区间之外，判定就不再随产地翻转。"""
    a = _load(SP)
    b = copy.deepcopy(a)
    b["inputs"]["gap_between_readings"] = 50.0   # 临界斜率被压到 0.0009
    assert k11(b) is False


def test_k12_red_when_reference_is_as_wide_as_cohort() -> None:
    """若参照类跨度与 cohort 相当，形状可迁移——那外推就不成立。"""
    a = _load(SP)
    b = copy.deepcopy(a)
    b["level_bracket"]["ref_span_bp"] = a["level_bracket"]["coh_span_bp"]
    assert k12(b) is False
    c = copy.deepcopy(a)
    c["level_bracket"]["ref_monotone_violations"] = 0   # 若无回升，平坦是真趋势
    assert k12(c) is False


def test_k13_red_when_both_shapes_agree_on_two_sigma() -> None:
    a = _load(SP)
    b = copy.deepcopy(a)
    b["level_bracket"]["t_optimistic"] = 1.9      # 两种形状都判不够
    assert k13(b) is False
    c = copy.deepcopy(a)
    c["level_bracket"]["t_pessimistic"] = 2.1      # 两种形状都判够
    assert k13(c) is False


def test_extrapolation_red_when_any_cell_is_shared() -> None:
    """只要共享一个 (date,symbol)，'外推'这个词就不准了——必须能红。"""
    a = _load(SP)
    b = copy.deepcopy(a)
    b["provenance"]["cell_overlap"] = 40
    assert provenance_is_extrapolation(b) is False


# ---------------------------------------------------------------- 口径一致性（本轮新增）
def test_coh_dc_is_flagged_as_mixed_universe() -> None:
    """cohort 的 dc/dΔ 分子是参照类的 dc/dk ⇒ 必须自报混用，否则下游当实测。"""
    a = _load(SP)
    c = a["recompute"]["coh"]
    assert mixes_universes(a) is True
    assert c["denominator_from"] == "cohort_secant"
    assert a["slope_sensitivity"]["mixed_numerator_from"] == "reference"


def test_flip_direction_is_not_identified() -> None:
    """K11 只说「对斜率敏感」；方向缺两个未测的量 ⇒ 显式 False，且不进 all_pass。"""
    a = _load(SP)
    assert a["verdict"]["K11_verdict_flips_with_slope"] is True
    assert a["verdict"]["K11b_flip_direction_identified"] is False
    assert flip_direction_is_open(a) is True
    others = [v for k, v in a["verdict"].items()
              if k not in ("all_pass", "K11b_flip_direction_identified")]
    assert a["verdict"]["all_pass"] == all(others)


def test_tangent_migration_is_recorded_but_inadmissible() -> None:
    """迁移的**算术**保留，但必须同时记下它不合法：弯曲方向相反。

    上一版把这条写成「K5 翻回 true」并当作结论。参照类的 Δ 随 k 下降（端点差 −18.78、
    含 94 段回升），cohort 随 k 上升（+94.65）——切线/割线比是形状的函数，形状反向时
    这个比不能换宇宙。所以 K5_at_migrated_tangent 只能当敏感性上界，不能当方向。
    """
    a = _load(SP)
    s = a["slope_sensitivity"]
    assert 0.5 < s["tangent_to_secant_ref"] < 1.0
    assert s["K5_at_secant"] is False
    assert s["K5_at_migrated_tangent"] is True
    assert s["se_c_at_migrated_tangent"] > a["inputs"]["gap_between_readings"] / 2
    # 关键：迁移被显式判为不合法，且理由是符号相反（可被数据推翻）
    assert s["migration_admissible"] is False
    assert s["curvature_sign_matches"] is False
    assert s["ref_endpoint_delta_bp"] * s["coh_endpoint_delta_bp"] < 0


def test_crit_flatten_is_nine_percent_not_two() -> None:
    """跨线要分母平多少：9.0%，不是 2%。

    上一版散文写「切线只需比割线平 2%（0.396×0.98）就跨线」——0.38811 仍高于临界
    0.3604，2% 根本不跨线。这条测试同时钉住算法，防止那句话以任何形式回来。
    """
    a = _load(SP)
    s, i = a["slope_sensitivity"], a["inputs"]
    star = i["dc_dk_per_bar"] * i["se_delta_k52_316d_bp"] / (i["gap_between_readings"] / 2.0)
    want = (1.0 - star / i["coh_endpoint_slope_bp_per_bar"]) * 100.0
    assert s["crit_flatten_pct"] == pytest.approx(want, abs=0.02)
    assert s["crit_flatten_pct"] > 5.0
    # 平 2% 不跨线：这是上一版那句话的反例
    assert i["coh_endpoint_slope_bp_per_bar"] * 0.98 > star
    # 平到 crit_flatten_pct 才刚好跨线
    assert i["coh_endpoint_slope_bp_per_bar"] * (1 - s["crit_flatten_pct"] / 100.0) == (
        pytest.approx(star, rel=0.01))


def test_ref_slope_caliber_band_exceeds_headroom() -> None:
    """参照类自己的斜率口径跨 2.73 倍，而判定余量只有 9.9% ⇒ 方向不由参照类给出。

    四个窗口是 kstar_condition 已发布的 slopes；这里验证产物转述的是那四个数，
    不是另算一套。
    """
    a, k = _load(SP), _load(KS)
    s = a["slope_sensitivity"]
    ddk = [abs(float(x["d_delta_dk"])) for x in k["slopes"]]
    dc = [abs(float(x["d_c_dk"])) for x in k["slopes"]]
    assert s["d_delta_dk_min"] == pytest.approx(min(ddk), abs=1e-5)
    assert s["d_delta_dk_max"] == pytest.approx(max(ddk), abs=1e-5)
    assert s["dc_dk_min"] == pytest.approx(min(dc), abs=1e-5)
    assert s["dc_dk_max"] == pytest.approx(max(dc), abs=1e-5)
    assert s["d_delta_dk_spread_ratio"] > 2.0


def test_secant_caliber_changes_the_ratio_by_three_times() -> None:
    """「切/割」的分母怎么量不是细节：max−min/239、端点/230、全区 OLS 差 2.7 倍。"""
    a = _load(SP)
    s = a["slope_sensitivity"]
    assert s["ref_secant_caliber"] == "ref_span(max-min)/239"
    assert s["ref_span_k"] == 230
    r = sorted((s["tangent_to_secant_ref"], s["tangent_to_secant_endpoint"],
                s["tangent_to_secant_full_range"]))
    assert r[-1] / r[0] > 2.0          # 0.299 … 0.851
    assert s["tangent_to_secant_full_range"] < s["tangent_to_secant_ref"]


def test_dc_dk_band_keeps_k5_false() -> None:
    """参照类 dc/dk 的口径带内 K5 不翻——翻只能来自 cohort 的分母，那里没有产地。"""
    a = _load(SP)
    s, i = a["slope_sensitivity"], a["inputs"]
    half = i["gap_between_readings"] / 2.0
    assert s["se_c_at_dc_dk_min"] == pytest.approx(
        i["se_delta_k52_316d_bp"] * s["dc_dk_min"] / i["coh_endpoint_slope_bp_per_bar"], abs=1e-4)
    assert s["se_c_at_dc_dk_max"] == pytest.approx(
        i["se_delta_k52_316d_bp"] * s["dc_dk_max"] / i["coh_endpoint_slope_bp_per_bar"], abs=1e-4)
    assert s["se_c_at_dc_dk_max"] < half
    assert s["K5_stable_across_dc_dk_band"] is True


def test_headroom_is_same_order_as_caliber_error() -> None:
    """跨线余量与口径误差同阶：「c 线性」距边界只剩 0.59% ⇒ 结论在噪声里。"""
    a = _load(SP)
    s = a["slope_sensitivity"]
    assert s["dc_dk_headroom_pct"] < 15.0
    assert s["dc_dk_linear_headroom_pct"] < 2.0
    assert abs(s["dc_dk_linear_c"] - 1.0 / (a["inputs"]["coh_bars"] - 1)) <= 1e-6


def test_crit_dc_dk_reproduces_from_inputs() -> None:
    """临界 dc/dk 必须能从 (gap/2)/se·割线 反推，不是独立填的。"""
    a = _load(SP)
    s, i = a["slope_sensitivity"], a["inputs"]
    want = ((i["gap_between_readings"] / 2.0) / i["se_delta_k52_316d_bp"]
            * i["coh_endpoint_slope_bp_per_bar"])
    assert s["crit_dc_dk"] == pytest.approx(want, rel=1e-3)


def test_mixed_flag_red_when_numerator_is_cohort() -> None:
    """若分子真换成 cohort 自己的 dc/dk，混用标记必须能被判否。"""
    a = _load(SP)
    b = copy.deepcopy(a)
    b["recompute"]["coh"]["numerator_from"] = "cohort"
    assert mixes_universes(b) is False


def test_direction_red_when_both_calibers_agree() -> None:
    """两种斜率口径给出同一个 K5，方向就确定了——检查器必须能红。"""
    a = _load(SP)
    b = copy.deepcopy(a)
    b["slope_sensitivity"]["K5_at_migrated_tangent"] = False
    assert flip_direction_is_open(b) is False
