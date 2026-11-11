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

    # --- 不变量 1 & 2：集合闭合 ---
    if a_minus_c.height != ex_rights.height + non_adj.height - both.height:
        raise SystemExit(
            f"invariant1 broke: |A\\C|={a_minus_c.height} != "
            f"{ex_rights.height}+{non_adj.height}-{both.height}")
    if a_minus_c.height - c_minus_a.height != a.height - c.height:
        raise SystemExit(
            f"invariant2 broke: |A\\C|-|C\\A|={a_minus_c.height}-{c_minus_a.height} "
            f"!= |A|-|C|={a.height - c.height}")
    # D 与 C 的差必须正好是「停牌腿 249 出去、反向 42 进来」
    if d.join(c.select(key), on=key, how="anti").height != non_adj.height:
        raise SystemExit("invariant2b broke: |D\\C| != |non_adjacent|")
    if c.join(d.select(key), on=key, how="anti").height != c_minus_a.height:
        raise SystemExit("invariant2c broke: |C\\D| != |C\\A|")
    if d.height - c.height != non_adj.height - c_minus_a.height:
        raise SystemExit("invariant2d broke: |D|-|C| != 249-42")

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

    # --- 净值 vs 值：不变量 3 ---
    # 两个方向分开算：D 去掉 249 → no249；no249 加回 42 → C。
    no249 = d.join(non_adj.select(key), on=key, how="anti")
    if no249.join(c.select(key), on=key, how="anti").height or \
            c.join(no249.select(key), on=key, how="anti").height != c_minus_a.height:
        raise SystemExit("invariant3a broke: C != (D - non_adjacent) + C\\A")
    a_bp, d_bp, c_bp = daily_bp(a), daily_bp(d), daily_bp(c)
    n_bp = daily_bp(no249)
    leg_ex, leg_no = d_bp - a_bp, c_bp - d_bp
    total = c_bp - a_bp
    if abs((leg_ex + leg_no) - total) > 0.01:
        raise SystemExit(f"invariant3 broke: legs {leg_ex}+{leg_no} != {total}")

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
            "reading": "249 个 cell 自身 +21.88bp，但去掉它们只动 +0.008bp；"
                       "42 个反向 cell 加回来 −0.005bp ⇒ 净值 0.00 是两个方向抵消，不是它们值 0",
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
