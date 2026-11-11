"""⑨ 的三处口径审计：gap 定义、pen~i1 的斜率、VWAP 均值的稳健性。

为什么需要它
------------
`_diag_l1_vwap_entry.py` 交付了「121.5 → 6.4bp」，但它自己带进文档的三个量都有口径问题，
而文档把其中一个的原因写反了。本脚本只做审计，不产生新的策略结论：

1. **缺口池有三个分母**。该脚本用 `raw_open / raw_close.shift(1).over(symbol)` 定义 gap，
   而 `_diag_l1_indep_recount.py` 用「日历相邻 + 复权」定义。两者差 45.7 vs 47.1。
   文档把这 1.4bp 归给「停牌跨日」。实测：停牌跨日只多 249 个 cell、值 0.01bp；
   真正的差额是 **除权伪缺口**（原始价跌、复权价没跌）。
2. **`pen_over_i1` 不是斜率**。`robustness()` 里 `by_window[k]["pen_over_i1"]` 算的是
   `mean(pen)/mean(i1)`；文档把它当「斜率」与 0.5 比，还出现 1.266 —— 单调路径下斜率
   不可能 >1。真正的斜率要跑 OLS，且要报截距（截距才是「不随涨跌走的那部分惩罚」）。
3. **5.3% / −0.3bp 是删掉 14 个 cell 之后的均值**。`in_band` 只筛掉 14/243,598，
   但这 14 个单独贡献 +1,045bp 的池均值。文档报的是筛后均值，等于让 0.006% 的样本
   决定一个数的量级，而没写这件事。

同时把「机械解释」的零假设换成可测的那个：日内**线性路径**下
`slope = 成交量质心`，不是 0.5。质心与收盘竞价占比都能从本地分钟库直接测
（`data/kline_minute` 19 个全日分区 + 2 个残段），于是「VWAP 含收盘竞价」这个
替代解释可以被**定量**否掉，而不是靠「0.887 > 0.5」。

口径
----
- 日线：`data/kline_daily_enriched/date=*/part.parquet`。
- cohort 与 `_diag_l1_vwap_entry.py` 逐位相同（316 日 / 2,752 只-日），不一致 `SystemExit`。
- 「复权 gap」= `open[d] / close[d-1] - 1`，两者都在同一复权序列上（`close` 列），
  且要求 d-1 是日历相邻交易日。
- 「原始 gap」= `raw_open[d] / raw_close[prev] - 1`，`prev` 取该标的上一**有数据**日
  （跨停牌），即原脚本口径。
- 除权判据：`adjf[d] / adjf[d-1] != 1`，`adjf = raw_close / close`。只有复权因子变了，
  原始 gap 才会与复权 gap 分叉。
- 分钟：每只每日 240 根（09:31→15:00）为全日；`k` 为会话内分钟序号 0…239，
  质心 = `Σ v·(k+0.5)/240 / Σv`。首根 bar（k=0）**含 09:25 集合竞价**，
  所以「竞价份额 ≤ 首根 bar 份额」。

产物 `_diag_l1_vwap_curvature_result.json`。只读分区，不联网，不写 data/。
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
from _diag_base_t0 import leg  # noqa: E402
from _diag_gaprev import N_DECILE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "_diag_l1_vwap_curvature_result.json"
GAP_MAX = -0.01
WINSOR = (1.0, 99.0)
EXPECT_DAYS = 316
EXPECT_CELLS = 2752


def load_daily() -> pl.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data" / "kline_daily_enriched" / "date=*" / "part.parquet")))
    cols = ["symbol", "date", "open", "close", "raw_close", "raw_high", "raw_low", "volume", "amount"]
    d = pl.concat([pl.read_parquet(f, columns=cols) for f in files], how="vertical_relaxed")
    d = d.with_columns(pl.col("date").cast(pl.Utf8)).filter(
        (pl.col("volume") > 0) & (pl.col("amount") > 0) & (pl.col("close") > 0)
        & (pl.col("open") > 0) & (pl.col("raw_close") > 0)
    )
    dates = sorted(d["date"].unique().to_list())
    d = d.with_columns(
        pl.col("date").replace_strict({x: i for i, x in enumerate(dates)}, return_dtype=pl.Int32).alias("di"),
        pl.col("date").str.slice(0, 4).alias("year"),
        (pl.col("amount") / (pl.col("volume") * 100.0)).alias("vwap"),
        (pl.col("raw_close") / pl.col("close")).alias("adjf"),
    )
    d = d.with_columns((pl.col("open") * pl.col("adjf")).alias("raw_open"))
    d = d.with_columns(((pl.col("vwap") >= pl.col("raw_low") * 0.999)
                        & (pl.col("vwap") <= pl.col("raw_high") * 1.001)).alias("in_band"))
    # 上一有数据日（跨停牌）：原脚本口径
    s = d.sort(["symbol", "di"]).with_columns(
        pl.col("raw_close").shift(1).over("symbol").alias("prev_raw_close"),
        pl.col("di").shift(1).over("symbol").alias("prev_di"),
    )
    # 日历相邻前一日：审计口径
    prev = d.select([pl.col("symbol"), (pl.col("di") + 1).alias("di"),
                     pl.col("close").alias("prev_close_adj"),
                     pl.col("adjf").alias("prev_adjf")])
    m = s.join(prev, on=["symbol", "di"], how="left")
    return m.with_columns([
        (pl.col("raw_open") / pl.col("prev_raw_close") - 1.0).alias("g_shift_raw"),
        (pl.col("raw_open") / (pl.col("prev_close_adj") * pl.col("adjf")) - 1.0).alias("g_cal_raw"),
        (pl.col("open") / pl.col("prev_close_adj") - 1.0).alias("g_cal_adj"),
        (pl.col("di") - pl.col("prev_di")).alias("day_gap"),
        (pl.col("adjf") / pl.col("prev_adjf")).alias("adjf_ratio"),
        (pl.col("raw_close") / pl.col("raw_open") - 1.0).alias("i1"),
        (pl.col("vwap") / pl.col("raw_open") - 1.0).alias("pen"),
        (pl.col("raw_close") / pl.col("vwap") - 1.0).alias("iv"),
    ])


def ols(x: np.ndarray, y: np.ndarray) -> dict:
    ok = np.isfinite(x) & np.isfinite(y)
    b, a = np.polyfit(x[ok], y[ok], 1)
    return {"slope": round(float(b), 3), "intercept_bp": round(float(a), 1), "n": int(ok.sum())}


def bp(s: pl.Series) -> np.ndarray:
    return s.to_numpy() * 1e4


def robust_block(i1: np.ndarray, iv: np.ndarray, pen: np.ndarray) -> dict:
    lo_i, hi_i = np.percentile(iv, WINSOR)
    lo_p, hi_p = np.percentile(pen, WINSOR)
    return {
        "i1_mean_bp": round(float(i1.mean()), 1),
        "i1_median_bp": round(float(np.median(i1)), 1),
        "iv_mean_bp": round(float(iv.mean()), 2),
        "iv_median_bp": round(float(np.median(iv)), 1),
        "iv_winsor_mean_bp": round(float(np.clip(iv, lo_i, hi_i).mean()), 2),
        "pen_mean_bp": round(float(pen.mean()), 1),
        "pen_median_bp": round(float(np.median(pen)), 1),
        "pen_winsor_mean_bp": round(float(np.clip(pen, lo_p, hi_p).mean()), 1),
        "max_abs_iv_bp": round(float(np.abs(iv).max()), 0),
    }


def gap_definition(m: pl.DataFrame) -> dict:
    """45.7 与 47.1 的差从哪来：逐层剥。"""
    def stat(tag: str, d: pl.DataFrame) -> dict:
        g = d.group_by("di").agg([pl.col("i1").mean().alias("day"), pl.len().alias("n")])
        return {
            "tag": tag,
            "cells": d.height,
            "days": g.height,
            "i1_daily_equal_bp": round(float(g["day"].mean() * 1e4), 2),
            "i1_cell_equal_bp": round(float(d["i1"].mean() * 1e4), 2),
        }

    shift = m.filter(pl.col("g_shift_raw") <= GAP_MAX)
    shift_band = shift.filter(pl.col("in_band"))
    non_adj = shift.filter(pl.col("day_gap") > 1)
    cal_adj = m.filter(pl.col("g_cal_adj") <= GAP_MAX)
    # 只被原始 gap 选中、复权 gap 存在但不达标（= 除权伪缺口）
    ex_div = shift.filter(pl.col("g_cal_adj").is_not_null() & (pl.col("g_cal_adj") > GAP_MAX))
    # 只去掉除权伪缺口，仍保留跨停牌：用于把 1.35bp 与停牌项分开
    shift_no_exdiv = shift.filter(~(pl.col("g_cal_adj").is_not_null() & (pl.col("g_cal_adj") > GAP_MAX)))
    out = {
        "A_shift_raw": stat("原始 gap + 跨停牌（原脚本）", shift),
        "B_shift_raw_in_band": stat("A 再过 in_band（原脚本产物里的池）", shift_band),
        "D_shift_raw_no_exdiv": stat("A 去掉除权伪缺口、保留跨停牌", shift_no_exdiv),
        "C_cal_adj": stat("日历相邻 + 复权（indep_recount 口径）", cal_adj),
        "only_non_adjacent": stat("A 里跨停牌的那部分", non_adj),
        "only_ex_rights": stat("A 里被复权否掉的伪缺口", ex_div),
    }
    out["only_ex_rights"]["adj_factor_changed_share"] = round(
        float((ex_div["adjf_ratio"].fill_null(1.0) != 1.0).mean()), 4)
    out["only_ex_rights"]["adjf_ratio_median"] = round(float(ex_div["adjf_ratio"].median()), 4)
    a = out["A_shift_raw"]["i1_daily_equal_bp"]
    d = out["D_shift_raw_no_exdiv"]["i1_daily_equal_bp"]
    c = out["C_cal_adj"]["i1_daily_equal_bp"]
    out["decomposition_bp"] = {
        "total_A_minus_C": round(a - c, 2),
        "ex_rights_leg": round(d - a, 2),
        "non_adjacent_leg": round(c - d, 2),
    }
    return out


def pen_slope(m: pl.DataFrame, pool: pl.DataFrame, cohort: pl.DataFrame) -> dict:
    """真斜率 vs 文档里的 mean 比。"""
    out: dict = {}
    x, y, dv = bp(m["i1"]), bp(m["pen"]), m["di"].to_numpy()
    out["market_pooled"] = ols(x, y)
    day = pl.DataFrame({"di": dv, "x": x, "y": y}).group_by("di").agg(
        [pl.col("x").mean().alias("mx"), pl.col("y").mean().alias("my")])
    dm = pl.DataFrame({"di": dv, "x": x, "y": y}).join(day, on="di")
    out["market_within_day"] = ols((dm["x"] - dm["mx"]).to_numpy(), (dm["y"] - dm["my"]).to_numpy())
    per_day = []
    for _, g in pl.DataFrame({"di": dv, "x": x, "y": y}).group_by("di"):
        if g.height >= 50:
            b, _ = np.polyfit(g["x"].to_numpy(), g["y"].to_numpy(), 1)
            if np.isfinite(b):
                per_day.append(b)
    out["market_per_day_slope"] = {
        "median": round(float(np.median(per_day)), 3),
        "p10": round(float(np.percentile(per_day, 10)), 3),
        "p90": round(float(np.percentile(per_day, 90)), 3),
        "days": len(per_day),
    }

    for tag, df in (("gap_pool", pool), ("cohort", cohort)):
        px, py = bp(df["i1"]), bp(df["pen"])
        blk = ols(px, py)
        blk["ratio_of_means_pen_over_i1"] = round(float(py.mean() / px.mean()), 3)
        blk["retention_at_mean"] = round(float(1.0 - blk["slope"] - blk["intercept_bp"] / px.mean()), 3)
        blk["mean_i1_bp"] = round(float(px.mean()), 1)
        out[tag] = blk

    by_year = {}
    for yv in sorted(pool["year"].unique().to_list()):
        g = pool.filter(pl.col("year") == yv)
        blk = ols(bp(g["i1"]), bp(g["pen"]))
        blk["ratio_of_means_pen_over_i1"] = round(float(bp(g["pen"]).mean() / bp(g["i1"]).mean()), 3)
        by_year[yv] = blk
    out["gap_pool_by_year"] = by_year
    return out


def by_gap_depth(m: pl.DataFrame) -> list[dict]:
    """gap 深度四档：复权口径 + `in_band`（作为数据质量筛，非结果筛）。"""
    p = m.filter((pl.col("g_cal_adj") <= GAP_MAX) & pl.col("in_band"))
    q = p.with_columns(pl.col("g_cal_adj").qcut([0.25, 0.5, 0.75],
                                                labels=["q1", "q2", "q3", "q4"]).alias("bucket")) \
         .group_by("bucket", maintain_order=True).agg([
             pl.len().alias("n"),
             (pl.col("g_cal_adj").mean() * 1e4).alias("gap"),
             (pl.col("i1").mean() * 1e4).alias("io"),
             (pl.col("i1").median() * 1e4).alias("io_med"),
             (pl.col("iv").mean() * 1e4).alias("iv"),
             (pl.col("iv").median() * 1e4).alias("iv_med"),
             (pl.col("pen").mean() * 1e4).alias("pen")])
    return sorted([{
        "bucket": r["bucket"],
        "n": int(r["n"]),
        "gap_bp": round(float(r["gap"]), 0),
        "open_to_close_bp": round(float(r["io"]), 1),
        "open_to_close_median_bp": round(float(r["io_med"]), 1),
        "vwap_to_close_bp": round(float(r["iv"]), 1),
        "vwap_to_close_median_bp": round(float(r["iv_med"]), 1),
        "open_to_vwap_bp": round(float(r["pen"]), 1),
    } for r in q.to_dicts()], key=lambda x: x["gap_bp"])


def outlier_sensitivity(shift_pool: pl.DataFrame, cohort: pl.DataFrame) -> dict:
    """`in_band` 只筛掉 14 个 cell，但这 14 个决定了池均值的量级。"""
    good = shift_pool.filter(pl.col("in_band"))
    bad = shift_pool.filter(~pl.col("in_band"))
    iv_all = bp(shift_pool["iv"])
    def blk(df: pl.DataFrame) -> dict:
        v, i1, pen = bp(df["iv"]), bp(df["i1"]), bp(df["pen"])
        out = robust_block(i1, v, pen)
        out["iv_daily_equal_bp"] = round(
            float(pl.DataFrame({"di": df["di"], "iv": v}).group_by("di")
                  .agg(pl.col("iv").mean())["iv"].mean()), 2)
        return out
    return {
        "pool_before_screen": dict(blk(shift_pool), cells=shift_pool.height),
        "pool_after_screen": dict(blk(good), cells=good.height),
        "out_of_band_cells": {
            "cells": bad.height,
            "share": round(bad.height / shift_pool.height, 6),
            "iv_mean_bp": round(float(bp(bad["iv"]).mean()), 0) if bad.height else None,
            "iv_max_bp": round(float(bp(bad["iv"]).max()), 0) if bad.height else None,
            "mean_shift_due_to_screen_bp": round(float(iv_all.mean() - bp(good["iv"]).mean()), 1),
        },
        "cohort": dict(blk(cohort), cells=cohort.height, out_of_band=int((~cohort["in_band"]).sum())),
        "note": "均值对 14 个越界 cell 的敏感度；中位与 1/99 缩尾给的是另一个数",
    }


def minute_null() -> dict:
    """线性路径下的零假设 = 成交量质心；收盘竞价份额可直测。"""
    rows = []
    for p in sorted(glob.glob(str(ROOT / "data" / "kline_minute" / "date=*" / "part.parquet"))):
        dt = os.path.basename(os.path.dirname(p)).replace("date=", "")
        x = pl.read_parquet(p, columns=["symbol", "datetime", "volume", "amount", "open", "close"])
        x = x.sort(["symbol", "datetime"]).with_columns(
            pl.col("datetime").dt.hour().cast(pl.Int32).alias("h"),
            pl.col("datetime").dt.minute().cast(pl.Int32).alias("mi"))
        # Int8 的 h*60 会溢出（540 mod 256 = 28），必须先 cast
        x = x.with_columns(((pl.col("h") * 60 + pl.col("mi")) - 571
                            - pl.when(pl.col("h") * 60 + pl.col("mi") > 690).then(90).otherwise(0)).alias("k"))
        for sym, g in x.group_by("symbol"):
            v = g["volume"].to_numpy()
            k = g["k"].to_numpy()
            tv = float(v.sum())
            if tv <= 0:
                continue
            rows.append({
                "date": dt, "symbol": sym[0], "bars": g.height, "tv": tv,
                "ta": float(g["amount"].sum()),
                "centroid": float((v * ((k + 0.5) / 240.0)).sum() / tv),
                "s_first_bar": float(v[k == 0].sum() / tv),
                "s_close_auction": float(v[k >= 237].sum() / tv),
                "s_1500": float(v[k == 239].sum() / tv),
                "o": float(g["open"][0]), "c": float(g["close"][-1]),
            })
    mi = pl.DataFrame(rows)
    full = mi.filter(pl.col("bars") == 240)
    q = lambda c, p_: round(float(full[c].quantile(p_)), 4)
    d = pl.concat([pl.read_parquet(f, columns=["symbol", "date", "open", "close", "volume", "amount"])
                   for f in sorted(glob.glob(str(ROOT / "data" / "kline_daily_enriched" / "date=*" / "part.parquet")))],
                  how="vertical_relaxed").with_columns(pl.col("date").cast(pl.Utf8))
    j = full.join(d, on=["date", "symbol"], how="inner")
    j = j.with_columns([
        (pl.col("ta") / pl.col("amount") - 1.0).alias("amt_ratio"),
        (pl.col("ta") / (pl.col("tv") * 100.0) / pl.col("o") - 1.0).alias("p_min"),
        (pl.col("c") / pl.col("o") - 1.0).alias("i1d"),
    ])
    sub = j.filter(pl.col("i1d").abs() > 20e-4)
    cs = sub["centroid"].to_numpy()
    ce = sub["p_min"].to_numpy() / sub["i1d"].to_numpy()
    up = sub["i1d"].to_numpy() > 0
    by_date = mi.group_by("date").agg([
        pl.col("bars").min().alias("bars_min"), pl.len().alias("n_symbols"),
        (pl.col("tv") / 1.0).len().alias("_n")]).sort("date")
    j_all = mi.join(d, on=["date", "symbol"], how="inner").with_columns(
        (pl.col("ta") / pl.col("amount") - 1.0).alias("amt_ratio"))
    return {
        "coverage": {
            "partitions": mi["date"].n_unique(),
            "symbol_days": mi.height,
            "full_day_symbol_days": full.height,
            "truncated_dates": by_date.filter(pl.col("bars_min") < 240)
            .select(["date", "bars_min"]).to_dicts(),
        },
        # 全部 826 个只-日（含残段）：这就是「中位好看、均值难看」的那一组
        "all_symbol_days": {
            "n": mi.height,
            "amount_ratio_median_bp": round(float((j_all["amt_ratio"] * 1e4).median()), 2),
            "amount_ratio_mean_bp": round(float((j_all["amt_ratio"] * 1e4).mean()), 2),
            "within_1bp_share": round(float(((j_all["amt_ratio"] * 1e4).abs() < 1.0).mean()), 4),
        },
        "corroboration": {
            "n": j.height,
            "amount_ratio_median_bp": round(float((j["amt_ratio"] * 1e4).median()), 2),
            "amount_ratio_mean_bp": round(float((j["amt_ratio"] * 1e4).mean()), 2),
            "within_1bp_share": round(float(((j["amt_ratio"] * 1e4).abs() < 1.0).mean()), 4),
            # 逐位相等占比 <1 ⇒ 日线与分钟是两个来源，不是聚合产物
            "exact_volume_share": round(float((j["tv"] == j["volume"]).mean()), 4),
            "exact_amount_share": round(float((j["ta"] == j["amount"]).mean()), 4),
            "abs_amount_diff_median_cny": round(float((j["ta"] - j["amount"]).abs().median()), 0),
        },
        "volume_centroid": {"median": q("centroid", .5), "p10": q("centroid", .1), "p90": q("centroid", .9)},
        "s_first_bar": {"median": q("s_first_bar", .5), "p10": q("s_first_bar", .1), "p90": q("s_first_bar", .9)},
        "s_close_auction": {"median": q("s_close_auction", .5), "p10": q("s_close_auction", .1),
                            "p90": q("s_close_auction", .9)},
        "implied_centroid": {
            "n": int(cs.size),
            "median_C_eff": round(float(np.median(ce)), 4),
            "median_C_minus_C_eff": round(float(np.median(ce - cs)), 4),
            "median_C_eff_minus_C": round(float(np.median(ce - cs)), 4),
            "share_C_eff_gt_C": round(float((ce > cs).mean()), 3),
            "median_gap_up_days": round(float(np.median((ce - cs)[up])), 4),
            "median_gap_down_days": round(float(np.median((ce - cs)[~up])), 4),
            "small_sample_slope": round(float(np.polyfit(sub["i1d"].to_numpy() * 1e4,
                                                         sub["p_min"].to_numpy() * 1e4, 1)[0]), 3),
            "reading": "线性路径要求 C_eff == C。实际中位差 +0.152、64.5% 的日子 C_eff 更高 "
                       "⇒ 线性模型被数据拒绝（路径是「快速下探后走平」）。于是 0.658 只作投影系数，"
                       "不能读成时间质心；前载幅度不能拿 0.658-0.404 当量（那是被拒绝模型的残差）。",
        },
        "linear_path_null": {
            "comment": "线性路径下 slope **就是**成交量质心 C。收盘竞价那 1.03% 的权重已经在 C 里，"
                       "再写 C*(1-w)+w 是把同一段权重加第二遍 —— 旧产物的 0.410 由此而来。",
            "C_median": q("centroid", .5),
            "w_close_median": q("s_close_auction", .5),
            "null_slope": q("centroid", .5),
            # 旧键保留（老引用不会 KeyError），值是被撤回的双计版本
            "null_slope_with_auction": round(q("centroid", .5) * (1 - q("s_close_auction", .5))
                                             + q("s_close_auction", .5), 4),
            "double_counted_legacy": round(q("centroid", .5) * (1 - q("s_close_auction", .5))
                                           + q("s_close_auction", .5), 4),
        },
    }


def build_cohort() -> pl.DataFrame:
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, _ = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    d_map = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    c, o = mats["close"], mats["open"]
    tradable = np.isfinite(c) & (c > 0) & np.isfinite(o) & (o > 0)
    C2 = leg(mats, "c2")
    picks = ap.pick_daily(df, "mom20", ascending=False).select("date", "symbols").to_dicts()
    cells: list[tuple[str, str]] = []
    for r in picks:
        i0 = d_map[str(r["date"])]
        if i0 + 2 >= T:
            continue
        sel = np.array([s_map[x] for x in r["symbols"]], dtype=np.int64)
        c0 = c[i0]
        pool = tradable[i0] & np.isfinite(C2[i0]) & np.isfinite(turn[i0]) & (c0 > 0)
        if pool.sum() < 200:
            continue
        lim = c0 * (1.0 - abs(GAP_MAX))
        fill = pool & tradable[i0 + 1] & (o[i0 + 1] <= lim) & (lim > 0)
        good = sel[fill[sel]]
        if good.size < 3:
            continue
        fpool = np.flatnonzero(pool)[fill[np.flatnonzero(pool)]]
        if fpool.size < 50:
            continue
        for j in good:
            cells.append((str(dates[i0 + 1]), symbols[j]))
    need = pl.DataFrame({"date": [x[0] for x in cells], "symbol": [x[1] for x in cells]})
    return need


def main() -> None:
    t0 = time.time()
    m = load_daily()
    need = build_cohort()
    cohort = m.join(need, on=["date", "symbol"], how="inner").unique(subset=["date", "symbol"])
    n_day = int(need["date"].n_unique())
    if (n_day, need.height) != (EXPECT_DAYS, EXPECT_CELLS):
        raise SystemExit(f"cohort drift: {n_day} days / {need.height} cells, "
                         f"expected {EXPECT_DAYS} / {EXPECT_CELLS}")

    pool = m.filter(pl.col("g_cal_adj") <= GAP_MAX)   # 审计后的口径：日历相邻 + 复权
    gd = gap_definition(m)
    ps = pen_slope(m, pool, cohort)
    os_ = outlier_sensitivity(m.filter(pl.col("g_shift_raw") <= GAP_MAX), cohort)
    mn = minute_null()

    out = {
        "purpose": "审计 ⑨ 的三处口径：gap 定义 / pen~i1 的斜率 / VWAP 均值的稳健性",
        "cohort": {"n_days": n_day, "n_cells": need.height},
        "gap_definition": gd,
        "pen_slope": ps,
        "gap_pool_by_depth": by_gap_depth(m),
        "outlier_sensitivity": os_,
        "minute_null": mn,
        "elapsed_s": 0.0,
    }
    out["elapsed_s"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print("== gap 定义 ==")
    for k in ("A_shift_raw", "B_shift_raw_in_band", "C_cal_adj", "only_non_adjacent", "only_ex_rights"):
        v = gd[k]
        print(f"  {k:20s} cells {v['cells']:7d} days {v['days']:3d} "
              f"daily {v['i1_daily_equal_bp']:7.2f}bp cell {v['i1_cell_equal_bp']:7.2f}bp")
    print(f"  除权伪缺口里复权因子变了的占比 {gd['only_ex_rights']['adj_factor_changed_share']}")
    print("== pen~i1 ==")
    for k, v in ps.items():
        print(f"  {k:24s} {v}")
    print("== 分钟 ==")
    print(f"  coverage {mn['coverage']}")
    print(f"  corroboration {mn['corroboration']}")
    print(f"  centroid {mn['volume_centroid']}  s_first {mn['s_first_bar']}  s_close {mn['s_close_auction']}")
    print(f"  implied {mn['implied_centroid']}")
    print(f"  null {mn['linear_path_null']}")
    print(f"elapsed {out['elapsed_s']}s -> {OUT.name}")


if __name__ == "__main__":
    main()
