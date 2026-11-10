"""Two remaining attacks on the short-horizon result: benchmark matching and stability.

analysis/_diag_fillable.py showed the short-horizon momentum edge survives
lock-up fillability and is not market beta. Two things can still void it:

  G1 BENCHMARK.  Every round so far compared a 20-stock basket to the
     equal-weighted mean of the whole 5581-name cross-section. That active
     return contains a size / liquidity / turnover tilt alongside momentum, so
     "momentum works at h=1" and "high-turnover names beat the average in a
     bull tape" are the same observation. Kill condition: replace the universe
     benchmark with a STRATIFIED-MATCHED benchmark -- for every pick, draw a
     random name from the same 20-day-turnover decile, so the benchmark has the
     picks' turnover composition by construction. If the active mean collapses
     toward zero, the effect is a liquidity tilt, not a signal.

  G2 STABILITY.  A single 18-month window, one regime, one arm. Kill condition:
     the sign must hold in both halves of the sample (first 183 / last 184
     daily cohorts) for the cell to be called stable rather than a product of
     one stretch.

Also records the correct inference convention: with a portfolio that persists
across adjacent cohort dates, Var(mean of n overlapping h-day bets) ~
Var(a)*h/n, i.e. SE = sd/sqrt(n_eff) with n_eff = n/h. The iid placebo in
_diag_fillable.py measures sd(null t) = 1/sqrt(h) exactly, which is that same
fact seen from the other side, not a contradiction.

ASCII output only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
import horizon_scale as hs  # noqa: E402

SEED = 20261109
H_GRID = (1, 2, 3, 5, 20)
N_STOCK = ap.N_STOCK
N_MATCH = 50
ARMS = [("mom_bottom20", True), ("mom_top20", False)]
N_DECILE = 10


def matched_benchmark(F: np.ndarray, i0: int, sel: np.ndarray, dec_full: np.ndarray,
                      buckets: list[np.ndarray], rng: np.random.Generator) -> float:
    """Mean h-day return of a turnover-decile-matched random basket of len(sel)."""
    if sel.size == 0:
        return float("nan")
    dec = dec_full[sel]
    total = 0.0
    used = 0
    for k in range(N_DECILE):
        c = int(np.sum(dec == k))
        if c == 0:
            continue
        b = buckets[k]
        if b.size == 0:
            return float("nan")
        pick = rng.choice(b, size=c, replace=min(c, b.size))
        total += float(np.sum(F[i0][pick]))
        used += c
    return total / used if used else float("nan")


def stats(a: np.ndarray, h: int) -> dict:
    a = a[np.isfinite(a)]
    n = a.size
    if n < 20:
        return {"n": n}
    n_eff = n / h
    mean_bp = float(a.mean()) * 1e4
    sd_bp = float(a.std(ddof=0)) * 1e4
    se = sd_bp / np.sqrt(n_eff)
    return {"n": n, "n_eff": round(n_eff, 1), "mean_bp": round(mean_bp, 1),
            "sd_bp": round(sd_bp, 1), "se_bp": round(se, 1), "t": round(mean_bp / se, 2)}


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    tradable = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)
    to20 = ac.grid_of(df, "to20", dates, symbols)
    print(f"panel dates={T} symbols={S}  to20 finite cells={int(np.isfinite(to20).sum())}")

    F20 = hs.fwd_grid(mats, 20)
    out: dict = {"seed": SEED, "h_grid": list(H_GRID), "match": {}, "split": {}}

    print("\n############ [G1] universe benchmark vs turnover-decile-matched benchmark")
    for arm_label, ascending in ARMS:
        pk = ap.pick_daily(df, "mom20", ascending)
        picks_rows = pk.select("date", "symbols").to_dicts()
        out["match"][arm_label] = {}
        print(f"\n== {arm_label}")
        for h in H_GRID:
            F = F20 if h == 20 else hs.fwd_grid(mats, h)
            pool_ok = tradable & np.isfinite(F) & np.isfinite(to20)
            a_uni, a_match = [], []
            for r in picks_rows:
                i0 = date_index[str(r["date"])]
                if i0 + h >= T:
                    continue
                pool = np.flatnonzero(pool_ok[i0])
                if pool.size < N_STOCK:
                    continue
                sel_all = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
                sel = sel_all[pool_ok[i0][sel_all]]
                if sel.size < 5:
                    continue
                # deciles of 20d turnover inside today's pool
                vals = to20[i0][pool]
                order = np.argsort(vals, kind="stable")
                rk = np.empty(pool.size, dtype=np.int64)
                rk[order] = np.arange(pool.size)
                dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool.size)
                dec_full = np.full(S, -1, dtype=np.int64)
                dec_full[pool] = dec
                buckets = [pool[dec == k] for k in range(N_DECILE)]
                u = float(np.mean(F[i0][pool]))
                m_pick = float(np.mean(F[i0][sel]))
                rng = np.random.default_rng(SEED + 7919 * h + i0)
                bm = np.mean([matched_benchmark(F, i0, sel, dec_full, buckets, rng)
                              for _ in range(N_MATCH)])
                a_uni.append(m_pick - u)
                a_match.append(m_pick - float(bm))
            su, sm = stats(np.array(a_uni), h), stats(np.array(a_match), h)
            out["match"][arm_label][str(h)] = {"universe": su, "matched": sm}
            print(f"  h={h:3d} universe-bench t={su.get('t'):+6.2f} (mean {su.get('mean_bp'):+8.1f}bp)"
                  f" | matched-bench t={sm.get('t'):+6.2f} (mean {sm.get('mean_bp'):+8.1f}bp)"
                  f" | n={sm.get('n')}")

    print("\n############ [G2] split-half stability (matched benchmark)")
    for arm_label, ascending in ARMS:
        pk = ap.pick_daily(df, "mom20", ascending)
        picks_rows = pk.select("date", "symbols").to_dicts()
        out["split"][arm_label] = {}
        for h in (1, 2, 3):
            F = hs.fwd_grid(mats, h)
            pool_ok = tradable & np.isfinite(F) & np.isfinite(to20)
            a = []
            for r in picks_rows:
                i0 = date_index[str(r["date"])]
                if i0 + h >= T:
                    continue
                pool = np.flatnonzero(pool_ok[i0])
                if pool.size < N_STOCK:
                    continue
                sel_all = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
                sel = sel_all[pool_ok[i0][sel_all]]
                if sel.size < 5:
                    continue
                vals = to20[i0][pool]
                order = np.argsort(vals, kind="stable")
                rk = np.empty(pool.size, dtype=np.int64)
                rk[order] = np.arange(pool.size)
                dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool.size)
                dec_full = np.full(S, -1, dtype=np.int64)
                dec_full[pool] = dec
                buckets = [pool[dec == k] for k in range(N_DECILE)]
                rng = np.random.default_rng(SEED + 7919 * h + i0)
                bm = np.mean([matched_benchmark(F, i0, sel, dec_full, buckets, rng)
                              for _ in range(N_MATCH)])
                a.append(float(np.mean(F[i0][sel])) - float(bm))
            a = np.array(a)
            half = a.size // 2
            s1, s2 = stats(a[:half], h), stats(a[half:], h)
            out["split"][arm_label][str(h)] = {"first": s1, "second": s2}
            print(f"  {arm_label:13s} h={h}  1st half mean={s1.get('mean_bp'):+8.1f}bp t={s1.get('t'):+5.2f}"
                  f" | 2nd half mean={s2.get('mean_bp'):+8.1f}bp t={s2.get('t'):+5.2f}")

    p = Path("analysis/_diag_match_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
