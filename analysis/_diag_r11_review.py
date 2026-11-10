"""R11 review (independent): the 60-session experiment in R11's P4 is under-powered.

R11 (docs/asymmetry-round4.md sec 12.16) claims the direct estimator
    x = P(V_day > Q_seal_at_0930)
is computable from 60 forward depth5 sessions as ``#{V>Q}/60``.  That treats
60 *sessions* as 60 *registrations*.  The registration density on the locked
branch is 28.15/yr (41 cells / 1.456 y), i.e. ~6.7 locked registrations per
60 sessions.  This script:

  1. rebuilds the same masks and asserts the 57/41/16 parity (anchor);
  2. rolls a 60-session window over the panel and reports the distribution of
     locked registrations per window (what 60 sessions actually buys);
  3. Clopper-Pearson exact intervals at the observed window size;
  4. power of "see at least one fill" at x = 0.5/1/2/7/10/25/50 percent;
  5. the registrations needed to separate x = 0.5% from x = 7% at 80% power;
  6. cluster structure of the 41 locked cells (independent units vs cells);
  7. the same window count for *all-market* one-price cells (why "collect the
     whole market" changes the parameter, not the sample size of the strategy).

No production code touched.  Output analysis/_diag_r11_review_result.json.
ASCII only (GBK console).
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import _diag_ce_tail_audit as a1  # noqa: E402
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
from _diag_ce_tail_verify import limit_grids  # noqa: E402

N_WINDOW = 60
X_GRID = [0.005, 0.01, 0.02, 0.07, 0.10, 0.25, 0.50]
Z80 = 1.959964 + 0.841621


# ---------------------------------------------------------------- binomial
def binom_pmf(k: int, n: int, p: float) -> float:
    if k < 0 or k > n:
        return 0.0
    return math.comb(n, k) * (p ** k) * ((1.0 - p) ** (n - k))


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k), n <= ~600 so comb() stays in float range."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return min(1.0, sum(binom_pmf(i, n, p) for i in range(k + 1)))


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k)."""
    return 1.0 - binom_cdf(k - 1, n, p)


def _bisect(f, lo: float, hi: float, it: int = 90) -> float:
    flo = f(lo)
    for _ in range(it):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if flo * fm <= 0.0:
            hi = mid
        else:
            lo, flo = mid, fm
    return 0.5 * (lo + hi)


def cp_interval(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson exact interval for k successes in n trials."""
    lo = 0.0 if k == 0 else _bisect(lambda p: binom_sf(k, n, p) - alpha / 2.0, 0.0, 1.0)
    hi = 1.0 if k == n else _bisect(lambda p: binom_cdf(k, n, p) - alpha / 2.0, 0.0, 1.0)
    return lo, hi


def n_to_separate(p0: float, p1: float, alpha: float = 0.05,
                  power: float = 0.80) -> tuple[int, int] | None:
    """Smallest n with a k* s.t. P(X<=k*|p0)<=alpha and P(X<=k*|p1)>=power.

    Direction p1 < p0: reject the *high* rate when too few successes show up.
    """
    for n in range(1, 800):
        # k* candidates need P(X<=0|p0)= (1-p0)^n <= alpha at minimum.
        if binom_cdf(0, n, p0) > alpha:
            continue
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if binom_cdf(mid, n, p0) <= alpha:
                lo = mid
            else:
                hi = mid - 1
        if binom_cdf(lo, n, p1) >= power:
            return n, lo
    return None


def two_prop_n(p1: float, p2: float) -> int:
    """Two-sample proportions n per group, 80% power, two-sided 5%."""
    z = 1.959964 + 0.841621
    pbar = 0.5 * (p1 + p2)
    return int(math.ceil(z * z * (2.0 * pbar * (1.0 - pbar))
                         / ((p1 - p2) ** 2)))


# ---------------------------------------------------------------- clusters
def cluster_sizes(rows: list[tuple[int, str]], gap: int = 5) -> list[int]:
    """Group (date_idx, symbol) cells into runs by symbol, gap in sessions."""
    by_sym: dict[str, list[int]] = {}
    for t, s in rows:
        by_sym.setdefault(s, []).append(t)
    sizes: list[int] = []
    for ts in by_sym.values():
        ts = sorted(set(ts))
        cur = 1
        for a, b in zip(ts, ts[1:]):
            if b - a <= gap:
                cur += 1
            else:
                sizes.append(cur)
                cur = 1
        sizes.append(cur)
    return sorted(sizes, reverse=True)


def pct(v: list[int], q: float) -> int:
    return int(np.percentile(np.asarray(v, dtype=np.float64), q))


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
    open_at_limit = np.zeros((T, S), dtype=bool)
    open_at_limit[:-1] = (lu[1:] > 0) & np.isclose(o[1:], h[1:], rtol=0.0, atol=1e-9)

    sel_open = sub & open_at_limit
    sel_locked = sel_open & lock_entry
    sel_fill = sel_open & ~lock_entry

    fin_o = sel_open & np.isfinite(ex_sl5)
    fin_l = sel_locked & np.isfinite(ex_sl5)
    fin_f = sel_fill & np.isfinite(ex_sl5)
    n_o, n_l, n_f = int(fin_o.sum()), int(fin_l.sum()), int(fin_f.sum())
    m_l = float(np.mean(ex_sl5[fin_l])) * 1e4
    m_f = float(np.mean(ex_sl5[fin_f])) * 1e4
    assert (n_o, n_l, n_f) == (57, 41, 16), "parity 57/41/16 broken"
    assert abs(m_l - 6621.8) < 0.2 and abs(m_f - 212.3) < 0.2, "parity mean broken"
    span_years = T / 252.0
    per_year = n_l / span_years

    res: dict = {
        "panel": {"dates": int(T), "date_min": dates[0], "date_max": dates[-1]},
        "parity": {"n_open": n_o, "n_locked": n_l, "n_fill": n_f,
                   "mean_locked_bp": round(m_l, 1), "mean_fill_bp": round(m_f, 1),
                   "locked_per_year": round(per_year, 2)},
    }

    # --- rolling window: how many locked registrations in 60 sessions?
    win_l = [int(sel_locked[t:t + N_WINDOW].sum()) for t in range(T - N_WINDOW + 1)]
    win_o = [int(sel_open[t:t + N_WINDOW].sum()) for t in range(T - N_WINDOW + 1)]
    win_one = [int(one_price[t:t + N_WINDOW].sum()) for t in range(T - N_WINDOW + 1)]
    res["window_60"] = {
        "window_sessions": N_WINDOW,
        "windows": len(win_l),
        "locked_cells": {"min": min(win_l), "p25": pct(win_l, 25),
                         "median": pct(win_l, 50), "mean": round(float(np.mean(win_l)), 2),
                         "p75": pct(win_l, 75), "max": max(win_l)},
        "open_cells": {"min": min(win_o), "median": pct(win_o, 50),
                       "max": max(win_o)},
        "all_market_one_price_cells": {"min": min(win_one), "median": pct(win_one, 50),
                                       "max": max(win_one)},
        "locked_per_year_x_60_252": round(per_year * N_WINDOW / 252.0, 2),
    }

    # --- CP intervals at the realistic window size
    n_win = pct(win_l, 50)  # what 60 sessions actually buys (median)
    cp_rows = []
    for k in range(0, min(n_win + 3, 15)):
        lo, hi = cp_interval(k, n_win)
        cp_rows.append({"k": k, "cp_lo": round(lo, 4), "cp_hi": round(hi, 4)})
    res["cp_interval_at_window_size"] = {
        "n": n_win,
        "rows": cp_rows,
        "zero_fill_upper_bound": round(cp_interval(0, n_win)[1], 4),
    }

    # --- power to see at least one fill at n = median window (and at 6.7)
    pow_rows = []
    for x in X_GRID:
        pow_rows.append({
            "x": x,
            "p_at_least_1_n_median": round(1.0 - (1.0 - x) ** n_win, 4),
            "p_at_least_1_n_6p7": round(1.0 - (1.0 - x) ** 6.7, 4),
            "n_60pct_power": (int(math.ceil(math.log(0.4) / math.log(1.0 - x)))
                              if x < 1.0 else None),
        })
    res["power_at_window"] = {"n": n_win, "rows": pow_rows}

    # --- registrations to separate x = 0.5% from x = 7%
    sep = n_to_separate(0.07, 0.005)
    res["separate_0p5_vs_7"] = {
        "one_sample_80pct": (None if sep is None else
                             {"n": sep[0], "k_star": sep[1],
                              "years": round(sep[0] / per_year, 1)}),
        "two_prop_80pct_per_group": two_prop_n(0.005, 0.07),
        "two_prop_years": round(two_prop_n(0.005, 0.07) / per_year, 1),
        "note": "60 sessions buys ~6.7 registrations (median window 6); "
                "separating x=7% from x=0.5% by observing zero fills needs "
                "~42 registrations (~1.5y); proving x!=0 at x=0.5% still needs "
                "322 registrations (~11.4y, R11's own row).",
    }

    # --- cluster structure of the 41 locked cells
    rows = [(int(t), symbols[j]) for t, j in zip(*np.where(fin_l))]
    cl = cluster_sizes(rows)
    res["lock_clusters"] = {
        "cells": len(rows),
        "symbols": len({s for _t, s in rows}),
        "clusters_gap5": len(cl),
        "largest": cl[:8],
        "effective_n_note": "B=0.5% n=322 registrations assumes independence; "
                            "cells cluster into ~this many runs.",
    }

    # --- what the market-wide collector would buy (x is then a different x)
    res["interpretation"] = {
        "locked_cells_per_60_sessions_median": n_win,
        "so_60_sessions_is_not_60_observations": True,
        "all_market_60_sessions_median": pct(win_one, 50),
        "difference": "collecting all-market one-price boards gives a large "
                      "sample of a *different* parameter (not the strategy pool's x).",
    }

    out = Path("analysis/_diag_r11_review_result.json")
    out.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
