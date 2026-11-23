"""Round 5: audit the only positive cell found in the V3 grid.

V3 said chg +4~5% x turnover-bottom-decile earns +50~109bp overnight. A new
green light is more expensive than a red one, so: what is actually in that
cell? Candidates that would make it untradeable:
  - ST names whose 5% limit is not flagged by consecutive_limit_ups
  - one-word boards (open == high == low) that you cannot buy
  - names that were halted and resumed
  - turnover so low the print is not a market
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_tail_overnight_r5_result.json")


def main() -> None:
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore")
          .select(["symbol", "date", "open", "high", "low", "close", "raw_close", "volume",
                   "amount", "turnover_rate", "consecutive_limit_ups"]).collect())
    cal = df.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])
    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
        pl.col("open").shift(-1).over("symbol").alias("open_n"),
        pl.col("high").shift(-1).over("symbol").alias("high_n"),
        pl.col("low").shift(-1).over("symbol").alias("low_n"),
        pl.col("close").shift(-1).over("symbol").alias("close_n"),
        pl.col("close").shift(1).over("symbol").alias("close_p"),
        pl.col("open").shift(1).over("symbol").alias("open_p"),
        pl.col("volume").shift(1).over("symbol").alias("vol_p1"),
        pl.int_range(pl.len()).over("symbol").alias("row_n"),
    ])
    df = df.with_columns([
        (pl.col("close") / pl.col("close_p") - 1.0).alias("chg"),
        (pl.col("open") / pl.col("close_p") - 1.0).alias("gap_open"),
        pl.col("vol_p1").rolling_mean(5).over("symbol").alias("vol5"),
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0) / 1e8).otherwise(None).alias("fmcap"),
    ])
    df = df.with_columns([
        (pl.col("volume") / pl.col("vol5")).alias("vr"),
        (pl.col("open_n") / pl.col("close") - 1.0).alias("r_on"),
        ((pl.col("high") - pl.col("low")).abs() / pl.col("close_p")).alias("range_pct"),
    ])
    uni = df.group_by("date").agg(pl.col("r_on").mean().alias("uni_on"))
    df = df.join(uni, on="date", how="left")
    df = df.filter((pl.col("rn_n") == pl.col("rn") + 1) & pl.col("r_on").is_finite()
                   & (pl.col("row_n") >= 60))

    # rebuild the grid cells of interest: chg in [4,5) or [3,4), lowest turnover decile
    d = df.filter(pl.col("turnover_rate").is_finite() & (pl.col("turnover_rate") > 0)
                  & pl.col("chg").is_between(0.03, 0.05)
                  & (pl.col("consecutive_limit_ups") == 0))
    d = d.with_columns(
        (pl.col("turnover_rate").rank("ordinal").over("date") * 10 // pl.len().over("date")).alias("td"))
    low = d.filter(pl.col("td") == 0)

    out: dict = {"cells": {}}
    for lo, hi, nm in [(0.03, 0.04, "chg3-4 lowturn"), (0.04, 0.05, "chg4-5 lowturn")]:
        c = low.filter(pl.col("chg").is_between(lo, hi, closed="left"))
        if c.height < 100:
            continue
        one_word = c.filter(pl.col("high") == pl.col("low")).height
        st_like = c.filter((pl.col("chg") >= 0.0485) & (pl.col("chg") <= 0.0515)).height
        rec = {
            "n": int(c.height),
            "turnover_median_pct": round(float(c["turnover_rate"].median()), 2),
            "turnover_p90_pct": round(float(c["turnover_rate"].quantile(0.9)), 2),
            "amount_median_yi": round(float(c["amount"].median()) / 1e8, 3),
            "amount_p25_yi": round(float(c["amount"].quantile(0.25)) / 1e8, 3),
            "fmcap_median_yi": round(float(c["fmcap"].median()), 1),
            "one_word_board_share": round(one_word / c.height, 4),
            "range_median_pct": round(float(c["range_pct"].median()), 4),
            "looks_like_5pct_limit_share": round(st_like / c.height, 4),
            "gap_open_median_bp": round(float(c["gap_open"].median()) * 1e4, 1),
        }
        for col, label in [("r_on", "raw"), ]:
            g = c.group_by("date").agg((pl.col(col) - pl.col("uni_on")).mean().alias("a"),
                                       pl.col(col).mean().alias("m"), pl.len().alias("k")).sort("date")
            a = g["a"].to_numpy()
            m = g["m"].to_numpy()
            rec["active_bp"] = round(float(a.mean()) * 1e4, 1)
            rec["active_t"] = round(float(a.mean()) / (a.std(ddof=1) / np.sqrt(a.size)), 2)
            rec["raw_bp"] = round(float(m.mean()) * 1e4, 1)
            rec["n_dates"] = int(a.size)
        out["cells"][nm] = rec
        print(f"\n### {nm}")
        for k, v in rec.items():
            print(f"   {k:<30} {v}")

    # how many names per day in that cell, and their exchange mix
    for lo, hi, nm in [(0.04, 0.05, "chg4-5 lowturn")]:
        c = low.filter(pl.col("chg").is_between(lo, hi, closed="left"))
        per_day = c.group_by("date").agg(pl.len().alias("n"))["n"].to_numpy()
        print(f"\n  {nm}: names/day median={np.median(per_day):.0f}  p90={np.percentile(per_day,90):.0f}")
        out["cells"][nm]["names_per_day_median"] = float(np.median(per_day))
        boards = c.with_columns(
            pl.when(pl.col("symbol").str.starts_with("30")).then(pl.lit("GEM"))
             .when(pl.col("symbol").str.starts_with("68")).then(pl.lit("STAR"))
             .when(pl.col("symbol").str.ends_with(".BJ")).then(pl.lit("BJ"))
             .otherwise(pl.lit("main")).alias("b")).group_by("b").agg(pl.len()).sort("len", descending=True)
        print(f"  board mix: {boards.to_dicts()}")
        out["cells"][nm]["board_mix"] = boards.to_dicts()

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
