"""Round 5b: the T+1 constraint is not a ban on intraday trading -- it is a ban on
intraday trading *without inventory*.

Rounds 1-4 discarded the only robust cell in the panel (S0, open[d+1]->close[d+1],
matched active +55.9bp, t 4.71, stable in both halves) because "A-share T+1 forbids
selling what you bought today". That is true for a flat account. It is not true for an
account that already holds the name: shares bought on an earlier day remain sellable,
so a base position (底仓) can be round-tripped intraday. The binding constraint then
becomes inventory and capital, not a hard prohibition.

If that is right, the most robust object in this repo is not "unreachable" -- it is
reachable through an overlay, and the thing to measure is the overlay's own payoff.

This script measures the four legs separately, all against the signal-day turnover
decile matched benchmark, so the market's intraday drift is netted out:

    gap   close[d]   -> open[d+1]
    i1    open[d+1]  -> close[d+1]     the leg a base position can sell into
    n1    close[d+1] -> open[d+2]      the leg T+1 forces on a flat account
    i2    open[d+2]  -> close[d+2]

Pre-registered rules (written before the first run):
  B-1 parity   : the unconditional i1 cell must reproduce round 3 (+55.9bp, t 4.71).
  B-2 gate     : an overlay is harvestable only if active i1 has t >= 2.8 and
                 net (mean - 20bp) > 0 on the fill-conditioned subset.
  B-3 stability: both calendar halves positive, ratio in [0.5, 2].
  B-4 base     : the base position's own leg (gap + i1, i.e. S1) is reported, because
                 the overlay is only free if the inventory you must hold does not
                 lose what the overlay earns.
  B-5 capacity : names traded per day and the gap depth at fill are first-class
                 outputs; a cell that only fills 2 names a day is not a strategy.

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

SEED = 20261110
N_STOCK = ap.N_STOCK
N_DECILE = 10
DISCOUNTS = (0.0, 0.005, 0.01, 0.02, 0.03)
COST_BP = 20.0


def leg(mats: dict, kind: str) -> np.ndarray:
    o, c = mats["open"], mats["close"]
    out = np.full(o.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        if kind == "gap":      # close[d] -> open[d+1]
            r = o[1:] / c[:-1] - 1.0
        elif kind == "i1":     # open[d+1] -> close[d+1]
            r = c[1:] / o[1:] - 1.0
        elif kind == "n1":     # close[d+1] -> open[d+2]
            r = o[2:] / c[1:-1] - 1.0
        elif kind == "i2":     # open[d+2] -> close[d+2]
            r = c[2:] / o[2:] - 1.0
        elif kind == "c2":     # open[d+1] -> close[d+2]
            r = c[2:] / o[1:-1] - 1.0
        else:
            raise ValueError(kind)
    out[: r.shape[0]] = r
    return out


def stat(x: np.ndarray, held: int = 1) -> dict:
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 8:
        return {"n": n}
    n_eff = max(n / held, 1.0)
    mean_bp = float(x.mean()) * 1e4
    se = float(x.std(ddof=0)) * 1e4 / np.sqrt(n_eff)
    return {"n": n, "n_eff": round(n_eff, 1), "mean_bp": round(mean_bp, 1),
            "t": round(mean_bp / se, 2) if se else None,
            "hit": round(float((x > 0).mean()), 3),
            "p5_bp": round(float(np.percentile(x, 5)) * 1e4, 1),
            "p50_bp": round(float(np.percentile(x, 50)) * 1e4, 1),
            "p95_bp": round(float(np.percentile(x, 95)) * 1e4, 1)}


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    o, c = mats["open"], mats["close"]
    tradable = np.isfinite(c) & (c > 0) & np.isfinite(o) & (o > 0)

    picks_rows = ap.pick_daily(df, "mom20", False).select("date", "symbols").to_dicts()
    cohorts = []
    for r in picks_rows:
        i0 = date_index[str(r["date"])]
        if i0 + 2 >= T:
            continue
        cohorts.append((i0, np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)))

    L = {k: leg(mats, k) for k in ("gap", "i1", "n1", "i2", "c2")}
    print(f"panel dates={T} symbols={S} cohorts={len(cohorts)}")
    out: dict = {"seed": SEED, "n_cohorts": len(cohorts), "cost_bp": COST_BP, "cells": {}}

    for k in DISCOUNTS:
        label = "all" if k == 0 else f"gap<=-{k*100:.1f}%"
        print(f"\n############ [LEG] {label}")
        cell: dict = {"legs": {}, "fill_rate": None, "names_per_day": None}
        pool_leg = {kk: [] for kk in ("gap", "i1", "n1", "i2", "c2")}
        for kk in pool_leg:
            pool_leg[kk] = []
        all_trades = []
        n_names = []
        for i0, sel in cohorts:
            c0 = c[i0]
            pool = tradable[i0] & np.isfinite(L["c2"][i0]) & np.isfinite(turn[i0]) & (c0 > 0)
            if pool.sum() < 200:
                continue
            lim = c0 * (1.0 - k) if k > 0 else None
            fill = pool.copy() if k == 0 else (
                pool & tradable[i0 + 1] & (o[i0 + 1] <= lim) & (lim > 0))
            good = sel[fill[sel]]
            if good.size < 3:
                continue
            n_names.append(float(good.size))
            # matched benchmark: peers in the same signal-day turnover decile,
            # conditioned on the same fill rule, so the gap distribution matches
            pool_idx = np.flatnonzero(pool)
            tv = turn[i0][pool_idx]
            order = np.argsort(tv, kind="stable")
            rk = np.empty(pool_idx.size, dtype=np.int64)
            rk[order] = np.arange(pool_idx.size)
            dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool_idx.size)
            dec_full = np.full(S, -1, dtype=np.int64)
            dec_full[pool_idx] = dec
            fpool = pool_idx[fill[pool_idx]]
            if fpool.size < 50:
                continue
            dsel = dec_full[good]
            cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
            if cnt.sum() <= 0:
                continue
            w = cnt / cnt.sum()
            for kk, F in L.items():
                pm = np.full(N_DECILE, np.nan)
                for d in range(N_DECILE):
                    m = (dec_full[fpool] == d)
                    if m.sum() >= 5:
                        v = F[i0][fpool[m]]
                        v = v[np.isfinite(v)]
                        if v.size >= 5:
                            pm[d] = float(v.mean())
                ok = np.isfinite(pm) & (w > 0)
                if not ok.any():
                    continue
                bm = float(np.dot(w[ok] / w[ok].sum(), pm[ok]))
                r = F[i0][good]
                r = r[np.isfinite(r)]
                if r.size < 3:
                    continue
                pool_leg[kk].append(float(r.mean()) - bm)
                if kk == "i1":
                    all_trades.extend(r.tolist())

        for kk, arr in pool_leg.items():
            s = stat(np.array(arr), held=1)
            cell["legs"][kk] = s
            if kk in ("gap", "i1", "c2"):
                halves = [stat(x) for x in np.array_split(np.array(arr), 2)] if len(arr) >= 16 else []
                cell["legs"][kk]["halves"] = [h.get("mean_bp") for h in halves]
            print(f"  {kk:4s} active = {s.get('mean_bp'):+8.1f}bp  t={s.get('t'):+5.2f}  "
                  f"n={s.get('n')}  hit={s.get('hit')}  p5/p50/p95="
                  f"{s.get('p5_bp')}/{s.get('p50_bp')}/{s.get('p95_bp')}")
        cell["fill_rate"] = round(float(np.mean(n_names)) / N_STOCK, 3) if n_names else None
        cell["names_per_day"] = round(float(np.mean(n_names)), 1) if n_names else None
        # R4-C analogue: the previous survivor (S1) died here -- its best 5% of
        # cohorts carried 108% of the total. Apply the same test to i1.
        i1s = np.array(pool_leg["i1"])
        if i1s.size >= 20:
            cut = int(np.floor(0.95 * i1s.size))
            trimmed = np.sort(i1s)[:cut]
            cell["i1_drop_best5pct_mean_bp"] = round(float(trimmed.mean()) * 1e4, 1)
            cell["i1_drop_best5pct_share"] = round(
                float(np.sort(i1s)[cut:].sum() / i1s.sum()), 3)
        tr = np.array(all_trades)
        cell["trades"] = {"n": int(tr.size),
                          "mean_bp": round(float(tr.mean()) * 1e4, 1) if tr.size else None,
                          "hit": round(float((tr > 0).mean()), 3) if tr.size else None,
                          "p1_bp": round(float(np.percentile(tr, 1)) * 1e4, 1) if tr.size else None,
                          "p99_bp": round(float(np.percentile(tr, 99)) * 1e4, 1) if tr.size else None}
        print(f"  names/day = {cell['names_per_day']}  fill_rate = {cell['fill_rate']}")
        if tr.size:
            print(f"  per-trade i1: n={tr.size} mean={cell['trades']['mean_bp']:+.1f}bp "
                  f"hit={cell['trades']['hit']} p1/p99={cell['trades']['p1_bp']}/{cell['trades']['p99_bp']}")
        out["cells"][label] = cell

    # ---------------- verdict table ----------------
    print("\n############ [VERDICT] base-position overlay")
    print(f"{'rule':<14}{'names/d':>8}{'i1 act':>9}{'t':>7}{'net':>8}{'gap act':>9}{'c2 act':>8}{'halves':>16}{'drop5%':>9}{'share':>8}")
    for label, cell in out["cells"].items():
        i1 = cell["legs"]["i1"]
        h = i1.get("halves") or []
        print(f"{label:<14}{cell['names_per_day'] or 0:>8.1f}{i1.get('mean_bp', 0):>9.1f}"
              f"{i1.get('t', 0):>7.2f}{i1.get('mean_bp', 0) - COST_BP:>8.1f}"
              f"{cell['legs']['gap'].get('mean_bp', 0):>9.1f}{cell['legs']['c2'].get('mean_bp', 0):>8.1f}"
              f"{str(h):>16}{cell.get('i1_drop_best5pct_mean_bp', 0):>9.1f}"
              f"{cell.get('i1_drop_best5pct_share', 0):>8.2f}")

    p = Path("analysis/_diag_base_t0_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
