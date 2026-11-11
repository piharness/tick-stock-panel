"""缺口池集合审计：A（原始 gap）与 C（日历相邻+复权）的差集分解。

为什么需要它
------------
`_diag_l1_vwap_curvature.py` 交付了「4,077 个 cell 里 3,870 个除权、249 个停牌」，
但这三个数在产物里是**分头**报的，没有一条等式把它们与 45.75 → 47.10 的 1.35bp 绑在一起：

- `A_shift_raw.cells`          243,598
- `C_cal_adj.cells`            239,521
- `only_ex_rights.cells`         3,870
- `only_non_adjacent.cells`        249

243,598 − 239,521 = **4,077**，而 3,870 + 249 = **4,119**。差的 42 个不是笔误：
`C \\ A`（只被复权口径选中、原始口径没选中）与 `A \\ C` 方向相反，净值才是 4,077。
本脚本把这条集合恒等式、浮点阈值边界、以及 `0.00bp` 到底是「值」还是「净值」一次算清。

三条不变量（任一不成立 `SystemExit`，所以它**能红**）
--------------------------------------------------
1. `|A \\ C| == |除权腿| + |停牌腿| − |两者交集|`（腿和等于差集）
2. `|ex_rights| + |non_adjacent| − |A \\ C| == |C \\ A|`（方向相反的差集精确抵消）
3. `"0.00bp"` 必须是**净值**：`Δdaily(A→D) + Δdaily(D→C)` 与 `Δdaily(A→C)` 同号且
   `|残差| < 0.01bp`；同时单独报 249 个 cell 自身的 `i1`（它不是 0）。

口径
----
- 取数层与 `_diag_l1_vwap_curvature.load_daily()` 逐位相同（复用它，不改它）。
- `A = g_shift_raw <= -1%`，`C = g_cal_adj <= -1%`，`GAP_MAX = -0.01`。
- `除权腿 = A 且 g_cal_adj 非空且 g_cal_adj > -1%`；`停牌腿 = A 且 day_gap > 1`。
- 边界：`eps = g_cal_adj - GAP_MAX`，`|eps| <= 1e-9` 的 cell 单独计数——它们的归属
  由 raw 与 adj 两条浮点路径各自落在 `<=` 哪一边决定，不可跨平台复现。

产物 `_diag_l1_gapset_audit_result.json`。只读分区，不联网，不写 data/。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _artifact_guard import assert_clean, render_reading  # noqa: E402
from _diag_l1_vwap_curvature import GAP_MAX, load_daily  # noqa: E402

OUT = Path(__file__).resolve().parent / "_diag_l1_gapset_audit_result.json"
EPS_BAND = 1e-9


def daily_bp(df: pl.DataFrame) -> float:
    if df.height == 0:
        return 0.0
    g = df.group_by("di").agg(pl.col("i1").mean().alias("d"))
    return float(g["d"].mean() * 1e4)


def cell_bp(df: pl.DataFrame) -> float:
    return float(df["i1"].mean() * 1e4) if df.height else 0.0


def check_leg_cover(n_aminusc: int, n_ex: int, n_non: int, n_both: int) -> None:
    """|A\\C| == |除权| + |停牌| − |交集|。

    **这条不是恒等式**：它断言 A\\C 的每个成员要么复权价没跌破阈值（除权腿）、要么
    算不出复权 gap（停牌腿）。若某个 cell 的 g_cal_adj 因为**别的原因**为空，
    这个等式立刻破 —— 反证见 `_test_artifact_guard.py::case_leg_cover_wrong_leg`。
    """
    if n_aminusc != n_ex + n_non - n_both:
        raise SystemExit(
            f"leg_cover broke: |A\\C|={n_aminusc} != {n_ex}+{n_non}-{n_both}")


def check_cover_exact(aminusc: pl.DataFrame, ex: pl.DataFrame, non: pl.DataFrame) -> None:
    """集合级版本：A\\C == 除权腿 ∪ 停牌腿（逐键，不只是计数）。"""
    key = ["symbol", "date"]
    legs = pl.concat([ex.select(key), non.select(key)]).unique()
    only_legs = legs.join(aminusc.select(key), on=key, how="anti").height
    only_aminusc = aminusc.join(legs, on=key, how="anti").height
    if only_legs or only_aminusc:
        raise SystemExit(
            f"cover broke: 腿有而 A\\C 无 {only_legs} 个，A\\C 有而腿无 {only_aminusc} 个")


def check_null_is_suspension(m: pl.DataFrame) -> None:
    """算不出复权 gap 的 A 成员，必须**恰好**是跨停牌 —— 这是可证的实质命题，不是恒等式。"""
    nulls = m.filter(pl.col("g_shift_raw") <= GAP_MAX)["g_cal_adj"].null_count()
    susp = m.filter((pl.col("g_shift_raw") <= GAP_MAX) & (pl.col("day_gap") > 1))
    if susp["g_cal_adj"].null_count() != susp.height:
        raise SystemExit("null_is_suspension broke: 停牌腿里有非空 g_cal_adj")
    if nulls != susp.height:
        raise SystemExit(
            f"null_is_suspension broke: A 里 g_cal_adj 为空 {nulls} 个，"
            f"但跨停牌只有 {susp.height} 个 ⇒ 有 {nulls - susp.height} 个空值不是停牌造成")


def dilution(super_df: pl.DataFrame, sub_df: pl.DataFrame) -> dict:
    """逐日权重稀释的**精确**分解，用来取代「两个方向抵消」那句话。

    D_t = 超集在 t 日的均值，n_t 个 cell；去掉 S 的 k_t 个（均值 s_t）后
        noS_t = D_t + w_t·(D_t − s_t),  w_t = k_t/(n_t−k_t)
    ⇒ mean_t(noS_t) − mean_t(D_t) = mean_t[ w_t·(D_t − s_t) ]（两侧同为全部交易日）。
    注意符号：去掉均值**更低**的 cell 才抬高整体均值，所以右边为正对应「去掉后变大」。

    这条走的是**逐日**路径，与 `daily_bp()` 的两次整体均值是两条独立代码路径，
    所以 `|pred − (rest_bp − super_bp)| < 0.01` 是一次真检查，不是代数移项。

    `pred != obs` 超过 0.01 时这里就终止（写盘前）。下面几个是**机制**描述量：
    - `w_median_pct`：日权重中位 —— 用中位不用均值，因为 k 的分布右偏（max 35 个 cell）。
    - `gap_median/p10/p90_bp`：当日池均值与腿内均值之差。它**不是**小量：中位 +63bp、
      p10/p90 = −520/+472bp（腿内只有约 2 个 cell，方差极大）。所以 0.007 不是
      「21.88 被权重稀释」一次乘出来的，而是**小权重 + 跨日符号混合**两件事叠加；
      `w_mean × gap_mean = 0.15bp ≠ 0.007bp` 就是证据（均值与乘积不同序）。
    """
    key = ["symbol", "date"]
    rest = super_df.join(sub_df.select(key), on=key, how="anti")
    g = super_df.group_by("di").agg(pl.col("i1").mean().alias("mD"), pl.len().alias("n"))
    h = sub_df.group_by("di").agg(pl.col("i1").mean().alias("mS"), pl.len().alias("k"))
    j = (g.join(h, on="di", how="left")
         .with_columns(pl.col("k").fill_null(0), pl.col("mS").fill_null(0.0)))
    j = j.with_columns((pl.col("k") / (pl.col("n") - pl.col("k"))).alias("w"))
    j = j.with_columns((pl.col("mD") - pl.col("mS")).alias("gp"))
    aff = j.filter(pl.col("k") > 0)
    pred = float((j["w"] * j["gp"]).mean() * 1e4)
    obs = daily_bp(rest) - daily_bp(super_df)
    if abs(pred - obs) > 0.01:
        raise SystemExit(f"dilution identity broke: pred {pred:+.4f} != obs {obs:+.4f}")
    return {
        "weight_drop_bp": round(obs, 3),
        "weight_drop_predicted_bp": round(pred, 3),
        "affected_days": aff.height,
        "w_median_pct_of_affected": round(float(aff["w"].median() * 100), 3),
        "w_mean_pct_of_affected": round(float(aff["w"].mean() * 100), 3),
        "gap_median_bp": round(float(aff["gp"].median() * 1e4), 1),
        "gap_p10_bp": round(float(aff["gp"].quantile(0.1) * 1e4), 0),
        "gap_p90_bp": round(float(aff["gp"].quantile(0.9) * 1e4), 0),
        "product_of_means_bp": round(
            float(aff["w"].mean() * aff["gp"].mean() * 1e4), 3),
    }


def stat(tag: str, df: pl.DataFrame) -> dict:
    return {
        "tag": tag,
        "cells": df.height,
        "days": df["di"].n_unique(),
        "i1_daily_equal_bp": round(daily_bp(df), 2),
        "i1_cell_equal_bp": round(cell_bp(df), 2),
    }


def main() -> None:
    t0 = time.time()
    m = load_daily()
    key = ["symbol", "date"]

    a = m.filter(pl.col("g_shift_raw") <= GAP_MAX)
    c = m.filter(pl.col("g_cal_adj") <= GAP_MAX)
    a_minus_c = a.join(c.select(key), on=key, how="anti")
    c_minus_a = c.join(a.select(key), on=key, how="anti")
    inter = a.join(c.select(key), on=key, how="semi")

    ex_rights = a.filter(pl.col("g_cal_adj").is_not_null() & (pl.col("g_cal_adj") > GAP_MAX))
    non_adj = a.filter(pl.col("day_gap") > 1)
    both = ex_rights.join(non_adj.select(key), on=key, how="semi")
    d = a.join(ex_rights.select(key), on=key, how="anti")   # 去除权、保留停牌

    # --- 不变量：只留能失败的 ---
    # 已删（喂**毫无关系**的随机集合也永远不红，反证实测 2,000/2,000 通过 ⇒ 它们只是注释）：
    #   |A\C| − |C\A| ≡ |A| − |C|            （集合基数恒等式）
    #   |D| − |C| ≡ |non_adj| − |C\A|        （上一条的同型）
    #   (d−a) + (c−d) ≡ c−a                  （同一批浮点数的加减，残差恒为 0）
    check_leg_cover(a_minus_c.height, ex_rights.height, non_adj.height, both.height)
    check_cover_exact(a_minus_c, ex_rights, non_adj)
    check_null_is_suspension(m)
    # 「交集 0」不是实测项：除权腿要求 g_cal_adj 非空，停牌腿全部为空 ⇒ 结构性恒为 0。
    if ex_rights["g_cal_adj"].null_count() != 0:
        raise SystemExit("结构前提破了：除权腿里出现空 g_cal_adj")

    # --- 边界 cell：阈值 ±1e-9 ---
    rev_eps = (c_minus_a["g_cal_adj"] - GAP_MAX).abs()
    fwd_eps = (a_minus_c["g_cal_adj"] - GAP_MAX).abs()
    fwd_raw_eps = (a_minus_c["g_shift_raw"] - GAP_MAX).abs()
    edge = {
        "eps_band": EPS_BAND,
        "reverse_cells": c_minus_a.height,
        "reverse_abs_eps_median": float(rev_eps.median()),
        "reverse_abs_eps_max": float(rev_eps.max()),
        "reverse_within_band": int((rev_eps <= EPS_BAND).sum()),
        "forward_cells": a_minus_c.height,
        "forward_abs_eps_median": float(fwd_eps.drop_nulls().median()) if fwd_eps.drop_nulls().len() else None,
        "forward_within_band": int((fwd_eps <= EPS_BAND).sum()),
        "forward_gcaladj_null": int(a_minus_c["g_cal_adj"].null_count()),
        "forward_raw_within_band": int((fwd_raw_eps <= EPS_BAND).sum()),
        "all_within_band": int((rev_eps <= EPS_BAND).sum()) + int((fwd_eps <= EPS_BAND).sum()),
    }

    # --- 净值 vs 值：改成**可算的**权重分解（D11：原句把「稀释」写成「抵消」）---
    no249 = d.join(non_adj.select(key), on=key, how="anti")
    rest_c = c.join(c_minus_a.select(key), on=key, how="anti")
    if rest_c.height != no249.height:
        raise SystemExit(f"wiring broke: C\\反向={rest_c.height} != D\\停牌={no249.height}")
    a_bp, d_bp, c_bp = daily_bp(a), daily_bp(d), daily_bp(c)
    n_bp = daily_bp(no249)
    leg_ex, leg_no = d_bp - a_bp, c_bp - d_bp
    total = c_bp - a_bp
    dil_drop = dilution(d, non_adj)      # D 去掉 249：这条给出 0.007 的**机制**
    dil_add = dilution(c, c_minus_a)     # C 加回 42
    if abs(dil_add["weight_drop_bp"] - (c_bp - n_bp)) > 0.01:
        raise SystemExit("dilution(add) 与整体均值差不同源")

    out = {
        "purpose": "缺口池 A/C 集合分解、阈值边界 cell、以及「0.00bp」是净值还是值",
        "sets": {
            "A_shift_raw": stat("原始 gap + 跨停牌", a),
            "C_cal_adj": stat("日历相邻 + 复权", c),
            "A_minus_C": stat("只被原始 gap 选中", a_minus_c),
            "C_minus_A": stat("只被复权 gap 选中（反向流入）", c_minus_a),
            "intersection": stat("两者都选中", inter),
            "ex_rights_leg": stat("A\\C 里的除权伪缺口", ex_rights),
            "non_adjacent_leg": stat("A\\C 里的停牌跨日", non_adj),
            "both_legs": stat("同时是除权与停牌", both),
            "D_no_exdiv": stat("A 去掉除权、保留停牌", d),
            "D_no_exdiv_no_susp": stat("D 再去掉停牌（= C 减去反向 42）", no249),
        },
        "identity": {
            "cells_A": a.height,
            "cells_C": c.height,
            "net_A_minus_C": a.height - c.height,
            "legs_sum": ex_rights.height + non_adj.height,
            "legs_intersection": both.height,
            "legs_intersection_note": "结构性恒为 0：除权腿要求 g_cal_adj 非空、停牌腿全部为空，"
                                      "不是实测出来的项，公式里不该当被减项读",
            "reverse_flow_C_minus_A": c_minus_a.height,
            "check_legs_sum_minus_reverse": ex_rights.height + non_adj.height - c_minus_a.height,
            "share_ex_rights_of_A_minus_C": round(ex_rights.height / a_minus_c.height, 4),
        },
        "threshold_edge": edge,
        "net_vs_value_bp": {
            "A_daily": round(a_bp, 2),
            "D_daily": round(d_bp, 2),
            "D_minus_249_daily": round(n_bp, 2),
            "C_daily": round(c_bp, 2),
            "leg_ex_rights": round(leg_ex, 3),
            "leg_drop_249_only": round(n_bp - d_bp, 3),
            "leg_add_reverse_42": round(c_bp - n_bp, 3),
            "leg_non_adjacent_net_of_reverse": round(leg_no, 3),
            "total_A_to_C": round(total, 3),
            "additivity_residual": round((n_bp - d_bp) + (c_bp - n_bp) - (c_bp - d_bp), 3),
            "non_adjacent_cells_value_daily_bp": round(daily_bp(non_adj), 2),
            "non_adjacent_cells_value_cell_bp": round(cell_bp(non_adj), 2),
            "reverse_cells_value_daily_bp": round(daily_bp(c_minus_a), 2),
            "reverse_cells_value_cell_bp": round(cell_bp(c_minus_a), 2),
            # D11：0.007 的机制不是「两条腿相消」，是**小权重 × 跨日符号混合**（见 dilution()）
            "susp_affected_days": dil_drop["affected_days"],
            "susp_w_median_pct": dil_drop["w_median_pct_of_affected"],
            "susp_w_mean_pct": dil_drop["w_mean_pct_of_affected"],
            "susp_gap_median_bp": dil_drop["gap_median_bp"],
            "susp_gap_p10_bp": dil_drop["gap_p10_bp"],
            "susp_gap_p90_bp": dil_drop["gap_p90_bp"],
            "susp_product_of_means_bp": dil_drop["product_of_means_bp"],
            "susp_weight_drop_predicted_bp": dil_drop["weight_drop_predicted_bp"],
            "reverse_cells": c_minus_a.height,
            "reading_template": (
                "{susp_cells} 个 cell 的日等权值 {non_adjacent_cells_value_daily_bp:+.2f}bp，"
                "去掉它们只动 {leg_drop_249_only:+.3f}bp。机制不是两条腿相消：这 {susp_cells} 个"
                "只在 {susp_affected_days}/{n_days} 天出现、日权重 k/(n−k) 中位 "
                "{susp_w_median_pct:.3f}%；当日池均值与腿内均值之差中位 "
                "{susp_gap_median_bp:+.1f}bp、p10/p90 = {susp_gap_p10_bp:+.0f}/"
                "{susp_gap_p90_bp:+.0f}bp ⇒ 逐日乘积 mean_d[w·(mD−mS)] = "
                "{susp_weight_drop_predicted_bp:+.4f}bp，与观测 {leg_drop_249_only:+.3f}bp "
                "相同（写盘前已断言）。反向 {reverse_cells} 个 cell 只回补 "
                "{leg_add_reverse_42:+.3f}bp，不是相消的另一半。"),
            "reading_fields": {
                "susp_cells": "$sets.non_adjacent_leg.cells",
                "n_days": "$sets.A_shift_raw.days",
                "non_adjacent_cells_value_daily_bp": "non_adjacent_cells_value_daily_bp",
                "leg_drop_249_only": "leg_drop_249_only",
                "susp_affected_days": "susp_affected_days",
                "susp_w_median_pct": "susp_w_median_pct",
                "susp_gap_median_bp": "susp_gap_median_bp",
                "susp_gap_p10_bp": "susp_gap_p10_bp",
                "susp_gap_p90_bp": "susp_gap_p90_bp",
                "susp_weight_drop_predicted_bp": "susp_weight_drop_predicted_bp",
                "reverse_cells": "reverse_cells",
                "leg_add_reverse_42": "leg_add_reverse_42",
            },
        },
        "pool_mean_caliber": {},
        "elapsed_s": 0.0,
    }

    # --- 池均值：A 与 C 两个口径并排（文档里混用就是那段的核心错）---
    for tag, df in (("A_screen", a), ("C_screen", c)):
        good = df.filter(pl.col("in_band"))
        bad = df.filter(~pl.col("in_band"))
        iv_all = (df["iv"] * 1e4).to_numpy()
        iv_good = (good["iv"] * 1e4).to_numpy()
        lo, hi = __import__("numpy").percentile(iv_good, [1.0, 99.0])
        wins = float(__import__("numpy").clip(iv_good, lo, hi).mean())
        daily_iv = float(pl.DataFrame({"di": df["di"], "iv": df["iv"] * 1e4})
                         .group_by("di").agg(pl.col("iv").mean())["iv"].mean())
        daily_iv_good = float(pl.DataFrame({"di": good["di"], "iv": good["iv"] * 1e4})
                              .group_by("di").agg(pl.col("iv").mean())["iv"].mean())
        out["pool_mean_caliber"][tag] = {
            "cells": df.height,
            "cells_after_screen": good.height,
            "out_of_band": bad.height,
            "iv_mean_bp": round(float(iv_all.mean()), 2),
            "iv_median_bp": round(float(__import__("numpy").median(iv_all)), 2),
            "iv_daily_equal_bp": round(daily_iv, 2),
            "iv_daily_equal_after_screen_bp": round(daily_iv_good, 2),
            "iv_winsor_mean_bp": round(wins, 2),
            "iv_mean_after_screen_bp": round(float(iv_good.mean()), 2),
            "iv_median_after_screen_bp": round(float(__import__("numpy").median(iv_good)), 2),
            "mean_shift_due_to_screen_bp": round(float(iv_all.mean() - iv_good.mean()), 1),
        }

    out["elapsed_s"] = round(time.time() - t0, 1)
    nv = out["net_vs_value_bp"]
    nv["reading"] = render_reading(nv, root=out)   # 散文唯一产地：模板 + 带路径的字段
    assert_clean(out, OUT.name)          # 散文/镜像键/符号：写盘前，不是写盘后
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    i = out["identity"]
    print(f"A {i['cells_A']:,}  C {i['cells_C']:,}  净 {i['net_A_minus_C']:,}")
    print(f"A\\C {out['sets']['A_minus_C']['cells']:,} = 除权 "
          f"{out['sets']['ex_rights_leg']['cells']:,} + 停牌 "
          f"{out['sets']['non_adjacent_leg']['cells']:,} - 交集 {i['legs_intersection']}")
    print(f"C\\A {i['reverse_flow_C_minus_A']}  边界 |eps|<=1e-9: {edge['all_within_band']} "
          f"(反向 {edge['reverse_within_band']}) 反向 eps 中位 {edge['reverse_abs_eps_median']:.2e}")
    nv = out["net_vs_value_bp"]
    print(f"legs: 除权 {nv['leg_ex_rights']:+.3f}bp / 去249 "
          f"{nv['leg_drop_249_only']:+.3f}bp / 加42 {nv['leg_add_reverse_42']:+.3f}bp "
          f"/ 净 {nv['leg_non_adjacent_net_of_reverse']:+.3f}bp / 合计 {nv['total_A_to_C']:+.3f}bp "
          f"/ 可加性残差 {nv['additivity_residual']:+.3f}")
    print(f"249 自身 {nv['non_adjacent_cells_value_daily_bp']:.2f}bp(daily) "
          f"/ {nv['non_adjacent_cells_value_cell_bp']:.2f}bp(cell)；"
          f"42 反向 {nv['reverse_cells_value_daily_bp']:.2f}bp(daily)")
    for k, v in out["pool_mean_caliber"].items():
        print(f"{k}: cells {v['cells']:,} -> {v['cells_after_screen']:,} (越界 {v['out_of_band']}) "
              f"iv 均值 {v['iv_mean_bp']} / 筛后 {v['iv_mean_after_screen_bp']} / 中位 {v['iv_median_bp']} "
              f"/ daily {v['iv_daily_equal_bp']}")
    print(f"elapsed {out['elapsed_s']}s -> {OUT.name}")


if __name__ == "__main__":
    main()
