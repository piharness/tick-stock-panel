"""Round 4, part 2: what IS the open discount, and does it persist?

_part1 (_diag_legal.py) prices nine order types and finds the shape:

    S0 open[d+1]->close[d+1]  +55.9bp (t 4.71)  illegal under T+1
    S3 close[d]->open[d+1]    -28.9bp (t -4.12)  the picks GAP DOWN into the open
    S1 close[d]->close[d+1]   +25.0bp (t 1.83)  legal, = S3 + S0
    S1L close[d+1]->close[d+2] -10.9bp           one-day delay kills it
    S2 open[d+1]->open[d+2]    -1.5bp            round 3's "legal 24h"

So the mechanism sentence in round 3 -- "开盘竞价段的一次性错价, 日内补回" -- asserts
a specific causal shape: the intraday return is the REVERSAL of the open gap. That
assertion was never tested; the two numbers were only reported side by side. This
script tests it, because the two possible answers have opposite consequences:

  (a) intraday ~ -1 * gap with high R^2  -> the object is an auction dislocation,
      i.e. liquidity supply in the opening call, and the "momentum" label is a
      repackaging. Executable only by posting into that auction.
  (b) intraday barely related to gap     -> round 3's mechanism sentence is wrong
      and the intraday drift is information, not a price correction.

Pre-registered rules (written before this run; no probe was run for these):

    R4-E  MECHANISM.  Round 3's "一次性错价" wording survives only if the
          Fama-MacBeth slope of intraday on gap is in [-1.2, -0.6] with
          |t| >= 2, i.e. a near-unit reversal. If the slope is > -0.3 or |t| < 2,
          the mechanism sentence is withdrawn and the intraday leg is reported
          as an unexplained drift.
    R4-F  PERSISTENCE.  The (gap<0, intraday>0) pattern is a persistent property
          only if its sign holds at |t| >= 2 for at least 3 of sessions k=2..5
          after the signal. Otherwise it is a day-1 event and the report must
          say "one session", not "a persistent auction discount".
    R4-G  CONTAMINATION.  Excluding pick-days whose adjustment factor moved
          (|adjusted c2c - raw c2c| > 5bp, i.e. ex-dividend / split) must leave
          S0 within 10% of +55.9bp. Otherwise the decomposition is an
          adjustment artifact and is withdrawn.

Also reports a capacity floor: the median and 10th-percentile daily amount of
the picks, so "can you be the counterparty" has a number attached instead of a
shrug. Amount is the only size proxy in the panel; the auction's own imbalance
is not in the local data (data/auction_benchmark holds 1 date / 6 rows,
data/depth5 is empty, data/kline_minute covers 21 dates from 13:48 -- none of
these can size a call-auction fill, and none is used as evidence).

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
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261109
N_STOCK = ap.N_STOCK
N_DECILE = 10
ARMS = [("mom_top20", False), ("mom_bottom20", True)]
K_MAX = 10


def ratio_grid(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = num / den - 1.0
    ok = np.isfinite(num) & np.isfinite(den) & (den > 0) & np.isfinite(r)
    return np.where(ok, r, np.nan)


def session_grids(mats: dict) -> dict[str, np.ndarray]:
    o, c = mats["open"], mats["close"]
    prev_c = np.full_like(c, np.nan)
    prev_c[1:] = c[:-1]
    return {"gap": ratio_grid(o, prev_c), "intra": ratio_grid(c, o),
            "c2c1": ratio_grid(c, prev_c)}


def extra_grids(cols: list[str], dates: list[str],
                symbols: list[str]) -> dict[str, np.ndarray]:
    """Columns that asymmetry_probe.load_panel does not project (raw_close,
    amount), on the same calendar/symbol axes as calendar_mats."""
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


def cohort_setup(i0: int, sel_all: np.ndarray, pool_base: np.ndarray,
                 to20: np.ndarray, S: int) -> dict | None:
    """Investable pool + turnover deciles, both fixed on the signal date."""
    ok = pool_base[i0] & np.isfinite(to20[i0])
    pool = np.flatnonzero(ok)
    if pool.size < N_STOCK:
        return None
    sel = sel_all[ok[sel_all]]
    if sel.size < 5:
        return None
    vals = to20[i0][pool]
    order = np.argsort(vals, kind="stable")
    rk = np.empty(pool.size, dtype=np.int64)
    rk[order] = np.arange(pool.size)
    dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool.size)
    dec_full = np.full(S, -1, dtype=np.int64)
    dec_full[pool] = dec
    dsel = dec_full[sel]
    cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(np.float64)
    return {"pool": pool, "sel": sel, "dec": dec, "w": cnt / cnt.sum()}


def active_triplet(row: np.ndarray, st: dict) -> tuple[float, float, float] | None:
    """pick mean, universe mean, turnover-decile-matched mean, for one row."""
    rv = row[st["pool"]]
    rp = row[st["sel"]]
    if not np.isfinite(rp).any():
        return None
    dmean = np.array([
        float(np.nanmean(rv[st["dec"] == k])) if (st["dec"] == k).any() else np.nan
        for k in range(N_DECILE)
    ])
    m = float(np.nansum(st["w"] * dmean))
    if not np.isfinite(m):
        return None
    return float(np.nanmean(rp)), float(np.nanmean(rv)), m


def clean_json(o):
    """NaN is not valid JSON: keep nan for printing, write null to disk."""
    if isinstance(o, dict):
        return {k: clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def mean_t(a: np.ndarray, held: int = 1) -> dict:
    a = a[np.isfinite(a)]
    if a.size < 20:
        return {"n": int(a.size), "mean_bp": float("nan"),
                "se_bp": float("nan"), "t": float("nan")}
    se = float(a.std(ddof=0)) * 1e4 / np.sqrt(a.size / held)
    m = float(a.mean()) * 1e4
    return {"n": int(a.size), "mean_bp": round(m, 1), "se_bp": round(se, 1),
            "t": round(m / se, 2)}


def fama_macbeth(x: np.ndarray, y: np.ndarray, groups: np.ndarray) -> dict:
    """Per-cohort OLS slope of y on x, then t over cohorts."""
    slopes, inters = [], []
    for g in np.unique(groups):
        m = groups == g
        xg, yg = x[m], y[m]
        keep = np.isfinite(xg) & np.isfinite(yg)
        xg, yg = xg[keep], yg[keep]
        if xg.size < 8:
            continue
        vx = xg.var()
        if vx <= 0:
            continue
        b = float(((xg - xg.mean()) * (yg - yg.mean())).sum() / ((xg - xg.mean()) ** 2).sum())
        slopes.append(b)
        inters.append(float(yg.mean() - b * xg.mean()))
    s = np.array(slopes)
    if s.size < 20:
        return {"n_cohorts": int(s.size)}
    se = float(s.std(ddof=0) / np.sqrt(s.size))
    return {"n_cohorts": int(s.size), "slope": round(float(s.mean()), 3),
            "slope_se": round(se, 3), "slope_t": round(float(s.mean() / se), 2),
            "intercept_bp": round(float(np.mean(inters)) * 1e4, 1),
            "frac_negative": round(float((s < 0).mean()), 3)}


def ols(x: np.ndarray, y: np.ndarray) -> dict:
    keep = np.isfinite(x) & np.isfinite(y)
    x, y = x[keep], y[keep]
    if x.size < 20:
        return {"n": int(x.size)}
    b = float(((x - x.mean()) * (y - y.mean())).sum() / ((x - x.mean()) ** 2).sum())
    a = float(y.mean() - b * x.mean())
    pred = a + b * x
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    se_b = np.sqrt(ss_res / (x.size - 2) / float(((x - x.mean()) ** 2).sum()))
    return {"n": int(x.size), "slope": round(b, 3), "slope_se": round(float(se_b), 3),
            "slope_t": round(b / float(se_b), 2), "intercept_bp": round(a * 1e4, 1),
            "r2": round(float(r2), 3)}


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    pool_base = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)
    to20 = ac.grid_of(df, "to20", dates, symbols)
    extra = extra_grids(["raw_close", "amount"], dates, symbols)
    raw_c, amount = extra["raw_close"], extra["amount"]
    mom_grid = ac.grid_of(df, "mom20", dates, symbols)
    G = session_grids(mats)
    print(f"panel dates={T} symbols={S}")

    out: dict = {"seed": SEED, "arms": {}}

    # ---- [G2] is the picks' mom20 plausible? ----
    # Round 4's load-bearing number is "the top 20 by mom20 averaged +96.8% over
    # the prior 20 days". Before building anything on it, check that the
    # cross-section actually has 20 names that high.
    cs = {}
    for i0 in (60, 150, 250, 330):
        row = mom_grid[i0][np.isfinite(mom_grid[i0])]
        if row.size < 1000:
            continue
        cs[str(i0)] = {
            "n": int(row.size),
            "p50_pct": round(float(np.percentile(row, 50)) * 100, 1),
            "p90_pct": round(float(np.percentile(row, 90)) * 100, 1),
            "p99_pct": round(float(np.percentile(row, 99)) * 100, 1),
            "p999_pct": round(float(np.percentile(row, 99.9)) * 100, 1),
            "n_above_90pct": int((row > 0.90).sum()),
            "n_above_50pct": int((row > 0.50).sum()),
        }
    pool_all = mom_grid[np.isfinite(mom_grid)]
    cs["pooled"] = {"p999_pct": round(float(np.percentile(pool_all, 99.9)) * 100, 1),
                    "frac_above_90pct": round(float((pool_all > 0.90).mean()), 5)}
    out["mom20_cross_section"] = cs
    print("[G2] cross-section of mom20: " + " | ".join(
        f"d{i0} p99.9={v['p999_pct']:+.1f}% #>90%={v['n_above_90pct']}"
        for i0, v in cs.items() if i0 != "pooled"))

    for arm_label, ascending in ARMS:
        picks_rows = ap.pick_daily(df, "mom20", ascending).select("date", "symbols").to_dicts()
        setups = []
        for r in picks_rows:
            i0 = date_index[str(r["date"])]
            if i0 + K_MAX >= T:
                continue
            st = cohort_setup(i0, np.array([s_map[x] for x in r["symbols"]], dtype=np.int64),
                              pool_base, to20, S)
            if st is None:
                continue
            setups.append((i0, st))
        print(f"\n############ {arm_label}  cohorts={len(setups)}")
        A: dict = {}

        # ---- [A] cohort-level regression: does intraday undo the gap? ----
        gx, iy = [], []
        for i0, st in setups:
            tp = active_triplet(G["gap"][i0 + 1], st)
            ti = active_triplet(G["intra"][i0 + 1], st)
            if tp is None or ti is None:
                continue
            gx.append(tp[0] - tp[2])
            iy.append(ti[0] - ti[2])
        gx, iy = np.array(gx), np.array(iy)
        o = ols(gx, iy)
        A["cohort_ols_intraday_on_gap"] = o
        print(f"  [A] cohort-level intraday~gap (matched): slope {o['slope']:+.3f} "
              f"(t {o['slope_t']:+.2f}) R2 {o['r2']:.3f} intercept {o['intercept_bp']:+.1f}bp "
              f"n={o['n']}")

        # ---- [B] Fama-MacBeth cross-sectional slope inside each cohort ----
        xs, ys, gs = [], [], []
        for gi, (i0, st) in enumerate(setups):
            xr = G["gap"][i0 + 1][st["sel"]]
            yr = G["intra"][i0 + 1][st["sel"]]
            xs.append(xr)
            ys.append(yr)
            gs.append(np.full(xr.size, gi, dtype=np.int64))
        fm = fama_macbeth(np.concatenate(xs), np.concatenate(ys), np.concatenate(gs))
        A["fama_macbeth_intraday_on_gap"] = fm
        print(f"  [B] cross-sectional intraday~gap inside each cohort: slope {fm['slope']:+.3f} "
              f"(t {fm['slope_t']:+.2f}) intercept {fm['intercept_bp']:+.1f}bp "
              f"frac_neg {fm.get('frac_negative')} cohorts={fm.get('n_cohorts')}")

        # ---- [C] intraday active by entry-gap quintile ----
        qs = np.quantile(gx[np.isfinite(gx)], [0.2, 0.4, 0.6, 0.8])
        buckets = np.digitize(gx, qs)
        A["gap_quintile_intraday"] = {}
        print(f"  [C] intraday active by entry-gap quintile (matched):")
        for q in range(5):
            m = buckets == q
            st_q = mean_t(iy[m])
            A["gap_quintile_intraday"][str(q)] = {"gap_mean_bp": round(float(gx[m].mean()) * 1e4, 1),
                                                 **st_q}
            print(f"      Q{q + 1} gap {gx[m].mean()*1e4:+8.1f}bp  intraday "
                  f"{st_q.get('mean_bp'):+8.1f}bp (t {st_q.get('t'):+5.2f}) n={st_q['n']}")

        # ---- [D] the daily cycle on EVERY session from -K to +K ----
        # k=0 is the signal date itself: its legs are already known at the
        # signal close, so it is printed for the reconciliation in [G] but is
        # not a forecast. The benchmark composition is the signal-date turnover
        # decile for all k, so the pre-signal rows compare the same names'
        # earlier sessions against a mildly anachronistic basket -- stated, not
        # fixed, because the gap between the two (see below) is two orders of
        # magnitude larger than any composition drift could be.
        ks = list(range(-K_MAX, K_MAX + 1))
        n_set = len(setups)
        Gmat = np.full((len(ks), n_set), np.nan)
        Imat = np.full((len(ks), n_set), np.nan)
        for j, k in enumerate(ks):
            for idx, (i0, st) in enumerate(setups):
                s = i0 + k
                if s < 1 or s >= T:
                    continue
                tg = active_triplet(G["gap"][s], st)
                ti = active_triplet(G["intra"][s], st)
                if tg is None or ti is None:
                    continue
                Gmat[j, idx] = tg[0] - tg[2]
                Imat[j, idx] = ti[0] - ti[2]
        A["cycle"] = {}
        print(f"  [D] the daily cycle, gap and intraday, sessions k=-{K_MAX}..+{K_MAX} (matched):")
        for j, k in enumerate(ks):
            sg, si_ = mean_t(Gmat[j]), mean_t(Imat[j])
            A["cycle"][str(k)] = {"gap": sg, "intraday": si_}
            tag = "  <- signal date" if k == 0 else ""
            print(f"      k={k:3d} gap {sg.get('mean_bp'):+8.1f}(t{sg.get('t'):+6.2f}) | "
                  f"intraday {si_.get('mean_bp'):+8.1f}(t{si_.get('t'):+6.2f}) "
                  f"n={si_.get('n')}{tag}")

        # ---- [G] reconciliation: do the legs add back to the momentum itself? ----
        # mom20 = close[d]/close[d-20]-1 is the product of (1+gap)(1+intraday) over
        # the 20 sessions d-19..d. If the sum of the measured c2c legs does not
        # reproduce mom20 for the same picks, the leg indexing is wrong and every
        # number above is void.
        lhs, rhs, mat_mom = [], [], []
        for i0, st in setups:
            if i0 - 20 < 1:
                continue
            sel = st["sel"]
            ok = np.ones(sel.size, dtype=bool)
            leg = np.ones(sel.size)
            for s in range(i0 - 19, i0 + 1):
                v = G["c2c1"][s][sel]
                ok &= np.isfinite(v)
                leg = leg * np.where(np.isfinite(v), 1.0 + v, 1.0)
            if ok.sum() < 5:
                continue
            lhs.append(float(np.mean(leg[ok] - 1.0)))
            rhs.append(float(np.mean(mom_grid[i0][sel][ok])))
            mv = mom_grid[i0][st["pool"]]
            dmean = np.array([
                float(np.nanmean(mv[st["dec"] == k])) if (st["dec"] == k).any() else np.nan
                for k in range(N_DECILE)
            ])
            mat_mom.append(float(np.nansum(st["w"] * dmean)))
        lhs, rhs, mat_mom = np.array(lhs), np.array(rhs), np.array(mat_mom)
        A["reconciliation"] = {
            "n_cohorts": int(lhs.size),
            "compounded_20d_legs_pct": round(float(lhs.mean()) * 100, 2),
            "mom20_pct": round(float(rhs.mean()) * 100, 2),
            "ratio": round(float(lhs.mean() / rhs.mean()), 6) if rhs.mean() else None,
            "mom20_median_pct": round(float(np.median(rhs)) * 100, 2),
            "matched_mom20_pct": round(float(np.nanmean(mat_mom)) * 100, 2),
            "mom20_excess_over_matched_pct": round(float(rhs.mean() * 100
                                                       - np.nanmean(mat_mom) * 100), 2),
        }
        rec = A["reconciliation"]
        print(f"  [G] compounded 20d legs = {rec['compounded_20d_legs_pct']:+.2f}% vs mom20 = "
              f"{rec['mom20_pct']:+.2f}% (ratio {rec['ratio']:.6f}) | median mom20 "
              f"{rec['mom20_median_pct']:+.1f}% | matched basket mom20 "
              f"{rec['matched_mom20_pct']:+.2f}% -> excess "
              f"{rec['mom20_excess_over_matched_pct']:+.2f}%")

        # ---- [H] is the post-signal intraday drift stock persistence or signal? ----
        # If a name's intraday excess return is a persistent stock property, then
        # momentum -- which is built out of daily returns -- selects the names with
        # the biggest recent intraday drift, and the "edge" is that property, not a
        # forecast. Test: regress the post-signal intraday on the pre-signal one.
        pre_idx = [j for j, k in enumerate(ks) if -5 <= k <= -1]
        pre = np.nanmean(Imat[pre_idx], axis=0) if pre_idx else np.full(n_set, np.nan)
        post = Imat[ks.index(1)]
        A["persistence_regression"] = {
            "gap": ols(np.nanmean(Gmat[pre_idx], axis=0), Gmat[ks.index(1)]),
            "intraday": ols(pre, post),
        }
        pi = A["persistence_regression"]["intraday"]
        pg = A["persistence_regression"]["gap"]
        print(f"  [H] post(1) ~ pre(-5..-1): intraday slope {pi['slope']:+.3f} "
              f"(t {pi['slope_t']:+.2f}) R2 {pi['r2']:.3f} intercept {pi['intercept_bp']:+.1f}bp | "
              f"gap slope {pg['slope']:+.3f} (t {pg['slope_t']:+.2f}) R2 {pg['r2']:.3f}")

        # ---- [F] capacity proxy ----
        amt = []
        for i0, st in setups:
            v = amount[i0][st["sel"]]
            amt.append(np.nanmedian(v))
        amt = np.array([x for x in amt if np.isfinite(x)])
        A["capacity_median_amount_cny"] = {
            "n_cohorts": int(amt.size),
            "median": round(float(np.median(amt)), 0),
            "p10": round(float(np.percentile(amt, 10)), 0),
        }
        print(f"  [F] pick median daily amount: {np.median(amt)/1e8:.2f}e8 CNY "
              f"(cohort p10 {np.percentile(amt,10)/1e8:.2f}e8)")

        # ---- [E] contamination: drop adjustment-moved pick-days, redo S0/S1 ----
        prev_c = np.full_like(mats["close"], np.nan)
        prev_c[1:] = mats["close"][:-1]
        prev_raw = np.full_like(raw_c, np.nan)
        prev_raw[1:] = raw_c[:-1]
        adj = ratio_grid(mats["close"], prev_c)
        raw = ratio_grid(raw_c, prev_raw)
        moved = np.isfinite(adj) & np.isfinite(raw) & (np.abs(adj - raw) > 5e-4)
        s0a, s0b, s1a, s1b = [], [], [], []
        for i0, st in setups:
            s = i0 + 1
            if s >= T:
                continue
            ti = active_triplet(G["intra"][s], st)
            tc = active_triplet(G["c2c1"][s], st)
            if ti is None or tc is None:
                continue
            dirty = bool(moved[s][st["sel"]].any())
            (s0b if dirty else s0a).append(ti[0] - ti[2])
            (s1b if dirty else s1a).append(tc[0] - tc[2])
        A["contamination"] = {
            "all": {"S0": mean_t(np.array(s0a + s0b)), "S1": mean_t(np.array(s1a + s1b))},
            "clean": {"S0": mean_t(np.array(s0a)), "S1": mean_t(np.array(s1a))},
            "dropped": {"S0": mean_t(np.array(s0b)), "S1": mean_t(np.array(s1b))},
        }
        c = A["contamination"]
        print(f"  [E] drop adjustment-moved pick-days: clean S0 "
              f"{c['clean']['S0'].get('mean_bp'):+.1f}bp "
              f"(all {c['all']['S0'].get('mean_bp'):+.1f}, dropped "
              f"{c['dropped']['S0'].get('n')} cohorts "
              f"{c['dropped']['S0'].get('mean_bp'):+.1f}bp)")

        out["arms"][arm_label] = A

    p = Path("analysis/_diag_gaprev_result.json")
    p.write_text(json.dumps(clean_json(out), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
