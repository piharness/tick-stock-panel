"""Diagnose the size gap found by analysis/ce_path_audit.py [SIZE].

Under SL5% the audit's decision rule (paired block bootstrap of the per-cohort
CE difference) rejects 4.5% of zero-signal draws at a nominal 2.5% threshold,
while under no-exit it is nominal (2.5%). Two candidate causes:

  H1 block length. The per-cohort series is a sequence of OVERLAPPING windows
     spaced one day apart, so the dependence is induced by the window length,
     not by the exit rule. Moving block length H=20 is right for the no-exit
     structure; a stop that exits after ~10 days shortens the window, so H=20
     over-blocks and the bootstrap loses coverage.
  H2 anything else (selection, fat tails, paired-dependence).

If H1 holds, the false-positive rate should drop to nominal at a block length
close to the average holding period, and the audit's one rejection
(bottom20/SL5%, P=.01 at block 20) should weaken accordingly.

ASCII output for a GBK console.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_probe as ap  # noqa: E402
import ce_path_audit as cpa  # noqa: E402

BLOCKS = (5, 10, 20, 40)
DRAWS = 200
EXITS = [("hold20 (no SL/TP)", None, None), ("SL5%", 0.05, None)]
SEED = 20261109


def main() -> None:
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = cpa.ac.grid_of(df, "turnover_rate", dates, symbols)
    tradable = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)
    print(f"panel dates={T} symbols={S} hold={cpa.H}")

    for exit_label, sl, tp in EXITS:
        exit_day, exit_px = cpa.exit_paths(mats, sl, tp)
        ret = cpa.ret_grid(mats, exit_px)
        pool_ok = tradable & np.isfinite(ret)

        rows, umeans, holds = [], [], []
        for d_str, i0 in date_index.items():
            pool = np.flatnonzero(pool_ok[i0])
            if pool.size < cpa.N:
                continue
            rows.append(i0)
            umeans.append(float(np.mean(ret[i0][pool])))
        umeans = np.array(umeans)
        # average holding period of the universe under this exit rule
        for i0 in rows:
            pool = np.flatnonzero(pool_ok[i0])
            holds.append(float(np.mean(exit_day[i0][pool] - (i0 + 1) + 1)))
        hold_u = float(np.mean(holds))

        print(f"\n############ {exit_label}  universe avg holding = {hold_u:.1f} days")
        for block in BLOCKS:
            hits, deltas = 0, []
            for draw in range(DRAWS):
                rng = np.random.default_rng(SEED + 7000 + draw)
                pm = np.array(
                    [
                        float(np.mean(ret[i0][rng.choice(np.flatnonzero(pool_ok[i0]), size=cpa.N, replace=False)]))
                        for i0 in rows
                    ]
                )
                d = _boot_block(pm, umeans, 1.0, block, 200)
                deltas.append(d["delta_bp"])
                if d["p_gt0"] is not None and d["p_gt0"] >= 0.975:
                    hits += 1
            deltas = np.array(deltas)
            print(
                f"  block={block:3d} (hold/block={hold_u/block:4.1f})  "
                f"FP={hits}/{DRAWS} = {hits/DRAWS:5.1%}  "
                f"delta mean={deltas.mean():+6.1f} sd={deltas.std():5.1f} "
                f"p5={np.percentile(deltas,5):+6.1f} p95={np.percentile(deltas,95):+6.1f}"
            )

    # does the audit's one rejection depend on the block length?
    print("\n############ the single rejection, re-tested per block length")
    pk = ap.pick_daily(df, "mom20", True)
    for exit_label, sl, tp in EXITS:
        exit_day, exit_px = cpa.exit_paths(mats, sl, tp)
        ret = cpa.ret_grid(mats, exit_px)
        pool_ok = tradable & np.isfinite(ret)
        pmean, umean = [], []
        for r in pk.select("date", "symbols").to_dicts():
            i0 = date_index[str(r["date"])]
            sel = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
            good = sel[np.isfinite(ret[i0][sel])]
            pool = np.flatnonzero(pool_ok[i0])
            if good.size < 5 or pool.size < cpa.N:
                continue
            pmean.append(float(np.mean(ret[i0][good])))
            umean.append(float(np.mean(ret[i0][pool])))
        pmean, umean = np.array(pmean), np.array(umean)
        for block in BLOCKS:
            d = _boot_block(pmean, umean, 1.0, block, 400)
            print(
                f"  {exit_label:18s} block={block:3d}  delta={d['delta_bp']:+7.1f}bp"
                f" [{d['ci5']:+7.1f},{d['ci95']:+7.1f}]  P(>0)={d['p_gt0']:.3f}"
            )


def _boot_block(p, u, gamma, block, n):
    """Same estimator with an arbitrary block length."""
    rng = np.random.default_rng(SEED + 11000 + block)
    ok = np.isfinite(p) & np.isfinite(u)
    p, u = p[ok], u[ok]
    n_blocks = int(np.ceil(p.size / block))
    starts = np.arange(p.size - block + 1)
    out = np.empty(n)
    for i in range(n):
        pick = rng.choice(starts, size=n_blocks, replace=True)
        sel = np.concatenate([np.arange(s, s + block) for s in pick])
        out[i] = cpa.ac.ce_crra(p[sel], gamma) - cpa.ac.ce_crra(u[sel], gamma)
    return {
        "delta_bp": round(float(cpa.ac.ce_crra(p, gamma) - cpa.ac.ce_crra(u, gamma)) * 1e4, 1),
        "ci5": round(float(np.percentile(out, 5)) * 1e4, 1),
        "ci95": round(float(np.percentile(out, 95)) * 1e4, 1),
        "p_gt0": round(float((out > 0).mean()), 3),
    }


if __name__ == "__main__":
    main()
