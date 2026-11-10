"""Does the one surviving rejection from rounds 1-2 survive a matched benchmark?

Round 2 left exactly one result standing across every口径 and every block
length: bottom20 x SL5% is 73.2bp below "the same rule applied to the whole
pool" (per-cohort dCE), P=.005-.01. _diag_match.py has just shown that at h=20
the same 20-stock basket's active return against the equal-weighted universe
(-88.6bp) collapses to -11.1bp when the benchmark is matched on 20-day
turnover, because the universe benchmark embeds a turnover tilt that the picks
inherit. If the same thing happens to dCE, round 2's last pillar is a benchmark
composition effect, not an exit-structure effect.

Benchmark here is the exact conditional expectation of a turnover-decile-matched
20-name basket:
    bmean(d) = sum_k w_k(d) * mean_{s in decile k} ret(d, s)
with w_k(d) the share of that day's 20 picks sitting in decile k. Deterministic,
no Monte Carlo noise, and it has the picks' turnover composition by
construction. Every exit rule is applied to the benchmark exactly as to the
picks.

Parity: the universe-benchmark column must reproduce round 2 digit for digit
(bottom20/SL5% dCE = -73.2bp), reusing round 2's own seeds.

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
import ce_path_audit as cpa  # noqa: E402

SEED = 20261109
ARMS = [("mom_bottom20", True), ("mom_top20", False)]
EXITS = [
    ("hold20 (no SL/TP)", None, None),
    ("SL5%", 0.05, None),
    ("TP10%", None, 0.10),
    ("SL5%+TP10%", 0.05, 0.10),
    ("SL3%+TP30%", 0.03, 0.30),
    ("SL10%+TP5%", 0.10, 0.05),
]
N_DECILE = 10


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
    print(f"panel dates={T} symbols={S}")

    out: dict = {"seed": SEED, "results": {}, "parity": {}}
    for arm_idx, (arm_label, ascending) in enumerate(ARMS):
        pk = ap.pick_daily(df, "mom20", ascending)
        picks_rows = pk.select("date", "symbols").to_dicts()
        out["results"][arm_label] = {}
        print(f"\n############ {arm_label}")
        for exit_idx, (exit_label, sl, tp) in enumerate(EXITS):
            exit_day, exit_px = cpa.exit_paths(mats, sl, tp)
            ret = cpa.ret_grid(mats, exit_px)
            pool_ok = tradable & np.isfinite(ret) & np.isfinite(to20)

            pmean, umean, bmean, held = [], [], [], []
            for r in picks_rows:
                i0 = date_index[str(r["date"])]
                sel_all = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
                good = sel_all[np.isfinite(ret[i0][sel_all])]
                pool = np.flatnonzero(pool_ok[i0])
                if good.size < 5 or pool.size < cpa.N:
                    continue
                m_pick = float(np.mean(ret[i0][good]))
                m_uni = float(np.mean(ret[i0][pool]))

                # exact turnover-decile-matched benchmark expectation
                vals = to20[i0][pool]
                order = np.argsort(vals, kind="stable")
                rk = np.empty(pool.size, dtype=np.int64)
                rk[order] = np.arange(pool.size)
                dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool.size)
                dec_full = np.full(S, -1, dtype=np.int64)
                dec_full[pool] = dec
                dsel = dec_full[good]
                cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(np.float64)
                if cnt.sum() <= 0:
                    continue
                w = cnt / cnt.sum()
                rvals = ret[i0][pool]
                dmean = np.zeros(N_DECILE)
                for k in range(N_DECILE):
                    m = dec == k
                    if m.any():
                        dmean[k] = float(np.mean(rvals[m]))
                m_match = float(np.dot(w, dmean))

                pmean.append(m_pick)
                umean.append(m_uni)
                bmean.append(m_match)
                held.append(float(np.mean(exit_day[i0][good] - (i0 + 1) + 1)))

            pmean, umean, bmean = np.array(pmean), np.array(umean), np.array(bmean)
            cell_rng = np.random.default_rng(SEED + 100 * arm_idx + exit_idx)
            d_uni = cpa.boot_delta_cohort(pmean, umean, 1.0, rng=cell_rng)
            d_match = cpa.boot_delta_cohort(
                pmean, bmean, 1.0, rng=np.random.default_rng(SEED + 500 + 100 * arm_idx + exit_idx))

            if d_uni["delta_bp"] is not None:
                ref = ac.block_boot_delta(pmean, umean, 1.0)
                out["parity"][f"{arm_label}|{exit_label}"] = {
                    "delta_bp": d_uni["delta_bp"], "round2": ref["delta_bp"],
                    "match": d_uni["delta_bp"] == ref["delta_bp"]}

            def tstat(a: np.ndarray) -> float:
                a = a[np.isfinite(a)]
                return float(a.mean() / (a.std(ddof=0) / np.sqrt(a.size / cpa.H)))

            out["results"][arm_label][exit_label] = {
                "n": int(pmean.size),
                "hold_days": round(float(np.mean(held)), 1),
                "ce_g1_pick_bp": round(float(ac.ce_crra(pmean, 1.0)) * 1e4, 1),
                "universe": {**{k: d_uni[k] for k in ("delta_bp", "ci5", "ci95", "p_gt0")},
                             "mean_active_bp": round(float((pmean - umean).mean()) * 1e4, 1),
                             "t_mean": round(tstat(pmean - umean), 2)},
                "matched": {**{k: d_match[k] for k in ("delta_bp", "ci5", "ci95", "p_gt0")},
                            "mean_active_bp": round(float((pmean - bmean).mean()) * 1e4, 1),
                            "t_mean": round(tstat(pmean - bmean), 2)},
            }
            u, m = out["results"][arm_label][exit_label]["universe"], out["results"][arm_label][exit_label]["matched"]
            print(f"\n  == {exit_label}  hold={np.mean(held):4.1f}d n={pmean.size}")
            print(f"     universe bench: dCE={u['delta_bp']:+7.1f}bp P={u['p_gt0']:.3f}"
                  f"  mean_active={u['mean_active_bp']:+7.1f}bp t={u['t_mean']:+5.2f}")
            print(f"     matched  bench: dCE={m['delta_bp']:+7.1f}bp P={m['p_gt0']:.3f}"
                  f"  mean_active={m['mean_active_bp']:+7.1f}bp t={m['t_mean']:+5.2f}")

    ok = all(v["match"] for v in out["parity"].values())
    print(f"\n[parity] universe-benchmark dCE reproduces round 2 in "
          f"{sum(v['match'] for v in out['parity'].values())}/{len(out['parity'])} cells: {ok}")

    p = Path("analysis/_diag_matched_ce_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
