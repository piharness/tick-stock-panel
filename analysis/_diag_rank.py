"""Check the sort direction used by asymmetry_probe.pick_daily."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_probe as ap  # noqa: E402


def main() -> None:
    df = ap.add_features(ap.load_panel())
    picks = ap.pick_daily(df, "mom20", True).select("date", "symbols", "ret")
    sub = df.filter(pl.col("mom20").is_finite() & pl.col("fwd").is_finite())

    rows = []
    for row in picks.head(5).to_dicts():
        d = row["date"]
        day = sub.filter(pl.col("date") == d)
        allmom = day["mom20"].to_numpy()
        picked = day.filter(pl.col("symbol").is_in(row["symbols"]))["mom20"].to_numpy()
        n = allmom.size
        order = np.argsort(-allmom)
        top = set(np.flatnonzero(np.isin(np.arange(n), order[:20])).tolist())
        rows.append(
            {
                "date": str(d),
                "universe": n,
                "picked_mean_mom": round(float(picked.mean()), 4),
                "universe_mean_mom": round(float(allmom.mean()), 4),
                "picked_min": round(float(picked.min()), 4),
                "picked_max": round(float(picked.max()), 4),
                "universe_max": round(float(allmom.max()), 4),
                "universe_min": round(float(allmom.min()), 4),
            }
        )
    print(pl.DataFrame(rows))

    # full-sample: rank percentile of picked symbols
    pct = []
    for row in picks.to_dicts():
        day = sub.filter(pl.col("date") == row["date"])
        allmom = day["mom20"].to_numpy()
        if allmom.size < 100:
            continue
        picked = day.filter(pl.col("symbol").is_in(row["symbols"]))["mom20"].to_numpy()
        r = (allmom[:, None] < picked[None, :]).mean()  # avg not needed
        pct.append(float(np.mean([(allmom < v).mean() for v in picked])))
    print(f"mean cross-sectional percentile of picked mom20 = {np.mean(pct):.3f}  (1.0 = highest momentum)")

    for asc, label in [(True, "pick_daily(..., ascending=True)"), (False, "pick_daily(..., ascending=False)")]:
        p = ap.pick_daily(df, "mom20", asc)
        vals = []
        for row in p.to_dicts():
            day = df.filter((pl.col("date") == row["date"]) & pl.col("symbol").is_in(row["symbols"]))
            vals.extend(day["mom20"].to_numpy().tolist())
        print(f"{label}: picked mom20 mean={np.mean(vals):.4f}")


if __name__ == "__main__":
    main()
