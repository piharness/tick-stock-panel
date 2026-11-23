"""Round 3: replace the daily-bar assumption with real 5-min timestamps.

Round 1/2 priced entry at close[t] and exit at open[t+1]. Real execution is
14:50-14:57 buy and 09:35-10:00 sell. Two things can move the answer:
  (a) the day's 3-5% may be manufactured in the last 30 minutes -> tail entry
      buys the top of the impulse;
  (b) the next-day open print is thin; 09:35-09:45 may be materially different.

P&L is built as
    close[t+1]/close[t] * (p_exit[t+1]/raw_close[t+1]) / (p_entry[t]/raw_close[t]) - 1
so the ex-div adjustment comes from the daily qfq series while the intraday
timestamp offsets come from 5-min bars. Nothing depends on 5-min prices being
adjusted. No pivot() is used (this polars build returns only the index column).
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import time
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_tail_overnight_r3_result.json")
ENTRY_T = ["14:30:00", "14:50:00", "14:55:00", "15:00:00"]
EXIT_T = ["09:35:00", "09:45:00", "10:00:00", "10:30:00"]


def main() -> None:
    t0 = time.time()
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore")
          .select(["symbol", "date", "open", "close", "raw_close", "volume", "amount",
                   "turnover_rate", "consecutive_limit_ups"]).collect())
    cal = df.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])
    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
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
    df = df.with_columns((pl.col("rn_n") == pl.col("rn") + 1).alias("adj"))

    base_ok = ((pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0)
               & pl.col("chg").is_finite() & pl.col("vr").is_finite() & (pl.col("vr") > 0)
               & pl.col("fmcap").is_finite() & pl.col("turnover_rate").is_finite())
    cond_tail = ((pl.col("vr") > 1) & pl.col("turnover_rate").is_between(5, 10)
                 & pl.col("fmcap").is_between(50, 200) & pl.col("chg").is_between(0.03, 0.05))
    full = base_ok & (pl.col("lim20") == 1) & cond_tail
    ctrl = base_ok & (pl.col("lim20") == 0) & cond_tail

    need = df.filter((full | ctrl) & pl.col("adj")).select(
        ["symbol", "date", "rn", "close", "raw_close", "chg", "lim20"])
    print(f"[1] signals={need.height:,} dates={need['date'].n_unique()} t={time.time()-t0:.0f}s", flush=True)
    keep_dates = set(need["date"].unique().to_list())

    print("[2] 5-min snapshots ...", flush=True)
    want_t = ENTRY_T + EXIT_T
    frames, used = [], 0
    for f in sorted(glob.glob(str(DATA / "kline_5min" / "date=*" / "part.parquet"))):
        d = Path(f).parent.name.split("=")[-1]
        y, m, dd = (int(x) for x in d.split("-"))
        if _dt.date(y, m, dd) not in keep_dates:
            continue
        try:
            sub = (pl.scan_parquet(f)
                   .select(["symbol", "datetime", "close"])
                   .with_columns(pl.col("datetime").dt.strftime("%H:%M:%S").alias("hm"))
                   .filter(pl.col("hm").is_in(want_t))
                   .collect())
        except Exception:
            continue
        if sub.height == 0:
            continue
        frames.append(sub.with_columns(pl.lit(_dt.date(y, m, dd)).alias("date")))
        used += 1
    snap = pl.concat(frames, how="vertical_relaxed")
    print(f"    partitions={used} snapshot_rows={snap.height:,} t={time.time()-t0:.0f}s", flush=True)

    # split per timestamp (no pivot)
    sig = need
    for t in want_t:
        col = f"p{t.replace(':', '')}"
        s = snap.filter(pl.col("hm") == t).select(["symbol", "date", "close"]).rename({"close": col})
        sig = sig.join(s, on=["symbol", "date"], how="left")

    # next day date + qfq close
    nxt = cal.select([(pl.col("rn") - 1).alias("rn"), pl.col("date").alias("date_next")])
    sig = sig.join(nxt, on="rn", how="left")
    # close_n must be the t+1 close: match signal rn=t against a frame carrying rn-1
    nb = df.select(["symbol", (pl.col("rn") - 1).alias("rn_m1"),
                    pl.col("close").alias("close_n"), pl.col("raw_close").alias("raw_close_n")])
    sig = sig.join(nb, left_on=["symbol", "rn"], right_on=["symbol", "rn_m1"], how="left")
    for t in EXIT_T:
        col = f"p{t.replace(':', '')}"
        s = snap.filter(pl.col("hm") == t).select(["symbol", "date", "close"]).rename(
            {"date": "date_next", "close": col + "_n"})
        sig = sig.join(s, on=["symbol", "date_next"], how="left")

    print(f"[3] assembled={sig.height:,} t={time.time()-t0:.0f}s", flush=True)
    out: dict = {"meta": {"signals": int(sig.height), "partitions": used}}

    print("\n############ last-30-min contribution to the day's 3-5% move")
    for grp, name in [((pl.col("lim20") == 1), "formula"), ((pl.col("lim20") == 0), "control")]:
        s = sig.filter(grp)
        if s.height < 50:
            continue
        tail = (s["close"] / s["p143000"] - 1.0).to_numpy()
        tail2 = (s["close"] / s["p145500"] - 1.0).to_numpy()
        day = s["chg"].to_numpy()
        ok = np.isfinite(tail) & np.isfinite(day) & (np.abs(day) > 1e-9)
        t1 = tail[np.isfinite(tail)]
        t2 = tail2[np.isfinite(tail2)]
        share = tail[ok] / day[ok]
        rec = {
            "n": int(t1.size),
            "tail_1430_to_close_bp": round(float(t1.mean()) * 1e4, 1),
            "tail_1455_to_close_bp": round(float(t2.mean()) * 1e4, 1) if t2.size else None,
            "share_median": round(float(np.median(share)), 3),
            "share_mean": round(float(share.mean()), 3),
            "share_gt_50pct": round(float((share > 0.5).mean()), 3),
            "share_negative": round(float((share < 0).mean()), 3),
        }
        out[f"tail30_{name}"] = rec
        print(f"  {name:<9} n={rec['n']:>5,}  14:30->close {rec['tail_1430_to_close_bp']:+7.1f}bp  "
              f"14:55->close {rec['tail_1455_to_close_bp']}bp  "
              f"median share={rec['share_median']:.1%}  share>50%: {rec['share_gt_50pct']:.1%}  "
              f"tail negative: {rec['share_negative']:.1%}")

    print("\n############ P&L matrix: entry x exit (bp, before cost)")
    def leg(e, x):
        return (pl.col("close_n") / pl.col("close") *
                (pl.col(f"p{x.replace(':', '')}_n") / pl.col("raw_close_n")) /
                (pl.col(f"p{e.replace(':', '')}") / pl.col("raw_close")) - 1.0)

    mats = {}
    for grp, name in [((pl.col("lim20") == 1), "formula"), ((pl.col("lim20") == 0), "control")]:
        s = sig.filter(grp)
        rows = {}
        for e in ENTRY_T:
            row = {}
            for x in EXIT_T:
                v = s.select(leg(e, x).alias("r"))["r"].drop_nulls().to_numpy()
                v = v[np.isfinite(v)]
                row[x] = round(float(v.mean()) * 1e4, 1) if v.size >= 50 else None
            rows[e] = row
        mats[name] = rows
        print(f"\n  [{name}] bp              " + "".join(f"{x[:5]:>10}" for x in EXIT_T))
        for e in ENTRY_T:
            print(f"    entry {e[:5]}         " + "".join(
                f"{(rows[e][x] if rows[e][x] is not None else float('nan')):>10.0f}" for x in EXIT_T))
    out["pnl_matrix"] = mats

    f = mats["formula"]
    net = lambda v: round((v or 0) - 15, 1)
    out["net_1455_0935"] = net(f["14:55:00"]["09:35:00"])
    out["net_1455_0945"] = net(f["14:55:00"]["09:45:00"])
    out["net_1455_1000"] = net(f["14:55:00"]["10:00:00"])
    out["net_1500_0935"] = net(f["15:00:00"]["09:35:00"])
    print(f"\n  net (15bp cost): 14:55->09:35 {out['net_1455_0935']:+.1f}bp | "
          f"14:55->09:45 {out['net_1455_0945']:+.1f}bp | 14:55->10:00 {out['net_1455_1000']:+.1f}bp")

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}  elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
