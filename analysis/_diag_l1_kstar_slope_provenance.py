"""k* 判定的斜率产地：「不可辨」这个结论的唯一下游依赖，是谁给的。

为什么需要它
------------
`_diag_l1_kstar_condition.py` 的结论是「换参数化也判不了，316 天下 se_c 仍比口径差宽
4 倍，要 8,481 天」。这个结论把 116 天判死、也把 316 天判死。但它的 **两个** 定量
出口都共用同一个分母：

    se_c(T) = se_Δ · |dc/dΔ|,      |dc/dΔ| = |d_c_dk / d_delta_dk|

`d_delta_dk = −0.06949`、`d_c_dk = 0.00383` 来自 `_diag_l1_kstar_condition._curve()`
→ `_diag_l1_kcurve.load_reference()` → **`data/kline_minute/date=*`**。那是本地分钟库：
19 个可用分区（2026-10-09 → 11-04）、40 只、748 格，**全部 000xxx.SZ**。
（这三个数不写死在散文里，以 `provenance` 块的实测值为准；上一版此处留了 18/708/11-03 的
陈旧值，而产物早已是 19/748/11-04 —— 散文与产物不同产地，本身就是本条要修的病。）

本脚本只做一件事：把这 708 格与 cohort 的 2,752 格**按 (date, symbol) 求交**，然后
用 cohort 自己的两个实测端点（Δ(1)=14.64、Δ(240)=109.29）给出**同一宇宙内**的端点
斜率，替换参照类斜率重算一遍。如果结论翻转，说明「不可辨」不是数据的性质，是斜率的
产地。

口径
----
- 交集按 (date, symbol) 计，不按 symbol 计：两个时间窗必须真的重叠。
- cohort 端点斜率 = (Δ(240) − Δ(1)) / (FULL_BARS − 1)，是**连线**不是局部导数。
  它只回答「这条曲线的平均陡度比参照类高多少」，不回答 k≈52 处的局部斜率。
- 临界斜率 slope* 由判定边界反解：se_c = gap/2  ⇒  slope* = dc_dk·se_Δ/(gap/2)。
  slope* 落在 [参照类, cohort 端点] 之间 ⇒ K5 的取值**由斜率产地决定**，与数据无关。
- 水平口径（level_space）的余量同样不是一个数：参照类的归一化形状给 8.37bp，
  c 线性给 55.32bp。两者跨过 t=2 ⇒ 判决由形状决定。

预注册（可被本产物直接否证）
----------------------------
    K10 斜率跨宇宙。 参照类斜率与 cohort 端点斜率之比 ≥ 2 ⇒ 用参照类斜率算 se_c
        属跨宇宙外推，`dc_per_bp` 在 cohort 上没有产地。
    K11 判定由产地翻转。 slope* 严格落在 [参照类, cohort 端点] 之间 ⇒
        K5_inversion_ill_conditioned 的 true/false 随斜率来源翻转。
    K12 形状是选择效应。 cohort 的 Δ 跨度 ≥ 2 倍参照类跨度，且参照类曲线非单调
        （有回升段）⇒ 参照类的「平坦」是随机大盘股的日内震荡，不能迁移到低开 cohort。
    K13 水平口径跨 2σ。 最乐观形状 t ≥ 2 且最悲观形状 t < 2 ⇒ 判决由形状决定，
        必须实测曲线，而不是加窗口或加广度。
    K14 翻转方向不可判。 `dc/dΔ` 的分子是参照类的 dc/dk、分母是 cohort 的 [1,240]
        割线；把参照类自己的切/割比（0.816，按 max−min/239 量）迁移过来，K5 就翻回
        true。但这条迁移本身不合法：参照类的 Δ 随 k **下降**（端点差 −18.78、含 94 段
        回升），cohort 随 k **上升**（+94.65），弯曲方向相反 ⇒ 形状比换不了宇宙。
        再加上参照类的斜率口径自带 2.73× 跨度、而跨线只需 9.0% ⇒ 两个方向都能由
        「口径怎么选」造出来。K14 是诊断，不进 all_pass。

只读本地产物与本地分区，不联网。
"""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _artifact_guard import assert_clean, render_reading  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "_diag_l1_kstar_slope_provenance_result.json"
KCJ = HERE / "_diag_l1_kcurve_result.json"
KSJ = HERE / "_diag_l1_kstar_condition_result.json"
PART = ROOT / "data" / "research" / "l1_open_depth" / "part.parquet"
MINUTE = ROOT / "data" / "kline_minute"

COH_BARS = 240        # part.parquet 的实测 nbars（min=max=240）
REF_MIN_BARS = 200    # _diag_l1_kcurve.FULL_BARS：参照类的入选门槛，不是 240


def _reference_cells() -> dict:
    """本地分钟库的可用分区与 (date, symbol) 集合，判据与 load_reference 一致。

    门槛用 200（kcurve.FULL_BARS），不是 cohort 的 240：两者不是同一个数，
    混用会少算一个分区（19→18）。
    """
    cells: set[tuple[str, str]] = set()
    dates: list[str] = []
    syms: set[str] = set()
    for mf in sorted(glob.glob(str(MINUTE / "date=*" / "*.parquet"))):
        date = mf.split("date=")[1][:10]
        m = pl.read_parquet(mf, columns=["symbol", "datetime"])
        nb = int(m.group_by("symbol").len()["len"].min())
        t0 = str(m["datetime"].dt.time().min())
        if t0 != "09:31:00" or nb < REF_MIN_BARS:
            continue
        dates.append(date)
        s = set(m["symbol"].unique().to_list())
        syms |= s
        cells |= {(date, x) for x in s}
    return {"cells": cells, "dates": dates, "symbols": syms}


def main() -> None:
    t_start = time.time()
    kc = json.loads(KCJ.read_text(encoding="utf-8"))
    ks = json.loads(KSJ.read_text(encoding="utf-8"))

    # ---- 下游依赖：全部取自 kstar_condition 的已发布字段，不重算它的中间量
    ref_slope = float(ks["slope_main"]["d_delta_dk"])          # −0.06949
    dc_dk = float(ks["slope_main"]["d_c_dk"])                  # 0.00383
    se = float(ks["inputs"]["se_delta_k52_316d_bp"])           # 5.74
    gap = float(ks["position_space"]["gap_between_readings"])  # 0.122
    margin_c = float(ks["position_space"]["margin_below_gate"])  # 0.0111
    dc_ref = float(ks["position_space"]["dc_per_bp"])          # 0.05506
    break_shape = float(ks["level_space"]["delta_at_c_required_bp"])  # 99.55

    d1 = float(kc["endpoints"]["d1"]["cell_mean_bp"])          # 14.64
    d240 = float(kc["endpoints"]["d240"]["cell_mean_bp"])      # 109.29
    breakeven = float(kc["breakeven"]["delta_breakeven_bp"])  # 107.92
    c_req = float(kc["kstar"]["c_required"])                   # 0.4011
    t_have = float(kc["endpoints"]["d1"]["days"])              # 316

    # ---- 交集：按 (date, symbol)
    ref = _reference_cells()
    part = pl.read_parquet(PART).select("date", "symbol")
    pcells = set(map(tuple, part.rows()))
    overlap = pcells & ref["cells"]

    coh_slope = (d240 - d1) / (COH_BARS - 1)
    slope_ratio = coh_slope / abs(ref_slope)

    # 参照类的跨度与单调性：要先于敏感度块算（割线比要用）
    reff = kc["reference"]
    cd = [float(x) for x in reff["curve_delta_bp"]]
    ref_span = max(cd) - min(cd)
    ref_viol = int(reff["monotone_violations"])
    ref_r2 = float(reff["ols_r2"])

    # ---- 临界斜率：se_c 恰等于 gap/2 的那条线
    slope_star = dc_dk * se / (gap / 2.0)
    lo_s, hi_s = sorted((abs(ref_slope), coh_slope))

    # dc/dΔ 的**分子** dc_dk 取自参照类（kstar_condition.slope_main），
    # **分母** coh_slope 是 cohort 的 [1,240] 割线。这是两个宇宙、两种导数口径的商，
    # 不是 cohort 的 dc/dΔ。名字里必须带 mixed，否则下游会当实测用。
    dc_coh = dc_dk / coh_slope

    # ---- 口径一致性：割线 vs 切线，以及分子到底是谁的
    crit_dc_per_bp = (gap / 2.0) / se           # dc/dΔ 低于这条线，K5 才翻
    crit_dc_dk = crit_dc_per_bp * coh_slope     # 等价地，dc/dk 的线
    ref_secant = ref_span / (COH_BARS - 1)      # 参照类自己的割线，同一根数
    tan_to_sec = abs(ref_slope) / ref_secant    # 参照类切线比割线平多少
    dc_tangent_migrated = dc_dk / (coh_slope * tan_to_sec)

    # ---- 口径带：参照类自己的斜率不是一个数，是四个窗口给出的一个带
    # kstar_condition.slopes 已发布四个窗口（40-70 主口径、30-90、45-82、0-239）。
    # 主口径的 0.06949 与全区间 OLS 的 0.02545 差 2.73 倍；dc/dk 差 17.5%。
    # 判定余量只有 9.9% ⇒ 参照类内部的口径选择就能把它跨过去，方向不由参照类决定。
    band = ks["slopes"]
    ddk_abs = [abs(float(s["d_delta_dk"])) for s in band]
    dc_abs = [abs(float(s["d_c_dk"])) for s in band]
    ddk_lo, ddk_hi = min(ddk_abs), max(ddk_abs)
    dc_lo, dc_hi = min(dc_abs), max(dc_abs)
    se_at_dc_lo, se_at_dc_hi = se * dc_lo / coh_slope, se * dc_hi / coh_slope

    ref_endpoint = cd[-1] - cd[0]           # −18.78：端点差，不是 max−min
    ref_k = [int(x) for x in kc["reference"]["curve_k"]]
    ref_span_k = max(ref_k) - min(ref_k)    # 230，不是 cohort 的 239
    coh_endpoint = d240 - d1                # +94.65
    sign_matches = bool(ref_endpoint * coh_endpoint > 0)
    # 参照类的切/割比随「割线怎么量」再变一次：max−min/239、端点/230、全区 OLS
    ref_secant_endpoint = abs(ref_endpoint) / ref_span_k
    tan_to_sec_endpoint = abs(ref_slope) / ref_secant_endpoint
    tan_to_sec_full = ddk_lo / ref_secant
    # 跨线只需要分母比割线平多少
    crit_flatten_pct = (1.0 - slope_star / coh_slope) * 100.0

    sensitivity = {
        "mixed_numerator_from": "reference",
        "identified": False,
        "why": "cohort 的 dc/dk 与 k≈52 局部斜率都无产地：part.parquet 只有 b0_* 九列，无逐根成交额",
        "coh_secant_bp_per_bar": round(coh_slope, 5),
        "coh_tangent_k52": None,
        "crit_dc_per_bp": round(crit_dc_per_bp, 6),
        "crit_dc_dk": round(crit_dc_dk, 6),
        "dc_dk_ratio_needed": round(crit_dc_dk / dc_dk, 4),
        "dc_dk_headroom_pct": round((crit_dc_dk / dc_dk - 1.0) * 100.0, 2),
        "dc_dk_linear_c": round(1.0 / (COH_BARS - 1), 6),
        "dc_dk_linear_headroom_pct": round(
            (crit_dc_dk / (1.0 / (COH_BARS - 1)) - 1.0) * 100.0, 2),
        # ↓ 跨线条件是**分母**变平，不是分子变大；两个余量是两个方向，不许混写
        "crit_flatten_pct": round(crit_flatten_pct, 2),
        "tangent_to_secant_ref": round(tan_to_sec, 3),
        "ref_secant_caliber": "ref_span(max-min)/239",
        "ref_span_k": ref_span_k,
        "ref_secant_endpoint_bp_per_bar": round(ref_secant_endpoint, 5),
        "tangent_to_secant_endpoint": round(tan_to_sec_endpoint, 3),
        "tangent_to_secant_full_range": round(tan_to_sec_full, 3),
        "se_c_at_migrated_tangent": round(float(se * dc_tangent_migrated), 3),
        "K5_at_secant": bool(se * (dc_dk / coh_slope) > gap / 2.0),
        "K5_at_migrated_tangent": bool(se * dc_tangent_migrated > gap / 2.0),
        # ↓ 口径带：参照类内部的选择就能跨过 9.9% 的余量吗
        "d_delta_dk_min": round(ddk_lo, 5),
        "d_delta_dk_max": round(ddk_hi, 5),
        "d_delta_dk_spread_ratio": round(ddk_hi / ddk_lo, 2),
        "dc_dk_min": round(dc_lo, 5),
        "dc_dk_max": round(dc_hi, 5),
        "dc_dk_spread_ratio": round(dc_hi / dc_lo, 2),
        "se_c_at_dc_dk_min": round(se_at_dc_lo, 4),
        "se_c_at_dc_dk_max": round(se_at_dc_hi, 4),
        "K5_stable_across_dc_dk_band": bool(
            (se_at_dc_lo > gap / 2.0) == (se_at_dc_hi > gap / 2.0)),
        # ↓ 迁移是否合法：弯曲方向相反 ⇒ 切/割比换不了宇宙
        "ref_endpoint_delta_bp": round(ref_endpoint, 2),
        "ref_curve_first_bp": round(cd[0], 2),
        "ref_curve_last_bp": round(cd[-1], 2),
        "coh_endpoint_delta_bp": round(coh_endpoint, 2),
        "curvature_sign_matches": sign_matches,
        "migration_admissible": False,
        "migration_reason": (
            "参照类 Δ 随 k 下降（端点差为负、含回升段），cohort 随 k 上升；"
            "切线/割线比是曲线形状的函数，形状反向时该比不可迁移"
        ),
    }

    def se_c(t_days: float, dc: float) -> float:
        return se * np.sqrt(t_have / t_days) * dc

    def days_half_gap(dc: float) -> float:
        return t_have * (se * dc / (gap / 2.0)) ** 2

    recompute = {
        "ref": {
            "dc_per_bp": round(dc_ref, 5),
            "se_c_at": {f"{t}d": round(float(se_c(t, dc_ref)), 3) for t in (60, 116, 316)},
            "days_half_gap": int(round(days_half_gap(dc_ref))),
            "K5": bool(se_c(t_have, dc_ref) > gap / 2.0),
            "K6": bool(se_c(116, dc_ref) > gap / 2.0),
        },
        "coh": {
            "dc_per_bp": round(dc_coh, 5),
            # 自报混用：分子 dc_dk 是参照类的，分母是 cohort 割线 ⇒ 这不是 cohort 的 dc/dΔ
            "numerator_from": "reference",
            "denominator_from": "cohort_secant",
            "is_mixed": True,
            "se_c_at": {f"{t}d": round(float(se_c(t, dc_coh)), 3) for t in (60, 116, 316)},
            "days_half_gap": int(round(days_half_gap(dc_coh))),
            "K5": bool(se_c(t_have, dc_coh) > gap / 2.0),
            "K6": bool(se_c(116, dc_coh) > gap / 2.0),
        },
    }

    # ---- 水平口径的区间：参照类形状 vs c 线性
    delta_linear = d1 + (d240 - d1) * c_req
    m_hi = breakeven - delta_linear        # 乐观（c 线性）
    m_lo = breakeven - break_shape         # 悲观（参照类形状）
    t_hi = m_hi / se
    t_lo = m_lo / se
    d2s_lo = t_have * (2.0 * se / m_hi) ** 2
    d2s_hi = t_have * (2.0 * se / m_lo) ** 2

    # 参照类 Δ 的跨度与单调性已在上面算过（ref_span/ref_viol/ref_r2）
    coh_span = d240 - d1

    verdict = {
        "K10_slope_crosses_universe": bool(slope_ratio >= 2.0),
        "K11_verdict_flips_with_slope": bool(lo_s < slope_star < hi_s),
        "K12_shape_is_selection_effect": bool(
            coh_span / ref_span >= 2.0 and ref_viol > 0
        ),
        "K13_level_straddles_2sigma": bool(t_lo < 2.0 <= t_hi),
        # K11b 是**诊断**不是断言：判定确实随斜率翻转，但翻向哪边取决于两个从未被测的量
        # （cohort 的 dc/dk、cohort 在 k≈52 的局部斜率）。把它放进 all_pass 会让
        # 「结论对斜率敏感」冒充「翻转方向已知」——这是上一版的真错。
        "K11b_flip_direction_identified": False,
    }
    verdict["all_pass"] = all(
        v for k, v in verdict.items() if k not in ("all_pass", "K11b_flip_direction_identified")
    )

    out = {
        "purpose": "k* 判定链的斜率产地；检验「se_c 永远不够」是否只是参照类外推",
        "caliber": (
            "cohort 端点斜率 = (Δ(240)−Δ(1))/(FULL_BARS−1)，是连线不是局部导数；"
            "临界斜率由 se_c=gap/2 反解；水平口径区间取参照类形状与 c 线性两端"
        ),
        "inputs": {
            "ref_slope_bp_per_bar": round(ref_slope, 5),
            "coh_endpoint_slope_bp_per_bar": round(coh_slope, 5),
            "dc_dk_per_bar": round(dc_dk, 5),
            "se_delta_k52_316d_bp": round(se, 2),
            "gap_between_readings": round(gap, 3),
            "margin_below_gate": round(margin_c, 4),
            "d1_bp": round(d1, 2),
            "d240_bp": round(d240, 2),
            "breakeven_bp": round(breakeven, 2),
            "c_required": round(c_req, 4),
            "days_available": int(t_have),
            "coh_bars": COH_BARS,
            "ref_min_bars": REF_MIN_BARS,
        },
        "provenance": {
            "ref_cells": len(ref["cells"]),
            "ref_days": len(ref["dates"]),
            "ref_symbols": len(ref["symbols"]),
            "ref_date_min": ref["dates"][0] if ref["dates"] else None,
            "ref_date_max": ref["dates"][-1] if ref["dates"] else None,
            "coh_cells": len(pcells),
            "cell_overlap": len(overlap),
            "symbol_overlap": len(ref["symbols"] & set(part["symbol"].unique().to_list())),
        },
        "slope": {
            "slope_ratio_coh_over_ref": round(slope_ratio, 2),
            "critical_slope_bp_per_bar": round(slope_star, 5),
            "coh_headroom_pct": round((coh_slope / slope_star - 1.0) * 100.0, 1),
            "ref_shortfall_pct": round((1.0 - abs(ref_slope) / slope_star) * 100.0, 1),
        },
        "recompute": recompute,
        "slope_sensitivity": sensitivity,
        "level_bracket": {
            # pessimistic = 参照类归一化形状；optimistic = c 线性。两个都不是实测形状。
            "delta_pessimistic_bp": round(break_shape, 2),
            "delta_optimistic_bp": round(delta_linear, 2),
            "margin_pessimistic_bp": round(m_lo, 2),
            "margin_optimistic_bp": round(m_hi, 2),
            "t_pessimistic": round(t_lo, 2),
            "t_optimistic": round(t_hi, 2),
            "days2s_pessimistic": int(round(d2s_hi)),
            "days2s_optimistic": int(round(d2s_lo)),
            "coh_span_bp": round(coh_span, 2),
            "ref_span_bp": round(ref_span, 2),
            "ref_monotone_violations": ref_viol,
            "ref_ols_r2": round(ref_r2, 3),
            "span_ratio_coh_over_ref": round(coh_span / ref_span, 2),
        },
        "verdict": verdict,
        "findings": [
            "se_c 的两个因子（se 放大 2.60×、斜率 −0.06949）都来自本地分钟库，与 cohort 的 (date,symbol) 交集为 0",
            f"cohort 端点斜率是参照类的 {slope_ratio:.2f} 倍；临界斜率落在两者之间 ⇒ "
            "「不可辨」由斜率产地决定，不由数据决定",
            "但『翻转』本身也是拼出来的：dc_coh 的分子 dc_dk 是参照类的，分母才是 cohort 割线，"
            "cohort 从没有过自己的 dc/dΔ",
            f"跨线只需分母比割线平 {crit_flatten_pct:.2f}%，而参照类自己的斜率口径就跨 "
            f"{ddk_hi / ddk_lo:.2f} 倍 ⇒ 余量小于口径误差，方向不由参照类给出",
            f"参照类的切/割比不是一个数：max−min/239 给 {tan_to_sec:.3f}、端点/230 给 "
            f"{tan_to_sec_endpoint:.3f}、全区 OLS 给 {tan_to_sec_full:.3f}；"
            "且它的 Δ 随 k 下降而 cohort 上升，形状比不可迁移",
            "水平口径的余量在 8.37–55.32bp 之间，跨过 t=2 ⇒ 判决由曲线形状决定，加窗口和加广度都修不了",
            "参照类的平坦是 40 只随机大盘股的日内震荡（跨度 19.4bp、有回升），cohort 是低开 1% 以上的单边修复（跨度 94.65bp）",
        ],
    }

    tpl = (
        "`se_c` 能算成 {se316}，靠的是 `dc/dΔ`={dcb_ref}，而它的两个因子——"
        "斜率 {sref}bp/根与 se 放大 2.60×——都取自 `kline_minute`：{rcd} 天 × {rcs} 只、"
        "共 {rcc} 格，时间窗 {rd0}→{rd1}。把它与 cohort 的 {ccc} 格**按 (date,symbol) 求交**，"
        "得 {ovl} 格 ⇒ 这是外推，不是抽样。\n"
        "cohort 自己的两个端点给出斜率 {scoh}bp/根，是参照类的 {rat} 倍；"
        "判定边界 `se_c=gap/2` 反解出的临界斜率是 {sstar}bp/根——"
        "**恰好落在这两者之间**，离 cohort 端点只差 {hd}%。于是 `K5` 在参照类斜率下是 {k5r}"
        "（se_c={se316r}），在 cohort 斜率下是 {k5c}（se_c={se316c}）。"
        "同一条判定，换个斜率就换一个答案 ⇒ 它测的是斜率产地，不是 k*。\n"
        "水平口径更直接：`Δ(c=0.4011)` 在参照类形状下读 {dpess}bp（余量 {mpess}bp、t={tpess}），"
        "在 c 线性下读 {dopt}bp（余量 {mopt}bp、t={topt}）。打平线在两者之上都还没碰到，"
        "但 2σ 判定被两种形状从两侧夹住（{d2pess} 天 vs {d2opt} 天，{xr} 倍）。\n"
        "根因不是窗口、也不是广度：参照类的 Δ 只跨 {rsp}bp（max−min）、{rv} 段回升、R²={r2}，"
        "那是随机大盘股的日内来回；cohort 是低开 1% 以上的单边修复，跨 {csp}bp。"
        "再往下一层，**「翻转」这个结论自己也不成立**：`dc/dΔ` 的分子 {dcdk} 是参照类的 dc/dk，"
        "分母 {scoh} 是 cohort 的 [1,240] **割线**——两个宇宙、两种导数口径的商。"
        "把判定边界换算到 dc/dk，参照类的 {dcdk} 距它只剩 {dchd}%；"
        "「c 精确线性」给 {dclin}，距边界只剩 {linhd}%。"
        "换个方向算：要让 K5 翻成 true，只要求分母比割线平 {cf}%——"
        "而参照类自己的斜率口径就跨 {spr} 倍（{ddmin}–{ddmax}bp/根），"
        "**余量比口径误差还小**。"
        "把参照类的切/割比 {tts}（它是 max−min/239，不是端点割线：端点口径给 {tte}、"
        "全区 OLS 给 {ttf}）搬到 cohort，se_c 变成 {setan}＞gap/2 ⇒ K5 回到 true；"
        "但这条迁移不合法：参照类的 Δ 从 {dref0}bp 走到 {dref239}bp（{rv} 段回升），"
        "cohort 从 {d1}bp 走到 {d240}bp——**弯曲方向相反**，形状比换不了宇宙。"
        "所以 K11 只能读作「判定对斜率极其敏感」，不能读作「翻转方向已知」——"
        "方向上缺的两个量（cohort 的 dc/dk、k≈52 的局部斜率）都在 cohort 自己的曲线上。"
        "**用对照组的曲线形状去判实验组的参数化，结论只能是「形状没测」。**"
        "而测形状只要一次 2,752 格 × 240 根——这正是 `curves.parquet` 该做的事。"
    )
    fields = {
        "se316": "$recompute.ref.se_c_at.316d",
        "dcb_ref": "$recompute.ref.dc_per_bp",
        "sref": "$inputs.ref_slope_bp_per_bar",
        "rcd": "$provenance.ref_days",
        "rcs": "$provenance.ref_symbols",
        "rcc": "$provenance.ref_cells",
        "rd0": "$provenance.ref_date_min",
        "rd1": "$provenance.ref_date_max",
        "ccc": "$provenance.coh_cells",
        "ovl": "$provenance.cell_overlap",
        "scoh": "$inputs.coh_endpoint_slope_bp_per_bar",
        "rat": "$slope.slope_ratio_coh_over_ref",
        "sstar": "$slope.critical_slope_bp_per_bar",
        "hd": "$slope.coh_headroom_pct",
        "k5r": "$recompute.ref.K5",
        "se316r": "$recompute.ref.se_c_at.316d",
        "k5c": "$recompute.coh.K5",
        "se316c": "$recompute.coh.se_c_at.316d",
        "dpess": "$level_bracket.delta_pessimistic_bp",
        "mpess": "$level_bracket.margin_pessimistic_bp",
        "tpess": "$level_bracket.t_pessimistic",
        "dopt": "$level_bracket.delta_optimistic_bp",
        "mopt": "$level_bracket.margin_optimistic_bp",
        "topt": "$level_bracket.t_optimistic",
        "d2pess": "$level_bracket.days2s_pessimistic",
        "d2opt": "$level_bracket.days2s_optimistic",
        "xr": "$level_bracket.span_ratio",
        "rsp": "$level_bracket.ref_span_bp",
        "csp": "$level_bracket.coh_span_bp",
        "rv": "$level_bracket.ref_monotone_violations",
        "r2": "$level_bracket.ref_ols_r2",
        "dcdk": "$inputs.dc_dk_per_bar",
        "dchd": "$slope_sensitivity.dc_dk_headroom_pct",
        "dclin": "$slope_sensitivity.dc_dk_linear_c",
        "linhd": "$slope_sensitivity.dc_dk_linear_headroom_pct",
        "tts": "$slope_sensitivity.tangent_to_secant_ref",
        "setan": "$slope_sensitivity.se_c_at_migrated_tangent",
        "cf": "$slope_sensitivity.crit_flatten_pct",
        "spr": "$slope_sensitivity.d_delta_dk_spread_ratio",
        "ddmin": "$slope_sensitivity.d_delta_dk_min",
        "ddmax": "$slope_sensitivity.d_delta_dk_max",
        "tte": "$slope_sensitivity.tangent_to_secant_endpoint",
        "ttf": "$slope_sensitivity.tangent_to_secant_full_range",
        "d1": "$inputs.d1_bp",
        "d240": "$inputs.d240_bp",
        "dref0": "$slope_sensitivity.ref_curve_first_bp",
        "dref239": "$slope_sensitivity.ref_curve_last_bp",
    }
    out["level_bracket"]["span_ratio"] = round(d2s_hi / max(d2s_lo, 1.0), 1)
    out["reading_template"] = tpl
    out["reading_fields"] = fields
    out["elapsed_s"] = round(time.time() - t_start, 1)
    out["reading"] = render_reading(out)

    assert_clean(out, "kstar_slope_provenance")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in
                      ("provenance", "slope", "recompute", "level_bracket", "verdict")},
                     ensure_ascii=False, indent=1))
    print("\n" + out["reading"])


if __name__ == "__main__":
    main()
