"""Where does the one surviving edge live: intraday, overnight, or neither?

Round 3 so far: the only cell that clears multiple testing is 20-day momentum
winners at h=1 (buy open[d+1], sell close[d+1]), t~4.6 against both the
equal-weighted universe and a turnover-decile-matched basket, stable in both
halves, not explained by locked boards or beta. But h=1 is exactly the trade
A-share T+1 forbids: shares bought on day d+1 cannot be sold on day d+1.

The legal neighbours are:
    open[d+1] -> open[d+2]      one overnight + one full session, sellable
    open[d+1] -> close[d+2]     h=2 in the horizon grid, t ~ 1.4

So decompose the h=1 return into the parts a legal trade can and cannot reach:

    intraday  open[d+1] -> close[d+1]   (the part T+1 forbids)
    overnight close[d+1] -> open[d+2]   (the part a legal hold keeps)
    24h       open[d+1] -> open[d+2]    = intraday + overnight

Kill condition: if the 24h active mean is not positive with |t| >= 2 on both
benchmarks, then the surviving edge exists only in the untradeable window and
must be reported as such, not as a strategy.

ASCII output only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261109
N_STOCK = ap.N_STOCK
N_DECILE = 10
ARMS = [("mom_bottom20", True), ("mom_top20", False)]


def seg_grid(mats: dict, kind: str) -> np.ndarray:
    """One-session forward return grid; index d uses only information after d."""
    o, c = mats["open"], mats["close"]
    T = o.shape[0]
    out = np.full(o.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        if kind == "intraday":      # open[d+1] -> close[d+1]
            r = c[1:] / o[1:] - 1.0
            base = o[1:]
        elif kind == "overnight":   # close[d+1] -> open[d+2]
            r = o[2:] / c[1:-1] - 1.0
            base = c[1:-1]
        elif kind == "open2open":   # open[d+1] -> open[d+2]
            r = o[2:] / o[1:-1] - 1.0
            base = o[1:-1]
        else:
            raise ValueError(kind)
    r = np.where(np.isfinite(r) & (base > 0), r, np.nan)
    out[0:r.shape[0]] = r
    return out


def stats(a: np.ndarray) -> dict:
    a = a[np.isfinite(a)]
    n = a.size
    if n < 20:
        return {"n": n}
    mean_bp = float(a.mean()) * 1e4
    sd_bp = float(a.std(ddof=0)) * 1e4
    se = sd_bp / np.sqrt(n)
    return {"n": n, "mean_bp": round(mean_bp, 1), "sd_bp": round(sd_bp, 1),
            "se_bp": round(se, 1), "t": round(mean_bp / se, 2)}


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    tradable = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)
    to20 = ac.grid_of(df, "to20", dates, symbols)
    print(f"panel dates={T} symbols={S}")

    segs = {k: seg_grid(mats, k) for k in ("intraday", "overnight", "open2open")}
    out: dict = {"seed": SEED, "segments": {}}

    for arm_label, ascending in ARMS:
        pk = ap.pick_daily(df, "mom20", ascending)
        picks_rows = pk.select("date", "symbols").to_dicts()
        out["segments"][arm_label] = {}
        print(f"\n############ {arm_label}")
        for kind, F in segs.items():
            pool_ok = tradable & np.isfinite(F) & np.isfinite(to20)
            a_uni, a_match = [], []
            for r in picks_rows:
                i0 = date_index[str(r["date"])]
                if i0 + 2 >= T:
                    continue
                sel_all = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
                good = sel_all[pool_ok[i0][sel_all]]
                pool = np.flatnonzero(pool_ok[i0])
                if good.size < 5 or pool.size < N_STOCK:
                    continue
                rvals = F[i0][pool]
                vals = to20[i0][pool]
                order = np.argsort(vals, kind="stable")
                rk = np.empty(pool.size, dtype=np.int64)
                rk[order] = np.arange(pool.size)
                dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool.size)
                dec_full = np.full(S, -1, dtype=np.int64)
                dec_full[pool] = dec
                dsel = dec_full[good]
                cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(np.float64)
                w = cnt / cnt.sum()
                dmean = np.array([float(np.mean(rvals[dec == k])) if (dec == k).any() else 0.0
                                  for k in range(N_DECILE)])
                m_pick = float(np.mean(F[i0][good]))
                a_uni.append(m_pick - float(np.mean(rvals)))
                a_match.append(m_pick - float(np.dot(w, dmean)))
            su, sm = stats(np.array(a_uni)), stats(np.array(a_match))
            out["segments"][arm_label][kind] = {"universe": su, "matched": sm}
            print(f"  {kind:10s} universe t={su.get('t'):+6.2f} (mean {su.get('mean_bp'):+8.1f}bp, "
                  f"n={su.get('n')}) | matched t={sm.get('t'):+6.2f} (mean {sm.get('mean_bp'):+8.1f}bp)")

    # check the identity 24h = intraday + overnight on the picks' own mean
    print("\n############ [identity check] open2open vs intraday+overnight (mean active, bp)")
    for arm_label, _ in ARMS:
        i = out["segments"][arm_label]
        lhs = i["open2open"]["universe"]["mean_bp"]
        rhs = i["intraday"]["universe"]["mean_bp"] + i["overnight"]["universe"]["mean_bp"]
        print(f"  {arm_label:13s} open2open={lhs:+8.1f}  intraday+overnight={rhs:+8.1f}  gap={lhs-rhs:+.1f}")
        out["segments"][arm_label]["identity_gap_bp"] = round(float(lhs - rhs), 1)

    # ---------------- placebo for the ONE-SESSION estimator ----------------
    # Unlike the overlapping-h estimator, a single-session return has no induced
    # dependence, so iid random draws ARE the right null here. If a random
    # 20-name basket already earns a positive intraday active return, then the
    # picks' intraday number measures the basket, not the signal.
    print("\n############ [PLACEBO] random 20-name baskets, one-session segments")
    pk0 = ap.pick_daily(df, "mom20", True)
    rows0 = [date_index[str(r["date"])] for r in pk0.select("date").to_dicts()]
    out["placebo"] = {}
    for kind, F in segs.items():
        pool_ok = tradable & np.isfinite(F) & np.isfinite(to20)
        rows, pools, umeans = [], [], []
        for i0 in rows0:
            if i0 + 2 >= T:
                continue
            pool = np.flatnonzero(pool_ok[i0])
            if pool.size < N_STOCK:
                continue
            rows.append(i0)
            pools.append(pool)
            umeans.append(float(np.mean(F[i0][pool])))
        umeans = np.array(umeans)
        means, ts = [], []
        for draw in range(200):
            rng = np.random.default_rng(SEED + 31337 + draw)
            a = np.array([float(np.mean(F[i0][rng.choice(p, size=N_STOCK, replace=False)])) - u
                          for (i0, p), u in zip(zip(rows, pools), umeans)])
            means.append(float(a.mean()) * 1e4)
            ts.append(float(a.mean() / (a.std(ddof=0) / np.sqrt(a.size))))
        means, ts = np.array(means), np.array(ts)
        out["placebo"][kind] = {
            "draws": int(means.size), "null_mean_bp": round(float(means.mean()), 1),
            "null_sd_bp": round(float(means.std()), 1),
            "null_t_mean": round(float(ts.mean()), 2), "null_t_sd": round(float(ts.std()), 2),
        }
        print(f"  {kind:10s} null active mean={means.mean():+7.1f}bp (sd {means.std():.1f}) "
              f"| mean t={ts.mean():+5.2f} sd(t)={ts.std():.2f}  n_dates={len(rows)}")

    # If the stored open were biased or stale, intraday would pick up the error
    # and overnight would give it back -- exactly the signature found above. So
    # check the two cheap failure modes before trusting the decomposition.
    print("\n############ [DQ] is the stored open a real print?")
    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    fin = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (c > 0)
    outside = fin & ((o > h * 1.0001) | (o < l * 0.9999))
    prev_c = np.full_like(c, np.nan)
    prev_c[1:] = c[:-1]
    same = fin & np.isfinite(prev_c) & (np.abs(o - prev_c) <= 1e-9 * np.maximum(1.0, np.abs(prev_c)))
    out["dq"] = {
        "cells_checked": int(fin.sum()),
        "open_outside_high_low": int(outside.sum()),
        "open_outside_frac": round(float(outside.sum()) / max(1, int(fin.sum())), 5),
        "open_equals_prev_close_frac": round(float(same.sum()) / max(1, int(fin.sum())), 5),
    }
    print(f"  open outside [low,high]: {outside.sum()} / {fin.sum()} = "
          f"{out['dq']['open_outside_frac']:.3%}")
    print(f"  open exactly == prev close: {same.sum()} / {fin.sum()} = "
          f"{out['dq']['open_equals_prev_close_frac']:.3%}")
    # would a stale-open dataset manufacture this decomposition? the picks' open
    # gap would then be rank-dependent in the opposite direction to the intraday
    # return. Report the correlation, not a verdict.
    gap = np.full_like(c, np.nan)
    with np.errstate(invalid="ignore"):
        gap[1:] = o[1:] / c[:-1] - 1.0
    corr = []
    for arm_label, ascending in ARMS:
        pk = ap.pick_daily(df, "mom20", ascending)
        cs = []
        for r in pk.select("date", "symbols").to_dicts():
            i0 = date_index[str(r["date"])]
            if i0 + 2 >= T:
                continue
            sel = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
            g = gap[i0 + 1][sel]
            g = g[np.isfinite(g)]
            if g.size >= 5:
                cs.append(float(g.mean()))
        cs = np.array(cs)
        t = cs.mean() / (cs.std(ddof=0) / np.sqrt(cs.size))
        out["dq"][f"{arm_label}_open_gap_bp"] = round(float(cs.mean()) * 1e4, 1)
        out["dq"][f"{arm_label}_open_gap_t"] = round(float(t), 2)
        print(f"  {arm_label:13s} entry-day open gap = {cs.mean()*1e4:+7.1f}bp (t={t:+5.2f})")


    p = Path("analysis/_diag_intraday_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
