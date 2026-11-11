"""D13 的机制侧：cell 等权与逐日等权差 2.3–30.1bp，这个摆动是什么的函数。

为什么必须查
------------
D13 修完口径后发现，四条轴里**权重**那条的幅度最大（全样本 44.79 vs 47.10、最后 21 天
57.61 vs 27.47），而它从来没有被命名过。cell 等权高，只意味着一件事：**入池只数多的日子，
当日池均值更高**。若这个方向稳定且显著，池的 47.1bp 就有一大块是「市场级低开反弹」
（当天多少只票低开本身就是市场状态），不是「个股低开回补」——两者可交易性完全不同：
后者每天都能做，前者只在崩盘日有效。

第一版只做了三分位，得到「U 形、中段 18.21bp 低于 20bp 成本」。**那个读法是错的**，
本轮补两条判据把它拆开：

1. **基准**：同一天 `gap_c > GAP_MAX` 的**非池股**逐日等权 `mnp_d`（每日 ≥305 只），
   `ex_d = m_d − mnp_d`。池的日内收益里有一部分只是「当天市场怎么走」，非池股身上也有；
   `ex_d` 才是缺口溢价。用非池而不是全市场，是因为池占比在只数多的日子能到 24%、极值日更高，
   `m − 全市场均值` 会被 `(1−w)` 机械压向 0。
2. **块自举**：日间高自相关（F=1.68），且三分位边界是样本内定的 ⇒ 自举时**在重采样的
   日序列上重新定三分位**，把边界的不确定性也算进去。

`n_d` 与市场 gap 的相关是 **−0.855** ⇒ 池大小基本是「市场低开幅度」的代理，不是独立变量；
所以机制句只能写成「日状态（市场级）」，不能写成「缺口面积越大越好」。

口径
----
- 池与 `_diag_l1_vwap_entry.py` 完全同源（直接 import 它的 `load_daily` / `GAP_MAX`），
  即 C 口径（日历相邻 + 复权）⇒ 366 日 / 239,521 cell。
- `m_d` = 当日池内 `i1`（`raw_close/raw_open − 1`）的**日内等权均值**，bp。
- `n_d` = 当日入池只数。相关性与斜率都在**日序列**上算（N = 天数，不是 cell 数）。
- 分档按 `n_d` 三分位切，**不是**按收益切；三档的日等权均值必须加权回到 47.1（写盘前断言）。

产物 `_diag_d13_weighting_gap_result.json`。只读分区，不联网。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _artifact_guard import assert_clean, render_reading  # noqa: E402
from _diag_l1_vwap_entry import GAP_MAX, load_daily  # noqa: E402

OUT = Path(__file__).resolve().parent / "_diag_d13_weighting_gap_result.json"
EXPECT_DAYS = 366
EXPECT_CELLS = 239521


def ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    b, a = np.polyfit(x, y, 1)
    resid = y - (a + b * x)
    se = float(np.sqrt(resid.var(ddof=2) / ((x - x.mean()) ** 2).sum()))
    return float(b), float(a), float(b / se)


def slope_block_ci(x: np.ndarray, y: np.ndarray,
                   block: int = 20, B: int = 2000) -> tuple[float, float]:
    """日序列上的循环块自举：`n_d` 跨日高度自相关，iid 的 se 会把区间做窄。"""
    rng = np.random.default_rng(20261111)
    n = x.size
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(B, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n]
    xs, ys = x[idx], y[idx]
    xm = xs.mean(axis=1, keepdims=True)
    ym = ys.mean(axis=1, keepdims=True)
    b = ((xs - xm) * (ys - ym)).sum(axis=1) / ((xs - xm) ** 2).sum(axis=1)
    return float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx = pl.Series(x).rank().to_numpy()
    ry = pl.Series(y).rank().to_numpy()
    return float(np.corrcoef(rx, ry)[0, 1])


def tercile_contrast(n: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """返回 (中段均值, 两端均值, 中段 − 两端)。边界按传入的 n 现定。"""
    q1, q2 = np.quantile(n, [1 / 3, 2 / 3])
    mid = (n > q1) & (n <= q2)
    ends = ~mid
    return float(y[mid].mean()), float(y[ends].mean()), float(y[mid].mean() - y[ends].mean())


def boot_contrast(n: np.ndarray, y: np.ndarray, block: int = 20,
                  B: int = 2000, recut: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """块自举「中段 − 两端」。`recut=True` 时**每个重采样上重新定三分位**。

    固定标签（`recut=False`）只反映「给定这批日子」，不反映「三分位边界是样本内挑的」
    这件事；日间又高度自相关（F=1.68），iid 的 se 会把区间做窄。两个问题一起处理，
    两个口径都落盘：区间宽度差多少，就是边界不确定性值多少 bp。
    返回 (对比量分布, 中段均值分布)。
    """
    rng = np.random.default_rng(20261111)
    k = n.size
    nb = int(np.ceil(k / block))
    q1, q2 = np.quantile(n, [1 / 3, 2 / 3])
    fixed = np.where(n <= q1, 0, np.where(n <= q2, 1, 2))
    diff = np.empty(B)
    mid_mean = np.empty(B)
    for b in range(B):
        starts = rng.integers(0, k - block + 1, size=nb)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:k]
        if recut:
            mm, _, dd = tercile_contrast(n[idx], y[idx])
        else:
            lb = fixed[idx]
            mm = float(y[idx][lb == 1].mean())
            dd = mm - float(y[idx][lb != 1].mean())
        diff[b], mid_mean[b] = dd, mm
    return diff, mid_mean


def main() -> None:
    t0 = time.time()
    daily = load_daily().with_columns(
        (pl.col("raw_close") / pl.col("raw_open") - 1.0).alias("i1"))
    pool = daily.filter(pl.col("gap_c") <= GAP_MAX)
    rest = daily.filter(pl.col("gap_c") > GAP_MAX)

    day = pool.group_by("date").agg([
        pl.len().alias("n_names"),
        pl.col("i1").mean().alias("m1"),          # 当日池内逐只等权
        pl.col("gap_c").mean().alias("g1"),
    ]).sort("date")
    other = rest.group_by("date").agg([
        pl.len().alias("n_nonpool"),
        pl.col("i1").mean().alias("m_nonpool"),   # 同日非池股逐只等权 = 基准
    ]).sort("date")
    mkt = daily.group_by("date").agg([
        pl.len().alias("n_all"),
        pl.col("gap_c").mean().alias("mkt_gap"),
        pl.col("i1").mean().alias("mkt_i1"),
    ]).sort("date")
    day = day.join(other, on="date", how="inner").join(mkt, on="date", how="inner")

    n = day["n_names"].to_numpy().astype(float)
    m = day["m1"].to_numpy() * 1e4
    g = day["g1"].to_numpy() * 1e4
    mnp = day["m_nonpool"].to_numpy() * 1e4
    mgap = day["mkt_gap"].to_numpy() * 1e4
    ex = m - mnp

    if (day.height, pool.height) != (EXPECT_DAYS, EXPECT_CELLS):
        raise SystemExit(f"cohort drift: {day.height} 日 / {pool.height} cell")

    overall_day = float(m.mean())
    r = float(np.corrcoef(n, m)[0, 1])
    slope, intercept, t = ols(n, m)

    # 按 n_names 三分位切档（不按收益切），档内日等权
    q1, q2 = np.quantile(n, [1 / 3, 2 / 3])
    cuts = {"n_small": n <= q1, "n_mid": (n > q1) & (n <= q2), "n_large": n > q2}
    terc = {
        k: {
            "days": int(msk.sum()),
            "cells": int(n[msk].sum()),
            "mean_names": round(float(n[msk].mean()), 1),
            "i1_day_bp": round(float(m[msk].mean()), 2),
            "i1_cell_bp": round(float((n[msk] * m[msk]).sum() / n[msk].sum()), 2),
            "mean_gap_in_pool_bp": round(float(g[msk].mean()), 0),
            "i1_day_sd_bp": round(float(m[msk].std(ddof=1)), 1),
            "i1_day_se_bp": round(float(m[msk].std(ddof=1) / np.sqrt(msk.sum())), 1),
            "nonpool_day_bp": round(float(mnp[msk].mean()), 2),
            "excess_day_bp": round(float(ex[msk].mean()), 2),
            "market_gap_bp": round(float(mgap[msk].mean()), 1),
        }
        for k, msk in cuts.items()
    }
    # 三档日等权加权必须回到总体（否则分档漏样本）
    recon = sum(v["i1_day_bp"] * v["days"] for v in terc.values()) / day.height
    if abs(recon - overall_day) > 0.02:
        raise SystemExit(f"三档重建失败: {recon} != {overall_day}")
    # 超额必须与两个产地（池、非池）逐位自洽，且三档能重建回总体超额
    recon_ex = sum(v["excess_day_bp"] * v["days"] for v in terc.values()) / day.height
    if abs(recon_ex - float(ex.mean())) > 0.02:
        raise SystemExit(f"超额三档重建失败: {recon_ex} != {ex.mean()}")

    # 中段 − 两端：raw / 非池 / 超额 三条，各自块自举（边界每次重定）
    _, _, c_raw = tercile_contrast(n, m)
    _, _, c_np = tercile_contrast(n, mnp)
    _, _, c_ex = tercile_contrast(n, ex)
    b_raw, _ = boot_contrast(n, m)
    b_ex, _ = boot_contrast(n, ex)
    b_np, _ = boot_contrast(n, mnp)
    b_raw_fix, _ = boot_contrast(n, m, recut=False)
    b_ex_fix, _ = boot_contrast(n, ex, recut=False)
    # 第二个基准：全市场均值（含池）。池占比 w 让 m − 全市场被 (1−w) 压向 0，
    # 所以它只作稳健性，不作主口径；两个基准给出同方向同量级的结论才敢写。
    mk = day["mkt_i1"].to_numpy() * 1e4
    ex_mkt = m - mk
    _, _, c_exm = tercile_contrast(n, ex_mkt)
    b_exm, _ = boot_contrast(n, ex_mkt)

    def ci(a: np.ndarray) -> list[float]:
        return [round(float(np.percentile(a, 2.5)), 2),
                round(float(np.percentile(a, 97.5)), 2)]

    # gap 为空（无日历相邻前收）的 cell 两侧都不进；确认它小到不改变基准
    _nul = daily.filter(pl.col("gap_c").is_null())
    nul_n = _nul.height
    nul_share = nul_n / daily.height
    nul_mean_bp = float(_nul["i1"].mean() * 1e4)
    nul_effect_bp = nul_share * (nul_mean_bp - float(rest["i1"].mean() * 1e4))

    top = n >= np.quantile(n, 0.9)
    out = {
        "purpose": "cell 等权 − 逐日等权 的差，是否只是「入池只数多的日子收益高」",
        "caliber": "C（日历相邻 + 复权），与 _diag_l1_vwap_entry.py 同源",
        "panel": {
            "days": day.height,
            "cells": pool.height,
            "i1_day_equal_bp": round(overall_day, 2),
            "i1_cell_equal_bp": round(float(pool["i1"].mean() * 1e4), 2),
            "names_per_day_median": float(np.median(n)),
            "names_per_day_p10": round(float(np.quantile(n, 0.1)), 1),
            "names_per_day_p90": round(float(np.quantile(n, 0.9)), 1),
        },
        "relation": {
            "pearson": round(r, 3),
            "spearman": round(spearman(n, m), 3),
            "slope_bp_per_name": round(slope, 3),
            "intercept_bp": round(intercept, 2),
            "t_iid": round(t, 2),
            "slope_ci95_block20": [round(v, 3) for v in slope_block_ci(n, m)],
            "n_days": day.height,
            "note": "序列是 366 个交易日的 (n_d, m_d)，不是 239,521 个 cell；t 的自由度是天数",
        },
        "terciles_by_names": terc,
        "benchmark": {
            "definition": "非池股 = 同日 gap_c > GAP_MAX 的全部 cell，日内等权、再逐日等权；"
                          "不用全市场均值作基准，因为池占比在只数多的日子达 24%，"
                          "m − 全市场会被 (1−w) 机械压向 0。"
                          f"gap 为空（无日历相邻前收）的 {nul_n} 个 cell 两侧都不进，"
                          f"它们自身 i1 均值 {nul_mean_bp:.2f}bp，并进基准只动 "
                          f"{nul_effect_bp:+.2f}bp",
            "nonpool_names_per_day_min": int(day["n_nonpool"].min()),
            "nonpool_names_per_day_median": int(day["n_nonpool"].median()),
            "null_gap_cells": nul_n,
            "null_gap_share": round(nul_share, 5),
            "null_gap_mean_i1_bp": round(nul_mean_bp, 2),
            "null_gap_effect_on_benchmark_bp": round(nul_effect_bp, 2),
            "pool_day_bp": round(overall_day, 2),
            "nonpool_day_bp": round(float(mnp.mean()), 2),
            "excess_day_bp": round(float(ex.mean()), 2),
            "excess_day_sd_bp": round(float(ex.std(ddof=1)), 1),
            "excess_day_se_bp": round(float(ex.std(ddof=1) / np.sqrt(day.height)), 1),
            "market_gap_day_bp": round(float(mgap.mean()), 1),
            "corr_names_vs_market_gap": round(float(np.corrcoef(n, mgap)[0, 1]), 3),
            "allmarket_day_bp": round(float(mk.mean()), 2),
            "excess_vs_allmarket_day_bp": round(float(ex_mkt.mean()), 2),
            "contrast_mid_vs_ends": {
                "raw_bp": round(c_raw, 2),
                "raw_ci95_block20": ci(b_raw),
                "raw_ci95_block20_fixed_cuts": ci(b_raw_fix),
                "nonpool_bp": round(c_np, 2),
                "nonpool_ci95_block20": ci(b_np),
                "excess_bp": round(c_ex, 2),
                "excess_ci95_block20": ci(b_ex),
                "excess_ci95_block20_fixed_cuts": ci(b_ex_fix),
                "excess_vs_allmarket_bp": round(c_exm, 2),
                "excess_vs_allmarket_ci95_block20": ci(b_exm),
                "share_of_raw_explained_by_nonpool": round(c_np / c_raw, 3),
                "note": "对比量 = 中段均值 − 两端均值，逐日序列上算；块长 20 日，"
                        "2000 次重采样。`_fixed_cuts` 是同一次自举但沿用样本内三分位边界，"
                        "两版区间的宽度差就是「边界是挑出来的」这件事值多少 bp",
            },
        },
        "drop_top10pct_names_days": {
            "days_dropped": int(top.sum()),
            "threshold_names": round(float(np.quantile(n, 0.9)), 1),
            "i1_day_bp_kept": round(float(m[~top].mean()), 2),
            "i1_day_bp_all": round(overall_day, 2),
        },
        "elapsed_s": 0.0,
    }
    out["reading_template"] = (
        "池 {days} 日里，当日入池只数的中位 {med} 只（p10/p90 = {p10}/{p90}）。"
        "当日池均值与只数的相关是 Pearson {r} / Spearman {rho}，斜率 {b}bp/只"
        "（截距 {a}bp，t_iid {t}，块 CI {ci}）⇒ 只数多 100 只，当日均值高 {b100}bp。"
        "按只数三分位：少（{n1} 只，池内平均 gap {g1}bp）{v1}bp、中（{n2} 只，{g2}bp）{v2}bp、"
        "多（{n3} 只，{g3}bp）{v3}bp。"
        "去掉只数最多的 10%（≥{thr} 只，{ndrop} 天）后，日等权从 {all_bp}bp 落到 {keep_bp}bp。"
        "**但 U 形不是缺口溢价的性质**：同一批日子、同一权重下，非池股是 {e1}/{e2}/{e3}bp，"
        "超额（池−非池）{x1}/{x2}/{x3}bp——中段低 {c_ex}bp 的块 CI {ci_ex} 跨 0，"
        "而原始 {c_raw}bp 里有 {share} 在非池股身上同样出现。"
    )
    out["reading_fields"] = {
        "days": "panel.days",
        "med": "panel.names_per_day_median", "p10": "panel.names_per_day_p10",
        "p90": "panel.names_per_day_p90", "r": "relation.pearson",
        "rho": "relation.spearman", "b": "relation.slope_bp_per_name",
        "b100": "slope_bp_per_100_names", "a": "relation.intercept_bp",
        "t": "relation.t_iid", "ci": "relation.slope_ci95_block20",
        "n1": "terciles_by_names.n_small.mean_names",
        "v1": "terciles_by_names.n_small.i1_day_bp",
        "n2": "terciles_by_names.n_mid.mean_names",
        "v2": "terciles_by_names.n_mid.i1_day_bp",
        "n3": "terciles_by_names.n_large.mean_names",
        "v3": "terciles_by_names.n_large.i1_day_bp",
        "g1": "terciles_by_names.n_small.mean_gap_in_pool_bp",
        "g2": "terciles_by_names.n_mid.mean_gap_in_pool_bp",
        "g3": "terciles_by_names.n_large.mean_gap_in_pool_bp",
        "e1": "terciles_by_names.n_small.nonpool_day_bp",
        "e2": "terciles_by_names.n_mid.nonpool_day_bp",
        "e3": "terciles_by_names.n_large.nonpool_day_bp",
        "x1": "terciles_by_names.n_small.excess_day_bp",
        "x2": "terciles_by_names.n_mid.excess_day_bp",
        "x3": "terciles_by_names.n_large.excess_day_bp",
        "c_ex": "benchmark.contrast_mid_vs_ends.excess_bp",
        "ci_ex": "benchmark.contrast_mid_vs_ends.excess_ci95_block20",
        "c_raw": "benchmark.contrast_mid_vs_ends.raw_bp",
        "share": "benchmark.contrast_mid_vs_ends.share_of_raw_explained_by_nonpool",
        "thr": "drop_top10pct_names_days.threshold_names",
        "ndrop": "drop_top10pct_names_days.days_dropped",
        "all_bp": "drop_top10pct_names_days.i1_day_bp_all",
        "keep_bp": "drop_top10pct_names_days.i1_day_bp_kept",
    }
    out["slope_bp_per_100_names"] = round(slope * 100, 1)
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["reading"] = render_reading(out, root=out)
    assert_clean(out, label=OUT.name)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(out["reading"])
    print()
    for k, v in terc.items():
        print(f"  {k:8s} days {v['days']:3d}  cells {v['cells']:6d}  "
              f"mean {v['mean_names']:5.1f} 只  gap {v['mean_gap_in_pool_bp']:5.0f}  "
              f"i1 {v['i1_day_bp']:7.2f}(day) "
              f"{v['i1_cell_bp']:7.2f}(cell) | 非池 {v['nonpool_day_bp']:7.2f} "
              f"超额 {v['excess_day_bp']:7.2f} 市场gap {v['market_gap_bp']:7.1f}")
    print(f"  r={r:.3f} rho={out['relation']['spearman']} slope={slope:.3f}bp/只 "
          f"t={t:.2f} CI20={out['relation']['slope_ci95_block20']}")
    cc = out["benchmark"]["contrast_mid_vs_ends"]
    print(f"  中段−两端：raw {cc['raw_bp']}bp CI {cc['raw_ci95_block20']} | "
          f"非池 {cc['nonpool_bp']}bp CI {cc['nonpool_ci95_block20']} | "
          f"超额 {cc['excess_bp']}bp CI {cc['excess_ci95_block20']} "
          f"（非池解释 {cc['share_of_raw_explained_by_nonpool']}）")
    print(f"  corr(n, 市场gap)={out['benchmark']['corr_names_vs_market_gap']}")
    print(f"  去掉最多 10% 天数：{out['drop_top10pct_names_days']['i1_day_bp_kept']}bp "
          f"vs {out['drop_top10pct_names_days']['i1_day_bp_all']}bp")
    print(f"elapsed {out['elapsed_s']}s -> {OUT.name}")


if __name__ == "__main__":
    main()
