"""Third-round audit: is the holding period a free power lever?

Round 2 (docs/asymmetry-round2.md) ended with two recommendations. The first one
is the one that matters:

    "把持有期从 20 日压到 5 日（独立块 16->73）... 这两条才动得了唯一真正的约束。"

That sentence asserts that shortening the holding period buys power, because
n_indep = sample_days / holding_period. It is exactly the category of claim
round 2 was auditing: a quantity (trade frequency) treated as free because the
arithmetic looks free.

Two independent reasons it can be false, both cheap to measure:

  R1 SCALING.  A horizon-h bet has effect a_h and per-bet sd s_h. Over T days
     there are T/h independent bets, so t(h) = (a_h/s_h)*sqrt(T/h). Whether that
     rises as h falls is entirely an empirical question about how a_h scales:
     a_h ~ h^alpha gives t ~ h^(alpha-1). alpha=1 (constant drift) is exactly
     neutral; alpha=0.5 (one-shot jump / IC-invariant) favours short horizons.
     So "shorten it" is a hypothesis, not a lever. Measured here on the data.

  R2 COST.  A round trip costs c per holding period h, i.e. c*252/h per year.
     Net per-cohort edge is a_h - c, which must stay positive. A gross edge of
     1.5bp/day (== 30bp/20d, round 2's design target) breaks even only at
     h >= c/1.5. At c=20bp that is h >= 13.3 days. Below that the recommendation
     asks for a strategy whose edge is smaller than its cost -- a power gain
     spent on an untradeable object.

Estimand (deliberately NOT the CRRA certainty equivalent):
    a_h(d) = mean_{picks} fwd_h(d) - mean_{pool} fwd_h(d),  fwd_h = close[d+h]/open[d+1]-1
    i.e. the per-cohort active return in bp per h-day bet, net of cost where
    stated. A mean's SE is sd/sqrt(n_eff); a CE's is not, which is why round 2's
    MDE (built from the sd of the return series while testing a CE) is a
    mean-based MDE standing in for a CE test. Section [P] reconciles the two.

Pre-registered decision rule (written before the run):
    R3-A  The recommendation in round 2 section 7.2 is REVOKED if
          max_h t_net(h) < 2 at the repo's own cost convention.
    R3-B  "Holding period is a free power lever" is REFUTED if the gross
          t(h) curve is not increasing as h falls by at least 1.5x between
          h=20 and h=5 (the claim needs 2x to matter, 1.5x to be worth the
          cost increase).
    R3-C  If the zero-signal placebo rejects |t|>=2 at more than 2x nominal
          at some h, that h's row is not evidence either way.

Parity: at h=20 the cohort set and the active-return dispersion must reproduce
analysis/ce_path_audit_result.json power.hold20 (n=326, sd=439.7bp) digit for
digit, and the forward grid must equal ce_path_audit.ret_grid at h=20 exactly.

ASCII output only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
import ce_path_audit as cpa  # noqa: E402

SEED = 20261109
H_GRID = (1, 2, 3, 5, 10, 20, 40, 60)
N_STOCK = ap.N_STOCK
N_BOOT = 2000
N_PLACEBO = 100
N_DRAWS = 400

ARMS = [
    ("mom_bottom20", True),
    ("mom_top20", False),
]

# Round-trip cost in bp. 0 = the convention of every earlier round (zero cost).
# 20bp = 2.5bp/side commission + 5bp stamp duty on the sell side + 5bp/side
# slippage, which is the shape of backend/app/backtest/engine.py's defaults
# (fees_pct=0.0002, slippage_bps=5.0) once stamp duty is switched on.
COSTS_BP = (0.0, 10.0, 20.0, 30.0)

# Round 2's design target: a 30bp edge per 20-day hold == 1.5bp/day.
DELTA_PER_DAY_BP = (1.5, 3.0)


# --------------------------------------------------------------------------
def fwd_grid(mats: dict[str, np.ndarray], h: int) -> np.ndarray:
    """Forward return of a bet opened at next open and closed h days later."""
    o, c = mats["open"], mats["close"]
    T = o.shape[0]
    out = np.full(o.shape, np.nan)
    if h < 1 or h >= T:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        r = c[h:T] / o[1 : T - h + 1] - 1.0
    r = np.where(np.isfinite(r) & (o[1 : T - h + 1] > 0), r, np.nan)
    out[0 : T - h] = r
    return out


def nw_var(x: np.ndarray, lag: int) -> tuple[float, int]:
    """Newey-West variance of the mean with Bartlett kernel and lag L."""
    x = x[np.isfinite(x)]
    n = x.size
    if n < 10:
        return float("nan"), n
    d = x - x.mean()
    v = float((d * d).mean())
    L = int(min(lag, n - 2))
    for j in range(1, L + 1):
        v += 2.0 * (1.0 - j / (L + 1.0)) * float((d[j:] * d[:-j]).mean())
    return max(v, 0.0), n


def nw_t(x: np.ndarray, lag: int) -> float:
    v, n = nw_var(x, lag)
    if not np.isfinite(v) or v <= 0:
        return float("nan")
    return float(x[np.isfinite(x)].mean() / np.sqrt(v / n))


def block_boot_mean(x: np.ndarray, block: int, n: int = N_BOOT, seed: int = SEED) -> dict:
    """Moving-block bootstrap of the mean (the estimand here, not a CE)."""
    x = x[np.isfinite(x)]
    if x.size < block * 3:
        return {"se_bp": None, "ci5_bp": None, "ci95_bp": None}
    rng = np.random.default_rng(seed)
    starts = np.arange(x.size - block + 1)
    n_blocks = int(np.ceil(x.size / block))
    out = np.empty(n)
    for i in range(n):
        idx = rng.choice(starts, size=n_blocks, replace=True)
        sel = np.concatenate([np.arange(s, s + block) for s in idx])
        out[i] = x[sel].mean()
    return {
        "se_bp": round(float(out.std()) * 1e4, 1),
        "ci5_bp": round(float(np.percentile(out, 5)) * 1e4, 1),
        "ci95_bp": round(float(np.percentile(out, 95)) * 1e4, 1),
    }


def cohort_active(
    fwd: np.ndarray,
    picks_rows: list[dict],
    s_map: dict[str, int],
    date_index: dict[str, int],
    tradable: np.ndarray,
) -> np.ndarray:
    """Per-cohort active return for one horizon, using the SAME pick list."""
    pool_ok = tradable & np.isfinite(fwd)
    act: list[float] = []
    for r in picks_rows:
        i0 = date_index[str(r["date"])]
        sel = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
        good = sel[np.isfinite(fwd[i0][sel])]
        if good.size < 5:
            continue
        pool = np.flatnonzero(pool_ok[i0])
        if pool.size < N_STOCK:
            continue
        act.append(float(np.mean(fwd[i0][good]) - np.mean(fwd[i0][pool])))
    return np.array(act)


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    tradable = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)
    print(f"panel dates={T} symbols={S}  H_GRID={H_GRID}  costs_bp={COSTS_BP}")

    # ---------------- parity: h=20 must equal the round-2 grid ---------------
    F20 = fwd_grid(mats, 20)
    _, exit_px = cpa.exit_paths(mats, None, None)
    ref = cpa.ret_grid(mats, exit_px)
    both = np.isfinite(F20) & np.isfinite(ref)
    gap = float(np.max(np.abs(F20[both] - ref[both])))
    extra_rows = np.flatnonzero(np.isfinite(F20).any(axis=1) & ~np.isfinite(ref).any(axis=1))
    print(f"[parity] fwd_grid(h=20) vs ce_path_audit.ret_grid: max|diff|={gap:.3g} "
          f"extra cohort rows in this grid={extra_rows.tolist()} of T={T}")
    assert gap < 1e-12, "forward grid diverged from round 2"
    # exit_paths loops range(T-hold-1), so the last valid cohort date (d=T-hold-1)
    # is never computed there. Reproduce the round-2 series exactly on its own
    # grid, then run the horizon grid on the full one.
    pk_par = ap.pick_daily(df, "mom20", True)
    rows_par = pk_par.select("date", "symbols").to_dicts()
    act_par = cohort_active(ref, rows_par, s_map, date_index, tradable)
    par_n, par_sd = act_par.size, round(float(act_par.std(ddof=0)) * 1e4, 1)
    print(f"[parity] round-2 grid h=20: n={par_n} sd={par_sd}bp "
          f"(ce_path_audit power.hold20: n=326 sd=439.7bp)")
    assert par_n == 326 and abs(par_sd - 439.7) < 0.15, "h=20 series diverged from round 2"

    # ---------------- primary grid -----------------------------------------
    results: dict = {"seed": SEED, "h_grid": list(H_GRID), "costs_bp": list(COSTS_BP),
                     "parity": {"fwd_h20_max_abs_diff": gap,
                                "round2_grid_n": par_n, "round2_grid_sd_bp": par_sd,
                                "extra_cohort_rows": extra_rows.tolist()},
                     "arms": {}, "placebo": {}, "ce_vs_mean": {}}

    for arm_label, ascending in ARMS:
        pk = ap.pick_daily(df, "mom20", ascending)
        picks_rows = pk.select("date", "symbols").to_dicts()
        print(f"\n############ {arm_label}  pick-dates={len(picks_rows)}")
        results["arms"][arm_label] = {}

        for h in H_GRID:
            F = F20 if h == 20 else fwd_grid(mats, h)
            act = cohort_active(F, picks_rows, s_map, date_index, tradable)
            if act.size < 30:
                print(f"  h={h:3d}  n={act.size}  -- too few cohorts")
                continue
            n = act.size
            n_eff = n / h
            mean_bp = float(act.mean()) * 1e4
            sd_bp = float(act.std(ddof=0)) * 1e4
            se_mean_bp = sd_bp / np.sqrt(n_eff)
            t_mean = mean_bp / se_mean_bp
            lag = int(2 * h)
            v_nw, _ = nw_var(act, lag)
            se_nw_bp = float(np.sqrt(v_nw / n)) * 1e4 if np.isfinite(v_nw) else float("nan")
            boot = block_boot_mean(act, block=h, seed=SEED + h)
            row = {
                "n_cohorts": n,
                "n_indep": round(n_eff, 1),
                "mean_bp": round(mean_bp, 1),
                "sd_bp": round(sd_bp, 1),
                "se_mean_bp": round(float(se_mean_bp), 1),
                "t_mean": round(float(t_mean), 2),
                "se_nw_bp": round(se_nw_bp, 1),
                "t_nw": round(float(mean_bp / se_nw_bp), 2) if se_nw_bp else None,
                "boot": boot,
                "mean_per_day_bp": round(mean_bp / h, 2),
                "net": {},
            }
            for c in COSTS_BP:
                se = se_nw_bp if np.isfinite(se_nw_bp) else se_mean_bp
                net = mean_bp - c
                row["net"][f"c{c:g}"] = {
                    "mean_bp": round(net, 1),
                    "t_net": round(float(net / se), 2) if se else None,
                    "cost_drag_ann_pct": round(c * 252.0 / h / 100.0, 2),
                }
            results["arms"][arm_label][str(h)] = row
            print(
                f"  h={h:3d} n={n:4d} n_indep={n_eff:6.1f} mean={mean_bp:+8.1f}bp "
                f"sd={sd_bp:7.1f} se_nw={se_nw_bp:6.1f} t={row['t_nw']:+5.2f} "
                f"| net t @c20={row['net']['c20']['t_net']:+5.2f}"
            )

        if arm_label == "mom_bottom20":
            r20 = results["arms"][arm_label].get("20")
            if r20:
                print(f"  [note] full grid h=20: n={r20['n_cohorts']} sd={r20['sd_bp']}bp "
                      f"(round-2 grid gave n={par_n} sd={par_sd}bp; the difference is the "
                      f"one cohort date exit_paths never computes)")
                results["parity"]["full_grid_h20_n"] = r20["n_cohorts"]
                results["parity"]["full_grid_h20_sd_bp"] = r20["sd_bp"]

    # ---------------- placebo: zero signal through the same estimator -------
    # Random 20-stock picks, no signal. Any horizon whose |t| rejects more than
    # ~2x nominal is reporting a miscalibrated SE, not an effect.
    print("\n############ [PLACEBO] zero signal, " f"{N_PLACEBO} draws per horizon")
    pk0 = ap.pick_daily(df, "mom20", True)
    rows0 = [date_index[str(r["date"])] for r in pk0.select("date").to_dicts()]
    for h in H_GRID:
        F = F20 if h == 20 else fwd_grid(mats, h)
        pool_ok = tradable & np.isfinite(F)
        valid = []
        pools = []
        for i0 in rows0:
            pool = np.flatnonzero(pool_ok[i0])
            if pool.size >= N_STOCK:
                valid.append(i0)
                pools.append(pool)
        umean = np.array([float(np.mean(F[i0][p])) for i0, p in zip(valid, pools)])
        lag = int(2 * h)
        ts = []
        for draw in range(N_PLACEBO):
            rng = np.random.default_rng(SEED + 1000 * h + draw)
            a = np.empty(len(valid))
            for k, (i0, pool) in enumerate(zip(valid, pools)):
                sel = rng.choice(pool, size=N_STOCK, replace=False)
                a[k] = float(np.mean(F[i0][sel])) - umean[k]
            ts.append(nw_t(a, lag))
        ts = np.array([t for t in ts if np.isfinite(t)])
        fp = float(np.mean(np.abs(ts) >= 2.0))
        results["placebo"][str(h)] = {
            "draws": int(ts.size),
            "sd_of_t": round(float(ts.std()), 2),
            "p_abs_t_ge_2": round(fp, 3),
            "p_abs_t_ge_1p96": round(float(np.mean(np.abs(ts) >= 1.96)), 3),
        }
        print(f"  h={h:3d} n_dates={len(valid):3d} draws={ts.size} sd(t)={ts.std():.2f} "
              f"P(|t|>=2)={fp:.1%} (nominal 4.6%)")

    # ---------------- cost frontier: which h is even tradeable --------------
    print("\n############ [FRONTIER] net edge a_h - c and blocks needed")
    frontier = []
    for arm_label, _ in ARMS:
        for h in H_GRID:
            row = results["arms"][arm_label].get(str(h))
            if not row:
                continue
            for d in DELTA_PER_DAY_BP:
                target_gross = d * h
                for c in COSTS_BP:
                    net = target_gross - c
                    ok = net > 0
                    blocks = (2.802 * row["sd_bp"] / net) ** 2 if ok else None
                    frontier.append({
                        "arm": arm_label, "h": h, "delta_bp_per_day": d, "cost_bp": c,
                        "target_gross_bp": round(target_gross, 1),
                        "target_net_bp": round(net, 1),
                        "tradeable": bool(ok),
                        "blocks_needed": round(float(blocks), 0) if ok else None,
                        "years_needed": round(float(blocks) * h / 252.0, 1) if ok else None,
                    })
    results["frontier"] = frontier
    # compact view: the h at which a delta/day edge first clears each cost
    for d in DELTA_PER_DAY_BP:
        for c in COSTS_BP:
            hs = sorted({row["h"] for row in frontier
                         if row["delta_bp_per_day"] == d and row["cost_bp"] == c and row["tradeable"]})
            print(f"  delta={d:.1f}bp/day  c={c:4.1f}bp  tradeable h: "
                  f"{hs[0] if hs else 'NONE'} .. {hs[-1] if hs else '-'}  "
                  f"(break-even h >= {c / d:.1f})")

    # ---------------- [P] mean-MDE vs CE-MDE, reconciling round 2 -----------
    # Round 2 reported MDE(80%) = 2.802*sd/sqrt(n_eff) on the RETURN series while
    # the decision rule it criticised was a CE difference. The bootstrap SE of
    # the CE difference is in ce_path_audit_result.json; compare the two.
    j = json.loads(Path("analysis/ce_path_audit_result.json").read_text(encoding="utf-8"))
    print("\n############ [P] round-2 MDE: mean-based vs CE bootstrap SE")
    for arm_label, cell in (("mom_bottom20", "hold20 (no SL/TP)"), ("mom_bottom20", "SL5%")):
        d1 = j["results"][arm_label][cell]["delta_path_g1"]
        power_key = "hold20" if "hold20" in cell else "SL5%"
        pw = j["power"][power_key]
        # mean-based SE per 20d, expressed on the same annualised footing
        mean_se_ann = pw["se_bp"] / 1e4 * (252.0 / 20)
        ce_se_ann = d1["se_pct"] / 100.0
        print(f"  {arm_label}/{cell}: mean-based SE={mean_se_ann*100:.1f}%/yr "
              f"| CE bootstrap SE={ce_se_ann*100:.1f}%/yr "
              f"| ratio={mean_se_ann/ce_se_ann:.2f} | MDE(mean)={pw['mde80_bp']:.0f}bp/20d")
        results["ce_vs_mean"][cell] = {
            "mean_se_ann_pct": round(mean_se_ann * 100, 1),
            "ce_boot_se_ann_pct": round(ce_se_ann * 100, 1),
            "ratio": round(float(mean_se_ann / ce_se_ann), 2),
            "mde_mean_bp": pw["mde80_bp"],
            "mde_ce_implied_bp_per20d": round(ce_se_ann * 2.802 * 20 / 252 * 1e4, 0),
        }

    # ---------------- verdicts ---------------------------------------------
    print("\n############ [VERDICT] pre-registered rules")
    best = {}
    for arm_label, _ in ARMS:
        for c in (20.0,):
            cand = [(h, results["arms"][arm_label][str(h)]["net"][f"c{c:g}"]["t_net"])
                    for h in H_GRID if str(h) in results["arms"][arm_label]]
            cand = [(h, t) for h, t in cand if t is not None]
            top = max(cand, key=lambda x: x[1])
            best[arm_label] = {"cost_bp": c, "argmax_h": top[0], "t_net": top[1], "curve": cand}
            print(f"  {arm_label} @c=20bp: max t_net = {top[1]:+.2f} at h={top[0]}  curve="
                  + " ".join(f"h{h}:{t:+.2f}" for h, t in cand))
    r3a = all(best[a]["t_net"] < 2.0 for a in best)
    print(f"  R3-A  round-2 recommendation revoked (max t_net < 2 at c=20bp): {r3a}")

    ratio = {}
    for arm_label, _ in ARMS:
        t20 = results["arms"][arm_label]["20"]["t_nw"]
        t5 = results["arms"][arm_label]["5"]["t_nw"]
        ratio[arm_label] = round(float(t5 / t20), 2) if t20 else None
        print(f"  R3-B  {arm_label}: t(h=5)/t(h=20) = {ratio[arm_label]}  "
              f"(needs >=1.5 for 'shorten it' to pay)")
    results["verdict"] = {"r3a_revoked": r3a, "best_net": best, "t5_over_t20": ratio}

    out = Path("analysis/horizon_scale_result.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
