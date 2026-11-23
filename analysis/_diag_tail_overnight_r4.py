"""Round 4: capacity + equity curve + streak risk for the formula.

Answers the two questions a trader actually asks after "is it negative":
  - if I run it every day, what does the account look like?
  - how much money can it absorb before the print disappears?
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_tail_overnight_r4_result.json")


def main() -> None:
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore")
          .select(["symbol", "date", "open", "close", "raw_close", "volume", "amount",
                   "turnover_rate", "consecutive_limit_ups"]).collect())
    cal = df.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])
    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
        pl.col("open").shift(-1).over("symbol").alias("open_n"),
        pl.col("close").shift(1).over("symbol").alias("close_p"),
        pl.col("volume").shift(1).over("symbol").alias("vol_p1"),
        pl.int_range(pl.len()).over("symbol").alias("row_n"),
    ])
    df = df.with_columns([
        (pl.col("close") / pl.col("close_p") - 1.0).alias("chg"),
        pl.col("vol_p1").rolling_mean(5).over("symbol").alias("vol5"),
        (pl.col("consecutive_limit_ups") >= 1).cast(pl.Int8)
            .rolling_max(20).over("symbol").alias("lim20"),
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0) / 1e8).otherwise(None).alias("fmcap"),
    ])
    df = df.with_columns((pl.col("volume") / pl.col("vol5")).alias("vr"))
    df = df.with_columns((pl.col("open_n") / pl.col("close") - 1.0).alias("r_on"))

    base_ok = ((pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0)
               & pl.col("chg").is_finite() & pl.col("vr").is_finite() & (pl.col("vr") > 0)
               & pl.col("fmcap").is_finite() & pl.col("turnover_rate").is_finite()
               & (pl.col("rn_n") == pl.col("rn") + 1))
    full = (base_ok & (pl.col("lim20") == 1) & (pl.col("vr") > 1)
            & pl.col("turnover_rate").is_between(5, 10) & pl.col("fmcap").is_between(50, 200)
            & pl.col("chg").is_between(0.03, 0.05))
    sig = df.filter(full).select(["symbol", "date", "r_on", "amount", "fmcap", "turnover_rate"])

    out: dict = {"n_trades": int(sig.height)}

    # ---------- capacity ----------
    amt = sig["amount"].to_numpy()
    amt = amt[np.isfinite(amt)]
    q = np.percentile(amt, [5, 25, 50, 75, 95])
    out["capacity"] = {
        "median_amount_yuan": float(np.median(amt)),
        "p25": float(q[1]), "p75": float(q[3]),
        "median_amount_yi": round(float(np.median(amt)) / 1e8, 2),
        "share_below_1yi": round(float((amt < 1e8).mean()), 3),
        "share_below_5000w": round(float((amt < 5e7).mean()), 3),
        "implied_max_position_at_5pct_adv_yi": round(float(np.median(amt)) * 0.05 / 1e8, 3),
    }
    print("############ capacity")
    print(f"  median turnover on signal day: {np.median(amt)/1e8:.2f}e8  "
          f"(p25 {q[1]/1e8:.2f}  p75 {q[3]/1e8:.2f})")
    print(f"  share of signals with ADV < 1e8: {(amt<1e8).mean():.1%}   "
          f"< 0.5e8: {(amt<5e7).mean():.1%}")
    print(f"  position at 5% of ADV: {np.median(amt)*0.05/1e4:.0f}w yuan")

    # ---------- equity curve ----------
    daily = sig.group_by("date").agg(pl.col("r_on").mean().alias("m"), pl.len().alias("n")).sort("date")
    all_dates = cal["date"].to_list()
    dm = dict(zip(daily["date"].to_list(), daily["m"].to_list()))
    COST = 0.0015
    eq_full, eq_active = [1.0], [1.0]
    rets = []
    for d in all_dates:
        if d in dm:
            r = dm[d] - COST
            eq_full.append(eq_full[-1] * (1 + r))
            eq_active.append(eq_active[-1] * (1 + r))
            rets.append(r)
        else:
            eq_full.append(eq_full[-1])
    rets = np.array(rets)
    out["equity"] = {
        "trading_days": len(all_dates),
        "signal_days": int(len(rets)),
        "final_equity_full_calendar": round(eq_full[-1], 4),
        "final_equity_signal_days_only": round(eq_active[-1], 4),
        "years": round(len(all_dates) / 244, 1),
        "cagr_full": round(eq_full[-1] ** (244 / len(all_dates)) - 1, 4),
    }
    print("\n############ equity (reinvest 100% every signal day, 15bp cost)")
    print(f"  {len(all_dates)} calendar days / {len(rets)} signal days / {out['equity']['years']} years")
    print(f"  final equity, capital idle on no-signal days: {eq_full[-1]:.6f}")
    print(f"  final equity, compounding signal days only  : {eq_active[-1]:.6f}")
    print(f"  CAGR (full calendar)                        : {out['equity']['cagr_full']:.2%}")

    # ---------- streaks ----------
    s = np.where(rets > 0, 1, 0)
    worst = cur = 0
    for v in s:
        cur = 0 if v else cur + 1
        worst = max(worst, cur)
    peak, dd = -1e9, 0.0
    for v in eq_active:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    out["risk"] = {
        "daily_win_rate": round(float((rets > 0).mean()), 3),
        "worst_losing_streak_days": int(worst),
        "max_drawdown": round(dd, 4),
        "worst_day": round(float(rets.min()), 4),
        "best_day": round(float(rets.max()), 4),
    }
    print("\n############ risk")
    print(f"  daily win rate {out['risk']['daily_win_rate']:.1%}   "
          f"worst losing streak {worst} days   max DD {dd:.1%}")
    print(f"  best day {rets.max():+.2%}  worst day {rets.min():+.2%}")

    # ---------- top contributors (is it a few days?) ----------
    top = np.sort(rets)[-10:]
    print(f"\n  sum of best 10 days = {top.sum():+.2%}  vs total {rets.sum():+.2%}")
    out["concentration"] = {"best10_sum": round(float(top.sum()), 4),
                            "total_sum": round(float(rets.sum()), 4)}

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
