"""Round 4, part 3: is the "zero forward excess" a bull-market artifact?

Round 4's boundary said the answer needs "a second regime period" and that the
local machine has none. The calendar part of that is true: data/kline_daily and
data/kline_daily_enriched both cover exactly 367 sessions, 2025-05-07..2026-11-06,
and there is no other daily panel anywhere under data/. But "one regime" is a
stronger claim than "one calendar span", and that one was never checked.

data/regime_history covers 365 of those sessions with five states:

    strong 108 | weak 92 | lean_strong 79 | lean_weak 47 | range 39

so 38% of the sample is weak or lean_weak. That is within-sample regime
variation, and it is enough to ask whether round 4's headline is uniform:

    (a) in-window excess  mom20(picks) - mom20(matched)   +88.64% pooled
    (b) forward day-1     close[d] -> close[d+1]           +25.0bp pooled
    (c) forward days 2-10 mean daily c2c
    (d) forward 20d       close[d] -> close[d+20]          +26.3bp pooled

Regime is taken at d-1 (the last session whose state is known when the signal
is formed), bucketed the way round 1 did: strong|lean_strong -> strong,
weak|lean_weak -> weak, else range.

Pre-registered rules (written before the run):

    R5-A  If (b) is positive and |t| < 2 in EVERY bucket, round 4's "no legal
          one-day edge" survives the regime split and the boundary is narrowed
          from "needs a second regime" to "needs a second calendar span".
    R5-B  If (a) is positive with |t| >= 2 in the weak bucket as well, then the
          +88.64% in-window excess is not a bull-market artifact and round 4's
          boundary claim is withdrawn.
    R5-C  (d) is reported per bucket but is NOT evidence: n_eff = n/20 falls
          below 10 in every bucket, which by round 3's own rule R3-C makes the
          row unjudgeable. It is printed to show why it cannot be read.

ASCII output only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import _diag_gaprev as dg  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261109
BUCKETS = ("strong", "range", "weak")


def regime_map() -> dict[str, str]:
    """date -> bucket of the PREVIOUS session's state."""
    r = pl.read_parquet(ap.DATA / "regime_history" / "part.parquet").select(
        pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"), pl.col("state")
    )
    r = r.with_columns(
        pl.when(pl.col("state").is_in(["strong", "lean_strong"]))
        .then(pl.lit("strong"))
        .when(pl.col("state").is_in(["weak", "lean_weak"]))
        .then(pl.lit("weak"))
        .otherwise(pl.lit("range"))
        .alias("bucket")
    ).sort("date")
    r = r.with_columns(pl.col("bucket").shift(1).alias("b_t1"))
    return {row["date"]: row["b_t1"] for row in r.iter_rows(named=True)
            if row["b_t1"] is not None}


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    pool_base = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)
    to20 = ac.grid_of(df, "to20", dates, symbols)
    mom_grid = ac.grid_of(df, "mom20", dates, symbols)
    G = dg.session_grids(mats)
    rm = regime_map()
    print(f"panel dates={T} symbols={S}  regime days mapped={len(rm)}")
    cnt = {b: sum(1 for v in rm.values() if v == b) for b in BUCKETS}
    print(f"regime composition of mapped days: {cnt}")

    out: dict = {"seed": SEED, "regime_days": cnt, "arms": {}}

    for arm_label, ascending in [("mom_top20", False), ("mom_bottom20", True)]:
        picks_rows = ap.pick_daily(df, "mom20", ascending).select("date", "symbols").to_dicts()
        acc: dict[str, dict[str, list]] = {b: {"win": [], "s0": [], "s1": [], "s3": [],
                                               "f2_10": [], "s8": []} for b in BUCKETS}
        skipped = 0
        for r in picks_rows:
            i0 = date_index[str(r["date"])]
            dkey = str(r["date"])[:10]
            b = rm.get(dkey)
            if b is None:
                skipped += 1
                continue
            if i0 + 20 >= T:
                skipped += 1
                continue
            st = dg.cohort_setup(i0, np.array([s_map[x] for x in r["symbols"]], dtype=np.int64),
                                 pool_base, to20, S)
            if st is None:
                skipped += 1
                continue
            t0_ = dg.active_triplet(G["intra"][i0 + 1], st)
            t1_ = dg.active_triplet(G["c2c1"][i0 + 1], st)
            t3_ = dg.active_triplet(G["gap"][i0 + 1], st)
            t8_ = dg.active_triplet(G["c2c1"][i0 + 20], st)
            if None in (t0_, t1_, t3_, t8_):
                skipped += 1
                continue
            f = [dg.active_triplet(G["c2c1"][i0 + k], st) for k in range(2, 11)]
            f = [x for x in f if x is not None]
            if len(f) < 6:
                skipped += 1
                continue
            mv = mom_grid[i0][st["pool"]]
            dmean = np.array([
                float(np.nanmean(mv[st["dec"] == k])) if (st["dec"] == k).any() else np.nan
                for k in range(dg.N_DECILE)
            ])
            w_pick = float(np.nanmean(mom_grid[i0][st["sel"]]))
            w_match = float(np.nansum(st["w"] * dmean))
            acc[b]["win"].append(w_pick - w_match)
            acc[b]["s0"].append(t0_[0] - t0_[2])
            acc[b]["s1"].append(t1_[0] - t1_[2])
            acc[b]["s3"].append(t3_[0] - t3_[2])
            acc[b]["f2_10"].append(float(np.mean([x[0] - x[2] for x in f])))
            acc[b]["s8"].append(t8_[0] - t8_[2])

        print(f"\n############ {arm_label}  (cohorts skipped={skipped})")
        A: dict = {}
        for b in BUCKETS:
            n = len(acc[b]["win"])
            if n < 20:
                print(f"  {b:6s} n={n} too few")
                continue
            win = np.array(acc[b]["win"])
            s0 = dg.mean_t(np.array(acc[b]["s0"]))
            s1 = dg.mean_t(np.array(acc[b]["s1"]))
            s3 = dg.mean_t(np.array(acc[b]["s3"]))
            f2 = dg.mean_t(np.array(acc[b]["f2_10"]))
            s8 = dg.mean_t(np.array(acc[b]["s8"]), held=20)
            wse = float(win.std(ddof=0) / np.sqrt(win.size))
            A[b] = {
                "n": n,
                "in_window_excess_pct": round(float(win.mean()) * 100, 2),
                "in_window_excess_t": round(float(win.mean() / wse), 2),
                "s0_day1_intraday": s0, "s1_day1_c2c": s1, "s3_day1_gap": s3,
                "fwd_days2_10_daily": f2, "s8_fwd20": s8,
            }
            print(f"  {b:6s} n={n:3d} in-window excess {A[b]['in_window_excess_pct']:+7.2f}% "
                  f"(t {A[b]['in_window_excess_t']:+5.2f}) | day1 S1 c2c "
                  f"{s1['mean_bp']:+7.1f} (t {s1['t']:+5.2f}) | day1 S0 intraday "
                  f"{s0['mean_bp']:+7.1f} (t {s0['t']:+5.2f}) | days2-10 daily "
                  f"{f2['mean_bp']:+6.1f} (t {f2['t']:+5.2f})")
            print(f"  {'':6s} day1 gap S3 {s3['mean_bp']:+7.1f} (t {s3['t']:+5.2f}) | "
                  f"fwd20 S8 {s8['mean_bp']:+7.1f} (t {s8['t']:+5.2f}) "
                  f"n_eff={s8['n'] / 20:.1f}  <-- R3-C: n_eff<10, NOT evidence")
        out["arms"][arm_label] = A

    p = Path("analysis/_diag_regime_result.json")
    p.write_text(json.dumps(dg.clean_json(out), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
