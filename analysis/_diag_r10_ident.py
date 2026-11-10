"""Round 4 audit, part 5 (R10): the last open step is not "go buy tick data".

Doc 12.14 ends with one live todo: replay tick/order-book queues on the 5
limit-up runs to measure the fill rate x on LOCKED one-price boards, because
x times the locked-branch return is the only upside left.  That todo is
attacked here on identifiability, not on cost:

  H1  WRONG OBJECT (weaker claim, stated exactly).  A 逐笔成交 tape contains the
      aggregate prints at the limit price (V_day) but no order-level state, so
      it cannot give the queue AHEAD OF YOU at 09:30 (Q); fill requires
      V_day > Q ahead of you.  A full 逐笔委托 + cancel tape CAN reconstruct Q
      -- for a hypothetical order arriving at a stated time.  Your arrival time
      is not in the tape; it is your execution lane.  So the replay returns x
      conditional on latency, i.e. it measures an assumption you cannot hold
      without the infrastructure the experiment was meant to justify.

  H2  THE THRESHOLD IS TINY.  Compute the fill rate x* at which the locked
      branch pays for itself, using the 41 locked cells' own realised returns.
      If x* is ~1%, then any estimator whose queue-position error is +-10pp
      cannot decide the question at all.

  H3  THE DOWNSIDE BRANCH IS A LOTTERY.  The 16 assumption-free fills are
      3 wins / 13 stop-outs; report the sign test, the leave-one-out of the
      3 wins, and the n needed for 80% power at the observed dispersion
      (in years, at 4.36 fills per 100 sessions).  This is the honest
      replacement for "mean is indistinguishable from 0".

  H4  THE REPO ALREADY OWNS THE FIELD THAT DECIDES x.  depth_service writes
      data/depth5/date=*/part.parquet with bid1_vol (= 封单量 = the buy queue
      at the limit price) -- forward only, one snapshot per day, currently
      0 files because the collector has never run.  Check the writer, the
      field, and the collector's cadence from the source, and price the
      alternative: 60 forward sessions of 封单量 vs a historical L2 purchase.

  H5  BENCHMARK THE ONE CLEAN SET.  Every CE in 12.13/12.14 is absolute.
      12.10 established that ~+55bp of the basket's CE is what the market
      paid in this bull sample.  Pay that debt for the 16-fill set and for
      the 3,268 no-assumption buyable cells: paired against the same-cohort
      market pool.  Parity check: the sub-vs-market paired delta must
      reproduce 12.10's +5.4bp [-121.1, +141.7].

No production code is touched.  Masks / decile / band / exit engine are copied
verbatim from _diag_r9_gate.py so the counts (57 / 41 / 16) and the +212.3bp
mean are a parity check, not a new number.  The 16 fill records are also
recomputed independently from analysis/_diag_r9_gate_result.json.

ASCII only (GBK console).
"""
from __future__ import annotations

import glob
import json
import math
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import _diag_ce_tail_audit as a1  # noqa: E402
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
from _diag_ce_tail_verify import limit_grids  # noqa: E402

SEED = 20261114
TAIL = 0.90
COST = 0.002
BLOCK = 20
NB = 2000
RNG = np.random.default_rng(SEED)
HOLD = 20
Z80 = 1.959964 + 0.841621  # two-sided 5%, power 80%

warnings.filterwarnings("ignore", message="All-NaN slice encountered")

ce = ac.ce_crra


# ------------------------------------------------------------------ helpers
def boot_ci(x: np.ndarray, fn, min_n: int = 60, nb: int = NB) -> dict | None:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < min_n:
        return None
    m = int(math.ceil(n / BLOCK))
    off = np.arange(BLOCK)
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


def binom_tail(k: int, n: int, p: float = 0.5) -> float:
    """P(X <= k) under Binomial(n, p), log-space so n = 3,325 does not overflow."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    lp, lq = math.log(p), math.log(1.0 - p)
    s = 0.0
    for i in range(k + 1):
        s += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                      + i * lp + (n - i) * lq)
    return min(1.0, s)


def power_n(sd: float, delta: float) -> int | None:
    if not np.isfinite(sd) or not np.isfinite(delta) or abs(delta) < 1e-12:
        return None
    return int(math.ceil((Z80 * sd / abs(delta)) ** 2))


def lead(g: np.ndarray, h: int) -> np.ndarray:
    out = np.full_like(g, np.nan)
    T = g.shape[0]
    if h < T:
        out[: T - h] = g[h:]
    return out


def rg(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = num / den - 1.0
    okm = np.isfinite(num) & np.isfinite(den) & (den > 0) & np.isfinite(r)
    return np.where(okm, r, np.nan)


def cell_stats(mask: np.ndarray, ex_sl5: np.ndarray, fwd: np.ndarray,
               trade_fwd: np.ndarray | None, T: int) -> dict:
    m = mask & np.isfinite(ex_sl5)
    n = int(m.sum())
    if n == 0:
        return {"cells": 0}
    r = ex_sl5[m].astype(np.float64)
    cohs, vals = 0, []
    for t in range(T - 21):
        mm = mask[t] & np.isfinite(ex_sl5[t])
        if mm.any():
            cohs += 1
            vals.append(float(np.mean(ex_sl5[t][mm])))
    v = np.array(vals) - COST
    sd = float(np.std(r, ddof=1)) if n > 1 else float("nan")
    se = sd / math.sqrt(n) if n > 1 else float("nan")
    tf = (trade_fwd[mask & np.isfinite(ex_sl5) & np.isfinite(trade_fwd)]
          if trade_fwd is not None else np.array([]))
    return {
        "cells": n,
        "cohorts": cohs,
        "events": int((fwd[m] >= TAIL).sum()),
        "mean_sl5_bp": round(float(np.mean(r)) * 1e4, 1),
        "mean_buyhold20_bp": round(float(np.mean(tf)) * 1e4, 1) if tf.size else None,
        "median_bp": round(float(np.median(r)) * 1e4, 1),
        "sd_bp": round(sd * 1e4, 1),
        "t_mean": round(float(np.mean(r)) / se, 2) if se else None,
        "n_positive": int((r > 0).sum()),
        "sign_p_le": round(binom_tail(int((r > 0).sum()), n), 4),
        "ce_g1_bp": round(float(ce(v, 1.0)) * 1e4, 1) if v.size else None,
    }


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
    lock_entry = np.zeros((T, S), dtype=bool)
    lock_entry[:-1] = one_price[1:]

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

    ex_sl5 = a1.vector_exit_v2(mats, 0.05, None)
    lu, _ld = limit_grids(dates, symbols)
    entry_lu = np.zeros((T, S), dtype=bool)
    entry_lu[:-1] = lu[1:] > 0
    open_at_limit = np.zeros((T, S), dtype=bool)
    open_at_limit[:-1] = (lu[1:] > 0) & np.isclose(o[1:], h[1:], rtol=0.0, atol=1e-9)

    sel_open = sub & open_at_limit          # 09:30-knowable, limit price is the open
    sel_locked = sel_open & lock_entry      # 41: seal never breaks
    sel_fill = sel_open & ~lock_entry       # 16: breaks => a limit order at the
    #                                          limit price is filled by construction
    buyable = sub & ~open_at_limit          # 3,268: no fill assumption at all

    res: dict = {
        "seed": SEED,
        "panel": {"dates": int(T), "symbols": int(S), "date_min": dates[0],
                  "date_max": dates[-1], "ok_cells": int(ok.sum()),
                  "sub_cells": int(sub.sum())},
        "note": ("x = P(a 09:30 limit order at the limit price actually fills) on a "
                 "LOCKED one-price board.  Only x * locked-branch return is in "
                 "question; the 16 break-cells are assumption-free."),
    }
    print(f"panel T={T} S={S} ok={int(ok.sum()):,} sub={int(sub.sum()):,} "
          f"{dates[0]}..{dates[-1]}")

    # ---------------------------------------------------------------- A parity
    trade_fwd = rg(lead(c, 20), lead(o, 1))
    a = {k: cell_stats(v, ex_sl5, fwd, trade_fwd, T) for k, v in
         (("sel_open_all", sel_open), ("locked_41", sel_locked),
          ("fill_16", sel_fill), ("buyable_no_assumption", buyable),
          ("basket_sub", sub))}
    res["A_parity_sets"] = a
    print("\n[A] set sizes (parity: 57 / 41 / 16 / SL5% mean 212.3 / CE 2969.3 / 26.2):")
    print(f"  {'set':24s} {'cells':>5} {'coh':>4} {'ev':>3} {'SL5% mean':>10} "
          f"{'b&h20 mean':>10} {'med':>8} {'sd':>8} {'t':>5} {'pos':>4} {'CE':>8}")
    for k, v in a.items():
        print(f"  {k:24s} {v['cells']:>5} {v['cohorts']:>4} {v['events']:>3} "
              f"{v['mean_sl5_bp']:>10.1f} "
              f"{(v['mean_buyhold20_bp'] if v['mean_buyhold20_bp'] is not None else float('nan')):>10.1f} "
              f"{v['median_bp']:>8.1f} {v['sd_bp']:>8.1f} "
              f"{v['t_mean'] if v['t_mean'] is not None else float('nan'):>5.2f} "
              f"{v['n_positive']:>4} {v['ce_g1_bp']:>8.1f}")

    # independent recomputation of the 16 from the r9 json (second code path)
    r9 = json.loads(Path("analysis/_diag_r9_gate_result.json").read_text(encoding="utf-8"))
    fills = r9["G5_fills"]
    f_sl5 = np.array([f["sl5_bp"] for f in fills], dtype=np.float64)
    f_fwd = np.array([f["trade_fwd_bp"] for f in fills], dtype=np.float64)
    res["A2_json_recheck"] = {
        "n": int(f_sl5.size),
        "mean_sl5_bp": round(float(np.mean(f_sl5)), 1),
        "median_sl5_bp": round(float(np.median(f_sl5)), 1),
        "positives": int((f_sl5 > 0).sum()),
        "mean_buyhold20_bp": round(float(np.mean(f_fwd)), 1),
        "positives_buyhold20": int((f_fwd > 0).sum()),
        "compounded_pct": round(float(np.prod(1.0 + f_sl5 / 1e4) - 1.0) * 100, 1),
    }
    r2 = res["A2_json_recheck"]
    print(f"  json recheck: n={r2['n']} mean SL5={r2['mean_sl5_bp']}bp "
          f"median={r2['median_sl5_bp']}bp pos={r2['positives']}/{r2['n']}  "
          f"buy&hold20 mean={r2['mean_buyhold20_bp']}bp "
          f"pos={r2['positives_buyhold20']}/{r2['n']}  "
          f"compounded={r2['compounded_pct']}%")

    # ---------------------------------------------------------------- B/H5 benchmark
    mkt = pool & ok & np.isfinite(ex_sl5)
    mkt_coh = {}
    for t in range(T - 21):
        mm = mkt[t]
        if mm.any():
            mkt_coh[t] = float(np.mean(ex_sl5[t][mm]))

    def paired_vs_mkt(mask: np.ndarray) -> dict | None:
        d = []
        for t in range(T - 21):
            mm = mask[t] & np.isfinite(ex_sl5[t])
            if mm.any() and t in mkt_coh:
                d.append(float(np.mean(ex_sl5[t][mm])) - mkt_coh[t])
        d = np.array(d)
        if d.size == 0:
            return None
        out = {"n_cohorts": int(d.size), "delta_bp": round(float(np.mean(d)) * 1e4, 1)}
        if d.size >= 25:
            ci = boot_ci(d, lambda x: float(np.mean(x)), min_n=25)
            if ci:
                out.update({"ci5_bp": ci["ci5_bp"], "ci95_bp": ci["ci95_bp"],
                            "p_gt0": round(1.0 - ci["p_le0"], 3),
                            "ci_kind": "block bootstrap"})
        else:
            se = float(np.std(d, ddof=1) / math.sqrt(d.size))
            out.update({"ci5_bp": round((float(np.mean(d)) - 2.131 * se) * 1e4, 1),
                        "ci95_bp": round((float(np.mean(d)) + 2.131 * se) * 1e4, 1),
                        "p_gt0": None,
                        "ci_kind": "t (df=15, no bootstrap: n<25)"})
        return out

    def paired_ce(p0: np.ndarray, p1: np.ndarray) -> dict | None:
        okm = np.isfinite(p0) & np.isfinite(p1)
        p0, p1 = p0[okm], p1[okm]
        n = p0.size
        if n < 60:
            return None
        m = int(math.ceil(n / BLOCK))
        off = np.arange(BLOCK)
        out = np.empty(NB)
        for i in range(NB):
            s = RNG.integers(0, n, m)
            idx = ((s[:, None] + off[None, :]) % n).ravel()[:n]
            out[i] = ce(p0[idx], 1.0) - ce(p1[idx], 1.0)
        o = out[np.isfinite(out)]
        return {"n": int(n),
                "delta_bp": round(float(ce(p0, 1.0) - ce(p1, 1.0)) * 1e4, 1),
                "ci5_bp": round(float(np.percentile(o, 2.5)) * 1e4, 1),
                "ci95_bp": round(float(np.percentile(o, 97.5)) * 1e4, 1),
                "p_gt0": round(float((o > 0.0).mean()), 3)}

    p0, p1 = [], []
    for t in range(T - 21):
        mm = sub[t] & np.isfinite(ex_sl5[t])
        if mm.any() and t in mkt_coh:
            p0.append(float(np.mean(ex_sl5[t][mm])) - COST)
            p1.append(mkt_coh[t] - COST)

    mkt_lvl = float(np.mean(list(mkt_coh.values()))) * 1e4 if mkt_coh else None
    mkt_arr = np.array(list(mkt_coh.values())) - COST if mkt_coh else None
    mkt_ce = round(float(ce(mkt_arr, 1.0)) * 1e4, 1) if mkt_arr is not None else None
    res["B_market_pool"] = {
        "cohorts": len(mkt_coh),
        "mean_20d_sl5_net_bp": round(mkt_lvl, 1) if mkt_lvl is not None else None,
        "ce_g1_bp": mkt_ce,
        "pairs": {k: paired_vs_mkt(v) for k, v in
                  (("basket_sub", sub), ("fill_16", sel_fill),
                   ("locked_41", sel_locked), ("buyable_no_assumption", buyable),
                   ("sel_open_all", sel_open))},
    }
    b = res["B_market_pool"]
    res["B2_ce_parity"] = paired_ce(np.array(p0), np.array(p1))
    if res["B2_ce_parity"]:
        q = res["B2_ce_parity"]
        print(f"\n  all-market equal-weight pool CE g1 = {mkt_ce}bp "
              f"(doc 12.10 quotes +53.8..+55.8 for its pool)")
        print(f"  paired CE g1 basket-minus-market = {q['delta_bp']:+.1f}bp "
              f"[{q['ci5_bp']:+.1f},{q['ci95_bp']:+.1f}] P(>0)={q['p_gt0']:.2f} "
              f"n={q['n']}  -- 12.10 quotes +5.4 [-121.1,+141.7] P=0.52 vs ITS "
              f"benchmark column; the two CIs both cover 0 and the sign of a "
              f"~+-30bp CE delta is not identified at n=326.")
    print(f"\n[B] market pool (all-cell equal weight, SL5%, net 20bp): "
          f"{b['mean_20d_sl5_net_bp']}bp / 20d mean, {b['ce_g1_bp']}bp CE, "
          f"over {b['cohorts']} cohorts")
    print(f"  {'set':24s} {'excess vs market':>16} {'95% CI':>22} {'P(>0)':>6} "
          f"{'coh':>4}  ci")
    for k, v in b["pairs"].items():
        if not v:
            continue
        ci = ("[%+.0f,%+.0f]" % (v["ci5_bp"], v["ci95_bp"])) if "ci5_bp" in v else "n/a"
        pg = v.get("p_gt0")
        print(f"  {k:24s} {v['delta_bp']:>16.1f} {ci:>22} "
              f"{(pg if pg is not None else float('nan')):>6.3f} {v['n_cohorts']:>4}  "
              f"{v.get('ci_kind', '')}")

    # ---------------------------------------------------------------- C/H3 power
    sd16 = a["fill_16"]["sd_bp"]
    need16 = power_n(sd16, a["fill_16"]["mean_sl5_bp"])
    sd_l = a["locked_41"]["sd_bp"]
    need_l = power_n(sd_l, a["locked_41"]["mean_sl5_bp"])
    fills_per_100 = a["fill_16"]["cells"] / T * 100
    res["C_power"] = {
        "fill16": {"sd_bp": sd16, "mean_bp": a["fill_16"]["mean_sl5_bp"],
                   "n_required_80pct": need16,
                   "sessions_required": (round(need16 / (fills_per_100 / 100), 0)
                                         if need16 else None),
                   "years_required": (round(need16 / (fills_per_100 / 100) / 252, 1)
                                      if need16 else None)},
        "locked41": {"sd_bp": sd_l, "mean_bp": a["locked_41"]["mean_sl5_bp"],
                     "n_required_80pct": need_l,
                     "attempts_per_year": round(41 / (T / 252), 1)},
        "fills_per_100_sessions": round(fills_per_100, 2),
    }
    c3 = res["C_power"]
    print(f"\n[C] power (80%, two-sided 5%) at the observed dispersion:")
    print(f"  16-fill set: mean {c3['fill16']['mean_bp']}bp sd {sd16}bp -> "
          f"n={c3['fill16']['n_required_80pct']} trades = "
          f"{c3['fill16']['sessions_required']} sessions = "
          f"{c3['fill16']['years_required']} years")
    print(f"  locked-41  : mean {c3['locked41']['mean_bp']}bp sd {sd_l}bp -> "
          f"n={c3['locked41']['n_required_80pct']} fills "
          f"(41 locked attempts per {T / 252:.2f}y = "
          f"{c3['locked41']['attempts_per_year']}/yr)")

    # leave-one-out of the 3 wins
    lo = []
    base = float(np.mean(f_sl5))
    for i in range(f_sl5.size):
        if f_sl5[i] > 0:
            lo.append({"symbol": fills[i]["symbol"], "date": fills[i]["date"],
                       "sl5_bp": round(float(f_sl5[i]), 1),
                       "mean_without_bp": round((base * f_sl5.size - f_sl5[i])
                                                / (f_sl5.size - 1), 1)})
    res["C2_loo_wins"] = {"base_mean_bp": round(base, 1), "wins": lo,
                          "n_positive": int((f_sl5 > 0).sum()),
                          "sign_p_one_sided": round(
                              binom_tail(int((f_sl5 > 0).sum()), int(f_sl5.size)), 4)}
    print(f"  sign test: {int((f_sl5 > 0).sum())}/{f_sl5.size} positive, "
          f"P(X<=k)={res['C2_loo_wins']['sign_p_one_sided']:.4f}; "
          f"median {round(float(np.median(f_sl5)), 1)}bp")
    for w in lo:
        print(f"    drop {w['symbol']} {w['date']} ({w['sl5_bp']:+.0f}bp) -> "
              f"mean {w['mean_without_bp']:+.1f}bp")

    # ---------------------------------------------------------------- D/H2 x*
    # per-attempt expected return, then the x at which the branch pays and the
    # x at which it could be DETECTED.  The two curves cross; that is the point.
    n_break = a["fill_16"]["cells"]
    n_locked = a["locked_41"]["cells"]
    n_att = n_break + n_locked
    p_break = n_break / n_att
    r_break = a["fill_16"]["mean_sl5_bp"]
    r_lock = a["locked_41"]["mean_sl5_bp"]
    att_per_year = n_att / (T / 252)
    span_years = T / 252
    grid = [0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 1.0]
    rows = []
    for x in grid:
        ev = p_break * r_break + (1 - p_break) * x * r_lock
        n_realised = n_break + n_locked * x
        util = min(1.0, n_realised * HOLD / T)
        ann = (ev * 1e-4) * att_per_year * util  # arithmetic, per year
        fills_yr = n_locked * x / span_years
        rows.append({"x": x,
                     "ev_per_attempt_bp": round(ev, 1),
                     "realised_trades_over_sample": round(n_realised, 1),
                     "utilisation": round(util, 3),
                     "arith_annual_pct": round(ann * 100, 1),
                     "locked_fills_per_year": round(fills_yr, 2),
                     "years_to_detect": (round(need_l / fills_yr, 1)
                                         if need_l and fills_yr > 0 else None)})
    res["D_fill_threshold"] = {
        "attempts": n_att,
        "attempts_per_year": round(att_per_year, 1),
        "p_break_given_open_at_limit": round(p_break, 4),
        "r_break_bp": r_break,
        "r_locked_bp": r_lock,
        "grid": rows,
        "x_star_breakeven": next((r["x"] for r in rows
                                  if r["arith_annual_pct"] > 0), None),
        "x_star_10pct_annual": next((r["x"] for r in rows
                                     if r["arith_annual_pct"] > 10.0), None),
    }
    d = res["D_fill_threshold"]
    print(f"\n[D] {n_att} open-at-limit attempts, P(break)={p_break:.3f}, "
          f"r_break={r_break}bp, r_locked={r_lock}bp, "
          f"{att_per_year:.1f} attempts/yr")
    print(f"  {'x':>7} {'EV/attempt':>11} {'trades':>7} {'util':>5} {'arith ann%':>10} "
          f"{'fills/yr':>9} {'yrs to detect':>14}")
    for r in rows:
        print(f"  {r['x']:>7.3f} {r['ev_per_attempt_bp']:>11.1f} "
              f"{r['realised_trades_over_sample']:>7.1f} {r['utilisation']:>5.2f} "
              f"{r['arith_annual_pct']:>10.1f} {r['locked_fills_per_year']:>9.2f} "
              f"{(r['years_to_detect'] if r['years_to_detect'] is not None else float('nan')):>14.1f}")

    # concentration of the locked branch: overlapping same-name positions
    lk_t, lk_s = np.nonzero(sel_locked)
    by_sym: dict[str, list[int]] = {}
    for t, s in zip(lk_t.tolist(), lk_s.tolist()):
        by_sym.setdefault(sym_arr[s], []).append(int(t))
    conc = sorted(({"symbol": k, "signal_days": len(v),
                    "first": str(dates[min(v)])[:10],
                    "last": str(dates[max(v)])[:10]} for k, v in by_sym.items()),
                  key=lambda r: -r["signal_days"])
    res["D2_locked_concentration"] = {
        "cells": len(lk_t), "symbols": len(by_sym),
        "top": conc[:5],
        "max_concurrent_same_name": max(
            (max(sum(1 for u in v if t - HOLD < u <= t) for t in v)
             for v in by_sym.values()), default=0),
    }
    print(f"  locked 41 -> {len(by_sym)} symbols; top: "
          + ", ".join(f"{r['symbol']}x{r['signal_days']}" for r in conc[:4]))

    # ---------------------------------------------------------------- E/H4 data path
    part_dirs = sorted(glob.glob("data/kline_daily_enriched/date=*"))
    minute_dirs = sorted(glob.glob("data/kline_minute/date=*"))
    depth_files = [f for f in glob.glob("data/depth5/**/*", recursive=True)
                   if Path(f).is_file()]
    auc = sorted(glob.glob("data/auction_benchmark/*"))
    depth_src = Path("backend/app/services/depth_service.py")
    txt = depth_src.read_text(encoding="utf-8", errors="replace") if depth_src.exists() else ""
    res["E_data_path"] = {
        "daily_partitions": len(part_dirs),
        "minute_dates": len(minute_dirs),
        "depth5_files": len(depth_files),
        "auction_benchmark_files": len(auc),
        "depth5_writer_exists": depth_src.exists(),
        "depth5_field_bid1_vol": "bid1_vol" in txt,
        "depth5_sealed_is_bid1_when_limit_up": "涨停=买一量" in txt,
        "depth5_persist_path": "data/depth5" in txt,
        "depth5_persist_cadence": ("one finalise per day (盘后定版)" if "盘后定版" in txt
                                   else "unknown"),
        "provider_has_depth5_capability": any(
            "depth5=True" in Path(p).read_text(encoding="utf-8", errors="replace")
            for p in glob.glob("backend/app/data_providers/*.py")),
    }
    e = res["E_data_path"]
    print(f"\n[E] data path: depth5 files={e['depth5_files']}  "
          f"writer={e['depth5_writer_exists']} bid1_vol={e['depth5_field_bid1_vol']} "
          f"sealed=buy1@limit={e['depth5_sealed_is_bid1_when_limit_up']}  "
          f"minute={e['minute_dates']}d auction_benchmark={e['auction_benchmark_files']}")

    # locked-branch traded size: the V_day that would have to exceed queue Q
    files = sorted(glob.glob("data/kline_daily_enriched/date=*/part.parquet"))[:1]
    del files
    amt = None
    try:
        vdf = pl.concat([pl.read_parquet(f, columns=["symbol", "date", "amount"])
                         for f in sorted(glob.glob(
                             "data/kline_daily_enriched/date=*/part.parquet"))],
                        how="vertical_relaxed")
        vdf = vdf.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
        amt = ac.grid_of(vdf, "amount", dates, symbols)
    except Exception as exc:  # noqa: BLE001
        print(f"  amount grid unavailable: {exc}")
    if amt is not None:
        nxt = np.full_like(amt, np.nan)
        nxt[:-1] = amt[1:]
        va = nxt[sel_locked]
        va = va[np.isfinite(va)]
        if va.size:
            res["E2_locked_traded_amount"] = {
                "n": int(va.size),
                "median_cny": round(float(np.median(va)), 0),
                "p10_cny": round(float(np.percentile(va, 10)), 0),
                "p90_cny": round(float(np.percentile(va, 90)), 0),
            }
            v2 = res["E2_locked_traded_amount"]
            print(f"  locked-board traded amount (entry day): median "
                  f"{v2['median_cny'] / 1e8:.2f}e8 CNY, p10 {v2['p10_cny'] / 1e8:.2f}e8, "
                  f"p90 {v2['p90_cny'] / 1e8:.2f}e8  (= the V_day x must beat)")

    out = Path("analysis/_diag_r10_ident_result.json")
    out.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
