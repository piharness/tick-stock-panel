"""Round 5c: is the intraday rebound a property of the PICKS or of the GAP?

Round 5b found that the one robust cell in the panel (open->close, matched active
+49.1bp, t 4.25, both halves positive) becomes larger when the entry is conditioned
on a weak open (+64.5bp at gap <= -1%, t 4.23, 8.7 names/day). The overnight leg is
-67.2bp (t -8.88), which is exactly the toll rounds 3-4 found.

Before calling that "a better picker", ask whether the picker is doing any work at all:
if every stock that opens down 1% rebounds +60bp intraday against its turnover-matched
peers, then the edge belongs to the gap, not to the ranking, and TSP's selection layer
is irrelevant to it.

Pre-registered (written before the first run):
  G-1 gap effect   : universe-wide active i1 for gap <= -k, t >= 2.8 on the date series.
  G-2 selection    : picks' active i1 must exceed the universe gap effect by >= 30bp
                     to be called "selection"; otherwise the edge is the gap.
  G-3 breadth      : report names/day in the gap set. An edge that needs 1,000 names
                     is an execution/liquidity business, not a stock-picking product.
  G-4 both arms    : the same table on mom_bottom20. A gap effect should not care
                     which side of the momentum sort the name came from.

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
N_DECILE = 10
DISCOUNTS = (0.0, 0.005, 0.01, 0.02, 0.03)


def stat(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 8:
        return {"n": n}
    mean_bp = float(x.mean()) * 1e4
    se = float(x.std(ddof=0)) * 1e4 / np.sqrt(n)
    return {"n": n, "mean_bp": round(mean_bp, 1), "t": round(mean_bp / se, 2)}


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

    gap = np.full(o.shape, np.nan)
    i1 = np.full(o.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        # row d must hold the leg that follows signal date d:
        #   gap[d] = close[d-1] -> open[d]   (used at d = i0 + 1)
        #   i1[d]  = open[d+1]  -> close[d+1] (used at d = i0)
        gap[1:] = o[1:] / c[:-1] - 1.0
        i1[:-1] = c[1:] / o[1:] - 1.0

    arms = {"mom_top20": False, "mom_bottom20": True}
    out: dict = {"seed": SEED, "universe": {}, "arms": {}}

    print("############ [UNIVERSE] active intraday by gap bucket (all names, matched benchmark)")
    for k in DISCOUNTS:
        per_date, n_names = [], []
        for i0 in range(T - 1):
            c0 = c[i0]
            pool = tradable[i0] & np.isfinite(i1[i0]) & np.isfinite(turn[i0]) & (c0 > 0)
            if pool.sum() < 500:
                continue
            pool_idx = np.flatnonzero(pool)
            tv = turn[i0][pool_idx]
            order = np.argsort(tv, kind="stable")
            rk = np.empty(pool_idx.size, dtype=np.int64)
            rk[order] = np.arange(pool_idx.size)
            dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool_idx.size)
            dec_full = np.full(S, -1, dtype=np.int64)
            dec_full[pool_idx] = dec
            if k > 0:
                sel = pool & (gap[i0 + 1] <= -k)
            else:
                sel = pool.copy()
            idx = np.flatnonzero(sel)
            if idx.size < 50:
                continue
            r = i1[i0][idx]
            ok = np.isfinite(r)
            idx, r = idx[ok], r[ok]
            if r.size < 50:
                continue
            n_names.append(float(idx.size))
            dsel = dec_full[idx]
            cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
            w = cnt / cnt.sum()
            dm = np.array([float(i1[i0][pool_idx[dec == d]].mean())
                           if (dec == d).any() else 0.0 for d in range(N_DECILE)])
            per_date.append(float(r.mean()) - float(np.dot(w, dm)))
        s = stat(np.array(per_date))
        s["names_per_day"] = round(float(np.mean(n_names)), 0) if n_names else None
        out["universe"][f"gap<=-{k*100:.1f}%"] = s
        print(f"  gap<=-{k*100:>4.1f}%  active={s.get('mean_bp'):+7.1f}bp  t={s.get('t'):+6.2f}  "
              f"n_dates={s.get('n')}  names/day={s.get('names_per_day')}")

    print("\n############ [ARMS] picks vs the same gap bucket")
    for arm, ascending in arms.items():
        pk = ap.pick_daily(df, "mom20", ascending).select("date", "symbols").to_dicts()
        out["arms"][arm] = {}
        print(f"\n  -- {arm} ({'lowest' if ascending else 'highest'} mom20)")
        for k in DISCOUNTS:
            act, n_names = [], []
            for r0 in pk:
                i0 = date_index[str(r0["date"])]
                if i0 + 1 >= T:
                    continue
                sel = np.array([s_map[s] for s in r0["symbols"]], dtype=np.int64)
                sel = sel[tradable[i0][sel] & np.isfinite(i1[i0][sel])]
                if k > 0:
                    sel = sel[gap[i0 + 1][sel] <= -k]
                if sel.size < 3:
                    continue
                n_names.append(float(sel.size))
                c0 = c[i0]
                pool = tradable[i0] & np.isfinite(i1[i0]) & np.isfinite(turn[i0]) & (c0 > 0)
                pool_idx = np.flatnonzero(pool)
                tv = turn[i0][pool_idx]
                order = np.argsort(tv, kind="stable")
                rk = np.empty(pool_idx.size, dtype=np.int64)
                rk[order] = np.arange(pool_idx.size)
                dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool_idx.size)
                dec_full = np.full(S, -1, dtype=np.int64)
                dec_full[pool_idx] = dec
                dsel = dec_full[sel]
                cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
                w = cnt / cnt.sum()
                dm = np.array([float(i1[i0][pool_idx[dec == d]].mean())
                               if (dec == d).any() else 0.0 for d in range(N_DECILE)])
                act.append(float(np.mean(i1[i0][sel])) - float(np.dot(w, dm)))
            s = stat(np.array(act))
            s["names_per_day"] = round(float(np.mean(n_names)), 1) if n_names else None
            halves = [stat(x) for x in np.array_split(np.array(act), 2)] if len(act) >= 16 else []
            s["halves"] = [h.get("mean_bp") for h in halves]
            out["arms"][arm][f"gap<=-{k*100:.1f}%"] = s
            print(f"    gap<=-{k*100:>4.1f}%  active={s.get('mean_bp'):+7.1f}bp  t={s.get('t'):+6.2f}  "
                  f"names/day={s.get('names_per_day')}  halves={s['halves']}")

    print("\n############ [G-2] selection premium = picks - universe")
    for arm in arms:
        for k in DISCOUNTS:
            key = f"gap<=-{k*100:.1f}%"
            a = out["arms"][arm][key].get("mean_bp")
            u = out["universe"][key].get("mean_bp")
            if a is not None and u is not None:
                prem = a - u
                out["arms"][arm][key]["selection_premium_bp"] = round(prem, 1)
                flag = "SELECTION" if prem >= 30 else "GAP"
                print(f"  {arm:<14}{key:<16} picks {a:+7.1f}  universe {u:+7.1f}  "
                      f"premium {prem:+6.1f}  -> {flag}")

    p = Path("analysis/_diag_gap_alpha_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
