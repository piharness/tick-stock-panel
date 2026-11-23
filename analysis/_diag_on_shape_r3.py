"""Round 3: the shape's information is in the OTHER leg.

Round 1/2 established: the overnight leg (close->next open) is negative for every
tradable shape; the quietest decile only lifts it from -68bp to -3.8bp, still
below the 15bp round-trip cost.

A-share structure says intraday (open->close) is +19bp market-wide. So the
question a trader should actually ask is not "what shape makes the overnight
leg positive" but "where does the shape's information live, and is the exit
time on the wrong side of the clock".

Three legs, same shape buckets, absolute and net:
    r_on  = close[t]   -> open[t+1]    (what the user trades)
    r_id  = open[t+1]  -> close[t+1]   (the leg the user gives up by selling early)
    r_cc  = close[t]   -> close[t+1]   (hold one full day)
Cost 15bp round trip, and a 10bp low-commission sensitivity.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_on_shape_r3_result.json")


def main() -> None:
    t0 = time.time()
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore")
          .select(["symbol", "date", "open", "high", "low", "close", "volume", "amount",
                   "turnover_rate", "consecutive_limit_ups"]).collect())
    cal = df.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])
    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
        pl.col("open").shift(-1).over("symbol").alias("open_n"),
        pl.col("close").shift(-1).over("symbol").alias("close_n"),
        pl.col("close").shift(1).over("symbol").alias("c1"),
        pl.int_range(pl.len()).over("symbol").alias("row_n"),
        pl.col("close").shift(1).rolling_std(20).over("symbol").alias("px20"),
    ])
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).alias("hl"),
        ((pl.col("high") - pl.col("low")) / pl.col("c1")).alias("range_pct"),
        (pl.col("close") / pl.col("c1") - 1).alias("chg"),
        (pl.col("open_n") / pl.col("close") - 1).alias("r_on"),
        (pl.col("close_n") / pl.col("open_n") - 1).alias("r_id"),
        (pl.col("close_n") / pl.col("close") - 1).alias("r_cc"),
        (pl.col("px20") / pl.col("c1")).alias("vol20"),
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0) / 1e8).otherwise(None).alias("fmcap"),
    ])

    uni = df.filter(
        (pl.col("rn_n") == pl.col("rn") + 1) & pl.col("r_on").is_finite() & pl.col("r_id").is_finite()
        & (pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0)
        & (pl.col("hl") > 0) & (pl.col("amount") >= 5e7)
        & pl.col("chg").is_finite() & (pl.col("close") > 0)
        & ~((pl.col("open") == pl.col("high")) & (pl.col("high") == pl.col("low")))
        & pl.col("range_pct").is_finite() & pl.col("turnover_rate").is_finite()
        & pl.col("vol20").is_finite()
    )
    uni = uni.with_columns([
        (pl.col("range_pct").rank("ordinal").over("date") / pl.len().over("date")).alias("p_r"),
        (pl.col("turnover_rate").rank("ordinal").over("date") / pl.len().over("date")).alias("p_t"),
        (pl.col("vol20").rank("ordinal").over("date") / pl.len().over("date")).alias("p_v"),
    ]).with_columns((((pl.col("p_r") + pl.col("p_t") + pl.col("p_v")) / 3.0) * 10)
                    .floor().clip(0, 9).alias("qg"))
    print(f"[1] rows={uni.height:,} dates={uni['date'].n_unique()} t={time.time()-t0:.0f}s", flush=True)

    out: dict = {}
    print("\n############ leg decomposition by quietness decile (absolute bp)")
    print(f"{'qD':>4}{'n/d':>7}{'r_on':>8}{'r_id':>8}{'r_cc':>8}   "
          f"{'on net15':>9}{'cc net15':>9}{'cc net10':>9}")
    rows = {}
    for g in range(10):
        s = uni.filter(pl.col("qg") == g)
        agg = s.group_by("date").agg([
            pl.col("r_on").mean().alias("on"), pl.col("r_id").mean().alias("id"),
            pl.col("r_cc").mean().alias("cc"), pl.len().alias("n")]).sort("date")
        on = agg["on"].to_numpy(); idd = agg["id"].to_numpy(); cc = agg["cc"].to_numpy()
        n = agg["n"].to_numpy()
        rec = {
            "n_trades": int(n.sum()), "names_per_day": round(float(n.mean()), 1),
            "r_on_bp": round(float(on.mean()) * 1e4, 1),
            "r_id_bp": round(float(idd.mean()) * 1e4, 1),
            "r_cc_bp": round(float(cc.mean()) * 1e4, 1),
            "on_net15": round(float(on.mean()) * 1e4 - 15, 1),
            "cc_net15": round(float(cc.mean()) * 1e4 - 15, 1),
            "cc_net10": round(float(cc.mean()) * 1e4 - 10, 1),
            "t_cc": round(float(cc.mean()) / (cc.std(ddof=1) / np.sqrt(cc.size)), 2),
            "win_cc": round(float((cc > 0).mean()), 3),
        }
        ys = s.with_columns(pl.col("date").dt.year().alias("y"))
        yr = {int(r["y"]): round(r["m"] * 1e4, 1) for r in
              ys.group_by("y").agg(pl.col("r_cc").mean().alias("m")).sort("y").to_dicts()}
        rec["cc_yearly"] = yr
        rec["cc_pos_years"] = f"{sum(1 for v in yr.values() if v > 0)}/{len(yr)}"
        rows[g] = rec
        out[f"quiet_D{g+1}"] = rec
        print(f"{g+1:>4}{rec['names_per_day']:>7.0f}{rec['r_on_bp']:>8.1f}{rec['r_id_bp']:>8.1f}"
              f"{rec['r_cc_bp']:>8.1f}   {rec['on_net15']:>9.1f}{rec['cc_net15']:>9.1f}"
              f"{rec['cc_net10']:>9.1f}")

    print("\n############ the quietest 10%: where does the P&L come from")
    q1 = rows[0]
    print(f"  r_on {q1['r_on_bp']:+.1f}bp | r_id {q1['r_id_bp']:+.1f}bp | r_cc {q1['r_cc_bp']:+.1f}bp")
    print(f"  sell at open  net15 {q1['on_net15']:+.1f}bp")
    print(f"  sell at close net15 {q1['cc_net15']:+.1f}bp  (t={q1['t_cc']:+.2f}, "
          f"day win {q1['win_cc']:.1%}, {q1['cc_pos_years']} years positive)")

    print("\n############ is 'quiet' the same as 'large cap'? quiet D1 split by mcap")
    uni2 = uni.filter(pl.col("qg") == 0).with_columns(
        ((pl.col("fmcap").rank("ordinal").over("date") - 1) * 5 // pl.len().over("date")).alias("mg"))
    print(f"{'mcapQ':>6}{'n/d':>7}{'r_on':>8}{'r_id':>8}{'r_cc':>8}{'cc net15':>10}{'fmcap亿':>10}")
    for mg in range(5):
        s = uni2.filter(pl.col("mg") == mg)
        agg = s.group_by("date").agg([
            pl.col("r_on").mean().alias("on"), pl.col("r_id").mean().alias("id"),
            pl.col("r_cc").mean().alias("cc"), pl.len().alias("n")])
        on = agg["on"].to_numpy(); idd = agg["id"].to_numpy(); cc = agg["cc"].to_numpy()
        n = agg["n"].to_numpy()
        rec = {
            "names_per_day": round(float(n.mean()), 1),
            "r_on_bp": round(float(on.mean()) * 1e4, 1),
            "r_id_bp": round(float(idd.mean()) * 1e4, 1),
            "r_cc_bp": round(float(cc.mean()) * 1e4, 1),
            "cc_net15": round(float(cc.mean()) * 1e4 - 15, 1),
            "median_fmcap_yi": round(float(s["fmcap"].median()), 1),
        }
        out.setdefault("quietD1_by_mcap", {})[f"Q{mg+1}"] = rec
        print(f"{mg+1:>6}{rec['names_per_day']:>7.0f}{rec['r_on_bp']:>8.1f}{rec['r_id_bp']:>8.1f}"
              f"{rec['r_cc_bp']:>8.1f}{rec['cc_net15']:>10.1f}{rec['median_fmcap_yi']:>10.0f}")

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}  elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
