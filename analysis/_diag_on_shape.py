"""What shape maximises the overnight leg (close[t] -> open[t+1])?

Scope discipline, because factor scans lie:
  * every factor is computable at t's close (no look-ahead);
  * the target is ALWAYS r_on minus the same-day cross-sectional mean, so a
    time-varying overnight premium cannot masquerade as a stock-level edge;
  * t is computed on the daily long-short series, not on pooled trades;
  * the sample is restricted to names you could actually buy at the close:
    no limit-up close, no one-word board, ADV >= 50m CNY, 60+ rows of history,
    calendar-adjacent exit;
  * deciles are formed inside each date, so market drift is differenced out;
  * every factor carries first-half / second-half signs. A factor that flips is
    noise regardless of its full-sample t.

No pivot() (this polars build returns only the index column).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
OUT = Path("analysis/_diag_on_shape_result.json")
N_G = 10


def decile_matrix(g: pl.DataFrame) -> np.ndarray | None:
    """(date, g, m) -> (n_dates, N_G) array, keeping only complete dates."""
    out = g.group_by("date").agg(pl.col("m").sort_by("g").alias("ms")).sort("date")
    out = out.filter(pl.col("ms").list.len() == N_G)
    if out.height < 100:
        return None
    return np.vstack(out["ms"].to_list())


def main() -> None:
    t0 = time.time()
    df = (pl.scan_parquet(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet"),
                          extra_columns="ignore")
          .select(["symbol", "date", "open", "high", "low", "close", "raw_close", "volume",
                   "amount", "turnover_rate", "consecutive_limit_ups"]).collect())
    cal = df.select("date").unique().sort("date").with_columns(pl.int_range(pl.len()).alias("rn"))
    df = df.join(cal, on="date").sort(["symbol", "rn"])

    df = df.with_columns([
        pl.col("rn").shift(-1).over("symbol").alias("rn_n"),
        pl.col("open").shift(-1).over("symbol").alias("open_n"),
        pl.col("close").shift(1).over("symbol").alias("c1"),
        pl.int_range(pl.len()).over("symbol").alias("row_n"),
        pl.col("close").shift(1).rolling_mean(5).over("symbol").alias("ma5"),
        pl.col("close").shift(1).rolling_mean(20).over("symbol").alias("ma20"),
        pl.col("volume").shift(1).rolling_mean(5).over("symbol").alias("v5"),
        pl.col("volume").shift(1).rolling_mean(20).over("symbol").alias("v20"),
        pl.col("turnover_rate").shift(1).rolling_mean(20).over("symbol").alias("t20"),
        pl.col("high").shift(1).rolling_max(20).over("symbol").alias("h20"),
        pl.col("low").shift(1).rolling_min(20).over("symbol").alias("l20"),
        pl.col("close").shift(5).over("symbol").alias("c5"),
        pl.col("close").shift(20).over("symbol").alias("c20"),
    ])

    df = df.with_columns([
        ((pl.col("high") - pl.col("low")) / pl.col("c1")).alias("range_pct"),
        (pl.col("high") - pl.col("low")).alias("hl"),
        (pl.col("close") / pl.col("c1") - 1).alias("chg"),
        (pl.col("open") / pl.col("c1") - 1).alias("gap"),
        (pl.col("close") / pl.col("open") - 1).alias("intraday"),
        (pl.col("close") / pl.col("ma5") - 1).alias("ma5_dev"),
        (pl.col("close") / pl.col("ma20") - 1).alias("ma20_dev"),
        (pl.col("volume") / pl.col("v5")).alias("vr5"),
        (pl.col("volume") / pl.col("v20")).alias("vr20"),
        (pl.col("turnover_rate") / pl.col("t20")).alias("turn_z"),
        (pl.col("h20") / pl.col("close") - 1).alias("dist_hi20"),
        (pl.col("l20") / pl.col("close") - 1).alias("dist_lo20"),
        (pl.col("close") / pl.col("c5") - 1).alias("ret5"),
        (pl.col("close") / pl.col("c20") - 1).alias("ret20"),
        (pl.col("open_n") / pl.col("close") - 1).alias("r_on"),
        pl.col("amount").log().alias("log_amt"),
    ])

    # shape of the bar: all need hl
    df = df.with_columns([
        pl.when(pl.col("hl") > 0)
          .then((pl.col("close") - pl.col("low")) / pl.col("hl")).alias("close_pos"),
        pl.when(pl.col("hl") > 0)
          .then((pl.col("high") - pl.max_horizontal(pl.col("open"), pl.col("close"))) / pl.col("hl"))
          .alias("upper_sh"),
        pl.when(pl.col("hl") > 0)
          .then((pl.min_horizontal(pl.col("open"), pl.col("close")) - pl.col("low")) / pl.col("hl"))
          .alias("lower_sh"),
        pl.when(pl.col("hl") > 0)
          .then((pl.col("close") - pl.col("open")) / pl.col("hl")).alias("body"),
        (pl.col("close") / pl.col("c1") - 1).alias("r1"),
    ])

    df = df.with_columns([
        ((pl.col("high") - pl.col("low")) / pl.col("hl").shift(1).rolling_mean(20).over("symbol")).alias("amp_ratio"),
        pl.col("r1").shift(1).rolling_std(20).over("symbol").alias("vol20"),
        (pl.col("chg") > 0).cast(pl.Int8).rolling_sum(5).over("symbol").alias("up5"),
    ])

    df = df.with_columns(
        pl.when(pl.col("consecutive_limit_ups") >= 1).then(pl.col("rn")).otherwise(None)
          .forward_fill().over("symbol").alias("last_lu")
    ).with_columns((pl.col("rn") - pl.col("last_lu")).fill_null(999).alias("d_since_lu"))

    uni = df.filter(
        (pl.col("rn_n") == pl.col("rn") + 1)
        & pl.col("r_on").is_finite()
        & (pl.col("row_n") >= 60)
        & (pl.col("consecutive_limit_ups") == 0)
        & (pl.col("hl") > 0)
        & (pl.col("amount") >= 5e7)
        & pl.col("chg").is_finite()
        & (pl.col("close") > 0)
        & ~((pl.col("open") == pl.col("high")) & (pl.col("high") == pl.col("low")))
    )
    nd = uni["date"].n_unique()
    print(f"[1] tradable rows={uni.height:,}  dates={nd}  names/day={uni.height/nd:.0f}  "
          f"t={time.time()-t0:.0f}s", flush=True)

    m = uni.group_by("date").agg(pl.col("r_on").mean().alias("mu"))
    uni = uni.join(m, on="date").with_columns((pl.col("r_on") - pl.col("mu")).alias("a"))
    print(f"    universe mean r_on = {uni['r_on'].mean()*1e4:+.2f}bp", flush=True)

    FACTORS = [
        ("close_pos", "close position in bar"),
        ("upper_sh", "upper shadow share"),
        ("lower_sh", "lower shadow share"),
        ("body", "body (signed)"),
        ("range_pct", "range / prev close"),
        ("amp_ratio", "range / 20d avg range"),
        ("gap", "today's gap"),
        ("intraday", "intraday open->close"),
        ("chg", "today's change"),
        ("turnover_rate", "turnover"),
        ("vr5", "vol ratio 5d"),
        ("vr20", "vol ratio 20d"),
        ("turn_z", "turnover / 20d avg"),
        ("ma5_dev", "close vs ma5"),
        ("ma20_dev", "close vs ma20"),
        ("ret5", "5d return"),
        ("ret20", "20d return"),
        ("dist_hi20", "distance to 20d high"),
        ("dist_lo20", "distance to 20d low"),
        ("vol20", "20d vol"),
        ("up5", "up days in 5"),
        ("d_since_lu", "days since limit-up"),
        ("log_amt", "log amount"),
    ]

    res: dict = {}
    print("\n############ decile scan   a = r_on - same-day cross-sectional mean (bp)")
    print(f"{'factor':<15}{'IC':>7}{'tIC':>6} {'D1':>7}{'D10':>7} {'D10-D1':>8}{'t':>7} {'mono':>6}   H1/H2")
    for f, zh in FACTORS:
        d = uni.filter(pl.col(f).is_finite())
        d = d.with_columns(
            ((pl.col(f).rank("ordinal").over("date") - 1) * N_G // pl.len().over("date")).alias("g"))
        g = d.group_by(["date", "g"]).agg(pl.col("a").mean().alias("m"))
        arr = decile_matrix(g)
        if arr is None:
            print(f"{f:<15}  (insufficient complete dates)")
            continue
        ls = arr[:, -1] - arr[:, 0]
        ls = ls[np.isfinite(ls)]
        if ls.size < 100:
            continue
        mu, sd = float(ls.mean()), float(ls.std(ddof=1))
        t = mu / (sd / np.sqrt(ls.size))
        ic = d.group_by("date").agg(
            pl.corr(pl.col(f).rank(), pl.col("a").rank()).alias("ic"))["ic"].drop_nulls().to_numpy()
        icm, ics = float(ic.mean()), float(ic.std(ddof=1))
        tic = icm / (ics / np.sqrt(ic.size)) if ics > 0 else 0.0
        dm = np.nanmean(arr, axis=0)
        mono = float(np.corrcoef(np.arange(N_G), dm)[0, 1])
        h1, h2 = ls[: ls.size // 2], ls[ls.size // 2:]
        res[f] = {
            "ic": round(icm, 4), "t_ic": round(tic, 2),
            "d1_bp": round(float(dm[0]) * 1e4, 1), "d10_bp": round(float(dm[-1]) * 1e4, 1),
            "ls_bp": round(mu * 1e4, 1), "t": round(float(t), 2), "mono": round(mono, 2),
            "h1_bp": round(float(h1.mean()) * 1e4, 1), "h2_bp": round(float(h2.mean()) * 1e4, 1),
            "deciles_bp": [round(float(x) * 1e4, 1) for x in dm],
            "n_dates": int(ls.size),
        }
        r = res[f]
        print(f"{f:<15}{r['ic']:>7.3f}{r['t_ic']:>6.1f} {r['d1_bp']:>7.1f}{r['d10_bp']:>7.1f} "
              f"{r['ls_bp']:>8.1f}{r['t']:>7.1f} {r['mono']:>6.2f}   {r['h1_bp']:>+6.1f}/{r['h2_bp']:>+6.1f}")

    OUT.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT}  elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
