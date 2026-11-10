"""Round 4 audit, part 2: can the sub-group actually be bought?

The CE verdict assumes entry at `open[d+1]` is available.  The sub-group is
vol20 decile 10 intersected with mom20 top 0.2% -- 20-day winners, i.e. exactly
the population most likely to sit on a locked board.  _diag_fillable.py already
established the A-share fillability convention (a one-price day is a locked
board; a limit-up close needs a queue) but _diag_ce_tail.py never applied it.

The question is not cosmetic.  If the right-tail events are entered on days the
stock could not be bought, then "entry into the tail is predictable" is a
statement about *identifying* an unbuyable stock, and the lift has no economic
content regardless of its CI.

Three fillability layers, each stricter:

    L1 one-price entry day      high[d+1] == low[d+1]        (locked board)
    L2 gap-up entry             open[d+1] >= close[d]*1.098  (open at the board)
    L3 limit-up close entry     consecutive_limit_ups[d+1]>=1

For each layer: how many of the 3,325 sub-group cells / 62 events survive, what
the base rate and lift become, and what the 6 exit rules' net CE becomes under
the gap-aware engine (v2) with the layer applied.  max-over-6 is recalibrated
on the survivors, because the audit in part 1 already moved SL5%.

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
import _diag_ce_tail_audit as a2  # noqa: E402
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261111
TAIL = 0.90
COST = 0.002
BLOCK = 20
NB = 2000
GAMMAS = (1.0, 5.0)
RNG = np.random.default_rng(SEED)

ce = ac.ce_crra


def limit_grids(dates: list[str], symbols: list[str]) -> tuple[np.ndarray, np.ndarray]:
    files = sorted(glob.glob(str(ap.DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    frames = [pl.read_parquet(f, columns=["symbol", "date", "consecutive_limit_ups",
                                          "consecutive_limit_downs"]) for f in files]
    src = pl.concat(frames, how="vertical_relaxed")
    src = src.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
    lu = ac.grid_of(src, "consecutive_limit_ups", dates, symbols)
    ld = ac.grid_of(src, "consecutive_limit_downs", dates, symbols)
    return np.nan_to_num(lu, nan=0.0), np.nan_to_num(ld, nan=0.0)


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
            "p_le0": round(float((o <= 0.0).mean()), 3),
            "drop": round(1.0 - float(o.size / nb), 4)}


def max_over_rules(pm: dict[str, np.ndarray], fn, block: int = BLOCK, nb: int = NB) -> dict | None:
    labels = list(pm)
    ok = np.ones(0, dtype=bool)
    for lab in labels:
        f = np.isfinite(pm[lab])
        ok = f if ok.size == 0 else (ok & f)
    arr = {lab: pm[lab][ok] for lab in labels}
    n = arr[labels[0]].size
    if n < 60:
        return None
    nbk = int(np.ceil(n / block))
    off = np.arange(block)
    obs = max(fn(arr[lab]) for lab in labels)
    out = np.empty(nb)
    for i in range(nb):
        s = RNG.integers(0, n, nbk)
        idx = ((s[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = max(fn(arr[lab][idx]) for lab in labels)
    o = out[np.isfinite(out)]
    return {"n": int(n), "observed_max_bp": round(float(obs) * 1e4, 1),
            "ci5_bp": round(float(np.percentile(o, 2.5)) * 1e4, 1),
            "ci95_bp": round(float(np.percentile(o, 97.5)) * 1e4, 1),
            "p_le0": round(float((o <= 0.0).mean()), 3)}


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
    lu, _ld = limit_grids(dates, symbols)

    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    both = np.isfinite(h) & np.isfinite(l)
    one_price = np.zeros((T, S), dtype=bool)
    one_price[both] = np.isclose(h[both], l[both], rtol=0.0, atol=1e-9)
    prev_c = np.full_like(c, np.nan)
    prev_c[1:] = c[:-1]
    gap_entry = np.zeros((T, S), dtype=bool)          # indexed by SIGNAL day d
    gap_entry[:-1] = (np.isfinite(o[1:]) & np.isfinite(prev_c[:-1])
                      & (o[1:] >= prev_c[:-1] * 1.098))
    lock_entry = np.zeros((T, S), dtype=bool)         # one-price on d+1
    lock_entry[:-1] = one_price[1:]
    lu_entry = np.zeros((T, S), dtype=bool)
    lu_entry[:-1] = lu[1:] > 0

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
    print(f"panel dates={T} symbols={S}")
    print(f"sub-group cells={int(sub.sum()):,} events={int((out_event & sub).sum())}")

    res: dict = {"seed": SEED, "fill_layers": {}}

    # ---- how much of the sub-group is fillable at all --------------------
    layers = {
        "L0_none": np.ones((T, S), dtype=bool),
        "L1_not_oneprice": ~lock_entry,
        "L2_not_gapup": ~gap_entry,
        "L3_not_limitup": ~lu_entry,
    }
    for name, keep in layers.items():
        m = sub & keep
        n_sub = int(m.sum())
        e_sub = int((out_event & m).sum())
        n_v10 = int((v10 & keep).sum())
        e_v10 = int((out_event & v10 & keep).sum())
        n_ok = int((ok & keep).sum())
        e_ok = int((out_event & ok & keep).sum())
        p_sub = e_sub / max(n_sub, 1)
        p_v10 = e_v10 / max(n_v10, 1)
        p_ok = e_ok / max(n_ok, 1)
        res["fill_layers"][name] = {
            "sub_cells": n_sub, "sub_events": e_sub,
            "sub_pct": round(p_sub * 100, 4),
            "lift_vs_base": round(p_sub / p_ok, 2) if p_ok else None,
            "lift_vs_v10": round(p_sub / p_v10, 2) if p_v10 else None,
            "sub_cells_dropped_pct": round(100 * (1 - n_sub / max(int(sub.sum()), 1)), 2),
            "sub_events_dropped_pct": round(100 * (1 - e_sub / max(int((out_event & sub).sum()), 1)), 2),
        }
        r = res["fill_layers"][name]
        print(f"  {name:16s} sub n={n_sub:5,} ev={e_sub:3d}  P={r['sub_pct']:7.4f}%  "
              f"lift {r['lift_vs_base']}x/{r['lift_vs_v10']}x  "
              f"dropped cells {r['sub_cells_dropped_pct']:5.1f}%  events {r['sub_events_dropped_pct']:5.1f}%")

    # ---- event-level fillability: the 62 right-tail events themselves ----
    ev_mask = out_event & sub
    n_ev = int(ev_mask.sum())
    if n_ev:
        res["event_entry_fill"] = {
            "events": n_ev,
            "oneprice_entry": int((ev_mask & lock_entry).sum()),
            "gapup_entry": int((ev_mask & gap_entry).sum()),
            "limitup_entry": int((ev_mask & lu_entry).sum()),
            "oneprice_or_gapup": int((ev_mask & (lock_entry | gap_entry)).sum()),
            "oneprice_pct": round(float((ev_mask & lock_entry).sum() / n_ev) * 100, 2),
        }
        print(f"\n  events whose entry day was a locked board: "
              f"{res['event_entry_fill']['oneprice_entry']}/{n_ev} "
              f"({res['event_entry_fill']['oneprice_pct']:.1f}%)")
        print(f"  events with a gap-up entry (open >= prev close*1.098): "
              f"{res['event_entry_fill']['gapup_entry']}/{n_ev}")
        print(f"  events with limit-up close on entry day: "
              f"{res['event_entry_fill']['limitup_entry']}/{n_ev}")

    # ---- CE under the gap-aware engine, with each fill layer -------------
    exits = ac.EXITS
    ret_by = {lab: a2.vector_exit_v2(mats, sl, tp) for lab, sl, tp in exits}
    res["ce_by_layer"] = {}
    for name, keep in layers.items():
        sub_k = sub & keep
        rows = []
        for t in range(T - 21):
            sel = np.flatnonzero(sub_k[t])
            if sel.size == 0:
                continue
            rec = {"i0": t}
            for lab, _s, _t in exits:
                r = ret_by[lab][t]
                good = sel[np.isfinite(r[sel])]
                rec[f"p_{lab}"] = float(np.mean(r[good])) if good.size else np.nan
                v10i = np.flatnonzero(v10[t] & keep[t] & np.isfinite(r))
                rec[f"v10_{lab}"] = float(np.mean(r[v10i])) if v10i.size else np.nan
            rows.append(rec)
        res["ce_by_layer"][name] = {"cohorts": len(rows), "rules": {}}
        print(f"\n[CE/{name}] cohorts={len(rows)}  (gap-aware engine v2, net of 20bp)")
        pm = {f"g{int(g)}": {} for g in GAMMAS}
        for lab, _s, _t in exits:
            p = np.array([r[f"p_{lab}"] for r in rows], dtype=np.float64)
            vv = np.array([r[f"v10_{lab}"] for r in rows], dtype=np.float64)
            rec = {"n": int(np.isfinite(p).sum()),
                   "mean_bp": round(float(np.nanmean(p)) * 1e4, 1)}
            for g in GAMMAS:
                net = circ_ci(p - COST, lambda x, gg=g: ce(x, gg))
                ok2 = np.isfinite(p) & np.isfinite(vv)
                d10 = None
                if ok2.sum() > 60:
                    m2 = int(ok2.sum()); nbk = int(np.ceil(m2 / BLOCK)); off = np.arange(BLOCK)
                    pa, va = p[ok2] - COST, vv[ok2] - COST
                    out = np.empty(NB)
                    for i in range(NB):
                        s = RNG.integers(0, m2, nbk)
                        idx = ((s[:, None] + off[None, :]) % m2).ravel()[:m2]
                        out[i] = ce(pa[idx], g) - ce(va[idx], g)
                    out = out[np.isfinite(out)]
                    d10 = {"delta_bp": round(float(ce(pa, g) - ce(va, g)) * 1e4, 1),
                           "ci5_bp": round(float(np.percentile(out, 2.5)) * 1e4, 1),
                           "p_gt0": round(float((out > 0).mean()), 3)}
                rec[f"g{int(g)}"] = {"net": net, "vs_v10": d10}
                pm[f"g{int(g)}"][lab] = p - COST
            res["ce_by_layer"][name]["rules"][lab] = rec
            g1 = rec["g1"]
            if g1["net"]:
                print(f"  {lab:16s} mean={rec['mean_bp']:+8.1f}  net g1={g1['net']['est_bp']:+7.1f} "
                      f"[{g1['net']['ci5_bp']:+8.1f},{g1['net']['ci95_bp']:+8.1f}] P={g1['net']['p_le0']}"
                      + (f"  d_v10={g1['vs_v10']['delta_bp']:+6.1f} [{g1['vs_v10']['ci5_bp']:+7.1f}] P={g1['vs_v10']['p_gt0']}"
                         if g1["vs_v10"] else ""))
        j = {}
        for g in (1.0, 5.0):
            j[f"g{int(g)}"] = max_over_rules(pm[f"g{int(g)}"], lambda x, gg=g: ce(x, gg))
        res["ce_by_layer"][name]["max_over_6"] = j
        for g, v in j.items():
            if v:
                print(f"  max-over-6 {g}: obs {v['observed_max_bp']:+7.1f} "
                      f"[{v['ci5_bp']:+7.1f},{v['ci95_bp']:+7.1f}] P(<=0)={v['p_le0']}")
            else:
                print(f"  max-over-6 {g}: too few cohorts")

    out = Path("analysis/_diag_ce_tail_audit2_result.json")
    out.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
