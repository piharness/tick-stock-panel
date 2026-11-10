"""Round 4, part 5: does the right-tail entry probability survive the exit rule?

§12.9 turns the "is there an edge" question into a base-rate question:

    P(fwd20 >= +90%) | vol20 decile 10 AND mom20 top 0.2%  = 1.8647%
    base rate                                             = 0.1991%   (8.99x)

The one sub-group that passes the two-half stability test is
vol20 decile 10 intersected with mom20 top 0.2%.  A lift is a statement about
*entry* into the tail.  It is not money until an exit rule survives the
certainty-equivalent (CE) test on the same sub-group.  This script runs that
test with the exit engine already in the repo -- no new data, no fitted
parameters, no new feature.

Method (same conventions as asymmetry_control.py):

  - signal date d: vol20 decile 10 within the day's pool AND mom20 in the
    day's top 0.2% (copied from _diag_tail.py, so n must reproduce 3,325);
  - entry at open[d+1], exit at close[d+20] or on SL/TP (ac.vector_exit);
  - the 6 pre-existing exit rules (ac.EXITS);
  - CE = certainty equivalent of the per-cohort mean payoff under CRRA
    gamma in {1,2,5}, gross and net of 20bp;
  - 95% CI from a circular moving-block bootstrap (block=20 cohorts, B=2000);
  - paired CE difference against (i) the same-vol-decile-10 pool and (ii) the
    whole day's pool, so "volatility" and "direction" are separated;
  - max-over-6-rules joint bootstrap so "the best of 6" is not read as if it
    were pre-registered.

Verdict rule (R7-E, as written in §12.9):

  a rule counts as money only if its NET CE's 95% CI lower bound is > 0 AND
  the max-over-6 joint bootstrap also clears zero.  Otherwise this panel
  closes on the directional question.

ASCII only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261109
TAIL = 0.90
COST = 0.002  # 20bp, one round trip
GAMMAS = (1.0, 2.0, 5.0)
RNG = np.random.default_rng(SEED)
N_BOOT = 2000
BLOCK = 20  # cohorts; equals the holding period, matching ac.block_boot


def ce(r: np.ndarray, gamma: float) -> float:
    return ac.ce_crra(r, gamma)


def circ_mean_ci(x: np.ndarray, fn, n_boot: int = N_BOOT, block: int = BLOCK) -> dict:
    """Circular moving-block bootstrap CI of fn(x) (fn = CE or mean)."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 3 * max(5, block // 2):
        return {"n": int(n), "ce": None, "ci5": None, "ci95": None, "p_le0": None}
    nb = int(np.ceil(n / block))
    off = np.arange(block)
    out = np.empty(n_boot)
    for i in range(n_boot):
        starts = RNG.integers(0, n, nb)
        idx = ((starts[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = fn(x[idx])
    out = out[np.isfinite(out)]
    return {
        "n": int(n),
        "ce": float(fn(x)),
        "ci5": float(np.percentile(out, 2.5)),
        "ci95": float(np.percentile(out, 97.5)),
        "p_le0": round(float((out <= 0.0).mean()), 3),
    }


def circ_paired_delta(p: np.ndarray, u: np.ndarray, fn, n_boot: int = N_BOOT,
                      block: int = BLOCK) -> dict:
    """Paired circular block bootstrap of fn(p) - fn(u) on the same resample."""
    ok = np.isfinite(p) & np.isfinite(u)
    p, u = p[ok], u[ok]
    n = p.size
    if n < 3 * max(5, block // 2):
        return {"n": int(n), "delta": None, "ci5": None, "ci95": None, "p_gt0": None}
    nb = int(np.ceil(n / block))
    off = np.arange(block)
    out = np.empty(n_boot)
    for i in range(n_boot):
        starts = RNG.integers(0, n, nb)
        idx = ((starts[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = fn(p[idx]) - fn(u[idx])
    out = out[np.isfinite(out)]
    return {
        "n": int(n),
        "delta": float(fn(p) - fn(u)),
        "ci5": float(np.percentile(out, 2.5)),
        "ci95": float(np.percentile(out, 97.5)),
        "p_gt0": round(float((out > 0.0).mean()), 3),
    }


def circ_max_rules(pmats: dict[str, np.ndarray], fn, n_boot: int = N_BOOT,
                   block: int = BLOCK) -> dict:
    """Bootstrap of max over rules of fn, so the best of 6 is calibrated."""
    labels = list(pmats)
    ok = np.ones(0, dtype=bool)
    n = None
    for lab in labels:
        x = pmats[lab]
        finite = np.isfinite(x)
        ok = finite if ok.size == 0 else (ok & finite)
        n = x.size
    arr = {lab: pmats[lab][ok] for lab in labels}
    n = arr[labels[0]].size
    nb = int(np.ceil(n / block))
    off = np.arange(block)
    obs_vals = [fn(arr[lab]) for lab in labels]
    obs_vals = [v for v in obs_vals if np.isfinite(v)]
    obs = max(obs_vals) if obs_vals else float("nan")
    out = np.empty(n_boot)
    for i in range(n_boot):
        starts = RNG.integers(0, n, nb)
        idx = ((starts[:, None] + off[None, :]) % n).ravel()[:n]
        vals = [fn(arr[lab][idx]) for lab in labels]
        vals = [v for v in vals if np.isfinite(v)]
        out[i] = max(vals) if vals else np.nan
    out = out[np.isfinite(out)]
    return {
        "n": int(n),
        "observed_max": float(obs),
        "ci5": float(np.percentile(out, 2.5)),
        "ci95": float(np.percentile(out, 97.5)),
        "p_le0": round(float((out <= 0.0).mean()), 3),
    }


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    vol = ac.grid_of(df, "vol20", dates, symbols)
    mom = ac.grid_of(df, "mom20", dates, symbols)
    G = dl.session_grids(mats)
    fwd = G["c2c20"]

    pool = (np.isfinite(mats["close"]) & (mats["close"] > 0)
            & np.isfinite(turn) & (turn > 0))
    ok = pool & np.isfinite(fwd) & np.isfinite(mom)
    out_event = np.isfinite(fwd) & (fwd >= TAIL)

    # ---- sub-group mask, copied verbatim from _diag_tail.py ----
    dec = np.full((T, S), np.nan)
    band = np.full((T, S), np.nan)
    for t in range(T):
        row = vol[t][pool[t]]
        if row.size >= 500 and np.isfinite(row).any():
            qs = np.nanpercentile(row, [10, 20, 30, 40, 50, 60, 70, 80, 90])
            dec[t] = np.searchsorted(qs, np.where(pool[t] & np.isfinite(vol[t]), vol[t], np.nan))
        row = mom[t][pool[t]]
        if row.size >= 500:
            thr = np.nanpercentile(row, 99.8)
            band[t] = np.where(pool[t] & np.isfinite(mom[t]), mom[t] >= thr, np.nan)
    sub = (dec == 9) & (band == 1.0) & ok
    vol10 = (dec == 9) & ok
    n_cells_ok, n_events = int(ok.sum()), int((out_event & ok).sum())
    base_rate = n_events / n_cells_ok
    base10 = (out_event & vol10).sum() / max(int(vol10.sum()), 1)
    n_sub = int(sub.sum())
    sub_events = int((out_event & sub).sum())
    sub_rate = sub_events / max(n_sub, 1)
    lift_base = sub_rate / base_rate
    lift_v10 = sub_rate / base10
    print(f"panel dates={T} symbols={S}")
    print(f"cells ok={n_cells_ok:,}  events={n_events:,}  base rate={base_rate * 100:.4f}%")
    print(f"vol10 pool: cells={int(vol10.sum()):,}  base={base10 * 100:.4f}%")
    print(f"sub-group vol10 & mom top0.2%: cells={n_sub:,}  events={sub_events}  "
          f"P={sub_rate * 100:.4f}%  lift_vs_base={lift_base:.2f}x  "
          f"lift_vs_vol10={lift_v10:.2f}x")

    # ---- exit engines: 6 pre-existing rules ----
    exits = ac.EXITS
    ret_by = {lab: ac.vector_exit(mats, sl, tp) for lab, sl, tp in exits}

    # ---- per-cohort series ----
    n_need = 21  # d+1 open ... d+20 close
    sub_rows: list[dict] = []
    for t in range(T - n_need):
        sel = np.flatnonzero(sub[t])
        if sel.size == 0:
            continue
        rec = {"i0": t, "n_sub": int(sel.size)}
        rec["n_ev"] = int((out_event[t][sel]).sum())
        for lab, _sl, _tp in exits:
            r = ret_by[lab][t]
            good = sel[np.isfinite(r[sel])]
            rec[f"n_{lab}"] = int(good.size)
            rec[f"p_{lab}"] = float(np.mean(r[good])) if good.size else np.nan
            v10 = np.flatnonzero((dec[t] == 9) & pool[t] & np.isfinite(r))
            rec[f"v10_{lab}"] = float(np.mean(r[v10])) if v10.size else np.nan
            uni = np.flatnonzero(pool[t] & np.isfinite(r))
            rec[f"uni_{lab}"] = float(np.mean(r[uni])) if uni.size else np.nan
        sub_rows.append(rec)
    n_cohorts = len(sub_rows)
    print(f"cohorts with a non-empty sub-group before expiry: {n_cohorts}")

    # ---- report table ----
    out: dict = {
        "seed": SEED,
        "subgroup": {"cells": n_sub, "events": sub_events,
                     "pct": round(sub_rate * 100, 4),
                     "lift_vs_base": round(lift_base, 2),
                     "lift_vs_vol10": round(lift_v10, 2),
                     "vol10_base_pct": round(base10 * 100, 4),
                     "cohorts": n_cohorts},
        "exits": {},
    }
    hdr = f"{'rule':16s} {'n':>4s} {'mean_bp':>8s} {'win':>5s}  "
    print("\n[CE] per-cohort mean payoff of the sub-group, CRRA CE (bp) "
          "[circular block CI, B=2000, block=20 cohorts]")
    print("     gross = raw payoff; net = payoff - 20bp")
    print(hdr + " | ".join(f"g{int(g)} gross/net [ci5,ci95] P(<=0)" for g in GAMMAS))

    ce_net = {}
    for lab, _sl, _tp in exits:
        p = np.array([r[f"p_{lab}"] for r in sub_rows], dtype=np.float64)
        v10 = np.array([r[f"v10_{lab}"] for r in sub_rows], dtype=np.float64)
        uni = np.array([r[f"uni_{lab}"] for r in sub_rows], dtype=np.float64)
        finite = p[np.isfinite(p)]
        rec = {
            "n_cohorts": int(finite.size),
            "mean_bp": round(float(finite.mean()) * 1e4, 1),
            "win": round(float((finite > 0).mean()), 3),
            "ce": {},
            "delta_vs_v10": {},
            "delta_vs_universe": {},
        }
        for g in GAMMAS:
            gross = circ_mean_ci(p, lambda x, gg=g: ce(x, gg))
            netx = circ_mean_ci(p - COST, lambda x, gg=g: ce(x, gg))
            rec["ce"][f"g{int(g)}"] = {
                "gross_bp": round(gross["ce"] * 1e4, 1) if gross["ce"] is not None else None,
                "gross_ci5": round(gross["ci5"] * 1e4, 1) if gross["ci5"] is not None else None,
                "gross_ci95": round(gross["ci95"] * 1e4, 1) if gross["ci95"] is not None else None,
                "net_bp": round(netx["ce"] * 1e4, 1) if netx["ce"] is not None else None,
                "net_ci5": round(netx["ci5"] * 1e4, 1) if netx["ci5"] is not None else None,
                "net_ci95": round(netx["ci95"] * 1e4, 1) if netx["ci95"] is not None else None,
                "net_p_le0": netx["p_le0"],
            }
            d10 = circ_paired_delta(p - COST, v10 - COST, lambda x, gg=g: ce(x, gg))
            duni = circ_paired_delta(p - COST, uni - COST, lambda x, gg=g: ce(x, gg))
            # same-cohort pools, so the absolute CE of the sub-group can be
            # split into "what the market paid" and "what the selection added"
            v10c = circ_mean_ci(v10 - COST, lambda x, gg=g: ce(x, gg))
            unic = circ_mean_ci(uni - COST, lambda x, gg=g: ce(x, gg))
            rec.setdefault("pool_ce", {})[f"g{int(g)}"] = {
                "v10_net_bp": round(v10c["ce"] * 1e4, 1) if v10c["ce"] is not None else None,
                "uni_net_bp": round(unic["ce"] * 1e4, 1) if unic["ce"] is not None else None,
            }
            rec["delta_vs_v10"][f"g{int(g)}"] = {
                "bp": round(d10["delta"] * 1e4, 1) if d10["delta"] is not None else None,
                "ci5": round(d10["ci5"] * 1e4, 1) if d10["ci5"] is not None else None,
                "ci95": round(d10["ci95"] * 1e4, 1) if d10["ci95"] is not None else None,
                "p_gt0": d10["p_gt0"],
            }
            rec["delta_vs_universe"][f"g{int(g)}"] = {
                "bp": round(duni["delta"] * 1e4, 1) if duni["delta"] is not None else None,
                "ci5": round(duni["ci5"] * 1e4, 1) if duni["ci5"] is not None else None,
                "ci95": round(duni["ci95"] * 1e4, 1) if duni["ci95"] is not None else None,
                "p_gt0": duni["p_gt0"],
            }
            ce_net[lab] = ce_net.get(lab, {})
            ce_net[lab][f"g{int(g)}"] = p - COST
        out["exits"][lab] = rec
        g1 = rec["ce"]["g1"]
        print(f"{lab:16s} {rec['n_cohorts']:4d} {rec['mean_bp']:+8.1f} {rec['win']:5.3f}  "
              + " | ".join(
                  f"{rec['ce'][f'g{int(g)}']['gross_bp']:+7.1f}/{rec['ce'][f'g{int(g)}']['net_bp']:+7.1f} "
                  f"[{rec['ce'][f'g{int(g)}']['net_ci5']:+7.1f},{rec['ce'][f'g{int(g)}']['net_ci95']:+7.1f}] "
                  f"{rec['ce'][f'g{int(g)}']['net_p_le0']:.2f}"
                  for g in GAMMAS))
        pc = rec.get("pool_ce", {}).get("g1", {})
        if pc:
            print(f"  {'':16s} pool CE g1 net: v10={pc['v10_net_bp']:+7.1f}  "
                  f"uni={pc['uni_net_bp']:+7.1f}")

    # ---- joint max-over-6 calibration (NET, gamma 1 and 5) ----
    print("\n[CE, max over 6 rules] net CE, so the best of 6 is not read as if pre-registered:")
    joint = {}
    for g in (1.0, 5.0):
        pm = {lab: ce_net[lab][f"g{int(g)}"] for lab, _s, _t in exits}
        res = circ_max_rules(pm, lambda x, gg=g: ce(x, gg))
        joint[f"g{int(g)}"] = {
            "observed_max_bp": round(res["observed_max"] * 1e4, 1),
            "ci5_bp": round(res["ci5"] * 1e4, 1),
            "ci95_bp": round(res["ci95"] * 1e4, 1),
            "p_le0": res["p_le0"],
        }
        print(f"  g{int(g)}: observed max net CE = {res['observed_max'] * 1e4:+7.1f}bp  "
              f"CI [{res['ci5'] * 1e4:+7.1f},{res['ci95'] * 1e4:+7.1f}]  "
              f"P(<=0)={res['p_le0']:.3f}")
    out["joint_max_net"] = joint

    # ---- block-length sensitivity: 40 cohorts (~2 months) vs the 20-session hold ----
    print("\n[CE, max over 6 rules] block=40 sensitivity (net):")
    joint40 = {}
    for g in (1.0, 5.0):
        pm = {lab: ce_net[lab][f"g{int(g)}"] for lab, _s, _t in exits}
        res = circ_max_rules(pm, lambda x, gg=g: ce(x, gg), block=40)
        joint40[f"g{int(g)}"] = {
            "observed_max_bp": round(res["observed_max"] * 1e4, 1),
            "ci5_bp": round(res["ci5"] * 1e4, 1),
            "ci95_bp": round(res["ci95"] * 1e4, 1),
            "p_le0": res["p_le0"],
        }
        print(f"  g{int(g)}: observed max net CE = {res['observed_max'] * 1e4:+7.1f}bp  "
              f"CI [{res['ci5'] * 1e4:+7.1f},{res['ci95'] * 1e4:+7.1f}]  "
              f"P(<=0)={res['p_le0']:.3f}")
    out["joint_max_net_block40"] = joint40

    # ---- diagnostics: overlap, n per cohort, event composition ----
    print("\n[diag] sub-group composition and overlap:")
    all_sub = np.flatnonzero(sub)
    stock_ids = np.unique(all_sub % S)
    per_day_n = np.array([r["n_sub"] for r in sub_rows])
    n_good_g1 = np.array([r[f"n_{exits[0][0]}"] for r in sub_rows])
    print(f"  unique stocks in sub-group: {stock_ids.size}")
    ev_counts = np.array([r["n_ev"] for r in sub_rows])
    print(f"  event cohorts: {int((ev_counts > 0).sum())} of {n_cohorts}  "
          f"events={int(ev_counts.sum())}  max per cohort={int(ev_counts.max())}")
    print(f"  per-cohort raw n: p50={np.percentile(per_day_n, 50):.0f} "
          f"p5={np.percentile(per_day_n, 5):.0f} max={per_day_n.max()}")
    print(f"  per-cohort usable n (hold20): p50={np.percentile(n_good_g1, 50):.0f} "
          f"p5={np.percentile(n_good_g1, 5):.0f} max={n_good_g1.max()}")

    out["diag"] = {
        "unique_stocks": int(stock_ids.size),
        "event_cohorts": int((ev_counts > 0).sum()),
        "events_in_cohorts": int(ev_counts.sum()),
        "per_cohort_raw_p50": float(np.percentile(per_day_n, 50)),
        "per_cohort_raw_p5": float(np.percentile(per_day_n, 5)),
        "per_cohort_usable_p50": float(np.percentile(n_good_g1, 50)),
        "per_cohort_usable_p5": float(np.percentile(n_good_g1, 5)),
    }

    p = Path("analysis/_diag_ce_tail_result.json")
    p.write_text(json.dumps(dg.clean_json(out), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
