"""Round 4 audit, part 3: stress the fillability verdict itself.

Part 2 (_diag_ce_tail_audit2.py, doc 12.12) replaced the closing reason
"too few events / ~1,450 sessions" with "there is no money on the fillable
subset".  That is a stronger claim than "no rule cleared CI>0", and it is
testable on its own terms:

  B1. L1 SL5% net CE is -38.0bp.  What is its own CI?  Does the L0->L1 flip
      itself clear zero in a PAIRED cohort bootstrap (same resampled indices)?
  B2. Does the right-tail lift survive L1?  Part 2 reported point estimates
      only (7.90x vs base, 2.78x vs vol10).  Add the cohort-cluster CI.
  B3. Are the 12 one-price entry days what the claim says they are:
      limit-UP boards (not limit-down, not suspensions)?  Check the calendar
      grid AND the symbol's own raw next-trading-day row.
  B4. L2 uses a flat 9.8% gap threshold.  On 20%-limit boards that is not a
      board.  How many gap-up events were actually NOT limit-up (i.e. were
      removed by L2 despite being buyable)?
  B5. If a signal is unbuyable at d+1, does one day of delay (d+2 open)
      still reach the tail?  Diagnostic only, not a verdict: this is the one
      remediation part 2 did not test.

No production code is touched; the v2 engine is imported from part 1 and the
mask/decile definitions are copied verbatim from part 2 for comparability.

ASCII only (GBK console).
"""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import _diag_ce_tail_audit as a1  # noqa: E402  (vector_exit_v2)
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261112
TAIL = 0.90
COST = 0.002
BLOCK = 20
NB = 2000
GAMMAS = (1.0, 5.0)
RNG = np.random.default_rng(SEED)

ce = ac.ce_crra


def circ_ci(x: np.ndarray, fn, block: int = BLOCK, nb: int = NB) -> dict | None:
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


def paired_delta(p0: np.ndarray, p1: np.ndarray, fn, block: int = BLOCK,
                 nb: int = NB) -> dict | None:
    ok = np.isfinite(p0) & np.isfinite(p1)
    p0, p1 = p0[ok], p1[ok]
    n = p0.size
    if n < 60:
        return None
    m = int(np.ceil(n / block))
    off = np.arange(block)
    out = np.empty(nb)
    for i in range(nb):
        s = RNG.integers(0, n, m)
        idx = ((s[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = fn(p0[idx]) - fn(p1[idx])
    o = out[np.isfinite(out)]
    return {"n": int(n), "delta_bp": round(float(fn(p0) - fn(p1)) * 1e4, 1),
            "ci5_bp": round(float(np.percentile(o, 2.5)) * 1e4, 1),
            "ci95_bp": round(float(np.percentile(o, 97.5)) * 1e4, 1),
            "p_gt0": round(float((o > 0.0).mean()), 3)}


def lift_ci(keep: np.ndarray, sub: np.ndarray, out_event: np.ndarray,
            v10: np.ndarray, T: int) -> dict:
    """Cohort-cluster circular block bootstrap of the sub rate and lift vs vol10."""
    c_ns, c_es, c_nv, c_ev = [], [], [], []
    for t in range(T - 21):
        n_s = int((sub[t] & keep[t]).sum())
        if n_s == 0:
            continue
        c_ns.append(n_s)
        c_es.append(int((out_event[t] & sub[t] & keep[t]).sum()))
        c_nv.append(int((v10[t] & keep[t]).sum()))
        c_ev.append(int((out_event[t] & v10[t] & keep[t]).sum()))
    c_ns = np.array(c_ns); c_es = np.array(c_es)
    c_nv = np.array(c_nv); c_ev = np.array(c_ev)
    m = c_ns.size
    nblk = int(np.ceil(m / BLOCK))
    off = np.arange(BLOCK)
    pr = np.empty(NB)
    lf = np.empty(NB)
    for i in range(NB):
        s = RNG.integers(0, m, nblk)
        idx = ((s[:, None] + off[None, :]) % m).ravel()[:m]
        ns, es = c_ns[idx].sum(), c_es[idx].sum()
        nv, ev = c_nv[idx].sum(), c_ev[idx].sum()
        pr[i] = es / max(ns, 1)
        lf[i] = (es / max(ns, 1)) / max(ev / max(nv, 1), 1e-12) if nv else np.nan
    pr = pr[np.isfinite(pr)]
    lf = lf[np.isfinite(lf)]
    p_sub = c_es.sum() / c_ns.sum()
    return {
        "cohorts": int(m),
        "sub_pct": round(float(p_sub * 100), 4),
        "sub_pct_ci": [round(float(np.percentile(pr, 2.5) * 100), 4),
                       round(float(np.percentile(pr, 97.5) * 100), 4)],
        "lift_vs_vol10": round(float(p_sub / (c_ev.sum() / c_nv.sum())), 2),
        "lift_ci": [round(float(np.percentile(lf, 2.5)), 2),
                    round(float(np.percentile(lf, 97.5)), 2)],
        "lift_p_le1": round(float((lf <= 1.0).mean()), 4),
    }


def limit_grids(dates: list[str], symbols: list[str]):
    files = sorted(glob.glob(str(ap.DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    frames = [pl.read_parquet(f, columns=["symbol", "date", "consecutive_limit_ups",
                                          "consecutive_limit_downs"]) for f in files]
    src = pl.concat(frames, how="vertical_relaxed")
    src = src.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
    lu = ac.grid_of(src, "consecutive_limit_ups", dates, symbols)
    ld = ac.grid_of(src, "consecutive_limit_downs", dates, symbols)
    return np.nan_to_num(lu, nan=0.0), np.nan_to_num(ld, nan=0.0)


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
    lu, ld = limit_grids(dates, symbols)

    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    both = np.isfinite(h) & np.isfinite(l)
    one_price = np.zeros((T, S), dtype=bool)
    one_price[both] = np.isclose(h[both], l[both], rtol=0.0, atol=1e-9)
    prev_c = np.full_like(c, np.nan)
    prev_c[1:] = c[:-1]
    gap_entry = np.zeros((T, S), dtype=bool)
    gap_entry[:-1] = (np.isfinite(o[1:]) & np.isfinite(prev_c[:-1])
                      & (o[1:] >= prev_c[:-1] * 1.098))
    lock_entry = np.zeros((T, S), dtype=bool)
    lock_entry[:-1] = one_price[1:]

    pool = (np.isfinite(c) & (c > 0) & np.isfinite(turn) & (turn > 0))
    ok = pool & np.isfinite(fwd) & np.isfinite(mom)
    out_event = np.isfinite(fwd) & (fwd >= TAIL)

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

    res: dict = {"seed": SEED}
    res["B0_panel"] = {
        "dates": int(T), "symbols": int(S), "ok_cells": int(ok.sum()),
        "sub_cells": int(sub.sum()),
        "sub_events": int((out_event & sub).sum()),
        "date_min": dates[0], "date_max": dates[-1],
    }
    print(f"panel T={T} S={S} ok={int(ok.sum()):,} sub={int(sub.sum()):,} "
          f"events={int((out_event & sub).sum())}  {dates[0]}..{dates[-1]}")

    # ---------------- B1: CE, CIs and the L0-L1 flip -----------------------
    exits = ac.EXITS
    ret_by = {lab: a1.vector_exit_v2(mats, sl, tp) for lab, sl, tp in exits}
    keep1 = ~lock_entry

    rows = []
    for t in range(T - 21):
        sel = np.flatnonzero(sub[t])
        if sel.size == 0:
            continue
        rec = {"i0": t}
        for lab, _s, _t in exits:
            r = ret_by[lab][t]
            g0 = sel[np.isfinite(r[sel])]
            rec[f"p0_{lab}"] = float(np.mean(r[g0])) if g0.size else np.nan
            g1 = sel[np.isfinite(r[sel]) & keep1[t][sel]]
            rec[f"p1_{lab}"] = float(np.mean(r[g1])) if g1.size else np.nan
        rows.append(rec)
    n_coh = len(rows)
    print(f"cohorts={n_coh}")

    res["B1_ce"] = {}
    for lab, _s, _t in exits:
        p0 = np.array([r[f"p0_{lab}"] for r in rows], dtype=np.float64)
        p1 = np.array([r[f"p1_{lab}"] for r in rows], dtype=np.float64)
        rec = {"L0": circ_ci(p0 - COST, lambda x: ce(x, 1.0)),
               "L1": circ_ci(p1 - COST, lambda x: ce(x, 1.0)),
               "paired_L0_minus_L1": paired_delta(p0 - COST, p1 - COST,
                                                  lambda x: ce(x, 1.0))}
        res["B1_ce"][lab] = rec
        l1 = rec["L1"]; pd = rec["paired_L0_minus_L1"]
        print(f"  {lab:16s} L1 net={l1['est_bp']:+7.1f} "
              f"[{l1['ci5_bp']:+7.1f},{l1['ci95_bp']:+7.1f}] P(<=0)={l1['p_le0']:.2f}"
              f"  | L0-L1={pd['delta_bp']:+7.1f} "
              f"[{pd['ci5_bp']:+7.1f},{pd['ci95_bp']:+7.1f}] P(>0)={pd['p_gt0']:.3f}")

    # ---------------- B2: does the lift survive L1? ------------------------
    res["B2_lift_L1"] = lift_ci(keep1, sub, out_event, v10, T)
    r2 = res["B2_lift_L1"]
    print(f"L1 lift: P={r2['sub_pct']:.4f}% CI {r2['sub_pct_ci']} "
          f"lift_vs_vol10={r2['lift_vs_vol10']:.2f}x CI {r2['lift_ci']} "
          f"P(<=1)={r2['lift_p_le1']}")

    # ---------------- B3: the one-price events, raw and grid ---------------
    ev_t, ev_s = np.nonzero(out_event & sub)
    n_ev = ev_t.size
    onep_ev = lock_entry[ev_t, ev_s]
    lu_ev = lu[ev_t + 1, ev_s] > 0
    ld_ev = ld[ev_t + 1, ev_s] > 0
    res["B3_events"] = {
        "events": int(n_ev),
        "oneprice_events": int(onep_ev.sum()),
        "oneprice_limitup_entryday": int((lu_ev & onep_ev).sum()),
        "oneprice_limitdown_entryday": int((ld_ev & onep_ev).sum()),
    }

    raw_cols = ["symbol", "date", "raw_high", "raw_low", "raw_close"]
    files = sorted(glob.glob(str(ap.DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    rawdf = pl.concat([pl.read_parquet(f, columns=raw_cols) for f in files],
                      how="vertical_relaxed")
    rawdf = rawdf.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
    dfd = (df.select(["symbol", "date", "high", "low"])
           .with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
           .join(rawdf, on=["symbol", "date"], how="left")
           .sort(["symbol", "date"]))
    sym_rows: dict[str, dict] = {}
    for key, part in dfd.partition_by("symbol", as_dict=True).items():
        sym = key[0] if isinstance(key, tuple) else key
        sym_rows[str(sym)] = {
            "date": [str(x)[:10] for x in part["date"].to_list()],
            "high": part["high"].to_numpy(),
            "low": part["low"].to_numpy(),
            "rhigh": part["raw_high"].to_numpy(),
            "rlow": part["raw_low"].to_numpy(),
            "rclose": part["raw_close"].to_numpy(),
        }
    mism = 0
    raw_one = 0
    for k in np.flatnonzero(onep_ev):
        t, s = int(ev_t[k]), int(ev_s[k])
        arr = sym_rows.get(symbols[s])
        if arr is None:
            continue
        dd = arr["date"]
        d0 = str(dates[t])[:10]
        j = dd.index(d0) if d0 in dd else -1
        if j < 0 or j + 1 >= len(dd):
            continue
        jj = j + 1
        adj_op = bool(np.isclose(arr["high"][jj], arr["low"][jj], rtol=0.0, atol=1e-9))
        raw_op = bool(np.isclose(arr["rhigh"][jj], arr["rlow"][jj], rtol=0.0, atol=1e-9))
        raw_one += int(raw_op)
        mism += int(adj_op != raw_op)
    res["B3_events"]["raw_one_price_events"] = int(raw_one)
    res["B3_events"]["adjusted_vs_raw_mismatch"] = int(mism)
    print(f"events={n_ev} one-price={int(onep_ev.sum())} "
          f"(limit-up {int((lu_ev & onep_ev).sum())}, "
          f"limit-down {int((ld_ev & onep_ev).sum())})  "
          f"raw one-price={raw_one} adj/raw mismatch={mism}")

    sl5 = ret_by["SL5%"]
    hold = ret_by["hold20 (no SL/TP)"]
    r_onep = sl5[ev_t, ev_s][onep_ev]
    r_rest = sl5[ev_t, ev_s][~onep_ev]
    res["B3_returns"] = {
        "oneprice_sl5_returns_bp": [round(float(x) * 1e4, 1) for x in r_onep],
        "oneprice_mean_sl5_bp": round(float(np.nanmean(r_onep)) * 1e4, 1),
        "oneprice_median_sl5_bp": round(float(np.nanmedian(r_onep)) * 1e4, 1),
        "oneprice_win_sl5": round(float(np.nanmean(r_onep > 0)), 3),
        "other_events_mean_sl5_bp": round(float(np.nanmean(r_rest)) * 1e4, 1),
        "other_events_median_sl5_bp": round(float(np.nanmedian(r_rest)) * 1e4, 1),
        "other_events_win_sl5": round(float(np.nanmean(r_rest > 0)), 3),
        "oneprice_mean_hold_bp": round(float(np.nanmean(hold[ev_t, ev_s][onep_ev])) * 1e4, 1),
        "other_events_mean_hold_bp": round(float(np.nanmean(hold[ev_t, ev_s][~onep_ev])) * 1e4, 1),
        "oneprice_sl5_equals_hold": int(np.sum(np.isclose(
            sl5[ev_t, ev_s][onep_ev], hold[ev_t, ev_s][onep_ev],
            rtol=0.0, atol=1e-12))),
    }
    br = res["B3_returns"]
    print(f"  SL5% cell returns: one-price mean {br['oneprice_mean_sl5_bp']:+.0f}bp "
          f"median {br['oneprice_median_sl5_bp']:+.0f}bp; "
          f"other events mean {br['other_events_mean_sl5_bp']:+.0f}bp "
          f"median {br['other_events_median_sl5_bp']:+.0f}bp")

    # ---- B3b: concentration of the 62 events across symbols / episodes ----
    def concentration(symbols_arr: np.ndarray, t_arr: np.ndarray) -> dict:
        by: dict[str, list[int]] = {}
        for t, s in zip(t_arr, symbols_arr):
            by.setdefault(s, []).append(int(t))
        episodes = 0
        for ts in by.values():
            ts = sorted(ts)
            run = 1
            for a, b in zip(ts, ts[1:]):
                if b != a + 1:
                    run += 1
            episodes += run
        top = max(by.values(), key=len) if by else []
        return {"unique_symbols": len(by), "episodes": episodes,
                "max_events_one_symbol": len(top),
                "total": int(len(t_arr))}

    all_c = concentration(np.asarray(symbols)[ev_s], ev_t)
    onep_c = concentration(np.asarray(symbols)[ev_s][onep_ev], ev_t[onep_ev])
    fill_c = concentration(np.asarray(symbols)[ev_s][~onep_ev], ev_t[~onep_ev])
    res["B3b_concentration"] = {"all_events": all_c, "oneprice_events": onep_c,
                                "fillable_events": fill_c}
    print(f"  events: {all_c['total']} cells, {all_c['unique_symbols']} symbols, "
          f"{all_c['episodes']} day-runs; one-price: {onep_c['total']} cells, "
          f"{onep_c['unique_symbols']} symbols, {onep_c['episodes']} runs "
          f"(max {onep_c['max_events_one_symbol']} from one symbol); "
          f"fillable: {fill_c['total']} cells, {fill_c['unique_symbols']} symbols, "
          f"{fill_c['episodes']} runs")

    # ---------------- B4: L2 threshold vs actual limit-up ------------------
    gap_ev = np.zeros(n_ev, dtype=bool)
    for i in range(n_ev):
        t, s = int(ev_t[i]), int(ev_s[i])
        if t + 1 < T and np.isfinite(o[t + 1, s]) and np.isfinite(c[t, s]) and c[t, s] > 0:
            gap_ev[i] = o[t + 1, s] >= c[t, s] * 1.098
    res["B4_L2_threshold"] = {
        "gapup_events": int(gap_ev.sum()),
        "gapup_but_not_limitup": int((gap_ev & ~lu_ev).sum()),
    }
    print(f"L2: gap-up events {int(gap_ev.sum())}, of which NOT actually limit-up: "
          f"{int((gap_ev & ~lu_ev).sum())}")

    # ---------------- B4b: the L2 mask itself has an off-by-one -----------
    # audit2: gap_entry[t] = o[t+1] >= prev_c[t]*1.098 = c[t-1]*1.098.
    # The entry day's previous close is c[t], not c[t-1].  Reproduce both.
    gap_correct = np.zeros((T, S), dtype=bool)
    gap_lag = np.zeros((T, S), dtype=bool)
    gap_correct[:-1] = (np.isfinite(o[1:]) & np.isfinite(c[:-1])
                        & (o[1:] >= c[:-1] * 1.098))
    gap_lag[:-1] = (np.isfinite(o[1:]) & np.isfinite(prev_c[:-1])
                    & (o[1:] >= prev_c[:-1] * 1.098))
    keep2 = ~gap_correct
    res["B4b_L2_mask"] = {
        "events_gapup_audit_offbyone": int((out_event & sub & gap_lag).sum()),
        "events_gapup_correct": int((out_event & sub & gap_correct).sum()),
        "cells_gapup_audit_offbyone": int((sub & gap_lag).sum()),
        "cells_gapup_correct": int((sub & gap_correct).sum()),
    }
    print(f"L2 mask: events audit={res['B4b_L2_mask']['events_gapup_audit_offbyone']} "
          f"correct={res['B4b_L2_mask']['events_gapup_correct']}  "
          f"cells audit={res['B4b_L2_mask']['cells_gapup_audit_offbyone']} "
          f"correct={res['B4b_L2_mask']['cells_gapup_correct']}")

    rows2 = []
    for t in range(T - 21):
        sel = np.flatnonzero(sub[t])
        if sel.size == 0:
            continue
        rec = {"i0": t}
        for lab, _s, _t in exits:
            r = ret_by[lab][t]
            g2 = sel[np.isfinite(r[sel]) & keep2[t][sel]]
            rec[f"p2_{lab}"] = float(np.mean(r[g2])) if g2.size else np.nan
        rows2.append(rec)
    res["B4b_CE_L2_correct"] = {}
    for lab, _s, _t in exits:
        p2 = np.array([r[f"p2_{lab}"] for r in rows2], dtype=np.float64)
        res["B4b_CE_L2_correct"][lab] = circ_ci(p2 - COST, lambda x: ce(x, 1.0))
        c2 = res["B4b_CE_L2_correct"][lab]
        print(f"  L2corr {lab:16s} net={c2['est_bp']:+7.1f} "
              f"[{c2['ci5_bp']:+7.1f},{c2['ci95_bp']:+7.1f}] P(<=0)={c2['p_le0']:.2f}")

    wide = 0
    for sym, arr in sym_rows.items():
        rc = arr["rclose"]
        rc = rc[np.isfinite(rc)]
        if rc.size < 60:
            continue
        rets = rc[1:] / rc[:-1] - 1.0
        if np.nanpercentile(np.abs(rets), 99.9) > 0.115:
            wide += 1
    res["B4_board"] = {"wide_limit_symbols": int(wide), "symbols": int(len(sym_rows))}
    print(f"symbols with p99.9 |raw c2c| > 11.5%: {wide} / {len(sym_rows)}")

    # ---------------- B5: delayed entry diagnostic -------------------------
    delay = []
    for k in np.flatnonzero(onep_ev):
        t, s = int(ev_t[k]), int(ev_s[k])
        if t + 21 >= T or not (np.isfinite(o[t + 2, s]) and o[t + 2, s] > 0
                               and np.isfinite(c[t + 21, s])):
            continue
        delay.append({
            "i0": int(t), "sym": symbols[s], "d0": str(dates[t])[:10],
            "d2_oneprice": bool(one_price[t + 2, s]),
            "d2_limitup": bool(lu[t + 2, s] > 0),
            "ret_o2_c21_bp": round(float(c[t + 21, s] / o[t + 2, s] - 1.0) * 1e4, 1),
        })
    res["B5_delay"] = {
        "n": len(delay),
        "d2_oneprice": int(sum(d["d2_oneprice"] for d in delay)),
        "mean_ret_bp": round(float(np.mean([d["ret_o2_c21_bp"] for d in delay])), 1) if delay else None,
        "median_ret_bp": round(float(np.median([d["ret_o2_c21_bp"] for d in delay])), 1) if delay else None,
        "events": delay,
    }
    if delay:
        print(f"delay d+2: n={len(delay)} d2 one-price={res['B5_delay']['d2_oneprice']} "
              f"mean {res['B5_delay']['mean_ret_bp']:+.0f}bp "
              f"median {res['B5_delay']['median_ret_bp']:+.0f}bp")

    out = Path("analysis/_diag_ce_tail_verify_result.json")
    out.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
