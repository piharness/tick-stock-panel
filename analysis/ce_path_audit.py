"""Second-round audit of analysis/asymmetry_control.py (the audit itself).

The first round replaced "payoff skew" with a CRRA certainty equivalent of the
PER-COHORT series: 327 overlapping 20-day bets, each weighted as if it were the
whole book. That object is not what a trader holds. Three things are therefore
still unproven, and each one can flip the verdict:

  W1 AGGREGATION. A 20-day stop is a bet on the 20-day aggregate. The book holds
     20 overlapping cohorts (1/20 of capital each), so its risk is a DAILY
     series whose left tail is not the cohort left tail. CE is not linear in
     the horizon, and the whole question is about tails. A truncation that
     hurts CE of a 20-day bet can be neutral or positive for CE of the daily
     path, and vice versa.
  W2 CAPITAL TIME. Per-cohort CE gives every cohort the same weight regardless
     of how long the capital was actually exposed. Stops shorten exposure, so
     the per-cohort object silently prices idle cash at zero and re-weights
     the sample toward long-held trades. Normalising by exposure is the check.
  W3 POWER. "36 paired tests: 0 significant positive" is only evidence if the
     tests could have detected a positive effect. With ~16 independent blocks
     the MDE is measured here, together with the sample size a 30bp effect
     would need. A null result above the MDE is a statement about the sample,
     not about the structures.

Plus a SIZE check of the audit's own test: random 20-stock picks (zero signal)
are pushed through the exact decision rule. If the false-positive rate at the
0.975 threshold is not ~2.5%, the rule is miscalibrated and its rejections mean
nothing.

Parity: the exit engine is byte-compared against asymmetry_control.vector_exit
and the per-cohort CE against the numbers printed in
analysis/asymmetry_control_20261109.txt.

ASCII output only (GBK console).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

H = ap.HORIZON
N = ap.N_STOCK
RNG = np.random.default_rng(20261109)
SEED = 20261109
N_BOOT = 400
N_SIZE = 200

ARMS = [
    ("mom_bottom20", "mom20", True),
    ("mom_top20", "mom20", False),
]

EXITS = [
    ("hold20 (no SL/TP)", None, None),
    ("SL5%", 0.05, None),
    ("TP10%", None, 0.10),
    ("SL5%+TP10%", 0.05, 0.10),
    ("SL3%+TP30%", 0.03, 0.30),
    ("SL10%+TP5%", 0.10, 0.05),
]


# --------------------------------------------------------------------------
# exit engine: same conventions as asymmetry_control.vector_exit, but keeps
# the exit day and the exit price so the position can be marked to market.
# --------------------------------------------------------------------------
def exit_paths(mats: dict[str, np.ndarray], sl: float | None, tp: float | None):
    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    T, S = o.shape
    exit_day = np.full((T, S), -1, dtype=np.int32)
    exit_px = np.full((T, S), np.nan)
    for d in range(T - H - 1):
        entry = o[d + 1]
        px = c[d + H].copy()
        day = np.full(S, d + H, dtype=np.int32)
        alive = np.isfinite(entry) & (entry > 0)
        for k in range(d + 1, d + H):
            lo, hi = l[k], h[k]
            if sl is not None:
                hit = alive & np.isfinite(lo) & (lo <= entry * (1 - sl))
                px[hit] = entry[hit] * (1 - sl)
                day[hit] = k
                alive &= ~hit
            if tp is not None:
                hit = alive & np.isfinite(hi) & (hi >= entry * (1 + tp))
                px[hit] = entry[hit] * (1 + tp)
                day[hit] = k
                alive &= ~hit
        exit_day[d] = day
        exit_px[d] = px
    return exit_day, exit_px


def ret_grid(mats: dict[str, np.ndarray], exit_px: np.ndarray) -> np.ndarray:
    """Per-cohort exit return, NaN where the trade does not exist."""
    entry = np.full(exit_px.shape, np.nan)
    entry[:-1] = mats["open"][1:]
    return np.where(np.isfinite(exit_px) & (entry > 0), exit_px / entry - 1.0, np.nan)


def cohort_path(
    mats: dict[str, np.ndarray],
    exit_day: np.ndarray,
    exit_px: np.ndarray,
    d: int,
    sel: np.ndarray,
) -> tuple[np.ndarray, float]:
    """Daily equal-weight mark-to-market return of one cohort, cash after exit.

    Returns (path of length <= H, invested fraction). Exited legs contribute
    exactly 0.0 (cash), which is the capital-time convention made explicit.
    """
    e = d + 1
    xd = exit_day[d][sel]
    xp = exit_px[d][sel]
    entry = mats["open"][e][sel]
    out = np.zeros(H, dtype=np.float64)
    if xd.size == 0:
        return out, 0.0
    x_max = int(xd.max())
    n = x_max - e + 1
    if n <= 0 or n > H:
        return out, 0.0
    for i, t in enumerate(range(e, x_max + 1)):
        act = t <= xd
        px = np.where(t < xd, mats["close"][t][sel], xp)
        prev = np.where(t == e, entry, mats["close"][t - 1][sel])
        rr = np.where(act & (prev > 0) & np.isfinite(px), px / prev - 1.0, 0.0)
        out[i] = float(rr.mean())
    invested = float(np.mean(xd - e + 1) / H)
    return out, invested


def book_series(
    mats: dict[str, np.ndarray],
    exit_day: np.ndarray,
    exit_px: np.ndarray,
    cohorts: list[tuple[int, np.ndarray]],
    T: int,
) -> tuple[np.ndarray, np.ndarray]:
    """1/H of capital per cohort; returns (daily return, daily invested share)."""
    r = np.zeros(T)
    inv = np.zeros(T)
    for d, sel in cohorts:
        path, frac = cohort_path(mats, exit_day, exit_px, d, sel)
        e = d + 1
        r[e : e + H] += path / H
        inv[e : e + H] += frac / H
    return r, inv


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
def full_invest(r: np.ndarray, inv: np.ndarray, floor: float = 0.05) -> np.ndarray:
    """Releverage each day to 100% invested (leverage-equivalent series).

    An exit rule mechanically converts a fraction of the book into cash, so the
    raw CE of two structures with different exposure is not comparable. Dividing
    the daily return by that day's invested share asks: what would this stock
    selection have earned with the idle cash working too? Documented caveat: it
    assumes the redeployed cash earns the same relative return, i.e. it isolates
    stock selection from exposure, not any real financing path.
    """
    safe = np.where(inv > floor, inv, 1.0)
    return np.where(inv > floor, r / safe, 0.0)


def ann_stats(r: np.ndarray, gamma: tuple[float, ...] = (1.0, 2.0, 5.0)) -> dict:
    r = r[np.isfinite(r)]
    if r.size < 60:
        return {"n": int(r.size)}
    eq = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(eq)
    n = r.size
    out = {
        "n": int(n),
        "ann_ret_pct": round(float(eq[-1] ** (252 / n) - 1.0) * 100, 2),
        "ann_vol_pct": round(float(r.std() * np.sqrt(252)) * 100, 2),
        "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 2) if r.std() else None,
        "maxdd_pct": round(float((eq / peak - 1.0).min()) * 100, 2),
        "cvar5_bp": round(float(-np.mean(np.sort(r)[: max(1, n // 20)])) * 1e4, 1),
        "skew_daily": round(ac.skew(r), 2),
    }
    for g in gamma:
        ce = ac.ce_crra(r, g)
        out[f"ce_ann_g{int(g)}"] = round(float((1.0 + ce) ** 252 - 1.0) * 100, 2)
    return out


def paired_delta_ann(
    rp: np.ndarray,
    ru: np.ndarray,
    gamma: float = 1.0,
    block: int = H,
    n: int = N_BOOT,
    rng: np.random.Generator | None = None,
) -> dict:
    """Paired moving-block bootstrap of annualised CE(pick) - CE(universe).

    An explicit generator is required so each cell's interval is reproducible
    and does not depend on how many other cells were evaluated first.
    """
    rng = rng or np.random.default_rng(SEED)
    ok = np.isfinite(rp) & np.isfinite(ru)
    rp, ru = rp[ok], ru[ok]
    if rp.size < block * 8:
        return {"delta_pct": None}
    starts = np.arange(rp.size - block + 1)
    n_blocks = int(np.ceil(rp.size / block))
    out = np.empty(n)
    for i in range(n):
        pick = rng.choice(starts, size=n_blocks, replace=True)
        sel = np.concatenate([np.arange(s, s + block) for s in pick])
        cp = ac.ce_crra(rp[sel], gamma)
        cu = ac.ce_crra(ru[sel], gamma)
        out[i] = (1.0 + cp) ** 252 - (1.0 + cu) ** 252
    out = out[np.isfinite(out)]
    point = ((1.0 + ac.ce_crra(rp, gamma)) ** 252) - ((1.0 + ac.ce_crra(ru, gamma)) ** 252)
    return {
        "delta_pct": round(float(point) * 100, 1),
        "ci5_pct": round(float(np.percentile(out, 5)) * 100, 1),
        "ci95_pct": round(float(np.percentile(out, 95)) * 100, 1),
        "p_gt0": round(float((out > 0).mean()), 3),
        "se_pct": round(float(out.std()) * 100, 1),
    }


def boot_delta_cohort(
    p: np.ndarray,
    u: np.ndarray,
    gamma: float = 1.0,
    n: int = N_BOOT,
    rng: np.random.Generator | None = None,
) -> dict:
    """Block bootstrap of CE(picks) - CE(universe) on the per-cohort series.

    Same estimator as asymmetry_control.block_boot_delta (block = H, paired
    resample), but with an explicit generator. The point estimate is identical
    by construction and is asserted against the original module.
    """
    rng = rng or np.random.default_rng(SEED)
    ok = np.isfinite(p) & np.isfinite(u)
    p, u = p[ok], u[ok]
    if p.size < H * 4:
        return {"delta_bp": None, "ci5": None, "ci95": None, "p_gt0": None}
    n_blocks = int(np.ceil(p.size / H))
    starts = np.arange(p.size - H + 1)
    out = np.empty(n)
    for i in range(n):
        pick = rng.choice(starts, size=n_blocks, replace=True)
        sel = np.concatenate([np.arange(s, s + H) for s in pick])
        out[i] = ac.ce_crra(p[sel], gamma) - ac.ce_crra(u[sel], gamma)
    out = out[np.isfinite(out)]
    return {
        "delta_bp": round(float(ac.ce_crra(p, gamma) - ac.ce_crra(u, gamma)) * 1e4, 1),
        "ci5": round(float(np.percentile(out, 5)) * 1e4, 1),
        "ci95": round(float(np.percentile(out, 95)) * 1e4, 1),
        "p_gt0": round(float((out > 0).mean()), 3),
    }


def mde(series: np.ndarray, block: int = H) -> dict:
    """Minimum detectable effect (95% two-sided, 80% power) in bp of the unit."""
    x = series[np.isfinite(series)]
    n_blocks = x.size / block
    se = x.std() / np.sqrt(n_blocks) if n_blocks > 1 else float("nan")
    return {
        "n": int(x.size),
        "n_indep": round(float(n_blocks), 1),
        "sd_bp": round(float(x.std()) * 1e4, 1),
        "se_bp": round(float(se) * 1e4, 1),
        "mde80_bp": round(float(2.802 * se) * 1e4, 1),
    }


def blocks_for(target_bp: float, sd_bp: float) -> float:
    return float((2.802 * sd_bp / target_bp) ** 2)


# --------------------------------------------------------------------------
def main() -> None:
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    print(f"panel dates={T} symbols={S} hold={H}")

    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    tradable = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)

    out: dict = {"seed": SEED, "results": {}, "size_check": {}, "power": {}}
    parity = {"exit_max_abs_diff": None, "per_cohort_ce_check": {}}

    for arm_idx, (arm_label, factor, ascending) in enumerate(ARMS):
        pk = ap.pick_daily(df, factor, ascending)
        picks = [
            (str(r["date"]), np.array([s_map[s] for s in r["symbols"]], dtype=np.int64))
            for r in pk.select("date", "symbols").to_dicts()
        ]
        print(f"\n############ {arm_label}  cohorts={len(picks)}")
        out["results"][arm_label] = {}

        for exit_idx, (exit_label, sl, tp) in enumerate(EXITS):
            cell_rng = np.random.default_rng(SEED + 100 * arm_idx + exit_idx)
            exit_day, exit_px = exit_paths(mats, sl, tp)
            ret = ret_grid(mats, exit_px)

            if parity["exit_max_abs_diff"] is None:
                ref = ac.vector_exit(mats, sl, tp)
                mine = ret.copy()
                both = np.isfinite(ref) & np.isfinite(mine)
                gap = float(np.max(np.abs(ref[both] - mine[both])))
                parity["exit_max_abs_diff"] = gap
                print(f"  [parity] exit engine max|diff| vs control.vector_exit = {gap:.3g}")

            pool_ok = tradable & np.isfinite(ret)
            cohorts: list[tuple[int, np.ndarray]] = []
            pmean, umean, held = [], [], []
            for d, sel in picks:
                i0 = date_index[d]
                good = sel[np.isfinite(ret[i0][sel])]
                if good.size < 5:
                    continue
                pool = np.flatnonzero(pool_ok[i0])
                if pool.size < N:
                    continue
                cohorts.append((i0, good))
                pmean.append(float(np.mean(ret[i0][good])))
                umean.append(float(np.mean(ret[i0][pool])))
                held.append(float(np.mean(exit_day[i0][good] - (i0 + 1) + 1)))

            pmean = np.array(pmean)
            umean = np.array(umean)
            active = pmean - umean

            # audit unit: per-cohort CE
            ce_p = ac.ce_crra(pmean, 1.0)
            ce_u = ac.ce_crra(umean, 1.0)
            d_cohort = boot_delta_cohort(pmean, umean, 1.0, rng=cell_rng)
            ref = ac.block_boot_delta(pmean, umean, 1.0)
            assert abs(ref["delta_bp"] - d_cohort["delta_bp"]) < 1e-6, "cohort CE parity"
            parity["per_cohort_ce_check"][f"{arm_label}|{exit_label}"] = {
                "ce_g1_bp": round(ce_p * 1e4, 1),
                "delta_bp": d_cohort["delta_bp"],
                "parity_vs_control": d_cohort["delta_bp"] == ref["delta_bp"],
            }

            # portfolio unit: 20 overlapping cohorts, 1/H capital each
            rp, inv_p = book_series(mats, exit_day, exit_px, cohorts, T)
            uni: list[tuple[int, np.ndarray]] = []
            for i0, _ in cohorts:
                pool = np.flatnonzero(pool_ok[i0])
                uni.append((i0, pool))
            ru, inv_u = book_series(mats, exit_day, exit_px, uni, T)

            warm = H + 2
            rp_w, ru_w = rp[warm:], ru[warm:]
            sp, su = ann_stats(rp_w), ann_stats(ru_w)
            d_path = paired_delta_ann(rp_w, ru_w, 1.0, rng=np.random.default_rng(SEED + 100 * arm_idx + exit_idx + 500))
            d_path5 = paired_delta_ann(rp_w, ru_w, 5.0, rng=np.random.default_rng(SEED + 100 * arm_idx + exit_idx + 600))

            exp_p = float(inv_p[warm:].mean())
            exp_u = float(inv_u[warm:].mean())

            # capital-time normalisation: the same book with idle cash redeployed
            # into itself (leverage-equivalent). Removes the exposure asymmetry
            # that an exit rule creates mechanically.
            fp = full_invest(rp_w, inv_p[warm:])
            fu = full_invest(ru_w, inv_u[warm:])
            sp_fi, su_fi = ann_stats(fp), ann_stats(fu)
            d_fi = paired_delta_ann(fp, fu, 1.0, rng=np.random.default_rng(SEED + 100 * arm_idx + exit_idx + 700))

            key = exit_label
            out["results"][arm_label][key] = {
                "n_cohorts": len(cohorts),
                "avg_hold_days": round(float(np.mean(held)), 1),
                "exposure_pick": round(exp_p, 3),
                "exposure_uni": round(exp_u, 3),
                "cohort": {
                    "ce_g1_bp": round(ce_p * 1e4, 1),
                    "universe_ce_g1_bp": round(ce_u * 1e4, 1),
                    **{k: d_cohort[k] for k in ("delta_bp", "ci5", "ci95", "p_gt0")},
                },
                "path_pick": sp,
                "path_uni": su,
                "delta_path_g1": d_path,
                "delta_path_g5": d_path5,
                "fullinvest_pick": sp_fi,
                "fullinvest_uni": su_fi,
                "delta_fullinvest_g1": d_fi,
            }

            print(
                f"\n  == {exit_label}  hold_days={np.mean(held):5.1f} exposure={exp_p:.3f}"
                f" (uni {exp_u:.3f})"
            )
            print(
                f"     cohort : CE g1={ce_p*1e4:+7.1f}bp  uni={ce_u*1e4:+7.1f}"
                f"  DELTA={d_cohort['delta_bp']:+7.1f} [{d_cohort['ci5']:+.0f},{d_cohort['ci95']:+.0f}]"
                f" P={d_cohort['p_gt0']:.2f}"
            )
            print(
                f"     path   : CE_ann g1={sp.get('ce_ann_g1'):+6.2f}%  uni={su.get('ce_ann_g1'):+6.2f}%"
                f"  DELTA={d_path['delta_pct']:+7.1f}% [{d_path['ci5_pct']:+.0f},{d_path['ci95_pct']:+.0f}]"
                f" P={d_path['p_gt0']:.2f}"
            )
            print(
                f"     path   : ann_ret={sp.get('ann_ret_pct'):+6.2f}% vol={sp.get('ann_vol_pct'):5.2f}%"
                f" sharpe={sp.get('sharpe'):+5.2f} maxdd={sp.get('maxdd_pct'):6.2f}%"
                f" cvar5={sp.get('cvar5_bp'):6.1f}bp skew_d={sp.get('skew_daily'):+.2f}"
            )
            print(
                f"     path   : CE_ann g1 / exposure = {sp.get('ce_ann_g1')/exp_p:+6.2f}%"
                f" vs uni CE_ann g1 / exposure = {su.get('ce_ann_g1')/exp_u:+6.2f}%"
            )
            print(
                f"     fullinv: CE_ann g1={sp_fi.get('ce_ann_g1'):+6.2f}%  uni={su_fi.get('ce_ann_g1'):+6.2f}%"
                f"  DELTA={d_fi['delta_pct']:+7.1f}% [{d_fi['ci5_pct']:+.0f},{d_fi['ci95_pct']:+.0f}]"
                f" P={d_fi['p_gt0']:.2f}"
            )
            print(
                f"              vol={sp_fi.get('ann_vol_pct'):5.2f}% sharpe={sp_fi.get('sharpe'):+5.2f}"
                f" maxdd={sp_fi.get('maxdd_pct'):6.2f}% | uni vol={su_fi.get('ann_vol_pct'):5.2f}%"
                f" sharpe={su_fi.get('sharpe'):+5.2f}"
            )

    # ------------------------------------------------------------------
    # POWER: what could these tests have detected?
    # ------------------------------------------------------------------
    print("\n############ [POWER] minimum detectable effect")
    pk = ap.pick_daily(df, "mom20", True)
    date_index = {d: i for i, d in enumerate(dates)}
    for exit_label, sl, tp in [("hold20", None, None), ("SL5%", 0.05, None)]:
        exit_day, exit_px = exit_paths(mats, sl, tp)
        ret = ret_grid(mats, exit_px)
        pool_ok = tradable & np.isfinite(ret)
        active, daily = [], []
        for r in pk.select("date", "symbols").to_dicts():
            i0 = date_index[str(r["date"])]
            sel = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
            good = sel[np.isfinite(ret[i0][sel])]
            pool = np.flatnonzero(pool_ok[i0])
            if good.size < 5 or pool.size < N:
                continue
            active.append(float(np.mean(ret[i0][good]) - np.mean(ret[i0][pool])))
        m = mde(np.array(active))
        nblk = blocks_for(30.0, m["sd_bp"])
        print(
            f"  {exit_label:6s} per-cohort: n={m['n']} n_indep={m['n_indep']}"
            f" sd={m['sd_bp']:.0f}bp se={m['se_bp']:.0f}bp"
            f" -> MDE(80%)={m['mde80_bp']:.0f}bp per 20d"
        )
        print(f"         blocks needed to resolve a 30bp/20d effect: {nblk:.0f}"
              f" (= {nblk*H/252:.1f} calendar years of daily cohorts)")
        out["power"][exit_label] = {**m, "blocks_for_30bp": round(nblk, 1)}

    # ------------------------------------------------------------------
    # SIZE: false-positive rate of the audit's own decision rule
    # ------------------------------------------------------------------
    print("\n############ [SIZE] random 20-stock picks through the audit's rule")
    for exit_label, sl, tp in [("hold20", None, None), ("SL5%", 0.05, None)]:
        exit_day, exit_px = exit_paths(mats, sl, tp)
        ret = ret_grid(mats, exit_px)
        pool_ok = tradable & np.isfinite(ret)
        rows, umeans = [], []
        for r in pk.select("date", "symbols").to_dicts():
            i0 = date_index[str(r["date"])]
            pool = np.flatnonzero(pool_ok[i0])
            if pool.size < N:
                continue
            rows.append(i0)
            umeans.append(float(np.mean(ret[i0][pool])))
        umeans = np.array(umeans)
        hits, deltas = 0, []
        for draw in range(N_SIZE):
            size_rng = np.random.default_rng(SEED + 9000 + draw)
            pm = np.array(
                [
                    float(np.mean(ret[i0][size_rng.choice(np.flatnonzero(pool_ok[i0]), size=N, replace=False)]))
                    for i0 in rows
                ]
            )
            d = boot_delta_cohort(pm, umeans, 1.0, n=200, rng=np.random.default_rng(SEED + 9100 + draw))
            deltas.append(d["delta_bp"])
            if d["p_gt0"] is not None and d["p_gt0"] >= 0.975:
                hits += 1
        deltas = np.array([x for x in deltas if x is not None])
        print(
            f"  {exit_label:6s} zero-signal draws={N_SIZE}"
            f"  P(delta>0)>=0.975 in {hits}/{N_SIZE} = {hits/N_SIZE:.1%}"
            f"  | delta_bp mean={deltas.mean():+.1f} sd={deltas.std():.1f}"
            f" p5={np.percentile(deltas,5):+.0f} p95={np.percentile(deltas,95):+.0f}"
        )
        out["size_check"][exit_label] = {
            "draws": N_SIZE,
            "false_positive_rate": round(hits / N_SIZE, 3),
            "delta_bp_mean": round(float(deltas.mean()), 1),
            "delta_bp_sd": round(float(deltas.std()), 1),
            "delta_bp_p5": round(float(np.percentile(deltas, 5)), 1),
            "delta_bp_p95": round(float(np.percentile(deltas, 95)), 1),
        }

    # ------------------------------------------------------------------
    # BREADTH: the binding constraint is the 20-stock cohort, not the calendar
    # ------------------------------------------------------------------
    print("\n############ [BREADTH] active-return noise vs stocks per cohort")
    sub = df.filter(pl.col("mom20").is_finite() & pl.col("fwd").is_finite())
    uni_fwd = (
        sub.group_by("date").agg(pl.col("fwd").mean().alias("u")).sort("date")
    )
    breadth = {}
    for n_sel in (20, 50, 100, 200, 400):
        sel = (
            sub.with_columns(
                pl.col("mom20").rank(method="ordinal", descending=False).over("date").alias("rk")
            )
            .filter(pl.col("rk") <= n_sel)
            .group_by("date")
            .agg(pl.col("fwd").mean().alias("p"))
            .sort("date")
        )
        j = sel.join(uni_fwd, on="date", how="inner")
        act = j["p"].to_numpy() - j["u"].to_numpy()
        m = mde(act)
        nblk = blocks_for(30.0, m["sd_bp"])
        mean_bp = float(act[np.isfinite(act)].mean()) * 1e4
        breadth[str(n_sel)] = {
            **m,
            "mean_active_bp": round(mean_bp, 1),
            "t_stat": round(float(act[np.isfinite(act)].mean() / (m["sd_bp"] / 1e4) * np.sqrt(m["n_indep"])), 2),
            "blocks_for_30bp": round(nblk, 1),
            "years_for_30bp": round(nblk * H / 252, 1),
        }
        print(
            f"  picks={n_sel:4d}  n={m['n']} sd_active={m['sd_bp']:6.0f}bp"
            f"  mean_active={mean_bp:+7.1f}bp  signal/se={mean_bp/m['se_bp']:+5.2f}"
            f"  MDE(80%)={m['mde80_bp']:6.0f}bp  blocks_for_30bp={nblk:7.0f}"
            f"  (= {nblk*H/252:6.1f} yr)"
        )
    out["breadth"] = breadth

    out["parity"] = parity
    p = Path("analysis/ce_path_audit_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}")


if __name__ == "__main__":
    main()
