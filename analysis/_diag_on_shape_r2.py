"""Round 2: can 'quietness' be pushed to a positive, cost-covered overnight leg?

Round 1 (decile scan) found the overnight leg is a monotone function of how
QUIET the bar was: range / turnover / 20d vol / days-since-limit-up all point
the same way, t in the 50-80 range, both halves same sign. But the best decile
was still -1.8bp net of the universe mean, i.e. below cost.

This round asks the only question that matters:
  Q1 can a composite 'quiet' score reach a positive NET overnight return?
  Q2 is it just market cap in disguise? (control: mcap decile x composite)
  Q3 does it survive per-year, and what is the capacity?
  Q4 what is the actual 2D shape (range x turnover) once you require positive?

Cost is 15bp round trip. Net is reported per trade and annualised on the
number of signal days actually available.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_on_shape_r2_result.json")
N_G = 10


def q(col: str) -> pl.Expr:
    return (pl.col(col).rank("ordinal").over("date") - 1) * N_G // pl.len().over("date")


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
        pl.col("close").shift(1).over("symbol").alias("c1"),
        pl.int_range(pl.len()).over("symbol").alias("row_n"),
        pl.col("turnover_rate").shift(1).rolling_mean(20).over("symbol").alias("t20"),
        pl.col("close").shift(1).rolling_std(20).over("symbol").alias("px20"),
        pl.col("high").shift(1).rolling_max(20).over("symbol").alias("h20"),
        pl.col("low").shift(1).rolling_min(20).over("symbol").alias("l20"),
    ])
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).alias("hl"),
        ((pl.col("high") - pl.col("low")) / pl.col("c1")).alias("range_pct"),
        (pl.col("close") / pl.col("c1") - 1).alias("chg"),
        (pl.col("open_n") / pl.col("close") - 1).alias("r_on"),
        (pl.col("px20") / pl.col("c1")).alias("vol20"),
        (pl.col("l20") / pl.col("close") - 1).alias("dist_lo20"),
        pl.col("amount").log().alias("log_amt"),
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0) / 1e8).otherwise(None).alias("fmcap"),
    ])
    df = df.with_columns([
        (pl.col("hl") / pl.col("hl").shift(1).rolling_mean(20).over("symbol")).alias("amp_ratio"),
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("range_close"),
    ])
    df = df.with_columns(
        pl.when(pl.col("consecutive_limit_ups") >= 1).then(pl.col("rn")).otherwise(None)
          .forward_fill().over("symbol").alias("last_lu")
    ).with_columns((pl.col("rn") - pl.col("last_lu")).fill_null(999).alias("d_since_lu"))

    uni = df.filter(
        (pl.col("rn_n") == pl.col("rn") + 1) & pl.col("r_on").is_finite()
        & (pl.col("row_n") >= 60) & (pl.col("consecutive_limit_ups") == 0)
        & (pl.col("hl") > 0) & (pl.col("amount") >= 5e7)
        & pl.col("chg").is_finite() & (pl.col("close") > 0)
        & ~((pl.col("open") == pl.col("high")) & (pl.col("high") == pl.col("low")))
        & pl.col("range_pct").is_finite() & pl.col("turnover_rate").is_finite()
        & pl.col("vol20").is_finite() & pl.col("fmcap").is_finite()
    )
    mu = uni.group_by("date").agg(pl.col("r_on").mean().alias("mu"))
    uni = uni.join(mu, on="date").with_columns((pl.col("r_on") - pl.col("mu")).alias("a"))
    print(f"[1] rows={uni.height:,} dates={uni['date'].n_unique()} "
          f"mean r_on={uni['r_on'].mean()*1e4:+.1f}bp  t={time.time()-t0:.0f}s", flush=True)

    # composite quietness: mean percentile of (range, turnover, vol20)
    uni = uni.with_columns([
        (pl.col("range_pct").rank("ordinal").over("date") / pl.len().over("date")).alias("p_r"),
        (pl.col("turnover_rate").rank("ordinal").over("date") / pl.len().over("date")).alias("p_t"),
        (pl.col("vol20").rank("ordinal").over("date") / pl.len().over("date")).alias("p_v"),
    ]).with_columns(
        ((pl.col("p_r") + pl.col("p_t") + pl.col("p_v")) / 3.0).alias("quiet")
    ).with_columns(q("quiet").alias("qg"))

    out: dict = {}
    COST = 0.0015

    def rep(sub: pl.DataFrame, label: str, cost_bp: float = 0.0) -> dict:
        g = sub.group_by("date").agg(
            pl.col("r_on").mean().alias("m"), pl.col("a").mean().alias("a"), pl.len().alias("n")
        ).sort("date")
        m = g["m"].to_numpy(); a = g["a"].to_numpy(); n = g["n"].to_numpy()
        sd = m.std(ddof=1)
        raw = float(m.mean()) * 1e4
        net = raw - cost_bp
        rec = {
            "n_trades": int(n.sum()), "names_per_day": round(float(n.mean()), 1),
            "raw_bp": round(raw, 1), "net_bp": round(net, 1),
            "active_bp": round(float(a.mean()) * 1e4, 1),
            "t_raw": round(float(m.mean()) / (sd / np.sqrt(m.size)), 2) if sd > 0 else None,
            "t_active": round(float(a.mean()) / (a.std(ddof=1) / np.sqrt(a.size)), 2) if a.std(ddof=1) > 0 else None,
            "win_day": round(float((m > 0).mean()), 3),
            "ann_pct": round(((1 + net / 1e4) ** 244 - 1) * 100, 1),
            "n_dates": int(m.size),
        }
        ys = sub.with_columns(pl.col("date").dt.year().alias("y"))
        yr = {}
        for r in ys.group_by("y").agg(pl.col("r_on").mean().alias("m"), pl.len().alias("n")).sort("y").to_dicts():
            if r["n"] >= 500:
                yr[int(r["y"])] = round(r["m"] * 1e4, 1)
        rec["yearly_bp"] = yr
        pos_years = sum(1 for v in yr.values() if v > 0)
        rec["pos_years"] = f"{pos_years}/{len(yr)}"
        out[label] = rec
        return rec

    print("\n############ [Q1] composite quietness deciles (a = vs same-day mean)")
    print(f"{'grp':>4}{'raw':>8}{'active':>8}{'t_a':>7}{'names/d':>9}{'pos yr':>8}   yearly")
    for g_ in range(N_G):
        s = uni.filter(pl.col("qg") == g_)
        r = rep(s, f"quiet_D{g_+1}")
        print(f"{g_+1:>4}{r['raw_bp']:>8.1f}{r['active_bp']:>8.1f}{r['t_active']:>7.1f}"
              f"{r['names_per_day']:>9.0f}{r['pos_years']:>8}   {list(r['yearly_bp'].values())}")

    print("\n############ [Q1b] tails: bottom 1/2/5/10/20/30% of quietness")
    print(f"{'cut':>6}{'raw':>8}{'active':>8}{'t_a':>7}{'names/d':>9}{'NET15':>8}{'ANN%':>8}")
    for pct in (0.01, 0.02, 0.05, 0.10, 0.20, 0.30):
        s = uni.filter(pl.col("quiet") <= pct)
        r = rep(s, f"quiet<= {pct:.0%}", cost_bp=15)
        print(f"{pct:>6.0%}{r['raw_bp']:>8.1f}{r['active_bp']:>8.1f}{r['t_active']:>7.1f}"
              f"{r['names_per_day']:>9.0f}{r['net_bp']:>8.1f}{r['ann_pct']:>8.1f}")
        out[f"quiet<= {pct:.0%}"]["median_amount_yi"] = round(float(s["amount"].median()) / 1e8, 2)
        out[f"quiet<= {pct:.0%}"]["median_fmcap_yi"] = round(float(s["fmcap"].median()), 1)
        out[f"quiet<= {pct:.0%}"]["median_turnover"] = round(float(s["turnover_rate"].median()), 2)

    print("\n############ [Q2] is it just market cap? quiet decile inside each mcap decile")
    uni2 = uni.with_columns(q("fmcap").alias("mg"))
    print(f"{'mcap D':>7}" + "".join(f"{'q'+str(i+1):>8}" for i in range(5)) + "   gradient")
    for mg in range(N_G):
        s = uni2.filter(pl.col("mg") == mg)
        # 5 quiet buckets inside mcap decile
        s = s.with_columns(((pl.col("quiet").rank("ordinal").over("date") - 1) * 5
                            // pl.len().over("date")).alias("qq"))
        vals = []
        for qq in range(5):
            ss = s.filter(pl.col("qq") == qq)
            g = ss.group_by("date").agg(pl.col("a").mean())["a"].to_numpy()
            vals.append(round(float(g.mean()) * 1e4, 1) if g.size > 50 else None)
        grad = (vals[-1] - vals[0]) if (vals[0] is not None and vals[-1] is not None) else None
        out.setdefault("mcap_x_quiet", {})[f"D{mg+1}"] = {"vals": vals, "gradient": grad}
        print(f"{mg+1:>7}" + "".join(f"{(v if v is not None else float('nan')):>8.1f}" for v in vals)
              + f"   {grad:+.1f}")

    print("\n############ [Q3] 2D: range decile x turnover decile (NET of 15bp, bp)")
    uni3 = uni.with_columns([q("range_pct").alias("rg"), q("turnover_rate").alias("tg")])
    grid = {}
    for rg in range(N_G):
        row = {}
        for tg in range(N_G):
            s = uni3.filter((pl.col("rg") == rg) & (pl.col("tg") == tg))
            if s.height < 2000:
                row[tg] = None
                continue
            g = s.group_by("date").agg(pl.col("r_on").mean().alias("m"))["m"].to_numpy()
            row[tg] = round(float(g.mean()) * 1e4 - 15, 1)
        grid[rg] = row
        out.setdefault("grid_range_x_turn", {})[f"range_D{rg+1}"] = row
    print("  rangeD\\turnD" + "".join(f"{'t'+str(i+1):>7}" for i in range(N_G)))
    for rg in range(N_G):
        print(f"  {rg+1:>10}" + "".join(
            f"{(grid[rg][t] if grid[rg][t] is not None else float('nan')):>7.0f}" for t in range(N_G)))

    print("\n############ [Q4] the winning cell, in absolute terms")
    best = uni3.filter((pl.col("rg") == 0) & (pl.col("tg") == 0))
    for lab, s in [("range D1 & turn D1", best),
                   ("range D1 & turn D1 & no lu 60d", best.filter(pl.col("d_since_lu") >= 60))]:
        if s.height < 500:
            continue
        r = rep(s, lab, cost_bp=15)
        print(f"  {lab:<32} n={r['n_trades']:>7,} names/d={r['names_per_day']:>5.0f} "
              f"raw={r['raw_bp']:+6.1f} net={r['net_bp']:+6.1f} t={r['t_raw']:+.1f} "
              f"pos_years={r['pos_years']} ann={r['ann_pct']:+.1f}%")
        print(f"     yearly: {r['yearly_bp']}")
        print(f"     median ADV={s['amount'].median()/1e8:.2f}e8  median fmcap={s['fmcap'].median():.0f}e8  "
              f"median turnover={s['turnover_rate'].median():.2f}%")

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}  elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
