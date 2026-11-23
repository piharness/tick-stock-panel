"""配对回放：已有策略「趋势突破」加/不加痕迹排除器。

策略（backend/app/strategy/seeds/trend_breakout.py，默认参数）：
  入场：close > MA60 且 close >= 60 日收盘新高 且 volume / 前 5 日均量 >= 2
  基础池：价格 5~200、总市值 >= 20 亿、成交额 >= 1 亿、非 ST、上市 >= 60 交易日
  出场：收盘跌破 MA20（前一日在 MA20 上方）→ 次日开盘卖；止损 -8%；最长持有 20 日
  组合：最多 10 只，等权，T+1 开盘成交，双边成本合计 0.15%

过滤（信号日收盘可知）：
  F1  近 20 日出现过涨停（consecutive_limit_ups > 0）
  F2  换手加速 turn_chg = mean(turn,5)/mean(turn,20)-1 位于当日基础池 top decile
  F3  F1 或 F2

输出：2019-01→2026-11 的净超额、回撤、换手、交易数、逐笔胜率。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / ".scratch" / "footprint20d"
OUT.mkdir(parents=True, exist_ok=True)

ENRICHED = DATA / "kline_daily_enriched"
SHARES = DATA / "backfill_20261117" / "fin_shares.parquet"
INSTR = DATA / "instruments" / "instruments.parquet"
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")

START = pl.date(2018, 6, 1)
REPLAY_START = pd.Timestamp("2019-01-02")
END = pd.Timestamp("2026-11-20")


def build() -> None:
    lf = pl.scan_parquet(str(ENRICHED / "*" / "part.parquet"))
    df = (
        lf.filter(pl.col("date") >= START)
        .select(
            "symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "raw_close", "raw_high", "raw_low", "turnover_rate", "consecutive_limit_ups",
        )
        .collect()
    )
    df = df.filter(pl.col("symbol").str.slice(0, 3).is_in(MAIN)).sort(["symbol", "date"])

    sh = (
        pl.read_parquet(SHARES)
        .select("symbol", "end_date", "total_shares")
        .filter(pl.col("total_shares") > 0)
        .sort(["symbol", "end_date"])
    )
    df = df.sort(["symbol", "date"]).join_asof(
        sh, left_on="date", right_on="end_date", by="symbol", strategy="backward"
    )

    ins = (
        pl.read_parquet(INSTR)
        .select("symbol", "name", "listing_date")
        .with_columns(pl.col("listing_date").str.to_date())
    )
    df = df.join(ins, on="symbol", how="left")

    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret"),
        pl.int_range(1, pl.len() + 1).over("symbol").alias("k"),
        pl.col("close").rolling_mean(20, min_samples=15).over("symbol").alias("ma20"),
        pl.col("close").rolling_mean(60, min_samples=45).over("symbol").alias("ma60"),
        pl.col("close").rolling_max(60, min_samples=45).over("symbol").alias("high60"),
        pl.col("volume").shift(1).rolling_mean(5, min_samples=4).over("symbol").alias("vol_ma5_prev"),
        (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("mom60"),
        pl.col("turnover_rate").rolling_mean(5, min_samples=4).over("symbol").alias("turn5"),
        pl.col("turnover_rate").rolling_mean(20, min_samples=15).over("symbol").alias("turn20"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int8).alias("is_limit_up"),
    )
    df = df.with_columns(
        (pl.col("turn5") / pl.col("turn20") - 1).alias("turn_chg"),
        pl.col("is_limit_up").rolling_sum(20, min_samples=15).over("symbol").alias("limit20"),
        pl.col("close").shift(1).over("symbol").alias("prev_close"),
        pl.col("ma20").shift(1).over("symbol").alias("prev_ma20"),
        (pl.col("volume") / pl.col("vol_ma5_prev")).alias("vol_ratio"),
        pl.col("name").str.contains("ST").cast(pl.Int8).alias("is_st"),
    )
    df = df.with_columns(
        ((pl.col("close") < pl.col("ma20")) & (pl.col("prev_close") >= pl.col("prev_ma20")))
        .cast(pl.Int8).alias("exit_signal"),
        (
            (pl.col("raw_close") >= 5.0) & (pl.col("raw_close") <= 200.0)
            & ((pl.col("raw_close") * pl.col("total_shares")) >= 2e9)
            & (pl.col("amount") >= 1e8)
            & (pl.col("k") >= 60) & (pl.col("is_st") == 0)
            & (pl.col("amount") > 0) & pl.col("open").is_not_null()
        ).cast(pl.Int8).alias("basic_ok"),
    )
    df = df.with_columns(
        (
            (pl.col("basic_ok") == 1)
            & (pl.col("close") > pl.col("ma60"))
            & (pl.col("close") >= pl.col("high60"))
            & (pl.col("vol_ratio") >= 2.0)
        ).cast(pl.Int8).alias("entry"),
    )

    # 分数：仅在基础池内做横截面百分位，权重与 META.scoring 一致
    df = df.with_columns(
        pl.when(pl.col("basic_ok") == 1).then(pl.col("mom60")).otherwise(None).alias("_m"),
        pl.when(pl.col("basic_ok") == 1).then(pl.col("vol_ratio")).otherwise(None).alias("_v"),
        pl.when(pl.col("basic_ok") == 1).then(pl.col("ret")).otherwise(None).alias("_c"),
        pl.when(pl.col("basic_ok") == 1).then(pl.col("turn_chg")).otherwise(None).alias("_t"),
    )
    # 与引擎 build_matrix_score 一致：按日 min-max 归一化，再按 0.4/0.3/0.3 加权
    for src, dst in [("_m", "n_m"), ("_v", "n_v"), ("_c", "n_c")]:
        df = df.with_columns(
            ((pl.col(src) - pl.col(src).min().over("date"))
             / (pl.col(src).max().over("date") - pl.col(src).min().over("date"))).alias(dst)
        )
    df = df.with_columns(
        (0.4 * pl.col("n_m").fill_null(0.5)
         + 0.3 * pl.col("n_v").fill_null(0.5)
         + 0.3 * pl.col("n_c").fill_null(0.5)).alias("score"),
        # 过滤阈值仍用基础池内的百分位，避免 min-max 被极端值扭曲
        (pl.col("_t").rank("average").over("date") / pl.col("_t").count().over("date") >= 0.9)
        .cast(pl.Int8).alias("turn_top"),
    )

    keep = [
        "symbol", "date", "open", "high", "low", "close", "raw_close", "raw_high", "raw_low",
        "amount", "ret", "entry", "exit_signal", "basic_ok", "limit20", "turn_top", "score",
        "ma60", "high60", "vol_ratio", "volume", "vol_ma5_prev",
    ]
    out = df.select(keep)
    out.write_parquet(OUT / "replay_daily.parquet")
    print("daily", out.shape, "dates", out["date"].n_unique(), "symbols", out["symbol"].n_unique())
    e = out.filter(pl.col("entry") == 1)
    print("raw signals", e.height, "by year", e.group_by(pl.col("date").dt.year()).agg(pl.len()).sort("date").to_dicts())
    print("signal limit20 share", e["limit20"].mean(), "turn_top share", e["turn_top"].mean())


def _pivot(df: pd.DataFrame, col: str) -> pd.DataFrame:
    return df.pivot(index="date", columns="symbol", values=col).sort_index()


_MAT: dict[int, tuple] = {}


def _matrices(panel: pd.DataFrame) -> tuple:
    key = id(panel)
    if key in _MAT:
        return _MAT[key]
    dates_all = sorted(panel["date"].unique())
    syms = sorted(panel["symbol"].unique())
    cols = {c: _pivot(panel, c).reindex(index=dates_all, columns=syms) for c in
            ["open", "high", "low", "close", "entry", "exit_signal", "basic_ok",
             "limit20", "turn_top", "score", "ret"]}
    out = (
        cols["open"].to_numpy(float), cols["high"].to_numpy(float),
        cols["low"].to_numpy(float), cols["close"].to_numpy(float),
        cols["entry"].to_numpy(float) == 1, cols["exit_signal"].to_numpy(float) == 1,
        cols["basic_ok"].to_numpy(float) == 1, cols["limit20"].to_numpy(float) > 0,
        cols["turn_top"].to_numpy(float) == 1, cols["score"].to_numpy(float),
        cols["ret"].to_numpy(float), syms, dates_all,
    )
    _MAT[key] = out
    return out


def replay(panel: pd.DataFrame, mode: str, max_pos: int = 10, cost: float = 0.0015,
           stop_pct: float | None = -0.08, use_signal_exit: bool = True,
           score_mode: str = "high", hold_days: int = 20, seed: int = 0,
           start=REPLAY_START, end=END) -> dict:
    op, hi, lo, cl, en, ex, bok, lim, ttop, score, ret, syms, all_dates = _matrices(panel)
    dates = [d for d in all_dates if start <= d <= end]
    idx = {d: i for i, d in enumerate(all_dates)}
    t0, t1 = idx[dates[0]], idx[dates[-1]]

    # 过滤掩码（信号日）
    if mode == "baseline":
        bad = np.zeros_like(en)
    elif mode == "no_limit":
        bad = lim
    elif mode == "no_turn":
        bad = ttop
    elif mode == "no_both":
        bad = lim | ttop
    else:
        raise ValueError(mode)

    cash = 1.0
    nav0 = 1.0
    pos: dict[int, dict] = {}
    trades = []
    navs, benchs, expo = [], [], []
    buy_val = sell_val = 0.0

    for t in range(t0, t1 + 1):
        # 1) 卖出（先止损，再信号，再到期）
        for a in list(pos):
            p = pos[a]
            px = None
            reason = None
            if t > p["entry_t"]:
                if stop_pct is not None and np.isfinite(op[t, a]) and op[t, a] <= p["stop"]:
                    px, reason = op[t, a], "stop"
                elif stop_pct is not None and np.isfinite(lo[t, a]) and lo[t, a] <= p["stop"]:
                    px, reason = p["stop"], "stop"
                elif use_signal_exit and t - 1 >= 0 and ex[t - 1, a]:
                    px, reason = (op[t, a] if np.isfinite(op[t, a]) else cl[t, a]), "signal"
                elif t - p["entry_t"] >= hold_days:
                    px, reason = (cl[t, a] if np.isfinite(cl[t, a]) else op[t, a]), "max_hold"
            if reason is not None and np.isfinite(px) and px > 0:
                proceeds = p["shares"] * px * (1 - cost / 2)
                cash += proceeds
                sell_val += p["shares"] * px
                trades.append({
                    "symbol": syms[a], "entry_date": all_dates[p["entry_t"]], "exit_date": all_dates[t],
                    "entry": p["entry_px"], "exit": px, "reason": reason,
                    "ret": (px * (1 - cost / 2)) / (p["entry_px"] * (1 + cost / 2)) - 1,
                    "hold": t - p["entry_t"],
                })
                del pos[a]

        # 2) 买入（信号来自 t-1 收盘）
        if t - 1 >= 0:
            base = np.flatnonzero(en[t - 1] & bok[t - 1])
            if len(base):
                # 当日候选池内按评分分五档（与事件层同口径）
                s_base = score[t - 1, base]
                order = np.argsort(np.argsort(s_base, kind="stable"), kind="stable")
                q_base = np.clip((order * 5 // max(len(base), 1)), 0, 4)
                qmap = np.full(en.shape[1], -1, dtype=int)
                qmap[base] = q_base
                cand = base[~bad[t - 1][base]]
                if score_mode == "no_q4":
                    cand = cand[qmap[cand] < 4]
                elif score_mode == "q0":
                    cand = cand[qmap[cand] == 0]
                # 可成交：有开盘价、非一字涨停、跳空 < 9.5%
                gap = np.where(np.isfinite(cl[t - 1]) & (cl[t - 1] > 0), op[t] / cl[t - 1] - 1, np.nan)
                oneword = (np.abs(hi[t] - lo[t]) < 1e-9) & (gap > 0.095)
                ok = np.isfinite(op[t]) & ~oneword & (gap < 0.095) & ~np.isnan(gap)
                cand = cand[ok[cand]]
                cand = [a for a in cand if a not in pos]
                if cand:
                    if score_mode == "random":
                        cand = list(np.random.default_rng(seed + t).permutation(cand))
                    else:
                        rev = score_mode == "low"
                        cand = sorted(cand, key=lambda a: score[t - 1, a] if rev else -score[t - 1, a])
                    cand = cand[: max(0, max_pos - len(pos))]
                    eq_open = cash + sum(p["shares"] * op[t, a] for a, p in pos.items()
                                         if np.isfinite(op[t, a]))
                    target = eq_open / max_pos
                    for a in cand:
                        if len(pos) >= max_pos:
                            break
                        px = op[t, a]
                        if not np.isfinite(px) or px <= 0:
                            continue
                        alloc = min(target, cash)
                        if alloc <= 1e-9:
                            break
                        shares = alloc / (px * (1 + cost / 2))
                        cash -= shares * px * (1 + cost / 2)
                        buy_val += shares * px
                        pos[a] = {"entry_t": t, "entry_px": px, "shares": shares,
                                  "stop": px * (1 + stop_pct) if stop_pct is not None else -np.inf}

        mv = sum(p["shares"] * (cl[t, a] if np.isfinite(cl[t, a]) else p["entry_px"])
                 for a, p in pos.items())
        nav = cash + mv
        navs.append(nav)
        expo.append(mv / nav if nav > 0 else 0)
        # 基准：t-1 通过基础池的股票，t 日等权收益
        m = bok[t - 1] & np.isfinite(ret[t])
        benchs.append(np.nanmean(ret[t, m]) if m.sum() >= 20 else 0.0)

    # 清仓（仅用于统计，不计入 nav 序列）
    nav = pd.Series(navs, index=dates)
    bench = pd.Series(benchs, index=dates)
    pr = nav.pct_change().fillna(nav.iloc[0] - 1)
    exr = pr - bench
    years = len(nav) / 252
    dd = (nav / nav.cummax() - 1).min()
    exnav = (1 + exr).cumprod()
    trades = pd.DataFrame(trades)
    tr = trades[trades["reason"] != "end"] if len(trades) else trades
    open_positions = [
        {"symbol": syms[a], "entry_date": str(all_dates[p["entry_t"]])[:10],
         "entry": float(p["entry_px"]), "shares": float(p["shares"])}
        for a, p in pos.items()
    ]
    return {
        "mode": mode,
        "days": len(nav),
        "cagr": float(nav.iloc[-1] ** (1 / years) - 1),
        "ann_excess": float(exr.mean() * 252),
        "sharpe_excess": float(exr.mean() / exr.std() * np.sqrt(252)) if exr.std() > 0 else np.nan,
        "max_dd": float(dd),
        "max_dd_excess": float((exnav / exnav.cummax() - 1).min()),
        "ann_turnover": float(buy_val / nav.mean() / years),
        "avg_exposure": float(np.mean(expo)),
        "trades": int(len(tr)),
        "trades_per_year": float(len(tr) / years),
        "win_rate": float((tr["ret"] > 0).mean()) if len(tr) else np.nan,
        "avg_trade_ret": float(tr["ret"].mean()) if len(tr) else np.nan,
        "avg_hold": float(tr["hold"].mean()) if len(tr) else np.nan,
        "nav": nav,
        "trades_df": trades,
        "open_positions": open_positions,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--max-pos", type=int, default=10)
    ap.add_argument("--cost", type=float, default=0.0015)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--score-mode", default="high", choices=["high", "low", "no_q4", "q0"])
    a = ap.parse_args()
    if a.build:
        build()
    df = pd.read_parquet(OUT / "replay_daily.parquet")
    df["date"] = pd.to_datetime(df["date"])
    res = {}
    curves = {}
    for mode in ["baseline", "no_limit", "no_turn", "no_both"]:
        r = replay(df, mode, max_pos=a.max_pos, cost=a.cost)
        curves[mode] = r.pop("nav")
        r["trades_df"].to_csv(OUT / f"replay_trades_{mode}.csv", index=False)
        r.pop("trades_df")
        res[mode] = r
        print(mode, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
    pd.DataFrame(curves).to_csv(OUT / "replay_nav.csv")
    json.dump(res, open(OUT / "replay_result.json", "w"), ensure_ascii=False, indent=1)
    if a.grid:
        grid = []
        for stop, sig in [(-0.08, True), (-0.15, True), (None, True), (None, False)]:
            r = replay(df, "no_both", max_pos=a.max_pos, cost=a.cost,
                       stop_pct=stop, use_signal_exit=sig)
            r.pop("trades_df"); r.pop("nav")
            grid.append({"stop": stop, "signal_exit": sig, **r})
            print("grid", stop, sig, {k: round(v, 4) if isinstance(v, float) else v for k, v in grid[-1].items()})
        json.dump(grid, open(OUT / "replay_grid.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
