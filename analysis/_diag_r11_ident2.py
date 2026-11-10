"""R11: R10's "unidentifiable" ruling is three repairable errors.

R10 (docs/asymmetry-round4.md sec 12.15) concludes that the locked-board fill
rate x is "measurable but unidentifiable" because (a) the x that pays is
small and (b) the x that is detectable is absurdly large, and the two sets do
not intersect.  This script recomputes every number in R10's tables from the
same panel and then tests the three load-bearing steps of that ruling.

  P1  WRONG TEST (pure arithmetic, no new data).
      sec 12.15 D's column "years to detect x != 0" is
          need_l / (locked_cells * x / span),  need_l = power_n(sd_lk, mean_lk)
      where power_n is the n for a two-sided *return* t-test at 80% power
      (20 fills at sd 10,325.8 / mean 6,621.8).  x is a frequency: to reject
      H0: x = 0 at 80% power the right n is ln(0.2)/ln(1-x) REGISTRATIONS,
      not 20 fills.  Compare both columns.  The doc column is ~12.4x high on
      every nonzero row.

  P2  NO ECONOMIC THRESHOLD (contradiction inside R10's own output).
      R10's own json has x_star_breakeven = 0.0 and x_star_10pct_annual = 0.0:
      at x = 0 the strategy already shows +20.3% arithmetic annual.  So "how
      large must x be to pay" is ill-posed -- x = 0 pays.  The x>0 branch is
      an increment on a base that is already the 16 break cells, and those
      are the only economically load-bearing object.

  P3  x IS DIRECTLY COMPUTABLE FROM A SHORT FORWARD SAMPLE.
      On a one-price board every share trades at the limit price, so the
      daily `amount` IS V, the traded size at the limit price.  Your 09:30
      limit order fills iff V exceeds the seal queue ahead of you Q.  So
      x = P(V > Q).  Q is not in the panel, but depth_service already
      computes bid1_vol (the seal) intraday; 60 forward sessions give 60
      (V, Q) pairs and a point estimate 1/60 * #{V_i > Q_i}.  That is a
      one-minute compute per day, no tick purchase, no 142 years.

  Also recomputed: V_locked vs V_break traded amounts, and whether traded
  amount alone separates the two board types (it should not, since the seal
  Q is what does).

ASCII only (GBK console).  No production code is touched; masks and the exit
engine are copied verbatim from _diag_r10_ident.py so 57 / 41 / 16 / +212.3bp
are a parity check.
"""
from __future__ import annotations

import glob
import json
import math
import sys
import time
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
HOLD = 20
RNG = np.random.default_rng(SEED)
Z80 = 1.959964 + 0.841621

# R10's per-locked-cell return test (a *filled cell* count, not a registration)
R10_NEED_L = 20


def binom_years(x: float, per_year: float) -> dict:
    """Registrations to reject x=0 at 80% power, and their span in years."""
    if x <= 0.0:
        return {"x": x, "registrations": None, "years": None}
    if x >= 1.0:
        return {"x": x, "registrations": 1, "years": round(1.0 / per_year, 2)}
    n = int(math.ceil(math.log(0.2) / math.log(1.0 - x)))
    return {"x": x, "registrations": n, "years": round(n / per_year, 1)}


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    m = np.isfinite(a) & np.isfinite(b)
    a, b = a[m], b[m]
    if a.size < 6:
        return None
    ra = np.argsort(np.argsort(a)).astype(np.float64)
    rb = np.argsort(np.argsort(b)).astype(np.float64)
    ra -= ra.mean()
    rb -= rb.mean()
    den = math.sqrt(float(ra @ ra) * float(rb @ rb))
    return round(float(ra @ rb / den), 3) if den > 0 else None


def lead(g: np.ndarray, h: int) -> np.ndarray:
    out = np.full_like(g, np.nan)
    T = g.shape[0]
    if h < T:
        out[: T - h] = g[h:]
    return out


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
    open_at_limit = np.zeros((T, S), dtype=bool)
    open_at_limit[:-1] = (lu[1:] > 0) & np.isclose(o[1:], h[1:], rtol=0.0, atol=1e-9)

    sel_open = sub & open_at_limit
    sel_locked = sel_open & lock_entry
    sel_fill = sel_open & ~lock_entry

    # --- parity on the bare counts and the SL5% means
    fin_o = sel_open & np.isfinite(ex_sl5)
    fin_l = sel_locked & np.isfinite(ex_sl5)
    fin_f = sel_fill & np.isfinite(ex_sl5)
    n_o, n_l, n_f = int(fin_o.sum()), int(fin_l.sum()), int(fin_f.sum())
    m_l = float(np.mean(ex_sl5[fin_l])) * 1e4
    m_f = float(np.mean(ex_sl5[fin_f])) * 1e4
    res: dict = {
        "seed": SEED,
        "panel": {"dates": int(T), "date_min": dates[0], "date_max": dates[-1]},
        "parity": {"sel_open_finite": n_o, "sel_locked_finite": n_l,
                   "sel_fill_finite": n_f,
                   "raw_open": int(sel_open.sum()),
                   "mean_sl5_locked_bp": round(m_l, 1),
                   "mean_sl5_fill_bp": round(m_f, 1)},
    }
    print(f"panel T={T} {dates[0]}..{dates[-1]}")
    print(f"[parity] raw open={int(sel_open.sum())} locked={int(sel_locked.sum())} "
          f"fill={int(sel_fill.sum())} -> finite {n_o}/{n_l}/{n_f}, "
          f"mean_sl5_locked={m_l:.1f}bp fill={m_f:.1f}bp")
    assert (n_o, n_l, n_f) == (57, 41, 16), "parity 57/41/16 broken"
    assert abs(m_f - 212.3) < 0.2, f"parity mean {m_f:.1f} != 212.3"

    span_years = T / 252.0
    att_per_year = n_o / span_years
    locked_per_year = n_l / span_years
    p_break = n_f / n_o
    r_break = 212.3
    r_lock = 6621.8

    # --- P1: the wrong test
    x_grid = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 1.0]
    p1 = []
    for x in x_grid:
        b = binom_years(x, locked_per_year)
        doc = R10_NEED_L / (locked_per_year * x)
        p1.append({"x": x,
                   "binom_registrations": b["registrations"],
                   "binom_years": b["years"],
                   "r10_years": round(doc, 1),
                   "ratio_r10_over_binom": (round(doc / b["years"], 1)
                                            if b["years"] else None)})
    res["P1_test_choice"] = {
        "locked_registrations_per_year": round(locked_per_year, 2),
        "r10_need_l": R10_NEED_L,
        "r10_need_l_meaning": ("n for a return t-test: 80% power, mean "
                               "6621.8bp, sd 10325.8bp"),
        "rows": p1,
        "ratio_median": round(float(np.median(
            [r["ratio_r10_over_binom"] for r in p1])), 1),
    }
    print(f"\n[P1] {locked_per_year:.2f} locked registrations/yr; "
          f"R10 divides n=20 (a return test) by fills/yr:")
    print(f"  {'x':>6} {'binom n':>9} {'binom yrs':>10} {'R10 yrs':>9} {'ratio':>6}")
    for r in p1:
        print(f"  {r['x']:>6.3f} {r['binom_registrations']:>9} "
              f"{r['binom_years']:>10.1f} {r['r10_years']:>9.1f} "
              f"{r['ratio_r10_over_binom']:>6.1f}")
    print(f"  median ratio = {res['P1_test_choice']['ratio_median']}x")

    # --- P2: x=0 already pays (R10's own x_star = 0.0)
    ev0 = p_break * r_break
    util0 = min(1.0, n_f * HOLD / T)
    ann0 = ev0 * 1e-4 * att_per_year * util0 * 100
    fill = ex_sl5[sel_fill & np.isfinite(ex_sl5)]
    n_fin = int(fill.size)
    se = float(np.std(fill, ddof=1) / math.sqrt(n_fin))
    tcrit = 2.131
    lo = float(np.mean(fill) * 1e4) - tcrit * se * 1e4
    hi = float(np.mean(fill) * 1e4) + tcrit * se * 1e4
    res["P2_no_threshold"] = {
        "ev_per_attempt_at_x0_bp": round(ev0, 1),
        "arith_annual_at_x0_pct": round(ann0, 1),
        "x_star_breakeven_in_r10_json": 0.0,
        "fill16_abs_ci95_bp": [round(lo, 1), round(hi, 1)],
        "fill16_t": round(float(np.mean(fill)) / se, 2),
        "fill16_positives": int((fill > 0).sum()),
        "reading": ("x=0 already pays; the only economic object is the 16 "
                    "break cells, whose absolute CI covers 0 and whose mean "
                    "is a function of 3 wins."),
    }
    p2 = res["P2_no_threshold"]
    print(f"\n[P2] at x=0: EV {ev0:.1f}bp/attempt, +{ann0:.1f}% arith annual "
          f"(R10's own x_star_breakeven=0.0)")
    print(f"  16-fill absolute 95% CI: [{lo:+.0f}, {hi:+.0f}]bp, "
          f"t={p2['fill16_t']}, {p2['fill16_positives']}/16 positive")

    # --- P3: V and V_break (traded size at the limit price)
    vdf = pl.concat([pl.read_parquet(f, columns=["symbol", "date", "amount"])
                     for f in sorted(glob.glob(
                         "data/kline_daily_enriched/date=*/part.parquet"))],
                    how="vertical_relaxed")
    vdf = vdf.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
    amt = ac.grid_of(vdf, "amount", dates, symbols)
    nxt_amt = lead(amt, 1)    # amount on entry day T+1 (lead(g,h)[t] = g[t+h])
    nxt_close = lead(c, 1)
    v_shares = nxt_amt / np.where(nxt_close > 0, nxt_close, np.nan)

    def vstats(mask: np.ndarray) -> dict | None:
        v = v_shares[mask]
        v = v[np.isfinite(v)]
        if v.size == 0:
            return None
        return {"n": int(v.size),
                "median_shares": round(float(np.median(v)), 0),
                "median_cny": round(float(np.median(nxt_amt[mask][
                    np.isfinite(nxt_amt[mask])])), 0),
                "p10_cny": round(float(np.percentile(
                    nxt_amt[mask][np.isfinite(nxt_amt[mask])], 10)), 0),
                "p90_cny": round(float(np.percentile(
                    nxt_amt[mask][np.isfinite(nxt_amt[mask])], 90)), 0)}

    res["P3_volume"] = {"locked": vstats(sel_locked), "break": vstats(sel_fill)}
    v3 = res["P3_volume"]
    if v3["locked"] and v3["break"]:
        ratio = (v3["break"]["median_cny"] / v3["locked"]["median_cny"]
                 if v3["locked"]["median_cny"] else None)
        res["P3_volume"]["break_over_locked_median"] = round(ratio, 2)
        print(f"\n[P3] traded amount on the one-price entry day "
              f"(= V at the limit price):")
        print(f"  locked {n_l}: median {v3['locked']['median_cny']/1e8:.2f}e8 CNY")
        print(f"  break  {n_f}: median {v3['break']['median_cny']/1e8:.2f}e8 CNY  "
              f"(break/locked = {ratio:.2f}x)")

    # does traded amount alone separate board type?
    v_l = v_shares[sel_locked]
    v_f = v_shares[sel_fill]
    v_l = v_l[np.isfinite(v_l)]
    v_f = v_f[np.isfinite(v_f)]
    # Mann-Whitney U, normal approximation
    if v_l.size >= 5 and v_f.size >= 5:
        allv = np.concatenate([v_l, v_f])
        ranks = np.argsort(np.argsort(allv)).astype(np.float64) + 1.0
        r1 = float(ranks[:v_l.size].sum())
        u1 = r1 - v_l.size * (v_l.size + 1) / 2.0
        mu = v_l.size * v_f.size / 2.0
        sd = math.sqrt(v_l.size * v_f.size * (v_l.size + v_f.size + 1) / 12.0)
        z = (u1 - mu) / sd if sd > 0 else 0.0
        res["P3_volume"]["mw_u_z"] = round(z, 2)
        print(f"  Mann-Whitney U z = {z:+.2f} (|z| small => V alone cannot "
              f"label the board; Q is required)")

    # amount vs realised return, within the locked set (selection direction)
    r_l = ex_sl5[sel_locked]
    vs_l = v_shares[sel_locked]
    rho_l = spearman(r_l, vs_l)
    r_all = ex_sl5[sel_open]
    vs_all = v_shares[sel_open]
    rho_all = spearman(r_all, vs_all)
    res["P3_volume"]["spearman_amount_vs_sl5_locked"] = rho_l
    res["P3_volume"]["spearman_amount_vs_sl5_open_all"] = rho_all
    print(f"  spearman(V, SL5%): locked {rho_l}, open-all {rho_all}")

    # --- P4: the direct forward estimator
    res["P4_forward_estimator"] = {
        "definition": ("x = P(V_day > Q_seal_at_0930); depth_service already "
                       "computes bid1_vol intraday, so 60 forward sessions "
                       "give a 60-observation point estimate and a Clopper-"
                       "Pearson CI"),
        "sessions": 60,
        "cost": "one depth5 write per day, already implemented, 0 files so far",
        "fixes": "replaces both the 142-year event-count estimator and the "
                 "tick purchase; it measures V/Q rather than waiting for fills",
    }
    print("\n[P4] direct estimator: x = P(V > Q) from V (panel) and Q "
          "(depth_service bid1_vol); 60 forward sessions, not 142 years")

    out = Path("analysis/_diag_r11_ident2_result.json")
    out.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nwrote {out}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
