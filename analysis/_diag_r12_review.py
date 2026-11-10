"""R12 review (R13): R12's arithmetic holds, but the x-experiment targets the wrong branch.

R12 (docs/asymmetry-round4.md sec 12.17) argues the 60-session depth collection in
R11 cannot resolve x = P(V > Q_0930) because 60 sessions only buys ~6.7 locked
registrations (median 6), so 0 fills has a Clopper-Pearson upper bound of 45.9%,
and separating x=7% from x=0.5% needs 42 registrations (~1.5y) while proving
x != 0 at x=0.5% needs 322 (~11.4y).  This script re-derives the numbers with an
INDEPENDENT implementation (own loader, own matrices, own exit loop) and then
tests the premise R12 never tests: whether the branch that x multiplies can
produce a fill at all.

Sections:
  1. parity rebuilt from raw partitions (57 / 41 / 16, +6621.8 / +212.3 bp)
  2. the fill branch is the OPEN branch: fill => board opened => x <= P(open)
  3. the payoff attached to x (r_locked) is unreachable for a queue placed at 0930
  4. what the tradable branch actually costs to establish (cells, years)
  5. first-passage / Poisson band on the "1.5 years" extrapolation
  6. SPRT vs fixed-n, if the frequency experiment is run anyway
  7. depth_service static facts: 15:02 default, persist keyed by enriched_date,
     symbol universe from enriched cache (why "persist at 0930" is not a config change)
  8. composition of the all-market one-price count (limit-up vs limit-down)

Read-only.  Output analysis/_diag_r12_review_result.json.  ASCII only (GBK console).
"""
from __future__ import annotations

import glob
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
import _diag_legal as dl  # noqa: E402

DATA = Path("data")
HORIZON = 20
SL = 0.05
SEED = 20261114
N_WINDOW = 60


# ------------------------------------------------------------------ data
def extra_grids(dates: list[str], symbols: list[str], cols: tuple[str, ...]) -> dict[str, np.ndarray]:
    """Session grids for columns the shared loader does not carry (amount, limit streaks)."""
    files = sorted(glob.glob(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    src = pl.concat([pl.read_parquet(f, columns=["symbol", "date", *cols]) for f in files],
                    how="vertical_relaxed")
    return {c: ac.grid_of(src, c, dates, symbols) for c in cols}


def load_panel() -> tuple[list[str], list[str], dict[str, np.ndarray]]:
    """Shared panel path (same loader / masks / deciles as R10-R12).

    The statistics below are computed independently of the R11/R12 scripts; the panel
    itself is the same object so the parity anchor is comparable.
    """
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    mats["turnover_rate"] = ac.grid_of(df, "turnover_rate", dates, symbols)
    mats["vol20"] = ac.grid_of(df, "vol20", dates, symbols)
    mats["mom20"] = ac.grid_of(df, "mom20", dates, symbols)
    mats["fwd"] = dl.session_grids(mats)["c2c20"]
    mats.update(extra_grids(dates, symbols, ("amount", "consecutive_limit_ups")))
    return dates, symbols, mats


def exit_sl5(mats: dict[str, np.ndarray]) -> np.ndarray:
    """SL5% / 20-session hold, own loop.  entry = next open; stop checked on every day."""
    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    T = o.shape[0]
    ret = np.full_like(o, np.nan)
    for d in range(T - HORIZON - 1):
        entry = o[d + 1]
        exit_px = c[d + HORIZON].copy()
        alive = np.isfinite(entry) & (entry > 0)
        lvl = entry * (1.0 - SL)
        for k in range(d + 1, d + HORIZON + 1):
            lk = l[k]
            hit = alive & np.isfinite(lk) & np.isfinite(lvl) & (lk <= lvl)
            if hit.any():
                ok = np.isfinite(o[k])
                px = np.where(ok, np.minimum(lvl, o[k]), lvl)
                exit_px = np.where(hit, px, exit_px)
                alive = alive & ~hit
        good = np.isfinite(entry) & (entry > 0) & np.isfinite(exit_px)
        ret[d] = np.where(good, exit_px / entry - 1.0, np.nan)
    return ret


# ------------------------------------------------------------------ stats
def mw_z(a: np.ndarray, b: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if a.size == 0 or b.size == 0:
        return float("nan")
    allv = np.concatenate([a, b])
    order = np.argsort(allv, kind="mergesort")
    ranks = np.empty(allv.size, dtype=np.float64)
    sv = allv[order]
    i = 0
    while i < sv.size:
        j = i
        while j + 1 < sv.size and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    ra = ranks[:a.size].sum()
    n1, n2 = a.size, b.size
    u = ra - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    sd = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    return float((u - mu) / sd) if sd else float("nan")


def _bisect(f, lo: float, hi: float, it: int = 80) -> float:
    flo = f(lo)
    for _ in range(it):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if flo * fm <= 0.0:
            hi = mid
        else:
            lo, flo = mid, fm
    return 0.5 * (lo + hi)


def binom_pmf(k: int, n: int, p: float) -> float:
    if k < 0 or k > n:
        return 0.0
    return math.comb(n, k) * p ** k * (1.0 - p) ** (n - k)


def cp_interval(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson, computed from the other tail than the R11 script (independent)."""
    lo = 0.0 if k == 0 else _bisect(lambda p: 1.0 - sum(binom_pmf(i, n, p) for i in range(k)) - alpha / 2.0, 0.0, 1.0)
    hi = 1.0 if k == n else _bisect(lambda p: sum(binom_pmf(i, n, p) for i in range(k + 1)) - alpha / 2.0, 0.0, 1.0)
    return lo, hi


def poisson_ci(k: int, z: float = 1.959964) -> tuple[float, float]:
    """Garwood exact-ish interval via chi-square quantiles approximated by Wilson-Hilferty."""
    def chi2_q(p: float, df: int) -> float:
        # Wilson-Hilferty: chi2_p(df) ~ df * (1 - 2/(9df) + z_p * sqrt(2/(9df)))^3
        from statistics import NormalDist
        zp = NormalDist().inv_cdf(p)
        return df * (1.0 - 2.0 / (9.0 * df) + zp * math.sqrt(2.0 / (9.0 * df))) ** 3
    lo = chi2_q(0.025, 2 * k) / 2.0 if k > 0 else 0.0
    hi = chi2_q(0.975, 2 * k + 2) / 2.0
    return lo, hi


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz), float-safe."""
    tiny, eps = 1e-300, 3e-12
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_cdf2(t: float, df: int) -> float:
    """Two-sided P(|T| <= t) for Student t with df degrees of freedom."""
    if t <= 0.0:
        return 0.0
    return 1.0 - _betai(df / 2.0, 0.5, df / (df + t * t))


def t_quantile(conf: float, df: int) -> float:
    lo, hi = 0.0, 40.0
    for _ in range(120):
        mid = 0.5 * (lo + hi)
        if t_cdf2(mid, df) < conf:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def t_half_width(sd: float, n: int, conf: float = 0.95) -> float:
    return t_quantile(conf, n - 1) * sd / math.sqrt(n)


def sprt_expected_n(p0: float, p1: float, alpha: float = 0.05, beta: float = 0.20,
                    n_sim: int = 40000, cap: int = 800) -> dict:
    """Wald SPRT Monte Carlo: H0 p0 vs H1 p1, returns expected trials under each."""
    a = math.log((1.0 - beta) / alpha)
    b = math.log(beta / (1.0 - alpha))
    l1 = math.log(p1 / p0)
    l0 = math.log((1.0 - p1) / (1.0 - p0))
    rng = np.random.default_rng(SEED)

    def run(p: float) -> tuple[float, float]:
        draws = rng.random((n_sim, cap)) < p
        inc = np.where(draws, l1, l0)
        cum = np.cumsum(inc, axis=1)
        hit_a = cum >= a
        hit_b = cum <= b
        stop = hit_a | hit_b
        any_stop = stop.any(axis=1)
        first = np.where(any_stop, stop.argmax(axis=1), cap - 1)
        trials = first + 1
        # final decision: boundary crossed at the first stop (A = accept H1)
        crossed_a = np.where(any_stop, cum[np.arange(n_sim), first] >= a, False)
        return float(trials.mean()), float(crossed_a.mean())

    n_h0, rej_h0 = run(p0)
    n_h1, rej_h1 = run(p1)
    return {"alpha": alpha, "beta": beta,
            "expected_n_under_h0": round(n_h0, 1),
            "expected_n_under_h1": round(n_h1, 1),
            "reject_rate_under_h0": round(rej_h0, 4),
            "reject_rate_under_h1": round(rej_h1, 4),
            "bounds": {"A": round(a, 4), "B": round(b, 4)}}


def fixed_n_design(p1: float, alpha: float = 0.05, power: float = 0.80) -> dict:
    n = int(math.ceil(math.log(alpha) / math.log(1.0 - p1)))
    return {"n": n, "k_star": 0,
            "p_zero_under_p1": round((1.0 - p1) ** n, 4)}


# ------------------------------------------------------------------ code facts
def code_facts(root: Path = Path("backend/app")) -> dict:
    """Static read of the depth path: where the time, the key and the universe come from."""
    ds = (root / "services/depth_service.py").read_text(encoding="utf-8").splitlines()
    pref = (root / "services/preferences.py").read_text(encoding="utf-8").splitlines()
    dp = (root / "jobs/daily_pipeline.py").read_text(encoding="utf-8").splitlines()

    def find(lines: list[str], pat: str) -> list[int]:
        rx = re.compile(pat)
        return [i + 1 for i, ln in enumerate(lines) if rx.search(ln)]

    facts = {
        "default_finalize_time_lines": find(pref, r'"depth_finalize_time", \{"hour": 15, "minute": 2\}'),
        "finalize_time_clamp_lines": find(pref, r'15 \* 60 \+ 1|18 \* 60'),
        "cron_job_lines": find(dp, r'id="depth_finalize"'),
        "persist_called_with_enriched_date": find(ds, r"self\._persist\(enriched_date\)"),
        "persist_partition_key": find(ds, r'f"date=\{ds\}"'),
        "universe_from_enriched": find(ds, r"get_enriched_latest\(\)"),
        "intraday_poll_persist_false": find(ds, r"_fetch_and_seal\(persist=False\)"),
        "poll_window_lines": find(ds, r"dt_time\(9, 30\) <= t <= dt_time\(11, 30\)"),
    }
    # does the persist key equal the *session the depth belongs to* or the enriched date?
    idx = facts["persist_called_with_enriched_date"]
    facts["persist_key_is_enriched_date"] = bool(idx)
    facts["note"] = ("an intraday finalize would (a) select symbols from the last finalized "
                     "session's enriched cache and (b) write date={enriched_date}, overwriting "
                     "that session's post-close snapshot; the 15:01-18:00 clamp makes 09:30 "
                     "unreachable through preferences at all")
    return facts


# ------------------------------------------------------------------ main
def main() -> None:
    t0 = time.time()

    def tic(msg: str) -> None:
        print(f"[{time.time() - t0:6.1f}s] {msg}", flush=True)

    df = None  # panel loaded through the shared path
    dates, symbols, mats = load_panel()
    T, S = mats["close"].shape
    tic(f"panel {T}x{S} {dates[0]}..{dates[-1]}")
    turn = mats["turnover_rate"]
    c, o, h, l = mats["close"], mats["open"], mats["high"], mats["low"]
    vol20, mom20, fwd, amount = mats["vol20"], mats["mom20"], mats["fwd"], mats["amount"]

    pool = np.isfinite(c) & (c > 0) & np.isfinite(turn) & (turn > 0)
    ok = pool & np.isfinite(fwd) & np.isfinite(mom20)

    dec = np.full((T, S), np.nan)
    band = np.full((T, S), np.nan)
    for t in range(T):
        m = pool[t] & np.isfinite(vol20[t])
        if m.sum() >= 500:
            qs = np.nanpercentile(vol20[t][m], [10, 20, 30, 40, 50, 60, 70, 80, 90])
            dec[t] = np.where(m, np.searchsorted(qs, vol20[t]), np.nan)
        row = mom20[t][pool[t]]
        if row.size >= 500:
            thr = float(np.nanpercentile(row, 99.8))
            band[t] = np.where(pool[t] & np.isfinite(mom20[t]), mom20[t] >= thr, np.nan)
    v10 = (dec == 9) & ok
    sub = v10 & (band == 1.0)

    ex_sl5 = exit_sl5(mats)
    tic("exit SL5% c2c20")
    lu = np.nan_to_num(mats["consecutive_limit_ups"], nan=0.0)
    open_at_limit = np.zeros((T, S), dtype=bool)
    open_at_limit[:-1] = (lu[1:] > 0) & np.isclose(o[1:], h[1:], rtol=0.0, atol=1e-9)

    both = np.isfinite(h) & np.isfinite(l)
    one_price = np.zeros((T, S), dtype=bool)
    one_price[both] = np.isclose(h[both], l[both], rtol=0.0, atol=1e-9)
    lock_entry = np.zeros((T, S), dtype=bool)
    lock_entry[:-1] = one_price[1:]

    sel_open = sub & open_at_limit
    sel_locked = sel_open & lock_entry
    sel_fill = sel_open & ~lock_entry
    fin_o = sel_open & np.isfinite(ex_sl5)
    fin_l = sel_locked & np.isfinite(ex_sl5)
    fin_f = sel_fill & np.isfinite(ex_sl5)
    n_o, n_l, n_f = int(fin_o.sum()), int(fin_l.sum()), int(fin_f.sum())
    r_l = ex_sl5[fin_l]
    r_f = ex_sl5[fin_f]
    m_l = float(np.mean(r_l)) * 1e4
    m_f = float(np.mean(r_f)) * 1e4
    span_y = T / 252.0
    per_year = n_l / span_y
    open_per_year = n_o / span_y
    fill_per_year = n_f / span_y

    res: dict = {"panel": {"dates": T, "symbols": S, "date_min": dates[0], "date_max": dates[-1],
                           "span_years": round(span_y, 3), "seed": SEED}}
    res["parity"] = {
        "n_open": n_o, "n_locked": n_l, "n_fill": n_f,
        "raw_open": int(sel_open.sum()), "raw_locked": int(sel_locked.sum()),
        "raw_fill": int(sel_fill.sum()),
        "mean_locked_bp": round(m_l, 1), "mean_fill_bp": round(m_f, 1),
        "locked_per_year": round(per_year, 2), "open_per_year": round(open_per_year, 2),
        "fill_per_year": round(fill_per_year, 2),
        "r12_expected": {"n_open": 57, "n_locked": 41, "n_fill": 16,
                         "mean_locked_bp": 6621.8, "mean_fill_bp": 212.3},
        "match": (n_o, n_l, n_f) == (57, 41, 16)
        and abs(m_l - 6621.8) < 0.2 and abs(m_f - 212.3) < 0.2,
    }

    # --- 2. fill is a subset of "board opened"
    p_open = n_f / n_o
    lo_f, hi_f = cp_interval(n_f, n_o)
    res["x_is_bounded_by_p_open"] = {
        "p_open_point": round(p_open, 4),
        "p_open_cp95": [round(lo_f, 4), round(hi_f, 4)],
        "why": "a queue joined at 0930 sits behind bid1_vol; it fills only if the queue is "
               "drained, which means the limit price was abandoned, which is exactly the "
               "h != l (opened) class.  So P(fill) <= P(open) and x = P(V>Q_0930) inherits it.",
        "r12_claimed_range": [0.005, 0.07],
        "r16_claimed_range": [0.07, 0.50],
        "implication": "if x is read as a fill probability, it is not 'unmeasured': it is "
                       "16/57 with 95% CI [17.0%, 41.5%], and 7% sits outside that interval. "
                       "If x is read as P(V > Q_0930), it is a mechanistic proxy bounded above "
                       "by the fill rate.  Either reading makes 42/322 registrations unnecessary.",
        "r16_upper_end_excluded": "the 50% endpoint of R16's [7%, 50%] is above the 41.5% upper "
                                  "confidence limit of the measured fill rate",
    }

    # --- 3. the payoff attached to x
    sd_f = float(np.std(r_f, ddof=1)) * 1e4
    ev_cell_bp = p_open * m_f
    res["x_branch_is_empty"] = {
        "formula_in_r15": "EV = p_break * r_break + (1 - p_break) * x * r_locked",
        "second_term_requires": "fill WHILE the board stays locked (h == l all day)",
        "but_fill_requires": "the 0930 queue to be drained -> h != l -> classified in the 16",
        "so_second_term": 0.0,
        "ev_per_open_cell_bp": round(ev_cell_bp, 1),
        "annualised_pct": round(ev_cell_bp * (open_per_year * 0.872) / 1e4 * 100, 1),
        "note": "matches sec 12.16 C's EV(x=0) = 0.2807 * 212.3 = 59.6bp, i.e. the x-term "
                "was never separable from the 16; measuring x cannot move it.",
    }

    # --- 4. cost of establishing the tradable branch
    half = t_half_width(sd_f, n_f)
    n_mean = int(math.ceil(((1.959964 + 0.841621) * sd_f / m_f) ** 2))
    # per-cell tradable return over the 57 open-at-limit entries: a locked board cannot
    # fill a queue joined at 0930, so it scores 0
    open_vals = ex_sl5[fin_o]
    opened_flag = ~lock_entry[fin_o]
    cell_ret = np.where(opened_flag, open_vals, 0.0)
    sd_cell = float(np.std(cell_ret, ddof=1)) * 1e4
    mu_cell = float(np.mean(cell_ret)) * 1e4
    n_cell = int(math.ceil(((1.959964 + 0.841621) * sd_cell / mu_cell) ** 2))
    res["tradable_branch_cost"] = {
        "fill_mean_bp": round(m_f, 1), "fill_sd_bp": round(sd_f, 1),
        "fill_mean_ci95_bp": [round(m_f - half, 1), round(m_f + half, 1)],
        "fill_t": round(m_f / (sd_f / math.sqrt(n_f)), 2),
        "fill_cells_needed_80pct": n_mean,
        "years_at_observed_fill_rate": round(n_mean / fill_per_year, 1),
        "per_open_cell_mean_bp": round(mu_cell, 1),
        "per_open_cell_sd_bp": round(sd_cell, 1),
        "open_cells_needed_80pct": n_cell,
        "years_at_observed_open_rate": round(n_cell / open_per_year, 1),
        "note": "this is the branch a fill actually lands in; x's experiments (42/322) never "
                "shorten these numbers because they multiply an empty term.",
    }

    # --- 5. how solid is the 1.5y extrapolation
    dens_lo, dens_hi = poisson_ci(n_l)
    res["density_band"] = {
        "registrations": n_l, "span_years": round(span_y, 3),
        "per_year_point": round(per_year, 2),
        "per_year_poisson95": [round(dens_lo / span_y, 2), round(dens_hi / span_y, 2)],
        "years_for_42_by_density": [round(42 / (dens_hi / span_y), 2), round(42 / (dens_lo / span_y), 2)],
        "panel_total_vs_42": f"the panel contains {n_l} locked registrations in {T} sessions: "
                             f"the 42-registration experiment cannot be validated inside it",
        "first_passage_note": "max locked cells in any 60-session window is 19, so 42 needs >=3 "
                              "such windows even at the historical maximum",
    }
    win_l = [int(sel_locked[t:t + N_WINDOW].sum()) for t in range(T - N_WINDOW + 1)]
    res["density_band"]["window60_max"] = max(win_l)
    res["density_band"]["window60_median"] = int(np.percentile(win_l, 50))

    # --- 6. SPRT vs fixed-n
    fixed = fixed_n_design(0.07)
    sprt = sprt_expected_n(0.005, 0.07)
    sprt["years_under_h0"] = round(sprt["expected_n_under_h0"] / per_year, 2)
    sprt["years_under_h1"] = round(sprt["expected_n_under_h1"] / per_year, 2)
    res["design_comparison"] = {
        "fixed_n": {**fixed, "years": round(fixed["n"] / per_year, 2)},
        "sprt": sprt,
        "note": "fixed-n needs 42 registrations regardless of the truth; SPRT stops at ~29 if x "
                "is really 0.5% and ~25 if it is really 7%.  It fixes the stopping rule only, not "
                "the object the registrations measure.",
    }

    # --- 7. depth code facts
    res["depth_path_code_facts"] = code_facts()

    # --- 8. composition of "all-market one-price"
    up = np.zeros((T, S), dtype=bool)
    dn = np.zeros((T, S), dtype=bool)
    prev_c = np.full((T, S), np.nan)
    prev_c[1:] = c[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        chg = c / prev_c - 1.0
    up = one_price & (chg >= 0.095)
    dn = one_price & (chg <= -0.095)
    other = one_price & ~up & ~dn
    win_up = [int(up[t:t + N_WINDOW].sum()) for t in range(T - N_WINDOW + 1)]
    win_dn = [int(dn[t:t + N_WINDOW].sum()) for t in range(T - N_WINDOW + 1)]
    win_oth = [int(other[t:t + N_WINDOW].sum()) for t in range(T - N_WINDOW + 1)]
    res["one_price_composition"] = {
        "total_per_window60_median": int(np.percentile([int(one_price[t:t + N_WINDOW].sum())
                                                        for t in range(T - N_WINDOW + 1)], 50)),
        "limit_up_median": int(np.percentile(win_up, 50)),
        "limit_down_median": int(np.percentile(win_dn, 50)),
        "other_median": int(np.percentile(win_oth, 50)),
        "note": "the 477-per-60-sessions figure counts h == l of any kind; only the limit_up "
                "slice is the long-side analogue of the strategy's registrations.",
    }

    # --- 8b. market-wide counterpart of the fillable branch (what a bigger sample would buy)
    open_all = open_at_limit & np.isfinite(ex_sl5)
    opened_all = open_all & ~lock_entry
    r_all = ex_sl5[opened_all]
    if r_all.size >= 30:
        m_all = float(np.mean(r_all)) * 1e4
        sd_all = float(np.std(r_all, ddof=1)) * 1e4
        hw_all = t_half_width(sd_all, r_all.size)
        welch = (m_f - m_all) / math.sqrt(sd_f ** 2 / n_f + sd_all ** 2 / r_all.size)
        res["market_wide_fill_branch"] = {
            "cells": int(r_all.size),
            "cells_per_year": round(r_all.size / span_y, 1),
            "mean_bp": round(m_all, 1), "sd_bp": round(sd_all, 1),
            "ci95_bp": [round(m_all - hw_all, 1), round(m_all + hw_all, 1)],
            "pool_fill_mean_bp": round(m_f, 1),
            "welch_t_pool_vs_market": round(welch, 2),
            "reading": "the pooled fillable branch is estimable at market scale in weeks; the "
                       "pool's 16 differ from it only by this t, so the pool's 'edge' on the "
                       "branch that can actually be filled is a 16-cell claim, not a sample-size "
                       "problem that 1.5 years of x would fix.",
        }

    out = Path("analysis/_diag_r12_review_result.json")
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
