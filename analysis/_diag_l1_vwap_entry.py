"""L1 的最后一格：把「以 open 成交」换成「按成交量比例摊开成交」，量出那段价差。

为什么不能按原计划做
--------------------
原计划是「在分钟数据上模拟按 VWAP 摊开买，与 open 口径逐日配对」。本地分钟库
`data/kline_minute` 只有 **21 个 date 分区**（2026-10-08 → 2026-11-05）、**40 个 symbol**、
2.0MB，而信号窗口是 367 个交易日 ⇒ 这个模拟在现有数据上跑不出来（`minute_coverage` 段）。

但「摊开买拿到多少 bp」不需要分钟数据。摊开成交的可执行形态是 **POV**（按成交量的固定
比例下单），其成交价就是**全日 VWAP**，而 VWAP 可直接从日线取：

    vwap = amount / (volume * 100)          # 元 / (手 * 100 股)

仪器校准（`instrument_check` 段）：本地那 21 天 × 40 只的分钟数据用
`sum(amount)/sum(volume)` 算出的全日 VWAP，与日线公式的差中位数 **0.02bp**、p90 0.4bp；
日线 `open` 与首根分钟 bar 的 `open` 逐位相同。⇒ 日线 VWAP 可以当分钟 VWAP 用。

口径
----
- cohort 与 `_diag_l1_annualize.py` 逐位相同（316 日 / 2,752 只-日），不一致直接退出。
- `open`/`close` 是**复权**价；`amount`/`volume` 是**原始**口径 ⇒ `raw_open =
  open * (raw_close / close)`，`i1_vwap = raw_close / vwap - 1`。
  `i1_open = close/open - 1` 复权不变，两段可比。
- 有效性门：`vwap ∈ [raw_low, raw_high]`（0.1% 容差）。越界剔除并计数——这条同时是量纲
  检查：若 `volume` 不是「手」，几乎全部细胞越界。
- 逐日配对：同一天同一批 cell 上算 `mean(i1_open) - mean(i1_vwap)`，再对日等权。
- 容量轴：POV 的 p% 参与率下回合资本 = 只数 × p × 当日 amount；年利润 = 资本 × 净bp × 208.4。
- 另有全缺口池（无选股器）与按 gap 深度四档的同一组量，用于判断这是 cohort 特性还是结构性。

产物 `_diag_l1_vwap_entry_result.json`。只读分区，不联网。

**口径（2026-11-11 修订，D13）**
本脚本原来把**三个口径**合并在一个键名 `open_entry_bp` 下：gap 定义（A 原始 / C 复权）、
权重（cell 等权 / 逐日等权）、以及 `in_band` 是否作用于 VWAP 相关的量。同一个产物里
`gap_pool_unselected.open_entry_bp = 45.7`（逐日）与 `gap_pool_by_window.full.open_entry_bp
= 43.8`（cell）是**同一批 243,584 个 cell** 的两个权重，键名却不区分。现在：

- 主口径一律 **C**：`gap_c = open / prev_close_adj - 1`，`prev_close_adj` 取**日历相邻**
  前一交易日（停牌断档不取「上一有数据日」）。与 `_diag_l1_vwap_curvature.py` 的
  `g_cal_adj`、`_diag_l1_gapset_audit.py` 的 `C_cal_adj`、`_diag_l1_indep_recount.py` 的 ①
  是同一个定义 ⇒ `47.10`（逐日）/ `44.79`（cell）/ `239,521` cell，可对拍。
- **权重显式进键名**：`open_entry_cell_bp` 与 `open_entry_day_bp` 并列，没有裸的
  `open_entry_bp`。
- **定义域显式**：`i1`（open→close）不用 VWAP，取全池；`vwap→close` 与 `open→vwap` 必须
  在 `in_band` 上算（13 个越界 cell 能把 cell 均值从 −6.24 抬到 +1,045.6）。两个 N 都落盘
  （`n_cells` / `n_cells_in_band`）。
- A 口径降级为 `a_legacy`，只做对照不再当结论：`45.7 / 243,584 / −0.3` 那一组**作废**。
- `by_gap_depth` **删除**：同一张表由 `_diag_l1_vwap_curvature.py:gap_pool_by_depth` 按 C
  口径产出（q1 59,879 / 90.9 / −4.1 / 91.4），本脚本再算一遍就是第二个产地。
  改为 `gap_pool_by_depth` 的引用块，N 由本池现算，测试断言与 curvature 求和一致。

`robustness` 段的 `pen_over_i1` 是 `mean(pen)/mean(i1)`，**不是斜率**（真斜率见
`_diag_l1_vwap_curvature.py` 的 `pen_slope`）。cohort 侧（121.5 / 6.4 / 115.1）不受影响。
"""
from __future__ import annotations

import glob
import json
import math
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
from _diag_l1_annualize import block_ci, nw_t  # noqa: E402
from _artifact_guard import assert_clean, render_reading  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "_diag_l1_vwap_entry_result.json"
GAP_MAX = -0.01
COST_BP = 20.0
FIRST_BAR_SHARE = 0.0352
DAYS_PER_YEAR = 242.0
IMPACT_LINE = 0.05
BAND_TOL = 1e-3
EXPECT_DAYS = 316
EXPECT_CELLS = 2752


def load_daily() -> pl.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data" / "kline_daily_enriched" / "date=*" / "part.parquet")))
    cols = ["symbol", "date", "open", "close", "raw_close", "raw_high", "raw_low", "volume", "amount"]
    src = pl.concat([pl.read_parquet(f, columns=cols) for f in files], how="vertical_relaxed")
    src = src.with_columns(pl.col("date").cast(pl.Utf8).alias("date"))
    src = src.filter(
        pl.col("volume").is_finite() & (pl.col("volume") > 0)
        & pl.col("amount").is_finite() & (pl.col("amount") > 0)
        & pl.col("raw_close").is_finite() & (pl.col("raw_close") > 0)
        & pl.col("open").is_finite() & (pl.col("open") > 0)
        & pl.col("close").is_finite() & (pl.col("close") > 0)
    )
    dates = sorted(src["date"].unique().to_list())
    src = src.with_columns([
        pl.col("date").replace_strict({x: i for i, x in enumerate(dates)},
                                      return_dtype=pl.Int32).alias("di"),
        (pl.col("amount") / (pl.col("volume") * 100.0)).alias("vwap"),
        (pl.col("raw_close") / pl.col("close")).alias("adj_f"),
    ])
    src = src.with_columns((pl.col("open") * pl.col("adj_f")).alias("raw_open"))
    src = src.with_columns(
        ((pl.col("vwap") >= pl.col("raw_low") * (1.0 - BAND_TOL))
         & (pl.col("vwap") <= pl.col("raw_high") * (1.0 + BAND_TOL))).alias("in_band")
    )
    src = src.sort(["symbol", "di"])
    # A 口径（旧）：上一「有数据日」的原始收盘 ⇒ 停牌断档与除权都不剔，已作废，只留对照
    src = src.with_columns(
        (pl.col("raw_open") / pl.col("raw_close").shift(1).over("symbol") - 1.0).alias("gap_a")
    )
    # C 口径（审计）：日历相邻前一交易日的**复权**收盘
    prev = src.select([pl.col("symbol"), (pl.col("di") + 1).alias("di"),
                       pl.col("close").alias("prev_close_adj")])
    src = src.join(prev, on=["symbol", "di"], how="left")
    return src.with_columns(
        (pl.col("open") / pl.col("prev_close_adj") - 1.0).alias("gap_c")
    )


def minute_coverage() -> dict:
    parts = sorted(glob.glob(str(ROOT / "data" / "kline_minute" / "date=*" / "part.parquet")))
    if not parts:
        return {"partitions": 0, "symbols": 0, "first": None, "last": None}
    syms: set[str] = set()
    for p in parts:
        syms |= set(pl.read_parquet(p, columns=["symbol"])["symbol"].unique().to_list())
    tags = [Path(p).parent.name.replace("date=", "") for p in parts]
    return {"partitions": len(parts), "symbols": len(syms),
            "first": min(tags), "last": max(tags)}


def instrument_check(daily: pl.DataFrame) -> dict:
    """日线 VWAP 公式 vs 分钟聚合成交价；日线 open vs 首根 bar open。"""
    parts = sorted(glob.glob(str(ROOT / "data" / "kline_minute" / "date=*" / "part.parquet")))
    if not parts:
        return {"n": 0}
    rows = []
    for p in parts:
        d = pl.read_parquet(p, columns=["symbol", "datetime", "open", "close", "volume", "amount"])
        g = d.sort("datetime").group_by("symbol").agg([
            pl.col("amount").sum().alias("m_amt"),
            pl.col("volume").sum().alias("m_vol"),
            pl.col("open").first().alias("m_open"),
            pl.col("close").last().alias("m_close"),
        ])
        g = g.with_columns(pl.lit(Path(p).parent.name.replace("date=", "")).alias("date"))
        rows.append(g)
    mi = pl.concat(rows).with_columns((pl.col("m_amt") / (pl.col("m_vol") * 100.0)).alias("vwap_min"))
    j = mi.join(daily, on=["date", "symbol"], how="inner")
    j = j.with_columns([
        (pl.col("vwap_min") / pl.col("vwap") - 1.0).alias("d_vwap"),
        (pl.col("m_open") / pl.col("raw_close") * pl.col("close") / pl.col("open") - 1.0).alias("d_open"),
    ])
    n = j.height
    if n == 0:
        return {"n": 0}
    a = j["d_vwap"].abs()
    o = j["d_open"].abs()
    return {
        "n_symbol_days": n,
        "dates": j["date"].n_unique(),
        "symbols": j["symbol"].n_unique(),
        "d_vwap_median_abs_bp": round(float(a.median()) * 1e4, 3),
        "d_vwap_p90_abs_bp": round(float(a.quantile(0.9)) * 1e4, 3),
        "d_vwap_within_1bp": round(float((a < 1e-4).mean()), 4),
        "d_open_median_abs_bp": round(float(o.median()) * 1e4, 3),
        "d_open_max_abs_bp": round(float(o.max()) * 1e4, 3),
    }


def daily_pair(x: pl.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """逐日等权的 (i1_open, i1_vwap, entry_penalty)，单位 bp。"""
    g = x.group_by("date").agg([
        (pl.col("raw_close") / pl.col("raw_open") - 1.0).mean().alias("io"),
        (pl.col("raw_close") / pl.col("vwap") - 1.0).mean().alias("iv"),
        (pl.col("vwap") / pl.col("raw_open") - 1.0).mean().alias("pen"),
    ]).sort("date")
    return (g["io"].to_numpy() * 1e4, g["iv"].to_numpy() * 1e4, g["pen"].to_numpy() * 1e4)


def slope(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 50:
        return float("nan"), float("nan")
    b = np.polyfit(x[m], y[m], 1)
    return float(b[0]), float(b[1])


def day_mean(x: np.ndarray, d: np.ndarray) -> float:
    """先日内等权、再对日等权。**权重必须写在键名里**，否则 43.8 与 45.7 会被读成矛盾。"""
    g = pl.DataFrame({"d": d, "x": x}).group_by("d").agg(pl.col("x").mean())
    return float(g["x"].mean())


def windows(d: np.ndarray) -> dict[str, np.ndarray]:
    w = {"full": np.ones(d.size, dtype=bool)}
    for y in ("2025", "2026"):
        w[y] = np.char.startswith(d, y)
    t21 = sorted(set(d.tolist()))[-21:]
    w["last21"] = np.isin(d, t21)
    return w


def pool_windows(pool: pl.DataFrame) -> dict:
    """同一池子的分窗统计：cell 等权与逐日等权**并列**，N 与定义域一并落盘。"""
    i1 = (pool["raw_close"] / pool["raw_open"] - 1.0).to_numpy() * 1e4
    iv = (pool["raw_close"] / pool["vwap"] - 1.0).to_numpy() * 1e4
    pen = (pool["vwap"] / pool["raw_open"] - 1.0).to_numpy() * 1e4
    ok = pool["in_band"].to_numpy()
    dt = pool["date"].to_numpy().astype(str)
    out = {}
    for k, m in windows(dt).items():
        mm = m & ok                      # VWAP 相关量只在 in_band 上算
        out[k] = {
            "n_cells": int(m.sum()),
            "n_cells_in_band": int(mm.sum()),
            "n_days": int(np.unique(dt[m]).size),
            "open_entry_cell_bp": round(float(i1[m].mean()), 2),
            "open_entry_day_bp": round(day_mean(i1[m], dt[m]), 2),
            "vwap_entry_cell_bp": round(float(iv[mm].mean()), 2),
            "vwap_entry_day_bp": round(day_mean(iv[mm], dt[mm]), 2),
            "pen_over_i1_in_band": round(float(pen[mm].mean() / i1[mm].mean()), 3),
            "pen_over_i1_all_cells": round(float(pen[m].mean() / i1[m].mean()), 3),
        }
    return out


def pool_block(pool: pl.DataFrame) -> dict:
    """全池汇总（不分窗）：两个 N、两个权重、以及 in_band 前后的 VWAP 均值。"""
    i1 = (pool["raw_close"] / pool["raw_open"] - 1.0).to_numpy() * 1e4
    iv = (pool["raw_close"] / pool["vwap"] - 1.0).to_numpy() * 1e4
    pen = (pool["vwap"] / pool["raw_open"] - 1.0).to_numpy() * 1e4
    ok = pool["in_band"].to_numpy()
    dt = pool["date"].to_numpy().astype(str)
    return {
        "n_days": int(np.unique(dt).size),
        "n_cells": int(i1.size),
        "n_cells_in_band": int(ok.sum()),
        "n_cells_out_of_band": int((~ok).sum()),
        "open_entry_cell_bp": round(float(i1.mean()), 2),
        "open_entry_day_bp": round(day_mean(i1, dt), 2),
        "vwap_entry_cell_bp": round(float(iv[ok].mean()), 2),
        "vwap_entry_day_bp": round(day_mean(iv[ok], dt[ok]), 2),
        "vwap_entry_cell_median_in_band_bp": round(float(np.median(iv[ok])), 2),
        "vwap_entry_cell_median_all_cells_bp": round(float(np.median(iv)), 2),
        "vwap_entry_cell_median_abs_gap_bp": round(
            abs(float(np.median(iv)) - float(np.median(iv[ok]))), 4),
        "entry_price_penalty_cell_bp": round(float(pen[ok].mean()), 2),
        "entry_price_penalty_day_bp": round(day_mean(pen[ok], dt[ok]), 2),
        "share_of_gross_kept_by_vwap_cell": round(float(iv[ok].mean() / i1.mean()), 3),
        "share_of_gross_kept_by_vwap_day": round(
            day_mean(iv[ok], dt[ok]) / day_mean(i1, dt), 3),
        "vwap_unfiltered": {
            "cell_bp": round(float(iv.mean()), 2),
            "day_bp": round(day_mean(iv, dt), 2),
            "why": "13 个 vwap∉[low,high] 的 cell 把均值抬到千位量级；中位不动 ⇒ VWAP 量必须 in_band",
        },
    }


def robustness(daily: pl.DataFrame, pool_c: pl.DataFrame, pool_a: pl.DataFrame) -> dict:
    """反证：pen≈i1 是不是「VWAP 含收盘集合竞价」的机械结果，还是真的集中在开盘。

    零假设是「日内路径线性」⇒ 斜率 0.5。全市场实测斜率本身就是基准，不用 0.5。
    """
    x = (daily["raw_close"] / daily["raw_open"] - 1.0).to_numpy() * 1e4
    y = (daily["vwap"] / daily["raw_open"] - 1.0).to_numpy() * 1e4
    a = daily["amount"].to_numpy()
    s_all, i_all = slope(x, y)
    q1, q2 = np.quantile(a, [1 / 3, 2 / 3])
    tercile = {}
    for lab, msk in (("small", a <= q1), ("mid", (a > q1) & (a <= q2)), ("large", a > q2)):
        s, _ = slope(x[msk], y[msk])
        tercile[lab] = {"n": int(msk.sum()), "slope_pen_on_i1": round(s, 3)}

    return {
        "note": "pen~i1 是 OLS 斜率，不是 mean 比。线性路径的零假设是 slope = 成交量质心 "
                "C ≈ 0.404（**不是 0.5**，收盘竞价那 1.03% 已在 C 里）；且该线性模型本身被拒绝"
                "（C_eff 中位 0.572 ≠ C 0.404）。pen_over_i1 = mean(pen)/mean(i1)，"
                "越过 1 的含义是「VWAP 高于收盘」= VWAP→收盘为负，不是「更前载」。"
                "见 `_diag_l1_vwap_curvature.py` / `_diag_l1_gapset_audit.py`。",
        "market_wide": {
            "n_stock_days": int(np.isfinite(x).sum()),
            "slope_pen_on_i1": round(s_all, 3),
            "intercept_bp": round(i_all, 2),
            "slope_by_amount_tercile": tercile,
        },
        "gap_pool_by_window": pool_windows(pool_c),
        "gap_pool_by_window_a_legacy": pool_windows(pool_a),
        "gap_caliber": {
            "C": "gap = open / prev_close_adj − 1，prev_close_adj 取日历相邻前一交易日（复权）",
            "A_legacy": "gap = raw_open / raw_close.shift(1) − 1，上一「有数据日」，含除权与停牌",
            "why_C": "A 多出的 4,077 个 cell 里 3,870 个是除权伪缺口、249 个是停牌跨日；"
                     "A→C 让池均值 +1.35bp（详见 _diag_l1_gapset_audit.py 的 A_minus_C）",
        },
        "weighting": {
            "cell": "池内每个 (symbol, date) 等权 ⇒ 大池日权重高",
            "day": "先日内等权、再对日等权 ⇒ 每个交易日等权",
            "rule": "键名必须带 cell_bp/day_bp；两者不同不是矛盾，是两种口径",
        },
        "domain": {
            "in_band": "VWAP 相关的量（vwap_entry、entry_price_penalty、pen_over_i1_in_band）",
            "all_cells": "open→close 与 pen_over_i1_all_cells；curvature 的 ratio_of_means 用的是这一版",
            "why_split": "13 个 vwap∉[low,high] 的 cell 只污染分母含 vwap 的均值；两个定义域都落盘，"
                         "引用时必须说清是哪一版（差 ≤0.004，不改变任何结论）",
        },
    }


def main() -> None:
    t0 = time.time()
    daily = load_daily()

    # ---------- cohort：与 annualize 逐位相同的一批 cell ----------
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
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
        pool_idx = np.flatnonzero(pool)
        tv = turn[i0][pool_idx]
        order = np.argsort(tv, kind="stable")
        rk = np.empty(pool_idx.size, dtype=np.int64)
        rk[order] = np.arange(pool_idx.size)
        dec_full = np.full(S, -1, dtype=np.int64)
        dec_full[pool_idx] = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool_idx.size)
        fpool = pool_idx[fill[pool_idx]]
        if fpool.size < 50:
            continue
        if np.bincount(dec_full[good][dec_full[good] >= 0], minlength=N_DECILE).sum() <= 0:
            continue
        for j in good:
            cells.append((str(dates[i0 + 1]), symbols[j]))

    n_day = len({x[0] for x in cells})
    n_cell = len(cells)
    if (n_day, n_cell) != (EXPECT_DAYS, EXPECT_CELLS):
        raise SystemExit(
            f"cohort drift: {n_day} days / {n_cell} cells, expected {EXPECT_DAYS} / {EXPECT_CELLS} "
            "- 与 _diag_l1_annualize 不再是同一批样本"
        )

    need = pl.DataFrame({"date": [x[0] for x in cells], "symbol": [x[1] for x in cells]})
    cw = daily.join(need, on=["date", "symbol"], how="inner").unique(subset=["date", "symbol"])
    bad = int((~cw["in_band"]).sum())
    cw_ok = cw.filter(pl.col("in_band"))
    io_d, iv_d, pen_d = daily_pair(cw_ok)
    diff = io_d - iv_d
    n = diff.size
    sd = float(diff.std(ddof=1))
    span_years = T / DAYS_PER_YEAR
    sig_per_year = n / span_years
    net_open = float(io_d.mean()) - COST_BP
    net_vwap = float(iv_d.mean()) - COST_BP

    # ---------- 全缺口池（无选股器）：C 口径为主，A 口径只做对照 ----------
    pool_c = daily.filter(pl.col("gap_c") <= GAP_MAX)
    pool_a = daily.filter(pl.col("gap_a") <= GAP_MAX)        # 已作废，仅用于「差多少」的对照
    blk_c = pool_block(pool_c)
    blk_a = pool_block(pool_a)

    # ---------- cohort 的 EV 是不是也随「当日池规模」变 ----------
    # 池侧实测：按当日入池只数三分位，池的 open→close 是 63.94 / 18.21 / 59.50bp。
    # 但**这个 U 形本身不可判**（块自举、重定边界后 CI 跨 0），而且 74% 的幅度在
    # 同日非池股身上同样出现 ⇒ 它是市场级日状态，不是缺口溢价。
    # 所以这一侧必须**两个镜头并排**：原始 EV（含市场 beta）与「减非池股」的 EV。
    _cd = cw_ok.group_by("date").agg(pl.len().alias("n_sig")).sort("date")
    _pn = pool_c.group_by("date").agg(pl.len().alias("n_pool")).sort("date")
    _np = (daily.filter(pl.col("gap_c") > GAP_MAX)
           .with_columns((pl.col("raw_close") / pl.col("raw_open") - 1.0).alias("_i1"))
           .group_by("date").agg(pl.col("_i1").mean().alias("m_nonpool")).sort("date"))
    _st = _cd.join(_pn, on="date", how="left").join(_np, on="date", how="left")
    if _st.height != io_d.size:
        raise SystemExit(f"cohort 日数 {_st.height} != {io_d.size}（顺序对齐失败）")
    pn = _st["n_pool"].fill_null(0).to_numpy().astype(float)
    mnp = _st["m_nonpool"].to_numpy().astype(float) * 1e4
    if np.isnan(mnp).any():
        raise SystemExit("非池基准有缺失日：cohort 的日子必须在非池里有样本")
    co_ex = io_d - mnp
    _q1, _q2 = np.quantile(pn, [1 / 3, 2 / 3])
    day_state = {}
    for lab, msk in (("pool_small", pn <= _q1), ("pool_mid", (pn > _q1) & (pn <= _q2)),
                     ("pool_large", pn > _q2)):
        day_state[lab] = {
            "days": int(msk.sum()),
            "mean_pool_names": round(float(pn[msk].mean()), 1),
            "cohort_i1_day_bp": round(float(io_d[msk].mean()), 2),
            "cohort_i1_day_sd_bp": round(float(io_d[msk].std(ddof=1)), 1),
            "cohort_i1_day_se_bp": round(float(io_d[msk].std(ddof=1) / np.sqrt(msk.sum())), 1),
            "cohort_net_day_after_cost_bp": round(float(io_d[msk].mean()) - COST_BP, 2),
            "cohort_excess_day_bp": round(float(co_ex[msk].mean()), 2),
            "cohort_excess_day_sd_bp": round(float(co_ex[msk].std(ddof=1)), 1),
            "cohort_excess_day_se_bp": round(float(co_ex[msk].std(ddof=1) / np.sqrt(msk.sum())), 1),
            "nonpool_day_bp": round(float(mnp[msk].mean()), 2),
        }
    _recon = sum(v["cohort_i1_day_bp"] * v["days"] for v in day_state.values()) / n
    if abs(_recon - float(io_d.mean())) > 0.02:
        raise SystemExit(f"分档重建失败: {_recon} != {io_d.mean()}")
    _recon_ex = sum(v["cohort_excess_day_bp"] * v["days"] for v in day_state.values()) / n
    if abs(_recon_ex - float(co_ex.mean())) > 0.02:
        raise SystemExit(f"cohort 超额分档重建失败: {_recon_ex} != {co_ex.mean()}")

    # ---------- 容量轴 ----------
    amounts = cw_ok["amount"].to_numpy().astype(float)
    a_med = float(np.median(amounts))
    names = n_cell / n_day
    cap_open = names * IMPACT_LINE * FIRST_BAR_SHARE * a_med
    prof_open = cap_open * (net_open / 1e4) * sig_per_year
    pov = {}
    for p in (0.05, 0.02, 0.01):
        cap = names * p * a_med
        pov[f"p={p:.0%}"] = {
            "participation": p,
            "round_capital_cny": round(cap, 0),
            "annual_profit_cny": round(cap * (net_vwap / 1e4) * sig_per_year, 0),
        }
    breakeven = prof_open / (names * 0.05 * a_med * sig_per_year / 1e4)

    out = {
        "cell": "top20 mom20, gap<=-1.0%, entry open[d+1] vs full-day VWAP, exit close[d+1]",
        "vwap_formula": "amount / (volume * 100)",
        "minute_coverage": minute_coverage(),
        "instrument_check": instrument_check(daily),
        "cohort": {
            "n_days": n,
            "n_cells_expected": n_cell,
            "n_cells_paired": cw_ok.height,
            "n_cells_out_of_band": bad,
            "names_per_day": round(names, 2),
        },
        "cohort_by_pool_size_tercile": day_state,
        "daily_bp": {
            "open_entry": round(float(io_d.mean()), 1),
            "vwap_entry": round(float(iv_d.mean()), 1),
            "net_open_after_cost": round(net_open, 1),
            "net_vwap_after_cost": round(net_vwap, 1),
            "entry_price_penalty": {
                "mean": round(float(pen_d.mean()), 1),
                "median": round(float(np.median(pen_d)), 1),
                "comment": "vwap/raw_open-1：摊开到 VWAP 要比 open 多付的价",
            },
            "share_of_gross_kept_by_vwap": round(float(iv_d.mean() / io_d.mean()), 3),
            "paired_diff": {
                "mean_bp": round(float(diff.mean()), 1),
                "sd_bp": round(sd, 1),
                "t_iid": round(float(diff.mean() / (sd / math.sqrt(n))), 2),
                "t_nw5": round(nw_t(diff, 5), 2),
                "ci95_block20_bp": [round(v, 1) for v in block_ci(diff)],
                "hit_positive": round(float((diff > 0).mean()), 3),
            },
        },
        "robustness": robustness(daily, pool_c, pool_a),
        "gap_pool_unselected": {
            "caliber": "C",
            "caliber_definition": "gap = open / prev_close_adj − 1，日历相邻前一交易日（复权）",
            **blk_c,
            "a_legacy": blk_a,
            "a_minus_c_open_entry_day_bp": round(
                blk_a["open_entry_day_bp"] - blk_c["open_entry_day_bp"], 2),
            "gap_pool_by_depth": {
                "role": "reference",
                "caliber": "C",
                "source": "_diag_l1_vwap_curvature_result.json#gap_pool_by_depth",
                "n_cells_in_band": blk_c["n_cells_in_band"],
                "note_template": "深度四档不在本脚本算：同一张表由 curvature 按 C 口径产出"
                                 "（{n} cell，q1…q4）。旧表用的是 A 口径，除权伪缺口会流进最深档，已删除；"
                                 "本池 N 与之相等是测试断言，不是抄写。",
                "note_fields": {"n": "n_cells_in_band"},
            },
        },
        "annualized": {
            "signals_per_year": round(sig_per_year, 1),
            "open_entry_round_capital": round(net_open * sig_per_year / 1e4, 4),
            "vwap_entry_round_capital": round(net_vwap * sig_per_year / 1e4, 4),
        },
        "capacity_axis": {
            "amount_median_cny": round(a_med, 0),
            "open_entry": {
                "round_capital_cny": round(cap_open, 0),
                "annual_profit_cny": round(prof_open, 0),
                "net_bp": round(net_open, 1),
            },
            "pov_entry": pov,
            "breakeven_net_bp_for_5pct_pov": round(breakeven, 1),
        },
        "elapsed_s": 0.0,
    }
    gp = out["gap_pool_unselected"]
    gp["reading_template"] = (
        "全缺口池 C 口径（日历相邻 + 复权）：{n_days} 日 / {n_cells} cell，"
        "open→close 逐日等权 {open_day}bp、cell 等权 {open_cell}bp；"
        "同一批 cell 上 VWAP→close 的 cell 中位 {iv_med_all}bp（全池）/ {iv_med}bp（in_band）、"
        "筛后均值 {iv_cell}bp、逐日等权 {iv_day}bp。"
        "{n_out} 个 in_band 越界 cell 能把 VWAP→close 的 cell 均值抬到 {iv_raw}bp、"
        "逐日等权抬到 {iv_raw_day}bp，而中位两个定义域只差 {iv_med_gap}bp。"
        "A 口径（{a_cells} cell）的 {a_open}bp 已作废，与 C 差 {a_minus_c}bp。"
    )
    gp["reading_fields"] = {
        "n_days": "n_days", "n_cells": "n_cells",
        "open_day": "open_entry_day_bp", "open_cell": "open_entry_cell_bp",
        "iv_med": "vwap_entry_cell_median_in_band_bp",
        "iv_med_all": "vwap_entry_cell_median_all_cells_bp",
        "iv_med_gap": "vwap_entry_cell_median_abs_gap_bp",
        "iv_cell": "vwap_entry_cell_bp", "iv_day": "vwap_entry_day_bp",
        "iv_raw": "vwap_unfiltered.cell_bp", "iv_raw_day": "vwap_unfiltered.day_bp",
        "n_out": "n_cells_out_of_band",
        "a_cells": "a_legacy.n_cells", "a_open": "a_legacy.open_entry_day_bp",
        "a_minus_c": "a_minus_c_open_entry_day_bp",
    }
    out["elapsed_s"] = round(time.time() - t0, 1)
    gp["reading"] = render_reading(gp, root=out)      # 散文的唯一产地
    gp["gap_pool_by_depth"]["note"] = render_reading(gp["gap_pool_by_depth"], root=out, stem="note")
    assert_clean(out, label=OUT.name)                 # 写盘前：违反则 SystemExit
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    mc = out["minute_coverage"]
    ic = out["instrument_check"]
    print(f"minute library: {mc['partitions']} partitions / {mc['symbols']} symbols "
          f"({mc['first']} -> {mc['last']})")
    print(f"instrument: n={ic['n_symbol_days']} vwap diff med {ic['d_vwap_median_abs_bp']}bp "
          f"p90 {ic['d_vwap_p90_abs_bp']}bp  open diff max {ic['d_open_max_abs_bp']}bp")
    co = out["cohort"]
    print(f"cohort: {co['n_days']} days / {co['n_cells_paired']}/{n_cell} paired / out-of-band {bad}")
    db = out["daily_bp"]
    print(f"open {db['open_entry']}bp  vwap {db['vwap_entry']}bp  "
          f"kept {db['share_of_gross_kept_by_vwap']*100:.1f}%  pen {db['entry_price_penalty']['mean']}bp")
    pd_ = db["paired_diff"]
    print(f"paired diff {pd_['mean_bp']}bp sd {pd_['sd_bp']} t {pd_['t_iid']} NW5 {pd_['t_nw5']} "
          f"CI {pd_['ci95_block20_bp']} hit {pd_['hit_positive']}")
    gp = out["gap_pool_unselected"]
    print(f"gap pool C ({gp['n_days']}d / {gp['n_cells']} cells, in_band {gp['n_cells_in_band']}): "
          f"open day {gp['open_entry_day_bp']}bp / cell {gp['open_entry_cell_bp']}bp | "
          f"vwap day {gp['vwap_entry_day_bp']}bp / cell {gp['vwap_entry_cell_bp']}bp")
    print(f"  A legacy ({gp['a_legacy']['n_cells']} cells): open day {gp['a_legacy']['open_entry_day_bp']}bp "
          f"/ cell {gp['a_legacy']['open_entry_cell_bp']}bp  (Δ {gp['a_minus_c_open_entry_day_bp']}bp)")
    rb = out["robustness"]
    mw = rb["market_wide"]
    print(f"robustness: market slope pen~i1 {mw['slope_pen_on_i1']} (intercept {mw['intercept_bp']}bp)")
    print(f"  by amount: {mw['slope_by_amount_tercile']}")
    for k, v in rb["gap_pool_by_window"].items():
        print(f"  pool {k:8s} n {v['n_cells']:7d}/{v['n_days']:3d}d  open "
              f"{v['open_entry_cell_bp']:6.2f}(cell) {v['open_entry_day_bp']:6.2f}(day)  vwap "
              f"{v['vwap_entry_cell_bp']:6.2f}/{v['vwap_entry_day_bp']:6.2f}  "
              f"pen/i1 {v['pen_over_i1_in_band']} (all {v['pen_over_i1_all_cells']})")
    print(f"  depth table -> {gp['gap_pool_by_depth']['source']} "
          f"(n_here {gp['gap_pool_by_depth']['n_cells_in_band']})")
    ca = out["capacity_axis"]
    print(f"capacity: open {ca['open_entry']['round_capital_cny']/1e4:.0f}wan -> "
          f"profit {ca['open_entry']['annual_profit_cny']/1e4:.0f}wan/yr")
    for k, v in ca["pov_entry"].items():
        print(f"  POV {k}: capital {v['round_capital_cny']/1e8:.2f}yi -> "
              f"profit {v['annual_profit_cny']/1e4:.0f}wan/yr")
    print(f"  breakeven net bp at 5% POV: {ca['breakeven_net_bp_for_5pct_pov']}")
    print(f"elapsed {out['elapsed_s']}s -> {OUT.name}")


if __name__ == "__main__":
    main()
