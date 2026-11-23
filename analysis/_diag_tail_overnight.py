"""Does the retail "tail entry -> next-open exit" formula carry any edge?

Formula under test (all filters observable at t's close):
  C1 limit-up within last 20 trading rows
  C2 volume ratio (today vs prior 5-day mean) > 1
  C3 turnover 5%-10%
  C4 free-float mcap 50-200e8  (fmcap = amount / (turnover_rate/100))
  C5 today's close-to-close change 3%-5%
  entry = close[t], exit = open[t+1]   => this is the OVERNIGHT leg only.

Design notes / traps avoided here:
  * rn (calendar rank) must differ by exactly 1 between t and t+1. shift() counts
    rows, not days: a name halted from t to t+5 would otherwise book a fake gap.
  * close is qfq-adjusted, raw_close is not; returns use `close` so ex-div gaps
    do not enter the P&L.
  * t statistics are computed on the DATE series (one obs per day), because all
    names on one day share a market-wide overnight shock.
  * edge is reported as ACTIVE vs the same-day universe, and vs a
    turnover-decile x mcap-decile matched benchmark, otherwise a negative
    overnight premium just makes everything look bad.
  * cost sensitivity: round trip 10/15/20 bp.

ASCII only (GBK console). Writes JSON next to the script.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
COLS = [
    "symbol", "date", "open", "high", "low", "close", "raw_close",
    "volume", "amount", "turnover_rate",
    "consecutive_limit_ups", "consecutive_limit_downs",
]
OUT = Path("analysis/_diag_tail_overnight_result.json")


def pct(x: np.ndarray, q: float) -> float:
    x = x[np.isfinite(x)]
    return float(np.percentile(x, q)) if x.size else float("nan")


def day_stat(sub: pl.DataFrame, ret_col: str, base_col: str | None = None) -> dict:
    """Aggregate to one observation per date, then t-test the daily means."""
    if sub.height == 0:
        return {"n": 0}
    g = sub.group_by("date").agg(
        pl.col(ret_col).mean().alias("m"),
        (pl.col(ret_col) - pl.col(base_col)).mean().alias("a") if base_col else pl.len().alias("k"),
        pl.len().alias("n_names"),
    ).sort("date")
    m = g["m"].to_numpy()
    n = g["n_names"].to_numpy()
    out = {
        "n_dates": int(g.height),
        "n_trades": int(n.sum()),
        "names_per_day": round(float(n.mean()), 1),
        "mean_bp": round(float(m.mean()) * 1e4, 1),
        "t": round(float(m.mean()) / (m.std(ddof=1) / np.sqrt(m.size)), 2) if m.size > 2 and m.std(ddof=1) > 0 else None,
        "median_bp": round(float(np.median(m)) * 1e4, 1),
        "win_rate": round(float((m > 0).mean()), 3),
        "p05_bp": round(pct(m, 5) * 1e4, 1),
        "p95_bp": round(pct(m, 95) * 1e4, 1),
    }
    if base_col:
        a = g["a"].to_numpy()
        out["active_bp"] = round(float(a.mean()) * 1e4, 1)
        out["active_t"] = round(float(a.mean()) / (a.std(ddof=1) / np.sqrt(a.size)), 2) if a.size > 2 and a.std(ddof=1) > 0 else None
    ys = sub.with_columns(pl.col("date").dt.year().alias("yr"))
    yearly = {}
    for r in ys.group_by("yr").agg(pl.col(ret_col).mean().alias("m"), pl.len().alias("n")).sort("yr").to_dicts():
        if r["n"] >= 20:
            yearly[int(r["yr"])] = [round(r["m"] * 1e4, 1), int(r["n"])]
    out["yearly_bp_n"] = yearly
    return out


def main() -> None:
    t0 = time.time()
    print("[1] load partitions ...", flush=True)
    df = (
        pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                        extra_columns="ignore")
        .select(COLS)
        .collect()
    )
    print(f"    rows={df.height:,}  symbols={df['symbol'].n_unique():,}  "
          f"dates={df['date'].n_unique():,}  t={time.time()-t0:.0f}s", flush=True)

    cal = df.select("date").unique().sort("date").with_columns(
        pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])

    print("[2] derive legs (calendar-adjacent only) ...", flush=True)
    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
        pl.col("open").shift(-1).over("symbol").alias("open_n"),
        pl.col("close").shift(-1).over("symbol").alias("close_n"),
        pl.col("close").shift(1).over("symbol").alias("close_p"),
        pl.col("volume").shift(1).over("symbol").alias("vol_p1"),
        pl.col("consecutive_limit_ups").shift(1).over("symbol").alias("clu_p1"),
        pl.int_range(pl.len()).over("symbol").alias("row_n"),
    ])
    df = df.with_columns([
        (pl.col("close") / pl.col("close_p") - 1.0).alias("chg"),
        pl.col("vol_p1").rolling_mean(5).over("symbol").alias("vol5"),
        (pl.col("consecutive_limit_ups") >= 1).cast(pl.Int8)
            .rolling_max(20).over("symbol").alias("lim20"),
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0) / 1e8)
          .otherwise(None).alias("fmcap"),
    ])
    df = df.with_columns((pl.col("volume") / pl.col("vol5")).alias("vr"))

    # legs
    df = df.with_columns([
        (pl.col("open_n") / pl.col("close") - 1.0).alias("r_on"),      # close[t] -> open[t+1]
        (pl.col("close_n") / pl.col("open_n") - 1.0).alias("r_id"),    # open[t+1] -> close[t+1]
        (pl.col("close_n") / pl.col("close") - 1.0).alias("r_cc"),     # close[t] -> close[t+1]
        (pl.col("close") / pl.col("open") - 1.0).alias("r_oc"),        # open[t] -> close[t]
    ])

    # tradable on the exit side: next calendar day exists and has an open
    ok_next = (pl.col("rn_n") == pl.col("rn") + 1) & pl.col("open_n").is_not_null() & (pl.col("open_n") > 0)
    df = df.with_columns(ok_next.alias("ok_next"))
    df = df.filter(pl.col("ok_next") & pl.col("r_on").is_finite() & (pl.col("close") > 0))

    # ---------- universe baseline (same day, all names) ----------
    print("[3] universe baseline ...", flush=True)
    uni = df.group_by("date").agg(
        pl.col("r_on").mean().alias("uni_on"),
        pl.col("r_id").mean().alias("uni_id"),
        pl.col("r_cc").mean().alias("uni_cc"),
        pl.col("r_oc").mean().alias("uni_oc"),
        pl.len().alias("uni_n"),
    ).sort("date")

    # turnover-decile x mcap-decile matched benchmark on each date
    print("[4] matched benchmark (turnover decile x mcap decile) ...", flush=True)
    d = df.filter(
        pl.col("turnover_rate").is_finite() & pl.col("fmcap").is_finite() &
        (pl.col("fmcap") > 0) & (pl.col("turnover_rate") > 0)
    )
    d = d.with_columns([
        (pl.col("turnover_rate").rank("ordinal").over("date") * 10 // pl.len().over("date")).alias("td"),
        (pl.col("fmcap").rank("ordinal").over("date") * 10 // pl.len().over("date")).alias("md"),
    ])
    bench = d.group_by(["date", "td", "md"]).agg(
        pl.col("r_on").mean().alias("b_on"), pl.len().alias("b_n")
    )
    df = df.join(d.select(["symbol", "date", "td", "md"]), on=["symbol", "date"], how="left")
    df = df.join(bench, on=["date", "td", "md"], how="left")
    df = df.join(uni, on="date", how="left")
    df = df.with_columns([
        (pl.col("r_on") - pl.col("b_on")).alias("a_m"),
        (pl.col("r_on") - pl.col("uni_on")).alias("a_u"),
    ])

    # ---------- filters ----------
    base_ok = (
        (pl.col("row_n") >= 60)              # drop the first 60 rows (IPO distortions)
        & (pl.col("consecutive_limit_ups") == 0)   # cannot buy a limit-up close
        & pl.col("chg").is_finite()
        & pl.col("vr").is_finite() & (pl.col("vr") > 0)
        & pl.col("fmcap").is_finite()
        & pl.col("turnover_rate").is_finite()
    )
    c1 = pl.col("lim20") == 1
    c2 = pl.col("vr") > 1.0
    c3 = pl.col("turnover_rate").is_between(5.0, 10.0)
    c4 = pl.col("fmcap").is_between(50.0, 200.0)
    c5 = pl.col("chg").is_between(0.03, 0.05)

    out: dict = {
        "meta": {
            "rows": int(df.height),
            "symbols": int(df["symbol"].n_unique()),
            "dates": int(df["date"].n_unique()),
            "date_min": str(df["date"].min()),
            "date_max": str(df["date"].max()),
        },
        "legs_universe": {},
        "single_conditions": {},
        "cumulative": {},
        "final": {},
    }

    print("\n############ [B] market-wide leg decomposition (all names, all days)")
    legs = df.group_by("date").agg([
        pl.col("r_on").mean(), pl.col("r_id").mean(), pl.col("r_cc").mean(), pl.col("r_oc").mean()
    ]).sort("date")
    for c, nm in [("r_on", "close->next_open (overnight)"), ("r_id", "next_open->next_close (intraday)"),
                  ("r_cc", "close->next_close (buy&hold 1d)"), ("r_oc", "open->close same day")]:
        x = legs[c].to_numpy()
        x = x[np.isfinite(x)]
        out["legs_universe"][nm] = {
            "mean_bp": round(float(x.mean()) * 1e4, 2),
            "t": round(float(x.mean()) / (x.std(ddof=1) / np.sqrt(x.size)), 2),
            "n_dates": int(x.size),
        }
        print(f"  {nm:<34} {x.mean()*1e4:+8.2f}bp  t={out['legs_universe'][nm]['t']:+6.2f}")

    print("\n############ [C] each condition alone (entry=close, exit=next open)")
    singles = {"C1 limit-up<=20d": c1, "C2 vr>1": c2, "C3 turn 5-10%": c3,
               "C4 fmcap 50-200": c4, "C5 chg 3-5%": c5}
    for nm, cond in singles.items():
        sub = df.filter(base_ok & cond)
        s = day_stat(sub, "r_on", "b_on")
        s["raw_bp"] = s.get("mean_bp")
        out["single_conditions"][nm] = s
        print(f"  {nm:<18} n={s.get('n_trades',0):>8,}  names/day={s.get('names_per_day')}  "
              f"on={s.get('mean_bp'):>+7.1f}bp  active(matched)={s.get('active_bp'):>+7.1f}  "
              f"t_a={s.get('active_t')}")

    print("\n############ [D] cumulative build-up")
    acc = pl.lit(True)
    order = [("C5 chg3-5", c5), ("C1 lim20", c1), ("C3 turn5-10", c3), ("C4 cap50-200", c4), ("C2 vr>1", c2)]
    for nm, cond in order:
        acc = acc & cond
        sub = df.filter(base_ok & acc)
        s = day_stat(sub, "r_on", "b_on")
        out["cumulative"][nm] = s
        print(f"  +{nm:<14} n={s.get('n_trades',0):>8,}  names/day={s.get('names_per_day')}  "
              f"on={s.get('mean_bp'):>+7.1f}bp  active={s.get('active_bp'):>+7.1f}  t_a={s.get('active_t')}")

    print("\n############ [E] THE FORMULA (all five)")
    full = base_ok & c1 & c2 & c3 & c4 & c5
    sub = df.filter(full)
    s = day_stat(sub, "r_on", "b_on")
    su = day_stat(sub, "r_on", "uni_on")
    s["vs_universe"] = su
    for c, nm in [("r_id", "next intraday"), ("r_cc", "hold to next close"), ("r_oc", "same-day open->close")]:
        s[f"leg_{nm}"] = day_stat(sub, c, "uni_" + {"r_id": "id", "r_cc": "cc", "r_oc": "oc"}[c])
    out["final"] = s

    print(f"  trades={s['n_trades']:,}  dates={s['n_dates']}  names/day={s['names_per_day']}")
    print(f"  overnight      {s['mean_bp']:+7.1f}bp  t={s['t']}  win={s['win_rate']}  "
          f"p05={s['p05_bp']}  p95={s['p95_bp']}")
    print(f"  active vs matched  {s['active_bp']:+7.1f}bp  t={s['active_t']}")
    print(f"  active vs universe {su['active_bp']:+7.1f}bp  t={su['active_t']}")
    for nm in ["next intraday", "hold to next close", "same-day open->close"]:
        L = s[f"leg_{nm}"]
        print(f"  {nm:<22} raw={L.get('mean_bp')}  active={L.get('active_bp')}  t_a={L.get('active_t')}")

    print("\n  yearly (overnight, bp | n_trades):")
    for y, (bp, n) in sorted(s["yearly_bp_n"].items()):
        print(f"    {y}  {bp:+7.1f}  {n:>7,}")

    print("\n############ [F] cost sensitivity on the overnight leg")
    m = s["mean_bp"]
    out["cost"] = {f"{c}bp": round(m - c, 1) for c in (0, 10, 15, 20, 30)}
    for c, v in out["cost"].items():
        print(f"  cost {c:<4} -> net {v:+7.1f}bp  (annualised on 244 days: {v/1e4*244*100:+.1f}%)")

    print("\n############ [G] robustness: sub-slices of the formula")
    slices = {}
    # by turnover bucket
    for lo, hi, nm in [(5, 7, "turn 5-7"), (7, 10.001, "turn 7-10")]:
        ss = df.filter(full & pl.col("turnover_rate").is_between(lo, hi))
        slices[nm] = day_stat(ss, "r_on", "b_on")
    # by mcap
    for lo, hi, nm in [(50, 100, "cap 50-100"), (100, 200.001, "cap 100-200")]:
        ss = df.filter(full & pl.col("fmcap").is_between(lo, hi))
        slices[nm] = day_stat(ss, "r_on", "b_on")
    # by days since last limit-up
    lu = df.with_columns(
        pl.when(pl.col("consecutive_limit_ups") >= 1).then(pl.col("rn"))
          .otherwise(None).forward_fill().over("symbol").alias("last_lu")
    ).with_columns((pl.col("rn") - pl.col("last_lu")).alias("d_since_lu"))
    for lo, hi, nm in [(0, 2, "lu 0-1d ago"), (2, 6, "lu 2-5d ago"), (6, 21, "lu 6-20d ago")]:
        ss = lu.filter(full & pl.col("d_since_lu").is_between(lo, hi))
        slices[nm] = day_stat(ss, "r_on", "b_on")
    # by chg
    for lo, hi, nm in [(0.03, 0.04, "chg 3-4"), (0.04, 0.05, "chg 4-5")]:
        ss = df.filter(full & pl.col("chg").is_between(lo, hi))
        slices[nm] = day_stat(ss, "r_on", "b_on")
    # by vr
    for lo, hi, nm in [(1, 1.5, "vr 1-1.5"), (1.5, 2.5, "vr 1.5-2.5"), (2.5, 1e9, "vr >2.5")]:
        ss = df.filter(full & pl.col("vr").is_between(lo, hi))
        slices[nm] = day_stat(ss, "r_on", "b_on")
    out["slices"] = slices
    for nm, v in slices.items():
        print(f"  {nm:<14} n={v.get('n_trades',0):>7,}  on={v.get('mean_bp')}  active={v.get('active_bp')}  t_a={v.get('active_t')}")

    print("\n############ [H] half-split stability (chronological)")
    dates = df["date"].unique().sort()
    mid = dates[int(dates.len() * 0.5)]
    for nm, cond in [("first_half", df["date"] <= mid), ("second_half", df["date"] > mid)]:
        ss = df.filter(full & cond)
        v = day_stat(ss, "r_on", "b_on")
        out.setdefault("halves", {})[nm] = v
        print(f"  {nm:<12} n={v.get('n_trades',0):>7,}  on={v.get('mean_bp')}  active={v.get('active_bp')}  t_a={v.get('active_t')}")

    print("\n############ [I] exit-side tradability of selected names")
    ss = df.filter(full)
    stop_open = ss.filter((pl.col("r_on") <= -0.092)).height  # cannot sell into a limit-down open
    hit_open = ss.filter((pl.col("r_on") >= 0.092)).height
    out["exit_side"] = {
        "n": int(ss.height),
        "open_down_9pct": int(stop_open),
        "open_down_9pct_share": round(stop_open / max(ss.height, 1), 4),
        "open_up_9pct": int(hit_open),
        "open_up_9pct_share": round(hit_open / max(ss.height, 1), 4),
    }
    print(f"  exit open <= -9.2%: {stop_open:,} ({stop_open/max(ss.height,1):.2%})   "
          f">= +9.2%: {hit_open:,} ({hit_open/max(ss.height,1):.2%})")

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}  elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
