"""Audit part 2: the things part 1 could not settle.

B1 zero-volume rows that would inflate the volume ratio
B2 is `close` really qfq? check that adj = close/raw_close is piecewise
   CONSTANT (a step function) per symbol, not drifting daily. If it drifts,
   close[t+1]/close[t] is not a return.
B3 compounding vs arithmetic annualisation (I reported arithmetic)
B4 amount>=50m filter: how much does it change the answer?
B5 "20 days" counted as rows vs calendar days
B6 day-level vs trade-level aggregation gap for the headline number
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_tail_audit2_result.json")
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close", "volume",
        "amount", "turnover_rate", "consecutive_limit_ups"]


def main() -> None:
    rep: dict = {}
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore").select(COLS).collect())

    # ---------- B1 ----------
    z = df.filter(pl.col("volume") <= 0)
    rep["B1_zero_vol"] = int(z.height)
    print(f"[B1] volume<=0 rows: {z.height:,}  ({z.height/max(df.height,1):.3%})")

    # ---------- B2 ----------
    print("\n[B2] is close a qfq series? adj = close/raw_close should be a step function")
    test_syms = ["000001.SZ", "600519.SH", "000002.SZ", "601398.SH"]
    b2 = {}
    for s in test_syms:
        d = df.filter(pl.col("symbol") == s).sort("date")
        if d.height < 100:
            continue
        adj = (d["close"] / d["raw_close"]).to_numpy()
        # count distinct values rounded to 1e-6
        uniq = np.unique(np.round(adj, 6))
        # is it monotone non-decreasing? (qfq anchored at today -> older values smaller)
        diffs = np.diff(adj)
        b2[s] = {
            "rows": int(d.height),
            "distinct_adj": int(uniq.size),
            "share_days_adj_changes": round(float((np.abs(diffs) > 1e-9).mean()), 4),
            "adj_first": round(float(adj[0]), 6),
            "adj_last": round(float(adj[-1]), 6),
            "monotone_nondecreasing": bool((diffs >= -1e-9).all()),
            "max_adj": round(float(adj.max()), 6),
        }
        print(f"  {s:<10} rows={b2[s]['rows']:>5}  distinct_adj={b2[s]['distinct_adj']:>4}  "
              f"days_with_change={b2[s]['share_days_adj_changes']:.2%}  "
              f"first={b2[s]['adj_first']:.4f} last={b2[s]['adj_last']:.4f}  "
              f"monotone={b2[s]['monotone_nondecreasing']}")
    rep["B2"] = b2

    # ---------- B4 / B5 / B6 ----------
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
        (pl.col("close") / pl.col("close_p") - 1).alias("chg"),
        pl.col("vol_p1").rolling_mean(5).over("symbol").alias("vol5"),
        (pl.col("open_n") / pl.col("close") - 1).alias("r_on"),
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0) / 1e8).otherwise(None).alias("fmcap"),
    ])
    df = df.with_columns((pl.col("volume") / pl.col("vol5")).alias("vr"))

    # two versions of "limit-up in last 20": rows vs calendar days
    df = df.with_columns(
        (pl.col("consecutive_limit_ups") >= 1).cast(pl.Int8).rolling_max(20).over("symbol").alias("lim20_rows")
    )
    lim_dates = df.filter(pl.col("consecutive_limit_ups") >= 1).select(["symbol", "rn"]).unique()
    # calendar version: for each (symbol, rn) is there a limit-up with rn in [rn-20, rn]?
    # cheap approximation: join limit-up rns and take min distance
    last_lu = (df.filter(pl.col("consecutive_limit_ups") >= 1)
               .select(["symbol", "rn"]).rename({"rn": "lu_rn"}))
    j = df.select(["symbol", "rn"]).join(last_lu, on="symbol", how="left").filter(
        (pl.col("lu_rn") <= pl.col("rn")) & (pl.col("lu_rn") >= pl.col("rn") - 20))
    j = j.group_by(["symbol", "rn"]).len().rename({"len": "n_lu"}).with_columns(pl.lit(1).alias("lim20_cal"))
    df = df.join(j, on=["symbol", "rn"], how="left").with_columns(
        pl.col("lim20_cal").fill_null(0))

    base = ((pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0)
            & pl.col("chg").is_finite() & pl.col("vr").is_finite() & (pl.col("vr") > 0)
            & pl.col("fmcap").is_finite() & pl.col("turnover_rate").is_finite()
            & (pl.col("rn_n") == pl.col("rn") + 1) & pl.col("r_on").is_finite())
    tail5 = ((pl.col("vr") > 1) & pl.col("turnover_rate").is_between(5, 10)
             & pl.col("fmcap").is_between(50, 200) & pl.col("chg").is_between(0.03, 0.05))

    def stat(sub: pl.DataFrame) -> dict:
        if sub.height == 0:
            return {"n": 0}
        g = sub.group_by("date").agg(pl.col("r_on").mean().alias("m"), pl.len().alias("k")).sort("date")
        m = g["m"].to_numpy()
        return {
            "n_trades": int(g["k"].sum()), "n_dates": int(m.size),
            "day_mean_bp": round(float(m.mean()) * 1e4, 1),
            "trade_mean_bp": round(float(sub["r_on"].mean()) * 1e4, 1),
            "t_day": round(float(m.mean()) / (m.std(ddof=1) / np.sqrt(m.size)), 2),
        }

    print("\n[B5] '20 days' as 20 rows vs 20 calendar trading days")
    for lab, cond in [("rows", pl.col("lim20_rows") == 1), ("calendar", pl.col("lim20_cal") == 1)]:
        s = stat(df.filter(base & cond & tail5))
        rep[f"B5_{lab}"] = s
        print(f"  {lab:<9} n={s['n_trades']:>7,} dates={s['n_dates']:>5} "
              f"day_mean={s['day_mean_bp']:>+7.1f}bp t={s['t_day']:>+7.1f}")
    both = df.filter(base & (pl.col("lim20_rows") == 1) & (pl.col("lim20_cal") == 1)).height
    only_rows = df.filter(base & (pl.col("lim20_rows") == 1) & (pl.col("lim20_cal") == 0)).height
    rep["B5_agreement"] = {"both": int(both), "rows_only": int(only_rows)}
    print(f"  disagreement: rows-but-not-calendar = {only_rows:,}")

    print("\n[B4] amount filter sensitivity")
    for lab, extra in [("no amount filter", pl.lit(True)),
                       (">= 20m", pl.col("amount") >= 2e7),
                       (">= 50m (used)", pl.col("amount") >= 5e7),
                       (">= 100m", pl.col("amount") >= 1e8)]:
        s = stat(df.filter(base & extra & (pl.col("lim20_rows") == 1) & tail5))
        rep[f"B4_{lab}"] = s
        print(f"  {lab:<16} n={s['n_trades']:>7,} day_mean={s['day_mean_bp']:>+7.1f}bp "
              f"t={s['t_day']:>+7.1f}")

    print("\n[B6] day-equal vs trade-equal aggregation (headline)")
    s = stat(df.filter(base & (pl.col("lim20_rows") == 1) & tail5))
    s["gap_bp"] = round(s["day_mean_bp"] - s["trade_mean_bp"], 1)
    rep["B6"] = s
    print(f"  day-equal {s['day_mean_bp']:+.1f}bp | trade-equal {s['trade_mean_bp']:+.1f}bp | "
          f"gap {s['gap_bp']:+.1f}bp")

    print("\n[B3] compounding vs arithmetic annualisation")
    g = df.filter(base & (pl.col("lim20_rows") == 1) & tail5).group_by("date").agg(
        pl.col("r_on").mean().alias("m")).sort("date")
    m = g["m"].to_numpy()
    for cost in (0.0, 0.0015):
        n = (m - cost)
        arith = float(n.mean()) * 244 * 100
        eq = float(np.prod(1 + n) ** (244 / n.size) - 1) * 100
        rep.setdefault("B3", {})[f"cost_{cost}"] = {
            "arith_pct": round(arith, 1), "compound_pct": round(eq, 1),
            "n_dates": int(n.size)}
        print(f"  cost={cost*1e4:.0f}bp: arithmetic {arith:+.1f}%/yr  "
              f"compounding {eq:+.1f}%/yr  ({n.size} signal days)")

    OUT.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
