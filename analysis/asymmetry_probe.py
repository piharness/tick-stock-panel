"""Non-symmetry probe: how much of TSP's searchable surface measures payoff shape?

Read-only. Uses local enriched daily partitions. Prints a compact report.

Purpose: test the claim that TSP's existing search objective (IC / Sharpe /
Sortino / robust median+worst) is a symmetric-moment objective, while the
asymmetry of the *payoff distribution* is a separate, mostly unsearched axis.

Outputs (ASCII labels to survive GBK console):
  A. cross-sectional fwd20 distribution per day (skew, tail ratio)
  B. four simple stock-pick portfolios' per-trade payoff shape
  C. same portfolios grouped by T-1 regime bucket
  D. rank disagreement between sharpe-like and skew-like ordering
  E. same signal, different exit rules -> payoff shape (the asymmetry lever)
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("data")
HORIZON = 20
N_STOCK = 20


def skewness(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 3:
        return float("nan")
    m, s = x.mean(), x.std()
    if s == 0:
        return float("nan")
    return float(((x - m) ** 3).mean() / s**3)


def kurtosis(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 4:
        return float("nan")
    m, s = x.mean(), x.std()
    if s == 0:
        return float("nan")
    return float(((x - m) ** 4).mean() / s**4)


def tail_ratio(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 20:
        return float("nan")
    p95, p5 = np.percentile(x, 95), np.percentile(x, 5)
    return float(p95 / abs(p5)) if p5 else float("nan")


def load_panel() -> pl.DataFrame:
    files = sorted(glob.glob(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    if not files:
        raise SystemExit("no enriched partitions found")
    frames = [
        pl.read_parquet(f, columns=["symbol", "date", "open", "high", "low", "close", "turnover_rate"])
        for f in files
    ]
    return pl.concat(frames, how="vertical_relaxed").sort(["symbol", "date"])


def add_features(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        (pl.col("close").shift(-HORIZON).over("symbol") / pl.col("close") - 1.0).alias("fwd"),
        (pl.col("close") / pl.col("close").shift(HORIZON).over("symbol") - 1.0).alias("mom20"),
        (pl.col("turnover_rate").rolling_mean(20).over("symbol")).alias("to20"),
        (pl.col("close").pct_change().rolling_std(20).over("symbol")).alias("vol20"),
    )


def payoff_stats(r: np.ndarray) -> dict:
    r = r[np.isfinite(r)]
    if r.size < 5:
        return {"n": int(r.size)}
    wins, losses = r[r > 0], r[r <= 0]
    avg_win = wins.mean() if wins.size else 0.0
    avg_loss = abs(losses.mean()) if losses.size else 0.0
    std = r.std()
    return {
        "n": int(r.size),
        "mean_bp": round(float(r.mean()) * 1e4, 1),
        "std_bp": round(float(std) * 1e4, 1),
        "sharpe_like": round(float(r.mean() / std * np.sqrt(252 / HORIZON)), 2) if std else None,
        "skew": round(skewness(r), 2),
        "kurt": round(kurtosis(r), 1),
        "win_rate": round(float((r > 0).mean()), 3),
        "payoff": round(float(avg_win / avg_loss), 2) if avg_loss else None,
        "tail_ratio": round(tail_ratio(r), 2),
        "p95_bp": round(float(np.percentile(r, 95)) * 1e4, 1),
        "p5_bp": round(float(np.percentile(r, 5)) * 1e4, 1),
    }


def pick_daily(df: pl.DataFrame, factor: str, ascending: bool) -> pl.DataFrame:
    sub = df.filter(pl.col(factor).is_finite() & pl.col("fwd").is_finite())
    return (
        sub.with_columns(
            pl.col(factor).rank(method="ordinal", descending=not ascending).over("date").alias("rk")
        )
        .filter(pl.col("rk") <= N_STOCK)
        .group_by("date")
        .agg(
            pl.col("fwd").mean().alias("ret"),
            pl.col("symbol").alias("symbols"),
        )
        .sort("date")
    )


def calendar_mats(df: pl.DataFrame) -> tuple[list[str], list[str], dict[str, np.ndarray]]:
    dates = sorted(df["date"].unique().to_list())
    symbols = sorted(df["symbol"].unique().to_list())
    wide = df.pivot(on="symbol", index="date", values=["open", "high", "low", "close"]).sort("date")
    mats = {}
    for col in ["open", "high", "low", "close"]:
        frame = wide.select([f"{col}_{s}" for s in symbols]).to_numpy().astype(np.float64)
        mats[col] = frame
    return [str(d) for d in dates], symbols, mats


def exit_rule_payoff(
    dates: list[str],
    symbols: list[str],
    mats: dict[str, np.ndarray],
    picks_by_date: dict[str, list[str]],
    sl: float | None,
    tp: float | None,
    hold: int = HORIZON,
) -> np.ndarray:
    """Path-dependent payoff: enter at next open, exit on SL/TP/hold.

    Conservative convention: on a bar where both SL and TP are touched, SL wins
    (adverse-first). Gap-through is ignored (documented limitation).
    """
    sym_idx = {s: i for i, s in enumerate(symbols)}
    date_idx = {d: i for i, d in enumerate(dates)}
    rets: list[float] = []
    for d, syms in picks_by_date.items():
        i0 = date_idx[d]
        if i0 + hold + 1 >= len(dates):
            continue
        for s in syms:
            j = sym_idx[s]
            entry = mats["open"][i0 + 1, j]
            if not np.isfinite(entry) or entry <= 0:
                continue
            exit_px = mats["close"][i0 + hold, j]
            for k in range(i0 + 1, i0 + hold):
                lo, hi = mats["low"][k, j], mats["high"][k, j]
                if sl is not None and np.isfinite(lo) and lo <= entry * (1 - sl):
                    exit_px = entry * (1 - sl)
                    break
                if tp is not None and np.isfinite(hi) and hi >= entry * (1 + tp):
                    exit_px = entry * (1 + tp)
                    break
            if np.isfinite(exit_px) and exit_px > 0:
                rets.append(exit_px / entry - 1.0)
    return np.array(rets, dtype=np.float64)


def main() -> None:
    df = add_features(load_panel())
    panel = df.filter(pl.col("fwd").is_finite())
    print(f"panel rows={panel.height} symbols={panel['symbol'].n_unique()} dates={panel['date'].n_unique()}")

    # A. cross-sectional distribution per day
    daily = (
        panel.group_by("date")
        .agg(
            pl.col("fwd").mean().alias("m"),
            pl.col("fwd").skew().alias("sk"),
            pl.col("fwd").quantile(0.95).alias("p95"),
            pl.col("fwd").quantile(0.05).alias("p05"),
        )
        .sort("date")
    )
    print("\n[A] daily cross-section fwd20 shape")
    print(
        daily.select(
            pl.col("sk").mean().alias("avg_cs_skew"),
            pl.col("sk").median().alias("med_cs_skew"),
            (pl.col("p95") / pl.col("p05").abs()).median().alias("med_tail_ratio"),
            pl.col("m").mean().alias("avg_cs_mean"),
        )
    )
    print(
        "  interpretation: positive avg_cs_skew means A-share cross-section is right-skewed "
        "at the market level; a long-only pick inherits that skew for free."
    )

    # B. portfolios
    print("\n[B] per-trade payoff of four naive picks (hold 20d, top20)")
    picks = {
        "momentum20_high": pick_daily(df, "mom20", True),
        "momentum20_low(reversal)": pick_daily(df, "mom20", False),
        "low_vol20": pick_daily(df, "vol20", False),
        "high_turnover20": pick_daily(df, "to20", True),
    }
    stats = {}
    for label, pk in picks.items():
        st = payoff_stats(pk["ret"].to_numpy())
        stats[label] = st
        print(f"  {label:28s} {st}")

    # C. regime-conditioned
    regime = pl.read_parquet(DATA / "regime_history" / "part.parquet").select(
        pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"),
        pl.col("state"),
    )
    regime = regime.with_columns(
        pl.when(pl.col("state").is_in(["strong", "lean_strong"]))
        .then(pl.lit("strong"))
        .when(pl.col("state").is_in(["weak", "lean_weak"]))
        .then(pl.lit("weak"))
        .otherwise(pl.lit("range"))
        .alias("bucket")
    )
    regime = regime.sort("date").with_columns(pl.col("bucket").shift(1).alias("bucket_t1"))
    print("\n[C] regime-conditioned (T-1 bucket) mean / skew")
    for label, pk in picks.items():
        joined = pk.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date")).join(
            regime, on="date", how="inner"
        )
        row = []
        for bucket in ["strong", "range", "weak"]:
            vals = joined.filter(pl.col("bucket_t1") == bucket)["ret"].to_numpy()
            if vals.size < 10:
                continue
            row.append(
                f"{bucket}: n={vals.size} mean={vals.mean()*1e4:7.1f}bp skew={skewness(vals):5.2f} "
                f"win={(vals > 0).mean():.2f}"
            )
        print(f"  {label:28s} {' | '.join(row)}")

    # D. rank disagreement
    print("\n[D] ranking by sharpe_like vs by skew")
    order_sharpe = sorted(stats, key=lambda k: -(stats[k].get("sharpe_like") or -99))
    order_skew = sorted(stats, key=lambda k: -(stats[k].get("skew") or -99))
    print("  sharpe order:", order_sharpe)
    print("  skew   order:", order_skew)

    # E. same signal, different exits
    print("\n[E] momentum-high picks under different exit rules (the asymmetry lever)")
    dates, symbols, mats = calendar_mats(df)
    mom_picks = {
        str(row["date"]): row["symbols"]
        for row in picks["momentum20_high"].select("date", "symbols").to_dicts()
    }
    exit_specs = [
        ("hold20 (no SL/TP)", None, None),
        ("SL5%", 0.05, None),
        ("SL10%", 0.10, None),
        ("TP10%", None, 0.10),
        ("SL5%+TP10%", 0.05, 0.10),
        ("SL3%+TP30% (trend)", 0.03, 0.30),
        ("SL10%+TP5% (mean-rev)", 0.10, 0.05),
    ]
    for label, sl, tp in exit_specs:
        r = exit_rule_payoff(dates, symbols, mats, mom_picks, sl, tp)
        print(f"  {label:24s} {payoff_stats(r)}")

    print("\n[E2] high-turnover picks under the same exit grid")
    to_picks = {
        str(row["date"]): row["symbols"]
        for row in picks["high_turnover20"].select("date", "symbols").to_dicts()
    }
    for label, sl, tp in exit_specs:
        r = exit_rule_payoff(dates, symbols, mats, to_picks, sl, tp)
        print(f"  {label:24s} {payoff_stats(r)}")


if __name__ == "__main__":
    main()
