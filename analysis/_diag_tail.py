"""Round 5 seed: the right tail, stated as a base-rate problem.

Rounds 1-4 audited "does mom20 predict the next 20 days" and answered no
(S8 = +26.3bp, t 0.12; days 2-10 = 0 in every bucket).  That is a statement
about a *signal*.  The thing that is actually large in this panel is the
cross-sectional right tail: p99.9 of mom20 = +111.5%, and 0.216% of stock-days
clear +90% over 20 sessions.

Round 4's part 3 shows the in-window excess is a selection statistic of that
tail.  That leaves exactly one question the earlier rounds never posed:

    is *entry into* the tail predictable from observables known at the signal
    date -- and if so, by how much over the base rate?

This script measures the base rate and the lift, nothing else.  No signal is
fitted, no threshold is tuned; the point is to put a number on whether the next
round has anything to search.

  (a) BASE RATE          P(fwd20 >= +90%) over all (date, stock) cells, and in
                         two calendar halves.
  (b) OWN-HISTORY LIFT   P(fwd20 >= +90% | mom20 in its own top 0.2%/1%/5%)
  (c) NEW FEATURE LIFT   P(fwd20 >= +90% | consecutive_limit_ups >= 1/2/3),
                         plus turnover and vol20 deciles.  consecutive_limit_ups
                         is in kline_daily_enriched and was never used in
                         rounds 1-4 (they used turnover_rate, to20, mom20, vol20).
  (d) WHAT IS LEFT       forward 20d active return of the extreme mom20 basket
                         (top 0.2% and top 1%) vs the turnover-decile-matched
                         baseline -- i.e. the same question as S8 at a more
                         extreme cut, where the *forward* number is not diluted
                         by names that merely made the top 0.36%.

Pre-registered reading (written before running):

    R7-A  If every lift in (b)/(c) is <= 1.5x the base rate, the tail is
          not predictable from these observables and the next round should not
          search this panel further for a directional signal.
    R7-B  If (d) is <= 0 at the extreme cut, "the tail has already happened"
          survives; if (d) > +50bp at the top-0.2% cut, the forward number is
          *not* zero everywhere and round 4's "zero" must be qualified.
    R7-C  Any lift that flips sign or halves across the two calendar halves is
          reported as unstable and not used.

ASCII output only (GBK console).
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
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261109
TAIL = 0.90
COST_BP = 20.0
N_DEC = dg.N_DECILE


def extra_grids(cols: list[str], dates: list[str], symbols: list[str]) -> dict[str, np.ndarray]:
    files = sorted(glob.glob(str(ap.DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    src = pl.concat([pl.read_parquet(f, columns=["symbol", "date", *cols])
                     for f in files], how="vertical_relaxed")
    d_map = {d: i for i, d in enumerate(dates)}
    s_map = {s: i for i, s in enumerate(symbols)}
    di = np.array([d_map.get(x, -1) for x in src["date"].cast(pl.Utf8).to_list()])
    si = np.array([s_map.get(x, -1) for x in src["symbol"].to_list()])
    keep = (di >= 0) & (si >= 0)
    out = {}
    for col in cols:
        g = np.full((len(dates), len(symbols)), np.nan)
        g[di[keep], si[keep]] = src[col].to_numpy().astype(np.float64)[keep]
        out[col] = g
    return out


def rate(mask: np.ndarray, ok: np.ndarray) -> tuple[float, int]:
    n = int(ok.sum())
    return (float((mask & ok).sum()) / n if n else float("nan")), n


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    to20 = ac.grid_of(df, "to20", dates, symbols)
    mom = ac.grid_of(df, "mom20", dates, symbols)
    vol = ac.grid_of(df, "vol20", dates, symbols)
    ex = extra_grids(["consecutive_limit_ups", "amount"], dates, symbols)
    clu = ex["consecutive_limit_ups"]
    G = dl.session_grids(mats)
    fwd = G["c2c20"]

    pool = (np.isfinite(mats["close"]) & (mats["close"] > 0)
            & np.isfinite(turn) & (turn > 0))
    ok = pool & np.isfinite(fwd) & np.isfinite(mom)
    out_event = np.isfinite(fwd) & (fwd >= TAIL)
    print(f"panel dates={T} symbols={S}  cells with fwd20&mom20 = {int(ok.sum()):,}")

    res: dict = {"seed": SEED, "tail_threshold_pct": TAIL * 100}
    base, n = rate(out_event, ok)
    res["base_rate"] = {"pct": round(base * 100, 4), "n": n}
    print(f"[a] BASE RATE P(fwd20 >= +{TAIL*100:.0f}%) = {base*100:.4f}%  (n={n:,})")

    halves = [(0, 200), (200, T)]
    hbase = []
    for lo, hi in halves:
        sl = np.zeros_like(ok)
        sl[lo:hi] = True
        r, nn = rate(out_event, ok & sl)
        hbase.append(round(r * 100, 4))
        print(f"    half [{lo}:{hi}) base = {r*100:.4f}%  (n={nn:,})")
    res["base_rate"]["halves_pct"] = hbase

    # ---- (b) own-history lift: mom20 percentile bands ----
    print("[b] lift by mom20 rank (same-day cross-section):")
    lifts = {}
    band_masks: dict[str, np.ndarray] = {}
    for pct, label in ((99.8, "top 0.2%"), (99.0, "top 1%"), (95.0, "top 5%"),
                       (50.0, "bottom 50%")):
        band = np.full_like(mom, np.nan)
        for t in range(T):
            row = mom[t][pool[t]]
            if row.size < 500:
                continue
            thr = np.nanpercentile(row, pct)
            band[t] = np.where(pool[t] & np.isfinite(mom[t]), mom[t] >= thr, np.nan)
        sel = band == 1.0
        band_masks[label] = sel
        r, nn = rate(out_event, ok & sel)
        lifts[label] = {"pct": round(r * 100, 4), "n": nn, "lift": round(r / base, 2)}
        print(f"    {label:12s} P={r*100:7.3f}%  n={nn:6,}  lift={r/base:5.2f}x")
    res["own_history"] = lifts

    # ---- (c) new-feature lift ----
    print("[c] lift by consecutive_limit_ups and by decile:")
    feats = {}
    for k in (1, 2, 3):
        sel = np.isfinite(clu) & (clu >= k)
        r, nn = rate(out_event, ok & sel)
        feats[f"clu>={k}"] = {"pct": round(r * 100, 4), "n": nn, "lift": round(r / base, 2)}
        print(f"    consec_limit_ups>={k}  P={r*100:7.3f}%  n={nn:6,}  lift={r/base:5.2f}x")
    for name, grid in (("to20", to20), ("vol20", vol), ("amount", ex["amount"])):
        dec = np.full_like(grid, np.nan)
        for t in range(T):
            row = grid[t][pool[t]]
            if row.size < 500 or not np.isfinite(row).any():
                continue
            qs = np.nanpercentile(row, [10, 20, 30, 40, 50, 60, 70, 80, 90])
            dec[t] = np.searchsorted(qs, np.where(pool[t] & np.isfinite(grid[t]), grid[t], np.nan))
        for d in (0, 9):
            sel = dec == d
            r, nn = rate(out_event, ok & sel)
            feats[f"{name}_dec{d+1}"] = {"pct": round(r * 100, 4), "n": nn,
                                         "lift": round(r / base, 2)}
            print(f"    {name:6s} decile {d+1:2d}  P={r*100:7.3f}%  n={nn:6,}  lift={r/base:5.2f}x")
        if name == "vol20":
            dec_vol = dec
    res["features"] = feats

    # ---- (c2) is the mom20 / clu lift anything beyond a volatility lift? ----
    # Both "past 20d return in the top 0.2%" and "fwd20 >= +90%" are easier for
    # high-vol names, so a raw lift can be a volatility lift wearing a direction
    # label.  Compare each signal to the base rate *inside* vol20 decile 10.
    print("[c2] incremental lift inside vol20 decile 10 (base there vs signal there):")
    inc: dict = {}
    sub_hi = ok & (dec_vol == 9)
    base_hi, n_hi = rate(out_event, sub_hi)
    print(f"    vol decile 10 base = {base_hi*100:.3f}%  (n={n_hi:,})")
    inc["vol_dec10_base"] = {"pct": round(base_hi * 100, 4), "n": n_hi}
    for label, sel in (("mom top 0.2%", band_masks["top 0.2%"]),
                       ("mom top 1%", band_masks["top 1%"]),
                       ("clu>=3", np.isfinite(clu) & (clu >= 3)),
                       ("mom top 0.2% AND clu>=3",
                        band_masks["top 0.2%"] & np.isfinite(clu) & (clu >= 3))):
        r, nn = rate(out_event, sub_hi & sel)
        inc[label] = {"pct": round(r * 100, 4), "n": nn,
                      "lift_vs_vol_dec10": round(r / base_hi, 2) if base_hi else None}
        print(f"    {label:24s} P={r*100:7.3f}%  n={nn:6,}  "
              f"lift vs vol-dec10 base={r/base_hi:5.2f}x")
        # same conditional lift on each half, so a break is visible
        hh = {}
        for lo2, hi2, tag in ((0, 200, "h1"), (200, T, "h2")):
            sl2 = np.zeros_like(ok)
            sl2[lo2:hi2] = True
            b2, _ = rate(out_event, ok & sl2 & (dec_vol == 9))
            r2, n2 = rate(out_event, ok & sl2 & (dec_vol == 9) & sel)
            hh[tag] = {"lift": round(r2 / b2, 2) if b2 else None, "n": n2}
        inc[label]["halves"] = hh
    res["incremental"] = inc

    # ---- (e) R7-C stability: the same lifts on the two calendar halves ----
    print("[e] R7-C stability of the lifts (two calendar halves):")
    stab: dict = {}
    for label, sel in (("mom top 0.2%", band_masks["top 0.2%"]),
                       ("mom top 1%", band_masks["top 1%"]),
                       ("clu>=1", np.isfinite(clu) & (clu >= 1)),
                       ("clu>=3", np.isfinite(clu) & (clu >= 3))):
        row = {}
        for lo, hi, tag in ((0, 200, "h1"), (200, T, "h2")):
            sl = np.zeros_like(ok)
            sl[lo:hi] = True
            r_b, _ = rate(out_event, ok & sl)
            r_s, nn = rate(out_event, ok & sl & sel)
            row[tag] = {"pct": round(r_s * 100, 4), "n": nn,
                        "lift": round(r_s / r_b, 2) if r_b else None}
        row["ratio_h2_h1"] = (round(row["h2"]["lift"] / row["h1"]["lift"], 2)
                             if row["h1"]["lift"] and row["h2"]["lift"] else None)
        stab[label] = row
        print(f"    {label:14s} h1 lift {row['h1']['lift']:5.2f}x  h2 lift "
              f"{row['h2']['lift']:5.2f}x  ratio {row['ratio_h2_h1']}")
    res["stability_halves"] = stab

    # ---- (d) what is left: extreme mom20 baskets, forward 20d active ----
    print("[d] forward 20d active return of the extreme mom20 baskets (matched):")
    dec_full = np.full((T, S), -1, dtype=np.int64)
    for t in range(T):
        p = np.flatnonzero(pool[t] & np.isfinite(to20[t]))
        if p.size < 200:
            continue
        vals = to20[t][p]
        order = np.argsort(vals, kind="stable")
        rk = np.empty(p.size, dtype=np.int64)
        rk[order] = np.arange(p.size)
        dec_full[t, p] = np.minimum(N_DEC - 1, (rk * N_DEC) // p.size)
    extreme = {}
    for cut, label in ((0.2, "top 0.2%"), (1.0, "top 1%"), (1.8, "top 0.36% (top20)")):
        k = max(5, int(round(5581 * cut / 100)))
        act, dates_used = [], []
        for t in range(T - 20):
            p = np.flatnonzero(pool[t] & np.isfinite(to20[t]) & np.isfinite(mom[t])
                               & np.isfinite(fwd[t]))
            if p.size < 500:
                continue
            thr = np.nanpercentile(mom[t][p], 100 - cut)
            sel = p[mom[t][p] >= thr]
            if sel.size < 5:
                continue
            dec = dec_full[t][sel]
            cnt = np.bincount(dec[dec >= 0], minlength=N_DEC).astype(float)
            if cnt.sum() == 0:
                continue
            w = cnt / cnt.sum()
            mv = fwd[t][p]
            dmean = np.array([np.nanmean(mv[dec_full[t][p] == kk])
                              if (dec_full[t][p] == kk).any() else np.nan
                              for kk in range(N_DEC)])
            a = float(np.nanmean(fwd[t][sel])) - float(np.nansum(w * dmean))
            if np.isfinite(a):
                act.append(a)
        a = np.array(act)
        se = a.std(ddof=0) / np.sqrt(a.size / 20) if a.size > 40 else float("nan")
        extreme[label] = {"n_cohorts": int(a.size),
                          "mean_bp": round(float(a.mean()) * 1e4, 1),
                          "t_n_eff": round(float(a.mean() * 1e4 / (se * 1e4)), 2)
                          if np.isfinite(se) else None,
                          "net_bp": round(float(a.mean()) * 1e4 - COST_BP, 1)}
        print(f"    {label:20s} forward active {extreme[label]['mean_bp']:+8.1f}bp  "
              f"t(n_eff) {extreme[label]['t_n_eff']:+6.2f}  net {extreme[label]['net_bp']:+7.1f}bp")
    res["extreme_baskets"] = extreme

    p = Path("analysis/_diag_tail_result.json")
    p.write_text(json.dumps(dg.clean_json(res), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
