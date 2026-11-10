"""Round 4 audit, part 4 (R9): attack the one sentence every round agreed on.

Doc 12.13 settles the direction as: (i) the right-tail entry probability is
predictable on the fillable subset (lift 2.78x, CI [1.75, 4.08]); (ii) the net CE
on that subset cannot be shown positive; (iii) the book CE is lifted by boards
you cannot buy.  Four premises of that settlement have never been tested:

  G1  LABEL/TRADE ALIGNMENT.  The tail label is fwd = c[t+20]/c[t] - 1 >= 0.90,
      but the trade enters at o[t+1] (vector_exit_v2) and exits at c[t+20].
      The label therefore contains the entry gap the trade cannot capture, and
      the gap is exactly what a locked board produces.  Restate the label on
      the tradeable basis (c[t+20]/o[t+1] - 1) and recompute the base rate, the
      subgroup rate and the lift with the same cohort-cluster bootstrap.
      If the lift does not survive, "prediction is real" was a labelling
      artifact of the gap.

  G2  FILL IS NOT BINARY.  L1 removes "one-price entry day" cells.  But a
      limit-up entry day that did NOT lock is buyable, and its forward return is
      the counterfactual a real buyer would receive.  Split the strategy basket
      by entry-day class (locked / limit-up but opened / not limit-up) and
      measure the forward return gap on all cells, not just the 62 events.

  G3  WHO OWNS THE +64.3bp.  The L0-L1 paired delta is "significant" (P=0.997)
      under a day-block bootstrap.  Its 62 locked cells are a handful of
      symbol-episodes.  Leave-one-episode-out: how much of the delta survives
      without the single largest run.

  G4  THE TWO STATED NEXT STEPS.  "wait for post-2026-11-07 data" and "replay
      tick queues for the 5 runs" are checked against the files on disk and
      against the 20-session forward requirement.

No production code is touched.  Mask, decile, band and exit-engine conventions
are copied verbatim from _diag_ce_tail_verify.py (doc 12.13) so the numbers are
comparable; the L0-L1 delta is reproduced as a parity check before G3 uses it.

ASCII only (GBK console).
"""
from __future__ import annotations

import glob
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import _diag_ce_tail_audit as a1  # noqa: E402  (vector_exit_v2)
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
from _diag_ce_tail_verify import limit_grids  # noqa: E402

SEED = 20261113
TAIL = 0.90
COST = 0.002
BLOCK = 20
NB = 2000
RNG = np.random.default_rng(SEED)

ce = ac.ce_crra


# --------------------------------------------------------------------- helpers
def circ_ci(x: np.ndarray, fn, nb: int = NB, block: int = BLOCK) -> dict | None:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 60:
        return None
    m = int(np.ceil(n / block))
    off = np.arange(block)
    out = np.empty(nb)
    for i in range(nb):
        s = RNG.integers(0, n, m)
        idx = ((s[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = fn(x[idx])
    o = out[np.isfinite(out)]
    return {"n": int(n), "est_bp": round(float(fn(x)) * 1e4, 1),
            "ci5_bp": round(float(np.percentile(o, 2.5)) * 1e4, 1),
            "ci95_bp": round(float(np.percentile(o, 97.5)) * 1e4, 1),
            "p_le0": round(float((o <= 0.0).mean()), 3)}


def paired_delta(p0: np.ndarray, p1: np.ndarray, fn) -> dict | None:
    ok = np.isfinite(p0) & np.isfinite(p1)
    p0, p1 = p0[ok], p1[ok]
    n = p0.size
    if n < 60:
        return None
    m = int(np.ceil(n / BLOCK))
    off = np.arange(BLOCK)
    out = np.empty(NB)
    for i in range(NB):
        s = RNG.integers(0, n, m)
        idx = ((s[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = fn(p0[idx]) - fn(p1[idx])
    o = out[np.isfinite(out)]
    return {"n": int(n), "delta_bp": round(float(fn(p0) - fn(p1)) * 1e4, 1),
            "ci5_bp": round(float(np.percentile(o, 2.5)) * 1e4, 1),
            "ci95_bp": round(float(np.percentile(o, 97.5)) * 1e4, 1),
            "p_gt0": round(float((o > 0.0).mean()), 3)}


def lift(keep: np.ndarray, sub: np.ndarray, ev: np.ndarray, base: np.ndarray,
         T: int) -> dict:
    """Cohort-cluster circular block bootstrap of P(ev|sub,keep) and the lift
    vs P(ev|base,keep).  Same estimator as doc 12.13 B."""
    c_ns, c_es, c_nv, c_ev = [], [], [], []
    for t in range(T - 21):
        n_s = int((sub[t] & keep[t]).sum())
        if n_s == 0:
            continue
        c_ns.append(n_s)
        c_es.append(int((ev[t] & sub[t] & keep[t]).sum()))
        c_nv.append(int((base[t] & keep[t]).sum()))
        c_ev.append(int((ev[t] & base[t] & keep[t]).sum()))
    c_ns = np.array(c_ns); c_es = np.array(c_es)
    c_nv = np.array(c_nv); c_ev = np.array(c_ev)
    m = c_ns.size
    if m < 60:
        return {"cohorts": int(m)}
    nblk = int(np.ceil(m / BLOCK))
    off = np.arange(BLOCK)
    pr = np.empty(NB)
    lf = np.empty(NB)
    for i in range(NB):
        s = RNG.integers(0, m, nblk)
        idx = ((s[:, None] + off[None, :]) % m).ravel()[:m]
        ns, es = c_ns[idx].sum(), c_es[idx].sum()
        nv, evs = c_nv[idx].sum(), c_ev[idx].sum()
        pr[i] = es / max(ns, 1)
        lf[i] = (es / max(ns, 1)) / max(evs / max(nv, 1), 1e-12) if nv else np.nan
    pr = pr[np.isfinite(pr)]
    lf = lf[np.isfinite(lf)]
    p_sub = c_es.sum() / c_ns.sum()
    return {
        "cohorts": int(m),
        "sub_pct": round(float(p_sub * 100), 4),
        "sub_pct_ci": [round(float(np.percentile(pr, 2.5) * 100), 4),
                       round(float(np.percentile(pr, 97.5) * 100), 4)],
        "base_pct": round(float(c_ev.sum() / max(c_nv.sum(), 1) * 100), 4),
        "lift": round(float(p_sub / (c_ev.sum() / c_nv.sum())), 2),
        "lift_ci": [round(float(np.percentile(lf, 2.5)), 2),
                    round(float(np.percentile(lf, 97.5)), 2)],
        "lift_p_le1": round(float((lf <= 1.0).mean()), 4),
    }


def lead(g: np.ndarray, h: int) -> np.ndarray:
    out = np.full_like(g, np.nan)
    T = g.shape[0]
    if h < T:
        out[: T - h] = g[h:]
    return out


def ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = num / den - 1.0
    okm = np.isfinite(num) & np.isfinite(den) & (den > 0) & np.isfinite(r)
    return np.where(okm, r, np.nan)


def runs_of(pairs: list[tuple[int, str]]) -> list[dict]:
    """Group (signal row, symbol) into per-symbol consecutive-row runs."""
    by: dict[str, list[int]] = {}
    for t, s in pairs:
        by.setdefault(s, []).append(int(t))
    eps: list[dict] = []
    for s, ts in by.items():
        ts = sorted(ts)
        cur = [ts[0]]
        for a, b in zip(ts, ts[1:]):
            if b != a + 1:
                eps.append({"symbol": s, "rows": cur})
                cur = []
            cur.append(b)
        eps.append({"symbol": s, "rows": cur})
    eps.sort(key=lambda e: (-len(e["rows"]), e["symbol"]))
    return eps


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    sym_arr = np.asarray(symbols)
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    vol = ac.grid_of(df, "vol20", dates, symbols)
    mom = ac.grid_of(df, "mom20", dates, symbols)
    G = dl.session_grids(mats)
    fwd = G["c2c20"]
    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]

    both = np.isfinite(h) & np.isfinite(l)
    one_price = np.zeros((T, S), dtype=bool)
    one_price[both] = np.isclose(h[both], l[both], rtol=0.0, atol=1e-9)

    # entry-day masks, indexed by SIGNAL row t (entry day = t+1)
    lock_entry = np.zeros((T, S), dtype=bool)
    lock_entry[:-1] = one_price[1:]
    gap_correct = np.zeros((T, S), dtype=bool)
    gap_correct[:-1] = (np.isfinite(o[1:]) & np.isfinite(c[:-1])
                        & (o[1:] >= c[:-1] * 1.098))

    pool = (np.isfinite(c) & (c > 0) & np.isfinite(turn) & (turn > 0))
    ok = pool & np.isfinite(fwd) & np.isfinite(mom)

    dec = np.full((T, S), np.nan)
    band = np.full((T, S), np.nan)
    for t in range(T):
        m = pool[t] & np.isfinite(vol[t])
        if m.sum() >= 500:
            qs = np.nanpercentile(vol[t][m], [10, 20, 30, 40, 50, 60, 70, 80, 90])
            dec[t] = np.where(m, np.searchsorted(qs, vol[t]), np.nan)
        row = mom[t][pool[t]]
        if row.size >= 500:
            thr = np.nanpercentile(row, 99.8)
            band[t] = np.where(pool[t] & np.isfinite(mom[t]), mom[t] >= thr, np.nan)
    v10 = (dec == 9) & ok
    sub = v10 & (band == 1.0)
    keep1 = ~lock_entry
    keep_all = np.ones((T, S), dtype=bool)

    ex_sl5 = a1.vector_exit_v2(mats, 0.05, None)
    ex_hold = a1.vector_exit_v2(mats, None, None)

    res: dict = {"seed": SEED, "panel": {"dates": int(T), "symbols": int(S),
                                         "date_min": dates[0], "date_max": dates[-1],
                                         "ok_cells": int(ok.sum()),
                                         "sub_cells": int(sub.sum())}}
    print(f"panel T={T} S={S} ok={int(ok.sum()):,} sub={int(sub.sum()):,}  "
          f"{dates[0]}..{dates[-1]}")

    # ================================================================== G1
    # The label pays a gap the trade cannot capture.
    trade_fwd = ratio(lead(c, 20), lead(o, 1))   # c[t+20]/o[t+1]-1
    basis = ok & np.isfinite(trade_fwd) & np.isfinite(fwd)
    ev_lab = basis & (fwd >= TAIL)
    ev_trd = basis & (trade_fwd >= TAIL)
    v10b = v10 & basis
    sub_b = sub & basis
    gap_ret = ratio(lead(o, 1), c)               # o[t+1]/c[t]-1

    res["G1_label_vs_trade"] = {
        "basis_cells": int(basis.sum()),
        "sub_cells": int(sub_b.sum()),
        "base_pct_label": round(float((ev_lab & basis).sum() / basis.sum() * 100), 4),
        "base_pct_trade": round(float((ev_trd & basis).sum() / basis.sum() * 100), 4),
        "sub_events_label": int((ev_lab & sub_b).sum()),
        "sub_events_trade": int((ev_trd & sub_b).sum()),
        "sub_events_both": int((ev_lab & ev_trd & sub_b).sum()),
    }
    print(f"\n[G1] basis={int(basis.sum()):,}  base P(label >=90%)="
          f"{res['G1_label_vs_trade']['base_pct_label']:.4f}%  "
          f"P(trade o[t+1]->c[t+20] >=90%)="
          f"{res['G1_label_vs_trade']['base_pct_trade']:.4f}%")

    cfgs = [
        ("label  c[t]->c[t+20]", ev_lab, keep_all),
        ("label  c[t]->c[t+20] |L1", ev_lab, keep1),
        ("trade  o[t+1]->c[t+20]", ev_trd, keep_all),
        ("trade  o[t+1]->c[t+20] |L1", ev_trd, keep1),
    ]
    res["G1_lift"] = {}
    print(f"  {'label definition':30s} {'P(sub)%':>9} {'95% CI':>20} "
          f"{'lift':>7} {'CI':>14} {'P(<=1)':>8}")
    for name, ev, kp in cfgs:
        r = lift(kp, sub_b, ev, v10b, T)
        res["G1_lift"][name] = r
        print(f"  {name:30s} {r['sub_pct']:>9.4f} "
              f"[{r['sub_pct_ci'][0]:>8.4f},{r['sub_pct_ci'][1]:>8.4f}] "
              f"{r['lift']:>7.2f} [{r['lift_ci'][0]:>5.2f},{r['lift_ci'][1]:>5.2f}] "
              f"{r['lift_p_le1']:>8.4f}")

    # what the gap does to the event set
    lab_only = ev_lab & ~ev_trd & sub_b
    trd_only = ev_trd & ~ev_lab & sub_b
    gf = gap_ret[ev_lab & sub_b]
    gf = gf[np.isfinite(gf)]
    tf = trade_fwd[ev_lab & sub_b]
    tf = tf[np.isfinite(tf)]
    lf = fwd[ev_lab & sub_b]
    lf = lf[np.isfinite(lf)]
    res["G1_event_gap"] = {
        "label_events_in_sub": int((ev_lab & sub_b).sum()),
        "label_only_events": int(lab_only.sum()),
        "trade_only_events": int(trd_only.sum()),
        "label_event_mean_entry_gap_bp": round(float(gf.mean()) * 1e4, 1),
        "label_event_median_entry_gap_bp": round(float(np.median(gf)) * 1e4, 1),
        "label_event_mean_label_ret_bp": round(float(lf.mean()) * 1e4, 1),
        "label_event_mean_trade_ret_bp": round(float(tf.mean()) * 1e4, 1),
        "label_event_median_trade_ret_bp": round(float(np.median(tf)) * 1e4, 1),
    }
    eg = res["G1_event_gap"]
    print(f"  label events in sub={eg['label_events_in_sub']} "
          f"(label-only {eg['label_only_events']}, trade-only {eg['trade_only_events']}); "
          f"mean entry gap {eg['label_event_mean_entry_gap_bp']:+.0f}bp, "
          f"label ret {eg['label_event_mean_label_ret_bp']:+.0f}bp vs "
          f"trade ret {eg['label_event_mean_trade_ret_bp']:+.0f}bp")

    # ================================================================== G2
    # Fill is not binary: locked vs limit-up-but-opened vs not-limit-up.
    lu, _ld = limit_grids(dates, symbols)
    entry_lu = np.zeros((T, S), dtype=bool)
    entry_lu[:-1] = lu[1:] > 0
    cls_locked = sub & lock_entry
    cls_opened = sub & entry_lu & ~lock_entry
    cls_plain = sub & ~entry_lu

    def cls_stats(mask: np.ndarray) -> dict:
        m = mask & np.isfinite(trade_fwd) & np.isfinite(ex_sl5)
        n = int(m.sum())
        if n == 0:
            return {"cells": 0}
        tfr = trade_fwd[m]
        slr = ex_sl5[m]
        ev = (fwd[m] >= TAIL)
        return {
            "cells": n,
            "mean_trade_fwd_bp": round(float(np.mean(tfr)) * 1e4, 1),
            "median_trade_fwd_bp": round(float(np.median(tfr)) * 1e4, 1),
            "mean_sl5_bp": round(float(np.nanmean(slr)) * 1e4, 1),
            "win_sl5": round(float(np.nanmean(slr > 0)), 3),
            "tail_event_pct": round(float(ev.mean() * 100), 4),
        }

    res["G2_entry_class"] = {
        "locked": cls_stats(cls_locked),
        "limit_up_opened": cls_stats(cls_opened),
        "not_limit_up": cls_stats(cls_plain),
    }
    print("\n[G2] strategy basket split by ENTRY-day class (t+1):")
    print(f"  {'class':22s} {'cells':>6} {'mean trd fwd':>13} {'med trd fwd':>12} "
          f"{'mean SL5%':>10} {'win':>6} {'P(tail)%':>9}")
    for k, v in res["G2_entry_class"].items():
        if v["cells"] == 0:
            print(f"  {k:22s} {0:>6}")
            continue
        print(f"  {k:22s} {v['cells']:>6} {v['mean_trade_fwd_bp']:>13.0f} "
              f"{v['median_trade_fwd_bp']:>12.0f} {v['mean_sl5_bp']:>10.0f} "
              f"{v['win_sl5']:>6.2f} {v['tail_event_pct']:>9.3f}")

    # per-cohort paired gap: opened-limit-up minus not-limit-up
    po, pp = [], []
    for t in range(T - 21):
        mb = cls_opened[t] & np.isfinite(trade_fwd[t])
        mc = cls_plain[t] & np.isfinite(trade_fwd[t])
        if mb.sum() and mc.sum():
            po.append(float(np.mean(trade_fwd[t][mb])))
            pp.append(float(np.mean(trade_fwd[t][mc])))
    d = paired_delta(np.array(po), np.array(pp), lambda x: float(np.mean(x)))
    res["G2_opened_minus_plain"] = d
    if d:
        print(f"  per-cohort gap (opened limit-up - not limit-up), trade fwd: "
              f"{d['delta_bp']:+.0f}bp [{d['ci5_bp']:+.0f},{d['ci95_bp']:+.0f}] "
              f"P(>0)={d['p_gt0']:.3f} n={d['n']}")

    # ================================================================== G3
    # Who owns the L0-L1 delta: per-episode decomposition + leave-one-out.
    lk_t, lk_s = np.nonzero(cls_locked)
    eps = runs_of(list(zip(lk_t.tolist(), sym_arr[lk_s].tolist())))
    ev_t, ev_s = np.nonzero(ev_lab & sub_b & lock_entry)
    res["G3a_concentration"] = {
        "locked_cells_in_sub": int(cls_locked.sum()),
        "locked_symbols": int(len(set(sym_arr[lk_s].tolist()))),
        "locked_episodes": int(len(eps)),
        "locked_tail_events": int((ev_lab & sub_b & lock_entry).sum()),
        "locked_tail_events_symbols": int(len(set(sym_arr[ev_s].tolist()))),
        "locked_tail_events_episodes": int(len(runs_of(
            list(zip(ev_t.tolist(), sym_arr[ev_s].tolist()))))),
        "episode_sizes": [{"symbol": e["symbol"], "rows": len(e["rows"])} for e in eps],
    }
    c3 = res["G3a_concentration"]
    print(f"\n[G3] locked cells in sub={c3['locked_cells_in_sub']} "
          f"-> {c3['locked_symbols']} symbols / {c3['locked_episodes']} episodes; "
          f"tail events among them={c3['locked_tail_events']} "
          f"({c3['locked_tail_events_symbols']} symbols / "
          f"{c3['locked_tail_events_episodes']} episodes)")

    coh = []
    for t in range(T - 21):
        sel = np.flatnonzero(sub[t] & np.isfinite(ex_sl5[t]))
        if sel.size:
            coh.append((t, ex_sl5[t][sel], lock_entry[t][sel], sym_arr[sel]))
    n_aff = sum(1 for _, _, lk, _ in coh if lk.any())
    res["G3b_affected_cohorts"] = int(n_aff)
    print(f"  cohorts containing >=1 locked cell: {n_aff} / {len(coh)}")

    # Fixed cohort set for every CE in G3, so the variants are comparable.
    idx = [i for i, (_t, _r, lk, _s) in enumerate(coh) if (~lk).any()]
    p0 = np.array([float(np.mean(coh[i][1])) for i in idx]) - COST
    p1 = np.array([float(np.mean(coh[i][1][~coh[i][2]])) for i in idx]) - COST
    delta = paired_delta(p0, p1, lambda x: ce(x, 1.0))
    res["G3c_parity_delta"] = delta
    if delta:
        print(f"  parity L0-L1 (SL5%, g1): {delta['delta_bp']:+.1f}bp "
              f"[{delta['ci5_bp']:+.1f},{delta['ci95_bp']:+.1f}] "
              f"P(>0)={delta['p_gt0']:.3f}  (doc 12.13: +64.3 [9.7,148.0] P=0.997)")

    ce_l1 = ce(p1, 1.0)
    ce_l0 = ce(p0, 1.0)
    rows = []
    print(f"  {'episode':14s} {'locked':>6} {'mean SL5%':>12} "
          f"{'delta w/o episode':>19} {'contribution':>13}")
    for e in eps:
        ex = {(t, e["symbol"]) for t in e["rows"]}
        vals = []
        rr = []
        for i in idx:
            t, r, lk, syms = coh[i]
            drop = np.array([lk[j] and (t, s) in ex for j, s in enumerate(syms)])
            v = r[~drop]
            vals.append(float(np.mean(v)) if v.size else float(p1[i] + COST))
            for j, s in enumerate(syms):
                if lk[j] and (t, s) in ex:
                    rr.append(float(r[j]))
        v_arr = np.array(vals) - COST
        drop_delta = (ce(v_arr, 1.0) - ce_l1) * 1e4
        contrib = (ce_l0 - ce_l1) * 1e4 - drop_delta
        rows.append({"symbol": e["symbol"], "locked_cells": len(e["rows"]),
                     "mean_sl5_bp": round(float(np.mean(rr)) * 1e4, 1) if rr else None,
                     "delta_without_episode_bp": round(float(drop_delta), 1),
                     "contribution_bp": round(float(contrib), 1)})
        print(f"  {e['symbol']:14s} {len(e['rows']):>6} "
              f"{(rows[-1]['mean_sl5_bp'] or 0.0):>12.0f} "
              f"{rows[-1]['delta_without_episode_bp']:>19.1f} "
              f"{rows[-1]['contribution_bp']:>13.1f}")
    res["G3d_episode_leaveout"] = rows
    if delta:
        res["G3d_full_delta_bp"] = delta["delta_bp"]

    # what the one-price cells are worth without the CE machinery: cell means
    onep = (ex_sl5[cls_locked] * 1e4)
    onep = onep[np.isfinite(onep)]
    other = ex_sl5[sub & ~lock_entry] * 1e4
    other = other[np.isfinite(other)]
    res["G3e_cellmeans"] = {
        "locked_n": int(onep.size), "locked_mean_bp": round(float(onep.mean()), 1),
        "locked_median_bp": round(float(np.median(onep)), 1),
        "fillable_n": int(other.size), "fillable_mean_bp": round(float(other.mean()), 1),
        "fillable_median_bp": round(float(np.median(other)), 1),
        "basket_mean_bp": round(float(np.mean(np.concatenate([onep, other]))), 1),
        "locked_share_of_basket_pct": round(float(onep.size / (onep.size + other.size) * 100), 2),
    }

    # ================================================================== G5
    # The 09:30-knowable selection nobody tested: prior rounds used "opened at
    # the limit" only as an EXCLUSION (L2).  As a SELECTION it is a limit-order
    # queue strategy.  Board-agnostic detector: the entry day closes at
    # limit-up and the open equals the day's high, so the open IS the limit
    # price.  A queued order fills only if the board trades off (not one-price).
    open_at_limit = np.zeros((T, S), dtype=bool)
    open_at_limit[:-1] = (lu[1:] > 0) & np.isclose(o[1:], h[1:], rtol=0.0, atol=1e-9)
    sel_open = sub & open_at_limit
    sel_fill = sel_open & ~lock_entry
    sel_closed_lu = sub & entry_lu & ~lock_entry   # needs the close, not 09:30

    def ce_series(mask: np.ndarray) -> tuple[np.ndarray, dict]:
        vals, cells, events, cohs = [], 0, 0, 0
        for t in range(T - 21):
            m = mask[t] & np.isfinite(ex_sl5[t])
            if not m.any():
                continue
            cohs += 1
            cells += int(m.sum())
            events += int((fwd[t][m] >= TAIL).sum())
            vals.append(float(np.mean(ex_sl5[t][m])))
        info = {"cells": cells, "cohorts": cohs, "events": events}
        return np.array(vals) - COST, info

    res["G5_selection"] = {}
    for name, mask in (("basket_all", sub), ("open_at_limit_all", sel_open),
                       ("open_at_limit_fillable", sel_fill),
                       ("closed_limit_up_not_locked", sel_closed_lu)):
        v, info = ce_series(mask)
        rec = dict(info)
        if v.size:
            rec["mean_bp"] = round(float(np.mean(v)) * 1e4, 1)
            rec["ce_g1_bp"] = round(float(ce(v, 1.0)) * 1e4, 1)
            ci = circ_ci(v, lambda x: ce(x, 1.0))
            if ci:
                rec["ci5_bp"] = ci["ci5_bp"]
                rec["ci95_bp"] = ci["ci95_bp"]
                rec["p_le0"] = ci["p_le0"]
        res["G5_selection"][name] = rec
    print("\n[G5] 09:30-knowable limit-open selection (net of 20bp cost):")
    print(f"  {'selection':28s} {'cells':>6} {'cohorts':>8} {'ev':>4} "
          f"{'mean':>7} {'CE g1':>8} {'95% CI':>20} {'P(<=0)':>7}")
    for name, v in res["G5_selection"].items():
        ci = ("[%+.0f,%+.0f]" % (v["ci5_bp"], v["ci95_bp"])) if "ci5_bp" in v else "n/a"
        print(f"  {name:28s} {v['cells']:>6} {v['cohorts']:>8} {v['events']:>4} "
              f"{v.get('mean_bp', float('nan')):>7.0f} {v.get('ce_g1_bp', float('nan')):>8.1f} "
              f"{ci:>20} {v.get('p_le0', float('nan')):>7.3f}")

    # throughput of the only set that fills with no assumption: open at the
    # limit AND traded off it (so a limit-price buy order is filled).
    fill_t, fill_s = np.nonzero(sel_fill)
    fills = []
    for t, s in zip(fill_t.tolist(), fill_s.tolist()):
        if not np.isfinite(ex_sl5[t, s]):
            continue
        fills.append({"date": str(dates[t])[:10], "symbol": symbols[s],
                      "sl5_bp": round(float(ex_sl5[t, s]) * 1e4, 1),
                      "trade_fwd_bp": round(float(trade_fwd[t, s]) * 1e4, 1)})
    res["G5_fills"] = fills
    res["G5_throughput"] = {
        "fill_cells": len(fills),
        "sessions": int(T),
        "fills_per_100_sessions": round(len(fills) / T * 100, 2),
        "one_per_n_sessions": round(T / len(fills), 1) if fills else None,
        "expected_concurrent_positions_hold20": round(len(fills) / T * 20, 2),
        "mean_bp": round(float(np.mean([f["sl5_bp"] for f in fills])), 1) if fills else None,
        "max_bp": round(float(np.max([f["sl5_bp"] for f in fills])), 1) if fills else None,
        "n_positive": int(sum(1 for f in fills if f["sl5_bp"] > 0)),
    }
    th = res["G5_throughput"]
    print(f"  fillable-at-open cells={th['fill_cells']} over {th['sessions']} sessions "
          f"(1 per {th['one_per_n_sessions']} sessions, ~{th['expected_concurrent_positions_hold20']} "
          f"concurrent slots at hold20), mean {th['mean_bp']}bp, "
          f"{th['n_positive']}/{th['fill_cells']} positive")

    # ================================================================== G6
    # Are the 62 "one-price" entry days limit-up boards, or just non-trading?
    vol_files = sorted(glob.glob(str(ap.DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    vdf = pl.concat([pl.read_parquet(f, columns=["symbol", "date", "volume"])
                     for f in vol_files], how="vertical_relaxed")
    vdf = vdf.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
    entry_vol = lead(ac.grid_of(vdf, "volume", dates, symbols), 1)

    lk_all_t, lk_all_s = np.nonzero(cls_locked)
    buckets: dict[str, list[float]] = {"no_trade_volume0": [], "limit_up_flag": [],
                                       "one_price_not_flagged": []}
    for t, s in zip(lk_all_t.tolist(), lk_all_s.tolist()):
        v = entry_vol[t, s]
        r = float(ex_sl5[t, s]) * 1e4 if np.isfinite(ex_sl5[t, s]) else None
        if not np.isfinite(v) or v <= 0:
            buckets["no_trade_volume0"].append(r if r is not None else np.nan)
        elif lu[t + 1, s] > 0:
            buckets["limit_up_flag"].append(r if r is not None else np.nan)
        else:
            buckets["one_price_not_flagged"].append(r if r is not None else np.nan)
    res["G6_locked_kind"] = {}
    print("\n[G6] what the 62 L1 one-price entry days actually are:")
    for k, v in buckets.items():
        a = np.array(v, dtype=np.float64)
        a = a[np.isfinite(a)]
        res["G6_locked_kind"][k] = {
            "cells": len(v),
            "finite_returns": int(a.size),
            "mean_sl5_bp": round(float(a.mean()), 1) if a.size else None,
            "n_positive": int((a > 0).sum()) if a.size else 0,
        }
        print(f"  {k:24s} cells={len(v):>3} mean SL5%="
              f"{(round(float(a.mean()), 1) if a.size else float('nan')):>9}bp "
              f"positive={int((a > 0).sum()) if a.size else 0}/{a.size}")
    res["G6_note"] = ("one_price also fires on suspended bars (o=h=l=c, volume 0), "
                      "which are unbuyable for a different reason than a locked board")

    # G6b: the exclusion filter, split by reason.  L1 removes 62 one-price
    # cells; only 42 of them are flagged limit-up boards.
    def ce_net(mask: np.ndarray) -> tuple[np.ndarray, dict | None]:
        vals = []
        for t in range(T - 21):
            m = sub[t] & mask[t] & np.isfinite(ex_sl5[t])
            if m.any():
                vals.append(float(np.mean(ex_sl5[t][m])))
        v = np.array(vals) - COST
        return v, circ_ci(v, lambda x: ce(x, 1.0))

    keep1a = ~(lock_entry & entry_lu)
    res["G6b_ce_by_filter"] = {}
    print(f"  {'filter':34s} {'CE g1':>8} {'95% CI':>20} {'P(<=0)':>7}")
    for name, mk in (("L0 all cells", keep_all),
                     ("L1 exclude all one-price", keep1),
                     ("L1a exclude flagged locked only", keep1a)):
        v, ci = ce_net(mk)
        rec = {"cells": int((sub & mk).sum()),
               "ce_g1_bp": round(float(ce(v, 1.0)) * 1e4, 1)}
        if ci:
            rec.update({"ci5_bp": ci["ci5_bp"], "ci95_bp": ci["ci95_bp"],
                        "p_le0": ci["p_le0"], "cohorts": ci["n"]})
        res["G6b_ce_by_filter"][name] = rec
        cis = ("[%+.1f,%+.1f]" % (ci["ci5_bp"], ci["ci95_bp"])) if ci else "n/a"
        print(f"  {name:34s} {rec['ce_g1_bp']:>8.1f} {cis:>20} "
              f"{rec.get('p_le0', float('nan')):>7.3f}")

    # ================================================================== G4
    part_dirs = sorted(glob.glob("data/kline_daily_enriched/date=*"))
    last_part = part_dirs[-1].rsplit("date=", 1)[1] if part_dirs else None
    minute_dirs = sorted(glob.glob("data/kline_minute/date=*"))
    minute_dates = set(d.rsplit("date=", 1)[1] for d in minute_dirs)
    depth_files = [f for f in glob.glob("data/depth5/**/*", recursive=True)
                   if Path(f).is_file()]
    ev_dates = sorted({str(dates[t])[:10] for t in ev_t.tolist()})
    covered = sorted(d for d in ev_dates if d in minute_dates)

    def sessions_after(start: str, k: int) -> str:
        d = date.fromisoformat(start)
        n = 0
        while n < k:
            d += timedelta(days=1)
            if d.weekday() < 5:
                n += 1
        return d.isoformat()

    res["G4_resources"] = {
        "daily_partitions": len(part_dirs),
        "daily_last_date": last_part,
        "daily_last_readable_date": dates[-1],
        "depth5_files": len(depth_files),
        "minute_partition_dates": len(minute_dates),
        "minute_first": min(minute_dates) if minute_dates else None,
        "minute_last": max(minute_dates) if minute_dates else None,
        "locked_event_dates": ev_dates,
        "locked_event_dates_covered_by_minute": covered,
        "first_evaluable_oos_signal_date": sessions_after(dates[-1], 20),
        "sessions_needed_by_doc_12_12_D": 730,
        "sessions_to_730_more_weekdays": sessions_after(dates[-1], 730),
    }
    g4 = res["G4_resources"]
    print(f"\n[G4] daily partitions={g4['daily_partitions']} last={g4['daily_last_date']}  "
          f"depth5 files={g4['depth5_files']}  "
          f"minute dates={g4['minute_partition_dates']} "
          f"({g4['minute_first']}..{g4['minute_last']})")
    print(f"  locked event dates {ev_dates[0]}..{ev_dates[-1]} "
          f"({len(ev_dates)} sessions), covered by minute data: {len(covered)}")
    print(f"  first evaluable OOS signal date (panel end + 20 sessions) = "
          f"{g4['first_evaluable_oos_signal_date']}")
    print(f"  panel end + 730 sessions (doc 12.12 D) = "
          f"{g4['sessions_to_730_more_weekdays']}")

    out = Path("analysis/_diag_r9_gate_result.json")
    out.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
