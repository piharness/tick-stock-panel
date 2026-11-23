"""Round 2: attack the round-1 result from four directions.

Round 1 said: the five-condition formula earns -99.7bp on the overnight leg
(t -37), and every added condition makes it worse. Before believing a number
that clean, try to break it:

  V1 limit-up flag audit  - is `consecutive_limit_ups` really a limit-up, on
                            the 10%/20%/30%/5% boards? Sample and check.
  V2 best-case exit       - sell at next day's HIGH instead of open. If the
                            ceiling is still negative the exit timing is not
                            the problem.
  V3 is it the formula or the bucket? - 2D grid of overnight active return over
                            (same-day change x turnover). Shows whether -100bp
                            is a local hole or the whole neighbourhood.
  V4 mirror               - the reverse trade (sell tail, buy next open) is the
                            same number with a sign flip; check whether the
                            negative leg is stable enough to be *used* as an
                            exit signal rather than an entry signal.
  V5 days-since-limit-up curve - monotone decay, or noise?
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close", "volume",
        "amount", "turnover_rate", "consecutive_limit_ups"]
OUT = Path("analysis/_diag_tail_overnight_r2_result.json")


def main() -> None:
    t0 = time.time()
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore").select(COLS).collect())
    cal = df.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])
    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
        pl.col("open").shift(-1).over("symbol").alias("open_n"),
        pl.col("high").shift(-1).over("symbol").alias("high_n"),
        pl.col("low").shift(-1).over("symbol").alias("low_n"),
        pl.col("close").shift(-1).over("symbol").alias("close_n"),
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
    df = df.with_columns([
        (pl.col("open_n") / pl.col("close") - 1.0).alias("r_on"),
        (pl.col("high_n") / pl.col("close") - 1.0).alias("r_on_best"),
        (pl.col("low_n") / pl.col("close") - 1.0).alias("r_on_worst"),
        (pl.col("close_n") / pl.col("open_n") - 1.0).alias("r_id"),
    ])
    df = df.filter((pl.col("rn_n") == pl.col("rn") + 1) & pl.col("open_n").is_not_null()
                   & (pl.col("open_n") > 0) & pl.col("r_on").is_finite())

    uni = df.group_by("date").agg(
        pl.col("r_on").mean().alias("uni_on"),
        pl.col("r_on_best").mean().alias("uni_best"),
    )
    df = df.join(uni, on="date", how="left")

    out: dict = {}

    # ---------------- V1 limit-up audit ----------------
    print("############ [V1] limit-up flag audit")
    lu = df.filter(pl.col("consecutive_limit_ups") >= 1)
    by_board = lu.with_columns(
        pl.when(pl.col("symbol").str.ends_with(".BJ")).then(pl.lit("BJ 30%"))
         .when(pl.col("symbol").str.starts_with("30") | pl.col("symbol").str.starts_with("688"))
             .then(pl.lit("GEM/STAR 20%"))
         .otherwise(pl.lit("main 10%")).alias("board")
    )
    audit = by_board.group_by("board").agg(
        pl.col("chg").median().alias("med_chg"),
        pl.col("chg").quantile(0.05).alias("p05_chg"),
        pl.col("chg").quantile(0.95).alias("p95_chg"),
        pl.len().alias("n"),
    ).sort("board")
    print(audit)
    out["V1_audit"] = audit.to_dicts()
    # post-2020 GEM specifically
    gem = df.filter(pl.col("symbol").str.starts_with("30") & (pl.col("date") >= pl.date(2020, 9, 1))
                    & (pl.col("consecutive_limit_ups") >= 1))
    print(f"  GEM post-2020-09 limit-up chg: median={gem['chg'].median():.4f} "
          f"n={gem.height:,}")
    out["V1_gem_post2020_median_chg"] = round(float(gem["chg"].median()), 4)

    # ---------------- V2 best-case exit ----------------
    print("\n############ [V2] exit-timing ceiling on the formula")
    base_ok = ((pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0)
               & pl.col("chg").is_finite() & pl.col("vr").is_finite() & (pl.col("vr") > 0)
               & pl.col("fmcap").is_finite() & pl.col("turnover_rate").is_finite())
    full = (base_ok & (pl.col("lim20") == 1) & (pl.col("vr") > 1)
            & pl.col("turnover_rate").is_between(5, 10) & pl.col("fmcap").is_between(50, 200)
            & pl.col("chg").is_between(0.03, 0.05))
    sub = df.filter(full)
    for col, nm in [("r_on", "sell at next OPEN (actual)"),
                    ("r_on_best", "sell at next HIGH (unreachable ceiling)"),
                    ("r_on_worst", "sell at next LOW (floor)")]:
        g = sub.group_by("date").agg(pl.col(col).mean().alias("m")).sort("date")["m"].to_numpy()
        out[f"V2_{col}"] = {"mean_bp": round(float(g.mean()) * 1e4, 1),
                            "t": round(float(g.mean()) / (g.std(ddof=1) / np.sqrt(g.size)), 2),
                            "win_rate_daily": round(float((g > 0).mean()), 3)}
        print(f"  {nm:<40} {g.mean()*1e4:+8.1f}bp  t={out[f'V2_{col}']['t']:+7.2f}  "
              f"win={out[f'V2_{col}']['win_rate_daily']}")
    # single-trade win rate
    tr = sub["r_on"].to_numpy()
    tr = tr[np.isfinite(tr)]
    print(f"  single-trade: n={tr.size:,} mean={tr.mean()*1e4:+.1f}bp "
          f"win={(tr>0).mean():.3f} median={np.median(tr)*1e4:+.1f}bp")
    out["V2_single_trade"] = {"n": int(tr.size), "mean_bp": round(float(tr.mean()) * 1e4, 1),
                              "win": round(float((tr > 0).mean()), 3),
                              "median_bp": round(float(np.median(tr)) * 1e4, 1)}

    # ---------------- V3 2D grid ----------------
    print("\n############ [V3] overnight active (vs same-day universe) grid: chg x turnover")
    ok = (pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0) \
         & pl.col("turnover_rate").is_finite() & (pl.col("turnover_rate") > 0) & pl.col("chg").is_finite()
    g = df.filter(ok).with_columns([
        pl.col("turnover_rate").qcut([0.1, 0.25, 0.5, 0.75, 0.9], labels=["1", "2", "3", "4", "5", "6"]).alias("tb"),
        ((pl.col("chg") * 100).floor() / 1).clip(-10, 10).alias("cb"),
    ])
    grid = g.group_by(["cb", "tb"]).agg(
        pl.col("r_on").mean().alias("m"), pl.len().alias("n")
    ).sort(["cb", "tb"])
    piv = {}
    for r in grid.to_dicts():
        piv.setdefault(r["cb"], {})[r["tb"]] = (round(r["m"] * 1e4, 0), r["n"])
    out["V3_grid"] = {str(k): v for k, v in piv.items()}
    print("  chg%bin |  t1(0-10p)  t2(10-25)  t3(25-50)  t4(50-75)  t5(75-90)  t6(90-100)   [bp]")
    for cb in sorted(piv):
        row = "".join(f"{piv[cb].get(t, ('  .',0))[0]:>10.0f}" for t in ["1", "2", "3", "4", "5", "6"])
        print(f"  {cb:>+6.0f} |{row}")

    # ---------------- V4 mirror ----------------
    print("\n############ [V4] the mirror trade")
    print("  entry=next open, exit=next close  ==  'sell the tail, buy back at next open'")
    for col, nm in [("r_id", "next intraday (the leg you GIVE UP selling at tail)")]:
        gg = sub.group_by("date").agg(pl.col(col).mean().alias("m")).sort("date")["m"].to_numpy()
        print(f"  {nm:<52} {gg.mean()*1e4:+7.1f}bp  t={gg.mean()/(gg.std(ddof=1)/np.sqrt(gg.size)):+.2f}")
        out["V4_next_intraday"] = {"mean_bp": round(float(gg.mean()) * 1e4, 1)}
    # combine: sell tail + buy open + sell next close  => -r_on + r_id
    comb = (-sub["r_on"] + sub["r_id"]).to_numpy()
    comb = comb[np.isfinite(comb)]
    print(f"  ['short' overnight then long next intraday] mean={comb.mean()*1e4:+.1f}bp "
          f"(single trade, before costs)")

    # ---------------- V5 days-since-limit-up ----------------
    print("\n############ [V5] overnight by days since last limit-up")
    lu2 = df.with_columns(
        pl.when(pl.col("consecutive_limit_ups") >= 1).then(pl.col("rn")).otherwise(None)
          .forward_fill().over("symbol").alias("last_lu")
    ).with_columns((pl.col("rn") - pl.col("last_lu")).alias("d_since"))
    curve = {}
    for d0 in range(0, 21):
        ss = lu2.filter(pl.col("d_since") == d0)
        if ss.height < 500:
            continue
        gd = ss.group_by("date").agg((pl.col("r_on") - pl.col("uni_on")).mean().alias("m"))
        v = gd["m"].drop_nulls().to_numpy()
        if v.size < 30:
            continue
        curve[d0] = {
            "active_bp": round(float(v.mean()) * 1e4, 1),
            "t": round(float(v.mean()) / (v.std(ddof=1) / np.sqrt(v.size)), 2),
            "n_dates": int(v.size), "n_names": int(ss.height),
            "raw_bp": round(float(ss["r_on"].mean()) * 1e4, 1),
        }
        print(f"  d={d0:>2}  raw={curve[d0]['raw_bp']:+7.1f}  active={curve[d0]['active_bp']:+7.1f}  "
              f"t={curve[d0]['t']:+6.2f}  n={curve[d0]['n_names']:,}")
    out["V5_curve"] = curve

    # ---------------- V6 what DOES work: positive cells ----------------
    print("\n############ [V6] scan: which (chg, turnover) cells have POSITIVE overnight?")
    pos = [(k, t, v) for k, row in piv.items() for t, v in row.items()]
    pos_hi = sorted([x for x in pos if x[2][1] >= 2000], key=lambda x: -x[2][0])[:12]
    for cb, tb, (bp, n) in pos_hi:
        print(f"  chg={cb:>+4.0f}%  turn_bucket={tb}  {bp:>+7.0f}bp  n={n:,}")
    out["V6_top_positive_cells"] = [{"chg_bin": a, "turn_bucket": b, "bp": c[0], "n": c[1]}
                                    for a, b, c in pos_hi]

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}  elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
