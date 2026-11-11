"""`_diag_l1_kstar_condition_result.json` 的不变量：把「还需多少天」变成可红的判据。

为什么这轮要单开一个文件
------------------------
上一轮建议「首轮窗口 60→116 天」，理由是「19bp 的差在 60 天下只有 1.44σ」。
这句话把**位置问题**（`c(k*)≥0.4011` 落在横轴哪里）换成了**水平问题**（19bp 的差能不能测出）。
两者的样本量要求差三个量级，而这句话看起来完全合理：19 是个正经的 bp 数，1.44 是真 σ。

四条各配坏样本：
① 决策量的 se 是 `se_Δ/|斜率|·dc/dk`，不是 `se_Δ`；
② 116 天下的 `se_c` 仍大于整段口径差 ⇒「116 天让 k* 可判」为假；
③ 19bp 在本产物里没有产地（既不等于 20bp 成本，也不等于 8bp 水平余量）；
④ 换到固定 c 的水平口径后，缺口是 594 天或 1.88 倍广度，不是无限。

`se_c` 是**下界**：斜率本身也有抽样误差，本文件没把它算进来。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = ROOT / "analysis"
SC = "_diag_l1_kstar_condition_result.json"
KC = "_diag_l1_kcurve_result.json"


def _load(name: str) -> dict:
    return json.loads((ANALYSIS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 检查器
def se_c(a: dict, t_days: float) -> float:
    """se_Δ(k=52) 按 1/√天数缩放，再乘 |dc/dΔ|。"""
    i = a["inputs"]
    return i["se_delta_k52_316d_bp"] * (i["t_days_available"] / t_days) ** 0.5 * a["position_space"]["dc_per_bp"]


def days_for(a: dict, target_c: float) -> float:
    return a["inputs"]["t_days_available"] * (
        se_c(a, a["inputs"]["t_days_available"]) / target_c
    ) ** 2


def k5_inversion_ill_conditioned(a: dict) -> bool:
    p = a["position_space"]
    return se_c(a, a["inputs"]["t_days_available"]) > p["gap_between_readings"] / 2


def k6_116d_does_not_identify(a: dict) -> bool:
    return se_c(a, 116) > a["position_space"]["gap_between_readings"] / 2


def k7_target_unsourced(a: dict) -> bool:
    t = a["power_target"]
    return not t["equals_cost"] and not t["equals_this_margin"]


def level_margin_identity(a: dict) -> float:
    """Δ(c=判据) + 余量 = 打平线。三者只许有一个产地。"""
    lv, i = a["level_space"], a["inputs"]
    return abs(lv["delta_at_c_required_bp"] + lv["margin_bp"] - i["breakeven_bp"])


# ---------------------------------------------------------------- 好样本
def test_se_c_is_derived_from_published_se_and_slope() -> None:
    """se_c 不许手填：必须能由 se_Δ 与 dc/dΔ 复算出来。"""
    a = _load(SC)
    assert se_c(a, 316) == pytest.approx(a["position_space"]["se_c_at"]["316d"], abs=5e-3)
    assert se_c(a, 60) == pytest.approx(a["position_space"]["se_c_at"]["60d"], abs=5e-3)


def test_116d_days_do_not_close_the_gap() -> None:
    """核心断言：116 天的 se_c 比整段口径差还宽 ⇒ 放窗口修不了 identified。"""
    a = _load(SC)
    p = a["position_space"]
    assert p["se_c_at"]["116d"] > p["gap_between_readings"]
    assert k6_116d_does_not_identify(a) is True
    assert a["verdict"]["K6_116d_does_not_identify"] is True


def test_inversion_needs_more_days_than_the_panel_has() -> None:
    """8481 / 256135 天：不是「60 天不够」，是这个参数化在可行窗口内不可辨。"""
    a = _load(SC)
    p = a["position_space"]
    assert p["days_for_half_gap"] == pytest.approx(days_for(a, p["gap_between_readings"] / 2), rel=0.02)
    assert p["days_for_half_gap"] > 5000
    assert p["days_for_margin_below"] > 1e5
    assert p["days_for_half_gap"] > 10 * a["inputs"]["t_days_available"]


def test_level_margin_is_an_identity_not_three_numbers() -> None:
    """Δ(判据)+余量=打平线；三个键任一被单独改动，这条会红。"""
    a = _load(SC)
    assert level_margin_identity(a) <= 0.02
    assert a["level_space"]["margin_bp"] > 0
    b = copy.deepcopy(a)
    b["level_space"]["margin_bp"] += 1.0
    assert level_margin_identity(b) > 0.9


def test_power_target_has_no_home_in_this_artifact() -> None:
    """19bp 既不等于 20bp 成本，也不等于 8.37bp 余量 ⇒ 它是从别的决策借来的。"""
    a = _load(SC)
    t = a["power_target"]
    assert t["power_target_bp"] == pytest.approx(19.0)
    assert t["equals_cost"] is False
    assert t["equals_this_margin"] is False
    assert k7_target_unsourced(a) is True
    assert 110 <= t["days_from_target"] <= 120


def test_breadth_beats_days_as_the_lever() -> None:
    """水平口径下缺口是 1.88 倍广度，不是无限天数。"""
    a = _load(SC)
    lv = a["level_space"]
    assert 1.0 < lv["breadth_multiple_needed"] < 4.0
    assert lv["names_per_day_for_2sigma_at_316d"] == pytest.approx(
        lv["names_per_day_now"] * lv["breadth_multiple_needed"], rel=0.02
    )
    assert a["verdict"]["K9_breadth_is_the_lever"] is True


def test_reading_is_the_template_rendered_from_fields() -> None:
    import sys

    sys.path.insert(0, str(ANALYSIS))
    from _artifact_guard import render_reading

    a = _load(SC)
    assert a["reading"] == render_reading(a)
    b = copy.deepcopy(a)
    b["position_space"]["se_c_at"]["116d"] = 0.001
    assert render_reading(b) != a["reading"]


def test_cross_artifact_inputs_still_agree_with_kcurve() -> None:
    """本产物所有输入都必须仍等于 kcurve 发布值；否则两份产物在讲两个故事。"""
    a, k = _load(SC), _load(KC)
    assert a["inputs"]["se_delta_k52_316d_bp"] == k["power"]["se_d52_316d_bp"]
    assert a["inputs"]["breakeven_bp"] == k["breakeven"]["delta_breakeven_bp"]
    assert a["inputs"]["c_required"] == k["kstar"]["c_required"]
    assert a["position_space"]["gap_between_readings"] == pytest.approx(
        k["kstar"]["c_at_high"] - k["kstar"]["c_at_low"], abs=1e-3
    )


# ---------------------------------------------------------------- 坏样本
def test_days_are_flagged_diagnostic_only() -> None:
    """days_for_* 的斜率取自参照类、且切/割口径不一致 ⇒ 必须自报为诊断量，不能当承诺。"""
    a = _load(SC)
    p = a["position_space"]
    assert p["diagnostic_only"] is True
    assert "参照类" in p["caveat"]
    assert "诊断" in a["reading"]
    # 反例：若有人把标记摘掉，下游就没有任何信号说这两个天数不可承诺
    b = copy.deepcopy(a)
    b["position_space"]["diagnostic_only"] = False
    assert b["position_space"]["diagnostic_only"] is not True


def test_k5_red_when_readings_happen_to_agree() -> None:
    """若两种归一化读数本来就在一起，反演就不病态——检查器必须能红。"""
    a = _load(SC)
    b = copy.deepcopy(a)
    b["position_space"]["gap_between_readings"] = 5.0
    assert k5_inversion_ill_conditioned(b) is False


def test_k6_red_when_precision_is_inflated() -> None:
    """把 se 缩小 10 倍（假装精度更高），116 天就会「够判」——正是原计划的错。"""
    a = _load(SC)
    b = copy.deepcopy(a)
    b["inputs"]["se_delta_k52_316d_bp"] = a["inputs"]["se_delta_k52_316d_bp"] / 10.0
    assert k6_116d_does_not_identify(b) is False


def test_k7_red_when_target_matches_this_margin() -> None:
    a = _load(SC)
    b = copy.deepcopy(a)
    b["power_target"]["equals_this_margin"] = True
    assert k7_target_unsourced(b) is False
