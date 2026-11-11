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
    src = src.with_columns([
        (pl.col("amount") / (pl.col("volume") * 100.0)).alias("vwap"),
        (pl.col("raw_close") / pl.col("close")).alias("adj_f"),
    ])
    src = src.with_columns((pl.col("open") * pl.col("adj_f")).alias("raw_open"))
    return src.with_columns(
        ((pl.col("vwap") >= pl.col("raw_low") * (1.0 - BAND_TOL))
         & (pl.col("vwap") <= pl.col("raw_high") * (1.0 + BAND_TOL))).alias("in_band")
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


def robustness(daily: pl.DataFrame, pool: pl.DataFrame) -> dict:
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

    px = (pool["raw_close"] / pool["raw_open"] - 1.0).to_numpy() * 1e4
    py = (pool["vwap"] / pool["raw_open"] - 1.0).to_numpy() * 1e4
    pv = (pool["raw_close"] / pool["vwap"] - 1.0).to_numpy() * 1e4
    pdate = pool["date"].to_numpy()
    windows = {"full": np.ones(px.size, dtype=bool)}
    for y_ in ("2025", "2026"):
        windows[y_] = np.char.startswith(pdate.astype(str), y_)
    tail = sorted(set(pdate.tolist()))[-21:]
    windows["last21"] = np.isin(pdate, tail)
    by_window = {
        k: {
            "n_cells": int(m.sum()),
            "open_entry_bp": round(float(px[m].mean()), 1),
            "vwap_entry_bp": round(float(pv[m].mean()), 1),
            "pen_over_i1": round(float(py[m].mean() / px[m].mean()), 3) if px[m].mean() else None,
        }
        for k, m in windows.items()
    }
    return {
        "note": "pen~i1 斜率 0.5 = 日内路径线性（VWAP 在中间）；越接近 1 = 涨幅越集中在开盘",
        "market_wide": {
            "n_stock_days": int(np.isfinite(x).sum()),
            "slope_pen_on_i1": round(s_all, 3),
            "intercept_bp": round(i_all, 2),
            "slope_by_amount_tercile": tercile,
        },
        "gap_pool_by_window": by_window,
        "weighting": "by_window 是 cell 等权；gap_pool_unselected 是逐日等权，两者的 open_entry_bp 不同不是矛盾",
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

    # ---------- 全缺口池（无选股器）：判这是 cohort 特性还是结构性 ----------
    pool = daily.sort(["symbol", "date"]).with_columns(
        (pl.col("raw_open") / pl.col("raw_close").shift(1).over("symbol") - 1.0).alias("gap")
    ).filter(pl.col("gap") <= GAP_MAX)
    pool = pool.filter(pl.col("in_band"))
    po_d, pv_d, pp_d = daily_pair(pool)
    p_n = po_d.size
    q = pool.with_columns(
        pl.col("gap").qcut([0.25, 0.5, 0.75], labels=["q1", "q2", "q3", "q4"]).alias("bucket")
    ).group_by("bucket").agg([
        pl.len().alias("n"),
        pl.col("gap").mean().alias("gap_mean"),
        (pl.col("raw_close") / pl.col("raw_open") - 1.0).mean().alias("io"),
        (pl.col("raw_close") / pl.col("vwap") - 1.0).mean().alias("iv"),
        (pl.col("vwap") / pl.col("raw_open") - 1.0).mean().alias("pen"),
    ]).sort("bucket")

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
        "robustness": robustness(daily, pool),
        "gap_pool_unselected": {
            "n_days": p_n,
            "n_cells": pool.height,
            "open_entry_bp": round(float(po_d.mean()), 1),
            "vwap_entry_bp": round(float(pv_d.mean()), 1),
            "entry_price_penalty_bp": round(float(pp_d.mean()), 1),
            "share_of_gross_kept_by_vwap": round(float(pv_d.mean() / po_d.mean()), 4),
            "by_gap_depth": [
                {
                    "bucket": str(r["bucket"]),
                    "n": int(r["n"]),
                    "gap_bp": round(float(r["gap_mean"]) * 1e4, 0),
                    "open_entry_bp": round(float(r["io"]) * 1e4, 1),
                    "vwap_entry_bp": round(float(r["iv"]) * 1e4, 1),
                    "entry_price_penalty_bp": round(float(r["pen"]) * 1e4, 1),
                }
                for r in q.to_dicts()
            ],
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
    out["elapsed_s"] = round(time.time() - t0, 1)
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
    print(f"gap pool ({gp['n_days']}d / {gp['n_cells']} cells): open {gp['open_entry_bp']}bp "
          f"vwap {gp['vwap_entry_bp']}bp kept {gp['share_of_gross_kept_by_vwap']*100:.1f}%")
    rb = out["robustness"]
    mw = rb["market_wide"]
    print(f"robustness: market slope pen~i1 {mw['slope_pen_on_i1']} (intercept {mw['intercept_bp']}bp)")
    print(f"  by amount: {mw['slope_by_amount_tercile']}")
    for k, v in rb["gap_pool_by_window"].items():
        print(f"  pool {k:8s} n {v['n_cells']:7d} open {v['open_entry_bp']:6.1f} "
              f"vwap {v['vwap_entry_bp']:6.1f} pen/i1 {v['pen_over_i1']}")
    for r in gp["by_gap_depth"]:
        print(f"  {r['bucket']} gap {r['gap_bp']:6.0f}bp  open {r['open_entry_bp']:6.1f}  "
              f"vwap {r['vwap_entry_bp']:6.1f}  pen {r['entry_price_penalty_bp']:6.1f}")
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
