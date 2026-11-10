"""Null-control extension of analysis/asymmetry_probe.py.

The probe reports RAW payoff shape of hand-labelled portfolios. Two things are
missing before any "asymmetry is a property of the exit rule" conclusion:

  A. ACTIVE: skew of per-cohort excess return (picks minus same-day universe
     under the SAME exit rule). The probe itself shows the cross-section is
     right-skewed, so raw skew is inherited for free and carries no signal.
  B. NULL: the same statistics for size-matched RANDOM picks drawn from the
     same pool. If a "positive skew" structure keeps its skew and a positive
     mean on random picks, the structure -- not the signal -- owns the shape.
  C. UTILITY: certainty equivalent per cohort under CRRA, block-bootstrapped,
     i.e. whether the positive-skew structure actually pays more than the
     higher-Sharpe one.

This module imports the probe rather than re-deriving its conventions, so the
raw column is guaranteed to reproduce it. Exit engine parity is asserted.

Outputs ASCII for a GBK console; writes JSON next to the script.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_probe as ap  # noqa: E402

N_DRAW = 300
RNG = np.random.default_rng(20261109)
SEED = 20261109

# (label, factor, ascending) -- ascending=True picks the LOW tail, matching
# probe.pick_daily's rank(descending=not ascending) convention. Note the probe
# labels its momentum calls the other way round; see _diag_rank.py.
ARMS = [
    ("mom_bottom20 (probe: 'momentum20_high')", "mom20", True),
    ("mom_top20 (probe: 'momentum20_low(reversal)')", "mom20", False),
]

EXITS = [
    ("hold20 (no SL/TP)", None, None),
    ("SL5%", 0.05, None),
    ("TP10%", None, 0.10),
    ("SL5%+TP10%", 0.05, 0.10),
    ("SL3%+TP30%", 0.03, 0.30),
    ("SL10%+TP5%", 0.10, 0.05),
]


def vector_exit(mats: dict[str, np.ndarray], sl: float | None, tp: float | None) -> np.ndarray:
    """[cohort, stock] exit-path return; same convention as ap.exit_rule_payoff."""
    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    hold = ap.HORIZON
    ret = np.full_like(o, np.nan)
    for d in range(o.shape[0] - hold - 1):
        entry = o[d + 1]
        exit_px = c[d + hold].copy()
        alive = np.isfinite(entry) & (entry > 0)
        for k in range(d + 1, d + hold):
            lo, hi = l[k], h[k]
            if sl is not None:
                hit = alive & np.isfinite(lo) & (lo <= entry * (1 - sl))
                exit_px[hit] = entry[hit] * (1 - sl)
                alive &= ~hit
            if tp is not None:
                hit = alive & np.isfinite(hi) & (hi >= entry * (1 + tp))
                exit_px[hit] = entry[hit] * (1 + tp)
                alive &= ~hit
        ok = np.isfinite(entry) & (entry > 0) & np.isfinite(exit_px)
        ret[d] = np.where(ok, exit_px / entry - 1.0, np.nan)
    return ret


def row_shift(grid: np.ndarray, lag: int, forward: bool) -> np.ndarray:
    out = np.full_like(grid, np.nan)
    for s in range(grid.shape[1]):
        col = grid[:, s]
        m = np.isfinite(col)
        vals = col[m]
        if vals.size <= lag:
            continue
        r = np.full(vals.size, np.nan)
        if forward:
            r[:-lag] = vals[lag:] / vals[:-lag] - 1.0
        else:
            r[lag:] = vals[lag:] / vals[:-lag] - 1.0
        out[m, s] = r
    return out


def grid_of(df: pl.DataFrame, col: str, dates: list[str], symbols: list[str]) -> np.ndarray:
    d_map = {d: i for i, d in enumerate(dates)}
    s_map = {s: i for i, s in enumerate(symbols)}
    g = np.full((len(dates), len(symbols)), np.nan)
    d_idx = np.array([d_map[d] for d in df["date"].cast(pl.Utf8).to_list()])
    s_idx = np.array([s_map[s] for s in df["symbol"].to_list()])
    g[d_idx, s_idx] = df[col].to_numpy().astype(np.float64, copy=False)
    return g


def skew(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 3:
        return float("nan")
    m, s = x.mean(), x.std()
    return float(((x - m) ** 3).mean() / s**3) if s else float("nan")


def stats(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    if x.size < 20:
        return {"n": int(x.size)}
    p95, p5 = np.percentile(x, 95), np.percentile(x, 5)
    srt = np.sort(x)
    k = max(1, int(0.05 * x.size))
    return {
        "n": int(x.size),
        "mean_bp": round(float(x.mean()) * 1e4, 1),
        "skew": round(skew(x), 2),
        "win": round(float((x > 0).mean()), 3),
        "tail": round(float(p95 / abs(p5)), 2) if p5 else None,
        "ex_best5_bp": round(float(srt[: x.size - k].mean()) * 1e4, 1),
    }


def ce_crra(r: np.ndarray, gamma: float) -> float:
    r = r[np.isfinite(r)]
    if r.size < 5:
        return float("nan")
    if gamma == 1.0:
        if (1.0 + r <= 0).any():
            return float("nan")
        return float(np.exp(np.mean(np.log1p(r))) - 1.0)
    w = 1.0 + r
    return float(np.mean(w ** (1.0 - gamma)) ** (1.0 / (1.0 - gamma)) - 1.0)


def block_boot(x: np.ndarray, fn, n: int = 400) -> tuple[float, float]:
    block = ap.HORIZON
    x = x[np.isfinite(x)]
    if x.size < block * 4:
        return (float("nan"), float("nan"))
    n_blocks = int(np.ceil(x.size / block))
    starts = np.arange(x.size - block + 1)
    out = np.empty(n)
    for i in range(n):
        idx = RNG.choice(starts, size=n_blocks, replace=True)
        out[i] = fn(np.concatenate([x[s : s + block] for s in idx]))
    out = out[np.isfinite(out)]
    return (float(np.percentile(out, 5)), float(np.percentile(out, 95)))


def block_boot_delta(p: np.ndarray, u: np.ndarray, gamma: float, n: int = 400) -> dict:
    """Paired block bootstrap of CE(picks) - CE(universe) on the same resample."""
    block = ap.HORIZON
    ok = np.isfinite(p) & np.isfinite(u)
    p, u = p[ok], u[ok]
    if p.size < block * 4:
        return {"delta_bp": None, "ci5": None, "ci95": None, "p_gt0": None}
    n_blocks = int(np.ceil(p.size / block))
    starts = np.arange(p.size - block + 1)
    out = np.empty(n)
    for i in range(n):
        pick = RNG.choice(starts, size=n_blocks, replace=True)
        sel = np.concatenate([np.arange(s, s + block) for s in pick])
        out[i] = ce_crra(p[sel], gamma) - ce_crra(u[sel], gamma)
    out = out[np.isfinite(out)]
    return {
        "delta_bp": round(float(ce_crra(p, gamma) - ce_crra(u, gamma)) * 1e4, 1),
        "ci5": round(float(np.percentile(out, 5)) * 1e4, 1),
        "ci95": round(float(np.percentile(out, 95)) * 1e4, 1),
        "p_gt0": round(float((out > 0).mean()), 3),
    }


def main() -> None:
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    hold = ap.HORIZON
    print(f"panel dates={len(dates)} symbols={len(symbols)} hold={hold}")

    turn = grid_of(df, "turnover_rate", dates, symbols)
    tradable = (
        np.isfinite(mats["close"])
        & (mats["close"] > 0)
        & np.isfinite(turn)
        & (turn > 0)
    )
    mom_row = row_shift(mats["close"], hold, forward=False)
    fwd_row = row_shift(mats["close"], hold, forward=True)

    results: dict[str, dict] = {}
    parity_checked = False

    for arm_label, factor, ascending in ARMS:
        pk = ap.pick_daily(df, factor, ascending)
        rows_pk = pk.select("date", "symbols").to_dicts()
        picks = [
            (str(r["date"]), np.array([s_map[s] for s in r["symbols"]], dtype=np.int64), r["symbols"])
            for r in rows_pk
        ]
        print(f"\n############ {arm_label}  cohorts={len(picks)}")

        for exit_label, sl, tp in EXITS:
            ret = vector_exit(mats, sl, tp)

            # parity: the vectorised engine must reproduce the probe exactly
            if not parity_checked:
                ref = ap.exit_rule_payoff(
                    dates, symbols, mats, {d: syms for d, _, syms in picks}, sl, tp
                )
                flat = np.concatenate(
                    [ret[date_index[d]][ix] for d, ix, _ in picks if d in date_index]
                )
                flat = flat[np.isfinite(flat)]
                if ref.size == flat.size:
                    gap = float(np.max(np.abs(ref - flat)))
                    print(f"  [parity] exit engine max|diff| vs probe = {gap:.3g} n={ref.size}")
                    assert gap < 1e-9, "vector exit engine diverged from probe"
                parity_checked = True

            pool_ok = tradable & np.isfinite(ret)
            rows, pmean, umean, all_trades = [], [], [], []
            for d, ix, _ in picks:
                i0 = date_index.get(d, -1)
                if i0 < 0:
                    continue
                good = ix[np.isfinite(ret[i0][ix])]
                if good.size < 5:
                    continue
                pool = np.flatnonzero(pool_ok[i0])
                if pool.size < ap.N_STOCK:
                    continue
                rows.append(i0)
                pmean.append(np.mean(ret[i0][good]))
                umean.append(np.mean(ret[i0][pool]))
                all_trades.append(ret[i0][good])

            pmean = np.array(pmean)
            umean = np.array(umean)
            active = pmean - umean
            raw = np.concatenate(all_trades)

            null = np.empty((len(rows), N_DRAW))
            for i, i0 in enumerate(rows):
                pool = np.flatnonzero(pool_ok[i0])
                emb = np.stack([RNG.choice(pool, size=ap.N_STOCK, replace=False) for _ in range(N_DRAW)])
                null[i] = np.mean(ret[i0][emb], axis=1) - umean[i]
            null_skew = np.array([skew(null[:, j]) for j in range(N_DRAW)])

            real_skew = skew(active)
            pct = float((null_skew < real_skew).mean()) if np.isfinite(real_skew) else float("nan")

            ces = {}
            for g in (1.0, 2.0, 5.0):
                lo, hi = block_boot(pmean, lambda s, gg=g: ce_crra(s, gg))
                ces[f"g{int(g)}"] = {
                    "ce_bp": round(ce_crra(pmean, g) * 1e4, 1),
                    "ci5": round(lo * 1e4, 1),
                    "ci95": round(hi * 1e4, 1),
                }
            ce_uni = {f"g{int(g)}": round(ce_crra(umean, g) * 1e4, 1) for g in (1.0, 2.0, 5.0)}
            delta = {f"g{int(g)}": block_boot_delta(pmean, umean, g) for g in (1.0, 2.0, 5.0)}

            key = f"{arm_label}|{exit_label}"
            results[key] = {
                "raw_trade": stats(raw),
                "active": stats(active),
                "universe": stats(umean),
                "null_skew_p50": round(float(np.nanpercentile(null_skew, 50)), 2),
                "null_skew_p95": round(float(np.nanpercentile(null_skew, 95)), 2),
                "null_skew_p5": round(float(np.nanpercentile(null_skew, 5)), 2),
                "active_skew_pctile": round(pct, 3),
                "ce_crra": ces,
                "ce_universe": ce_uni,
                "ce_delta_vs_universe": delta,
            }
            s_raw, s_act = results[key]["raw_trade"], results[key]["active"]
            d1, d5 = delta["g1"], delta["g5"]
            print(
                f"  {exit_label:16s} raw mean={s_raw['mean_bp']:+8.1f} skew={s_raw['skew']:+6.2f} win={s_raw['win']:.3f}"
                f" | active mean={s_act['mean_bp']:+8.1f} skew={s_act['skew']:+6.2f}"
                f" | null skew p50={results[key]['null_skew_p50']:+.2f} pctile={pct:.2f}"
            )
            print(
                f"  {'':16s} CE g1={ces['g1']['ce_bp']:+7.1f} [{ces['g1']['ci5']:+7.1f},{ces['g1']['ci95']:+7.1f}]"
                f" g5={ces['g5']['ce_bp']:+7.1f}"
                f" | universe CE g1={ce_uni['g1']:+7.1f} g5={ce_uni['g5']:+7.1f}"
            )
            print(
                f"  {'':16s} DELTA vs universe: g1={d1['delta_bp']:+7.1f}bp "
                f"[{d1['ci5']:+7.1f},{d1['ci95']:+7.1f}] P(>0)={d1['p_gt0']:.2f}"
                f" | g5={d5['delta_bp']:+7.1f}bp [{d5['ci5']:+7.1f},{d5['ci95']:+7.1f}] P(>0)={d5['p_gt0']:.2f}"
            )

    out = Path("analysis/asymmetry_control_result.json")
    out.write_text(json.dumps({"seed": SEED, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
