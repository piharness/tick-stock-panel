"""Round 4 audit of the CE verdict: three defects, quantified.

The §12.10/12.11 verdict ("6 exit rules all fail R7-E; the reason is event
count, ~1450 sessions") rests on `_diag_ce_tail.py`.  This audit does not
re-litigate the direction.  It asks whether the verdict's *mechanism* survives:

  A. SUBGROUP PURITY  `dec[t] = np.searchsorted(qs, where(pool&finite, vol, nan))`
     maps NaN volatility to index 9, i.e. NaN vol silently joins decile 10.
     Quantify how many cells/events that adds and recompute the lift cleanly.

  B. FILL PRICE  `ac.vector_exit` books an SL hit at `entry*(1-sl)` even when the
     bar OPENED through the stop.  Small caps gap and limit-down; the correct
     fill is min(stop, open) for SL and max(target, open) for TP.  Also the loop
     `range(d+1, d+hold)` never checks the final holding day.  Run three
     engines: v0 (repo), v1 (gap fill), v2 (gap fill + final day).

  C. POWER  The 1450-session number is §12.9's S1 power for a +25bp daily
     active return (sigma ~246bp/cohort, overlap factor 1.66).  CE's sampling
     unit is a cohort (n=326), so that number cannot be reused.  Compute the
     CE-appropriate requirement from the observed CI itself:
         k = (lower half-width / point estimate)**2
     i.e. the sample multiple needed for the 95% lower bound to clear zero.

Also: lift gets a cohort-clustered CI (it currently has a point value only),
and the CE bootstrap's nan-drop rate is reported (a biased-narrowing channel).

ASCII only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261110
TAIL = 0.90
COST = 0.002
BLOCK = 20
NB = 2000
GAMMAS = (1.0, 5.0)
RNG = np.random.default_rng(SEED)


def ce(r: np.ndarray, g: float) -> float:
    return ac.ce_crra(r, g)


def circ_ci(x: np.ndarray, fn, block: int = BLOCK, nb: int = NB) -> dict:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 3 * max(5, block // 2):
        return {"n": int(n), "est_bp": None, "ci5_bp": None, "ci95_bp": None,
                "p_le0": None, "drop": None}
    m = int(np.ceil(n / block))
    off = np.arange(block)
    out = np.empty(nb)
    for i in range(nb):
        s = RNG.integers(0, n, m)
        idx = ((s[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = fn(x[idx])
    fin = np.isfinite(out)
    drop = 1.0 - float(fin.mean())
    o = out[fin]
    return {
        "n": int(n),
        "est_bp": round(float(fn(x)) * 1e4, 1),
        "ci5_bp": round(float(np.percentile(o, 2.5)) * 1e4, 1),
        "ci95_bp": round(float(np.percentile(o, 97.5)) * 1e4, 1),
        "p_le0": round(float((o <= 0.0).mean()), 3),
        "drop": round(drop, 4),
    }


def vector_exit_v1(mats: dict[str, np.ndarray], sl, tp) -> np.ndarray:
    """Repo engine + gap-aware fill, same loop bounds."""
    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    hold = ap.HORIZON
    ret = np.full_like(o, np.nan)
    for d in range(o.shape[0] - hold - 1):
        entry = o[d + 1]
        exit_px = c[d + hold].copy()
        alive = np.isfinite(entry) & (entry > 0)
        for k in range(d + 1, d + hold):
            op = o[k]
            if sl is not None:
                lvl = entry * (1 - sl)
                hit = alive & np.isfinite(l[k]) & (l[k] <= lvl)
                px = np.where(np.isfinite(op), np.minimum(lvl, op), lvl)
                exit_px[hit] = px[hit]
                alive &= ~hit
            if tp is not None:
                lvl = entry * (1 + tp)
                hit = alive & np.isfinite(h[k]) & (h[k] >= lvl)
                px = np.where(np.isfinite(op), np.maximum(lvl, op), lvl)
                exit_px[hit] = px[hit]
                alive &= ~hit
        ok = np.isfinite(entry) & (entry > 0) & np.isfinite(exit_px)
        ret[d] = np.where(ok, exit_px / entry - 1.0, np.nan)
    return ret


def vector_exit_v2(mats: dict[str, np.ndarray], sl, tp) -> np.ndarray:
    """v1 + the final holding day d+hold is also checked."""
    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    hold = ap.HORIZON
    ret = np.full_like(o, np.nan)
    for d in range(o.shape[0] - hold - 1):
        entry = o[d + 1]
        exit_px = c[d + hold].copy()
        alive = np.isfinite(entry) & (entry > 0)
        for k in range(d + 1, d + hold + 1):
            op = o[k]
            if sl is not None:
                lvl = entry * (1 - sl)
                hit = alive & np.isfinite(l[k]) & (l[k] <= lvl)
                px = np.where(np.isfinite(op), np.minimum(lvl, op), lvl)
                exit_px[hit] = px[hit]
                alive &= ~hit
            if tp is not None:
                lvl = entry * (1 + tp)
                hit = alive & np.isfinite(h[k]) & (h[k] >= lvl)
                px = np.where(np.isfinite(op), np.maximum(lvl, op), lvl)
                exit_px[hit] = px[hit]
                alive &= ~hit
        ok = np.isfinite(entry) & (entry > 0) & np.isfinite(exit_px)
        ret[d] = np.where(ok, exit_px / entry - 1.0, np.nan)
    return ret


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    vol = ac.grid_of(df, "vol20", dates, symbols)
    mom = ac.grid_of(df, "mom20", dates, symbols)
    G = dl.session_grids(mats)
    fwd = G["c2c20"]

    pool = (np.isfinite(mats["close"]) & (mats["close"] > 0)
            & np.isfinite(turn) & (turn > 0))
    ok = pool & np.isfinite(fwd) & np.isfinite(mom)
    out_event = np.isfinite(fwd) & (fwd >= TAIL)
    print(f"panel dates={T} symbols={S} ok={int(ok.sum()):,} events={int((out_event & ok).sum()):,}")

    res: dict = {"seed": SEED}

    # ---------------------------------------------------------------- A
    # Reproduce the repo's decile map verbatim (nan -> 9), then a clean one.
    dec_repo = np.full((T, S), np.nan)
    dec_clean = np.full((T, S), np.nan)
    band = np.full((T, S), np.nan)
    for t in range(T):
        m = pool[t] & np.isfinite(vol[t])
        if m.sum() >= 500:
            qs = np.nanpercentile(vol[t][m], [10, 20, 30, 40, 50, 60, 70, 80, 90])
            dec_repo[t] = np.searchsorted(qs, np.where(m, vol[t], np.nan))
            dec_clean[t] = np.where(m, np.searchsorted(qs, vol[t]), np.nan)
        row = mom[t][pool[t]]
        if row.size >= 500:
            thr = np.nanpercentile(row, 99.8)
            band[t] = np.where(pool[t] & np.isfinite(mom[t]), mom[t] >= thr, np.nan)

    nan_to_9 = (dec_repo == 9) & ok & ~np.isfinite(vol)
    print(f"\n[A] cells where NaN vol20 was mapped into decile 10: {int(nan_to_9.sum()):,} "
          f"({nan_to_9.sum() / max(ok.sum(), 1) * 100:.3f}% of ok cells)  "
          f"events in them: {int((out_event & nan_to_9).sum())}")
    res["A_decimal_purity"] = {
        "nan_in_dec10_cells": int(nan_to_9.sum()),
        "nan_in_dec10_events": int((out_event & nan_to_9).sum()),
        "nan_in_dec10_share_of_ok": round(float(nan_to_9.sum() / max(ok.sum(), 1)), 6),
    }
    for tag, dec in (("repo(nan->9)", dec_repo), ("clean(nan->skip)", dec_clean)):
        v10 = (dec == 9) & ok
        sub = v10 & (band == 1.0)
        n_v, e_v = int(v10.sum()), int((out_event & v10).sum())
        n_s, e_s = int(sub.sum()), int((out_event & sub).sum())
        base = (out_event & ok).sum() / ok.sum()
        rec = {
            "vol10_cells": n_v, "vol10_events": e_v,
            "vol10_base_pct": round(e_v / n_v * 100, 4),
            "sub_cells": n_s, "sub_events": e_s,
            "sub_pct": round(e_s / n_s * 100, 4),
            "lift_vs_base": round((e_s / n_s) / base, 2),
            "lift_vs_vol10": round((e_s / n_s) / (e_v / n_v), 2),
        }
        res["A_decimal_purity"][tag] = rec
        print(f"    {tag:18s} vol10 n={n_v:,} ev={e_v}  sub n={n_s:,} ev={e_s}  "
              f"P={rec['sub_pct']:.4f}%  lift {rec['lift_vs_base']:.2f}x / {rec['lift_vs_vol10']:.2f}x")

    # cohort-clustered CI for the lift (repo decile map, since that is the
    # number under audit)
    v10 = (dec_repo == 9) & ok
    sub = v10 & (band == 1.0)
    c_n_s, c_e_s, c_n_v, c_e_v = [], [], [], []
    for t in range(T - 21):
        n_s = int(sub[t].sum())
        if n_s == 0:
            continue
        c_n_s.append(n_s)
        c_e_s.append(int((out_event[t] & sub[t]).sum()))
        c_n_v.append(int(v10[t].sum()))
        c_e_v.append(int((out_event[t] & v10[t]).sum()))
    c_n_s = np.array(c_n_s); c_e_s = np.array(c_e_s)
    c_n_v = np.array(c_n_v); c_e_v = np.array(c_e_v)
    m = c_n_s.size
    nblk = int(np.ceil(m / BLOCK))
    off = np.arange(BLOCK)
    lf = np.empty(NB)
    pr = np.empty(NB)
    for i in range(NB):
        s = RNG.integers(0, m, nblk)
        idx = ((s[:, None] + off[None, :]) % m).ravel()[:m]
        ns, es = c_n_s[idx].sum(), c_e_s[idx].sum()
        nv, ev = c_n_v[idx].sum(), c_e_v[idx].sum()
        pr[i] = es / max(ns, 1)
        lf[i] = (es / max(ns, 1)) / max(ev / max(nv, 1), 1e-12)
    lf = lf[np.isfinite(lf)]
    res["A_shift_ci"] = {
        "cohorts": int(m),
        "sub_pct": round(float(c_e_s.sum() / c_n_s.sum() * 100), 4),
        "sub_pct_ci": [round(float(np.percentile(pr, 2.5) * 100), 4),
                       round(float(np.percentile(pr, 97.5) * 100), 4)],
        "lift_vs_vol10": round(float((c_e_s.sum() / c_n_s.sum()) / (c_e_v.sum() / c_n_v.sum())), 2),
        "lift_ci": [round(float(np.percentile(lf, 2.5)), 2),
                    round(float(np.percentile(lf, 97.5)), 2)],
        "lift_p_le1": round(float((lf <= 1.0).mean()), 4),
    }
    print(f"    cohort-clustered: sub P={res['A_shift_ci']['sub_pct']:.4f}% "
          f"CI {res['A_shift_ci']['sub_pct_ci']}  lift={res['A_shift_ci']['lift_vs_vol10']:.2f}x "
          f"CI {res['A_shift_ci']['lift_ci']}  P(lift<=1)={res['A_shift_ci']['lift_p_le1']}")

    # ---------------------------------------------------------------- B/C
    exits = ac.EXITS
    engines = {"v0_repo": ac.vector_exit, "v1_gap": vector_exit_v1, "v2_gap_finalday": vector_exit_v2}
    res["B_engine"] = {}
    res["C_power"] = {}

    for eng_name, eng in engines.items():
        ret_by = {lab: eng(mats, sl, tp) for lab, sl, tp in exits}
        rows = []
        for t in range(T - 21):
            s = np.flatnonzero(sub[t])
            if s.size == 0:
                continue
            rec = {"i0": t}
            for lab, _s, _t in exits:
                r = ret_by[lab][t]
                good = s[np.isfinite(r[s])]
                rec[f"p_{lab}"] = float(np.mean(r[good])) if good.size else np.nan
                v10i = np.flatnonzero(v10[t] & np.isfinite(r))
                rec[f"v10_{lab}"] = float(np.mean(r[v10i])) if v10i.size else np.nan
                uni = np.flatnonzero(pool[t] & np.isfinite(r))
                rec[f"uni_{lab}"] = float(np.mean(r[uni])) if uni.size else np.nan
            rows.append(rec)
        res["B_engine"][eng_name] = {}
        print(f"\n[B] engine {eng_name}: cohorts={len(rows)}")
        for lab, _s, _t in exits:
            p = np.array([r[f"p_{lab}"] for r in rows], dtype=np.float64)
            v10x = np.array([r[f"v10_{lab}"] for r in rows], dtype=np.float64)
            unix = np.array([r[f"uni_{lab}"] for r in rows], dtype=np.float64)
            fin = p[np.isfinite(p)]
            rec = {"n": int(fin.size), "mean_bp": round(float(fin.mean()) * 1e4, 1),
                   "ce": {}}
            for g in GAMMAS:
                net = circ_ci(p - COST, lambda x, gg=g: ce(x, gg))
                d10 = None
                ok2 = np.isfinite(p) & np.isfinite(v10x)
                if ok2.sum() > 60:
                    m2 = int(ok2.sum()); nblk2 = int(np.ceil(m2 / BLOCK))
                    pa, va = p[ok2] - COST, v10x[ok2] - COST
                    out = np.empty(NB)
                    for i in range(NB):
                        s = RNG.integers(0, m2, nblk2)
                        idx = ((s[:, None] + off[None, :]) % m2).ravel()[:m2]
                        out[i] = ce(pa[idx], g) - ce(va[idx], g)
                    out = out[np.isfinite(out)]
                    d10 = {"delta_bp": round(float(ce(pa, g) - ce(va, g)) * 1e4, 1),
                           "ci5_bp": round(float(np.percentile(out, 2.5)) * 1e4, 1),
                           "ci95_bp": round(float(np.percentile(out, 97.5)) * 1e4, 1),
                           "p_gt0": round(float((out > 0).mean()), 3),
                           "drop": round(1.0 - float((out.size / NB)), 4)}
                rec["ce"][f"g{int(g)}"] = {"net": net, "vs_v10": d10}
                # power multiple from the NET lower half-width
                if net["est_bp"] and net["ci5_bp"] is not None:
                    lo_hw = net["est_bp"] - net["ci5_bp"]
                    if net["est_bp"] > 0:
                        rec["ce"][f"g{int(g)}"]["k_abs"] = round((lo_hw / net["est_bp"]) ** 2, 2)
                    else:
                        rec["ce"][f"g{int(g)}"]["k_abs"] = None
                if d10 and d10["delta_bp"] and d10["delta_bp"] > 0:
                    lo_hw = d10["delta_bp"] - d10["ci5_bp"]
                    rec["ce"][f"g{int(g)}"]["k_rel"] = round((lo_hw / d10["delta_bp"]) ** 2, 2)
                elif d10:
                    rec["ce"][f"g{int(g)}"]["k_rel"] = None
            res["B_engine"][eng_name][lab] = rec
            g1 = rec["ce"]["g1"]
            n1 = g1["net"]
            d1 = g1["vs_v10"]
            print(f"    {lab:16s} mean={rec['mean_bp']:+8.1f}  "
                  f"net g1={n1['est_bp']:+7.1f} [{n1['ci5_bp']:+8.1f},{n1['ci95_bp']:+8.1f}] "
                  f"P={n1['p_le0']} drop={n1['drop']} k_abs={g1.get('k_abs')}"
                  + (f"  d_v10={d1['delta_bp']:+6.1f} [{d1['ci5_bp']:+7.1f},{d1['ci95_bp']:+7.1f}] k_rel={g1.get('k_rel')}" if d1 else ""))

    # summary: v0 vs v1 vs v2 point estimates for the two rules that moved
    print("\n[B] engine comparison, net CE g1 point estimate (bp):")
    for lab, _s, _t in exits:
        v0 = res["B_engine"]["v0_repo"][lab]["ce"]["g1"]["net"]["est_bp"]
        v1 = res["B_engine"]["v1_gap"][lab]["ce"]["g1"]["net"]["est_bp"]
        v2 = res["B_engine"]["v2_gap_finalday"][lab]["ce"]["g1"]["net"]["est_bp"]
        print(f"    {lab:16s} v0={v0:+7.1f}  v1={v1:+7.1f}  v2={v2:+7.1f}  (v2-v0={v2 - v0:+6.1f})")

    out = Path("analysis/_diag_ce_tail_audit_result.json")
    out.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
