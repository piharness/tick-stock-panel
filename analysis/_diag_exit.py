"""Diagnose why the vectorised exit engine disagrees with asymmetry_probe's."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_probe as ap  # noqa: E402


def build_grid(df: pl.DataFrame) -> tuple[list[str], list[str], dict[str, np.ndarray]]:
    di, d_inv = np.unique(df["date"].cast(pl.Utf8).to_numpy(), return_inverse=True)
    si, s_inv = np.unique(df["symbol"].to_numpy(), return_inverse=True)
    mats = {}
    for name in ["open", "high", "low", "close", "turnover_rate"]:
        grid = np.full((di.size, si.size), np.nan)
        grid[d_inv, s_inv] = df[name].to_numpy().astype(np.float64, copy=False)
        mats[name] = grid
    return [str(d) for d in di], [str(s) for s in si], mats


def main() -> None:
    df = ap.add_features(ap.load_panel())
    picks = ap.pick_daily(df, "mom20", True)
    dates, symbols, mats = ap.calendar_mats(df)
    print("probe shapes", {k: v.shape for k, v in mats.items()}, len(dates), len(symbols))

    d2, s2, mine = build_grid(df)
    print("same dates:", d2 == dates, "same symbols:", s2 == symbols)
    for k in ["open", "high", "low", "close"]:
        a, b = mats[k], mine[k]
        if a.shape != b.shape:
            print(f"  {k}: shape {a.shape} vs {b.shape} -> SKIP")
            continue
        diff = np.nanmax(np.abs(a - b))
        print(f"  {k}: nanmax|diff|={diff:.6g} n_differ={(np.abs(a - b) > 1e-9).sum()}")

    mp = {str(r["date"]): r["symbols"] for r in picks.select("date", "symbols").to_dicts()}
    r_probe = ap.exit_rule_payoff(dates, symbols, mats, mp, None, 0.10)
    print(f"probe TP10 n={r_probe.size} mean_bp={r_probe.mean()*1e4:.1f} skew={ap.skewness(r_probe):.2f}")

    # replicate the same iteration order with the vectorised engine
    sym_idx = {s: i for i, s in enumerate(symbols)}
    day_idx = {d: i for i, d in enumerate(dates)}
    vec: list[float] = []
    for d, syms in mp.items():
        i0 = day_idx[d]
        if i0 + ap.HORIZON + 1 >= len(dates):
            continue
        for s in syms:
            col = sym_idx[s]
            entry = mine["open"][i0 + 1, col]
            if not np.isfinite(entry) or entry <= 0:
                continue
            px = mine["close"][i0 + ap.HORIZON, col]
            for k in range(i0 + 1, i0 + ap.HORIZON):
                if not np.isfinite(mine["high"][k, col]):
                    continue
                if mine["high"][k, col] >= entry * 1.10:
                    px = entry * 1.10
                    break
            if np.isfinite(px) and px > 0:
                vec.append(px / entry - 1.0)
    r_vec = np.array(vec)
    print(f"vec   TP10 n={r_vec.size} mean_bp={r_vec.mean()*1e4:.1f} skew={ap.skewness(r_vec):.2f}")
    if r_probe.size == r_vec.size:
        d = np.abs(r_probe - r_vec)
        print(f"  max|diff|={np.nanmax(d):.6g} n_differ={(d>1e-9).sum()}")

    # how many picks are not tradable on the signal day?
    turn = mine["turnover_rate"]
    bad = zero = miss = 0
    for d, syms in mp.items():
        i0 = day_idx[d]
        for s in syms:
            t = turn[i0, sym_idx[s]]
            if not np.isfinite(t):
                miss += 1
            elif t <= 0:
                zero += 1
    print(f"picks with turnover nan={miss} turnover<=0={zero}")

    # extreme closes far from open (possible adjustment artifacts)
    o, c = mats["open"], mats["close"]
    ratio = c / o
    big = np.isfinite(ratio) & ((ratio > 3) | (ratio < 0.33))
    print(f"single bar close/open >3x or <0.33x: {big.sum()} cells")


if __name__ == "__main__":
    main()
