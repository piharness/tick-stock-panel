"""Round 6: compose the overlay into a position and charge it capital.

Rounds 5b/5c measured four legs (gap, i1, n1, i2) and reported them one at a
time. A leg t-stat is not an investable object. The only thing that can be
bought is a *position*, and any position able to trade i1 also has to own the
gap, hold twice its notional for four hours, and pay a full round trip every
session.

So this script prices the whole position in one number: return on the capital
the position actually occupies, net of cost, both halves, with a cost sweep.

Structures priced (all equal-weight, 20 names, signal = mom20 top20 at close d):

  HOLD20   close[d] -> close[d+20], 1x capital, 2 sides      (the incumbent)
  HOLD1    close[d] -> close[d+1],  1x capital, 2 sides      ("same signal, 1 day")
  FLAT1    open[d+1] -> close[d+1], illegal without inventory, price of reference
  OVERLAY  buy N at open[d+1], sell N at close[d+1] on top of a base that is
           already held: marginal P&L = +i1, 1x extra capital for 4h, 2 sides
  BOOK     buy N at close[d], buy N at open[d+1], sell N at close[d+1]:
           legal, 2x capital from open to close, 1x overnight, 3 sides
           per-share terminal equity = 2*c1 - c0 - o1

  BOOK is the only structure in which the i1 exposure is reached without
  forecasting: the lot bought at close[d] is sellable at close[d+1], so the
  sale is legal while the lot bought at open[d+1] carries the overnight.

Pre-registered (written before the first run):
  OV-1 identity : BOOK's per-share return on capital must equal i1 + gap/2 to
                  first order; a gap of more than 5bp on the mean means one of
                  the two legs is mis-specified, not that the book is better.
  OV-2 gate     : the book is investable only if net-of-20bp-round-trip return
                  on capital has t >= 2.8 on the date series (327 draws).
  OV-3 halves   : both calendar halves positive with ratio in [0.5, 2].
  OV-4 buyable  : the base is bought at close d, so picks that closed limit-up
                  on d cannot be bought. The whole table is re-run without them.
  OV-5 marginal : the overlay is reported next to the book, because "add 1x
                  intraday to a book you already own" and "restructure the book
                  into 2x intraday" are different products with different t.

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
import asymmetry_probe as ap  # noqa: E402

SEED = 20261110
N_STOCK = 20
N_DECILE = 10
COST_ONE_WAY_BP = 10.0
COST_SWEEP = (0.0, 5.0, 10.0, 15.0, 20.0)
ANN = 250.0


def load_panel() -> pl.DataFrame:
    files = sorted(glob.glob(str(ap.DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    frames = [
        pl.read_parquet(f, columns=["symbol", "date", "open", "high", "low", "close",
                                    "turnover_rate", "amount", "consecutive_limit_ups"])
        for f in files
    ]
    return pl.concat(frames, how="vertical_relaxed").sort(["symbol", "date"])


def mats_of(df: pl.DataFrame):
    dates = sorted(df["date"].unique().to_list())
    symbols = sorted(df["symbol"].unique().to_list())
    wide = df.pivot(on="symbol", index="date", values=["open", "close", "turnover_rate",
                                                       "amount", "consecutive_limit_ups"]).sort("date")
    out = {}
    for col in ["open", "close", "turnover_rate", "amount", "consecutive_limit_ups"]:
        out[col] = wide.select([f"{col}_{s}" for s in symbols]).to_numpy().astype(np.float64)
    return [str(d) for d in dates], symbols, out


def shift_ratio(num: np.ndarray, den: np.ndarray, lead_num: int, lead_den: int, T: int, S: int) -> np.ndarray:
    """row i0 = num[i0+lead_num] / den[i0+lead_den] - 1, NaN when out of range."""
    out = np.full((T, S), np.nan)
    n = T - lead_num
    with np.errstate(divide="ignore", invalid="ignore"):
        r = num[lead_num: lead_num + n] / den[lead_den: lead_den + n] - 1.0
    out[: r.shape[0]] = r
    return out


def stat(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 8:
        return {"n": n}
    mean_bp = float(x.mean()) * 1e4
    sd_bp = float(x.std(ddof=0)) * 1e4
    se = sd_bp / np.sqrt(n)
    return {"n": n, "mean_bp": round(mean_bp, 1), "sd_bp": round(sd_bp, 1),
            "t": round(mean_bp / se, 2) if se else None,
            "sharpe_ann": round(mean_bp / sd_bp * np.sqrt(ANN), 2) if sd_bp else None,
            "ann_pct": round(mean_bp * ANN / 100.0, 1),
            "hit": round(float((x > 0).mean()), 3),
            "p5_bp": round(float(np.percentile(x, 5)) * 1e4, 1),
            "p50_bp": round(float(np.percentile(x, 50)) * 1e4, 1)}


def decile_weight(elig: np.ndarray, turn_row: np.ndarray, pick_idx: np.ndarray):
    """turnover-decile weight vector of the picks over the eligible peer pool."""
    pool = np.flatnonzero(elig)
    if pool.size < 200:
        return None
    tv = turn_row[pool]
    order = np.argsort(tv, kind="stable")
    rk = np.empty(pool.size, dtype=np.int64)
    rk[order] = np.arange(pool.size)
    dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool.size)
    dec_full = np.full(turn_row.size, -1, dtype=np.int64)
    dec_full[pool] = dec
    dsel = dec_full[pick_idx]
    cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
    if cnt.sum() <= 0:
        return None
    return cnt / cnt.sum(), pool, dec_full


def bench_active(F: np.ndarray, i0: int, w, pool, dec_full) -> float | None:
    pm = np.full(N_DECILE, np.nan)
    for d in range(N_DECILE):
        m = dec_full[pool] == d
        if m.sum() >= 5:
            v = F[i0][pool[m]]
            v = v[np.isfinite(v)]
            if v.size >= 5:
                pm[d] = float(v.mean())
    ok = np.isfinite(pm) & (w > 0)
    if not ok.any():
        return None
    return float(np.dot(w[ok] / w[ok].sum(), pm[ok]))


def main() -> None:
    t0 = time.time()
    df = load_panel()
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1.0).alias("mom20"),
    )
    dates, symbols, M = mats_of(df)
    T, S = M["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    d_map = {d: i for i, d in enumerate(dates)}
    o, c, turn = M["open"], M["close"], M["turnover_rate"]
    lu = M["consecutive_limit_ups"]

    # ---- legs, all aligned on the signal row i0 ----
    L = {
        "gap": shift_ratio(o, c, 1, 0, T, S),    # close[d]   -> open[d+1]
        "i1": shift_ratio(c, o, 1, 1, T, S),     # open[d+1]  -> close[d+1]
        "s1": shift_ratio(c, c, 1, 0, T, S),     # close[d]   -> close[d+1]
        "c2": shift_ratio(c, o, 2, 1, T, S),     # open[d+1]  -> close[d+2]
        "hold20": shift_ratio(c, c, 20, 0, T, S),  # close[d]  -> close[d+20]
    }
    # BOOK: 2*c1 - c0 - o1 over capital c0 + o1
    book = np.full((T, S), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        n = T - 2
        book[:n] = 2.0 * c[1:1 + n] / (c[:n] + o[1:1 + n]) - 1.0
    L["book"] = book
    # cost fraction of capital, per structure
    cost_frac = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        cost_frac["book"] = np.full((T, S), np.nan)
        cost_frac["book"][:n] = ((c[:n] + o[1:1 + n] + c[1:1 + n]) / (c[:n] + o[1:1 + n]))
        cost_frac["s1"] = np.full((T, S), np.nan)
        cost_frac["s1"][:T - 1] = ((c[:T - 1] + c[1:]) / c[:T - 1])
        cost_frac["i1"] = np.full((T, S), np.nan)
        cost_frac["i1"][:T - 1] = ((o[1:] + c[1:]) / o[1:])

    picks = (df.filter(pl.col("mom20").is_finite())
             .with_columns(pl.col("mom20").rank(method="ordinal", descending=True).over("date").alias("rk"))
             .filter(pl.col("rk") <= N_STOCK)
             .group_by("date").agg(pl.col("symbol")).sort("date"))
    rows = []
    for r in picks.to_dicts():
        i0 = d_map.get(str(r["date"]))
        if i0 is None or i0 + 20 >= T:
            continue
        rows.append((i0, np.array([s_map[s] for s in r["symbol"]], dtype=np.int64)))
    print(f"panel {dates[0]}..{dates[-1]}  dates={T} symbols={S} cohorts={len(rows)}")

    out = {"seed": SEED, "panel": [dates[0], dates[-1]], "dates": T,
           "cohorts": len(rows), "cost_one_way_bp": COST_ONE_WAY_BP, "variants": {}}

    for label, drop_lu in (("all", False), ("buyable", True)):
        print(f"\n############ [VARIANT] {label}"
              + ("  (drop picks that closed limit-up on d)" if drop_lu else ""))
        acc = {k: [] for k in ("gap", "i1", "s1", "c2", "book", "hold20")}
        cost_acc = {k: [] for k in ("book", "s1", "i1")}
        lu_split = {"lu": [], "nolu": []}
        n_names, amt = [], []
        book_legs = []          # for the OV-1 identity check
        for i0, sel in rows:
            if drop_lu:
                sel = sel[lu[i0][sel] < 1]
            if sel.size < 5:
                continue
            elig = (np.isfinite(c[i0]) & (c[i0] > 0) & np.isfinite(o[i0 + 1]) &
                    np.isfinite(L["book"][i0]) & np.isfinite(turn[i0]))
            dw = decile_weight(elig, turn[i0], sel)
            if dw is None:
                continue
            w, pool, dec_full = dw
            got = False
            for k in acc:
                F = L[k]
                v = F[i0][sel]
                v = v[np.isfinite(v)]
                if v.size < 5:
                    continue
                bm = bench_active(F, i0, w, pool, dec_full)
                if bm is None:
                    continue
                acc[k].append(float(v.mean()) - bm)
                got = True
            for k in cost_acc:
                CF = cost_frac[k][i0][sel]
                CF = CF[np.isfinite(CF)]
                if CF.size:
                    cost_acc[k].append(float(CF.mean()))
            if got:
                n_names.append(float(sel.size))
                bk0 = L["book"][i0][sel]
                m_lu = lu[i0][sel] >= 1
                for tag, mm in (("lu", m_lu), ("nolu", ~m_lu)):
                    vv = bk0[mm]
                    vv = vv[np.isfinite(vv)]
                    if vv.size:
                        lu_split[tag].append((float(vv.mean()), vv.size))
                a = M["amount"][i0][sel]
                a = a[np.isfinite(a)]
                if a.size:
                    amt.append(float(np.median(a)))
                g = L["gap"][i0][sel]
                i_ = L["i1"][i0][sel]
                bk = L["book"][i0][sel]
                fin = np.isfinite(g) & np.isfinite(i_) & np.isfinite(bk)
                if fin.sum() >= 3:
                    book_legs.append((float((i_ + g / 2.0)[fin].mean()),
                                      float(bk[fin].mean()),
                                      float(g[fin].mean()), float(i_[fin].mean())))
        cell = {}
        for k, arr in acc.items():
            s = stat(np.array(arr))
            s["halves"] = [h.get("mean_bp") for h in
                           (stat(x) for x in np.array_split(np.array(arr), 2))] if len(arr) >= 16 else []
            cell[k] = s
        cell["names_per_day"] = round(float(np.mean(n_names)), 1) if n_names else None
        cell["median_amount_cny"] = round(float(np.median(amt)), 0) if amt else None
        # OV-1 identity
        if book_legs:
            bl = np.array(book_legs)
            cell["identity_book_minus_i1_plus_halfgap_bp"] = round(float((bl[:, 1] - bl[:, 0]).mean()) * 1e4, 2)
            cell["raw_gap_bp"] = round(float(bl[:, 2].mean()) * 1e4, 1)
            cell["raw_i1_bp"] = round(float(bl[:, 3].mean()) * 1e4, 1)
        # cost sweep on the book and on the marginal overlay
        sweep = {}
        for h in COST_SWEEP:
            for k in ("book", "i1", "s1"):
                arr = np.array(acc[k]); cf = np.array(cost_acc[k])
                if arr.size and cf.size and cf.size == arr.size:
                    net = arr - h * 1e-4 * cf
                    sweep[f"{k}@{h:.0f}bp"] = round(float(net.mean()) * 1e4, 1)
        cell["net_sweep_bp"] = sweep
        if lu_split["lu"] or lu_split["nolu"]:
            wl = sum(n for _, n in lu_split["lu"]); wn = sum(n for _, n in lu_split["nolu"])
            ml = (sum(m * n for m, n in lu_split["lu"]) / wl * 1e4) if wl else None
            mn = (sum(m * n for m, n in lu_split["nolu"]) / wn * 1e4) if wn else None
            cell["book_raw_split_bp"] = {
                "limit_up_at_close": round(ml, 1) if ml is not None else None,
                "tradable_at_close": round(mn, 1) if mn is not None else None,
                "share_limit_up": round(wl / (wl + wn), 3) if (wl + wn) else None}
        out["variants"][label] = cell

        print(f"  names/day={cell['names_per_day']}  median amount={cell['median_amount_cny']}")
        print(f"  {'structure':<40}{'gross':>9}{'t':>7}{'net10':>8}{'sd':>8}{'Sh':>7}{'ann%':>7}  halves")
        spec = [("HOLD20 close[d]->close[d+20] (incumbent)", "hold20", None),
                ("HOLD1  close[d]->close[d+1]  (same signal,1d)", "s1", "s1"),
                ("FLAT1  open[d+1]->close[d+1] (no inventory)", "i1", "i1"),
                ("BOOK   2x intraday / 1x overnight (legal)", "book", "book")]
        for name, key, ck in spec:
            s = cell[key]
            net = s.get("mean_bp")
            if ck and ck in cost_acc:
                cf = np.array(cost_acc[ck]); arr = np.array(acc[key])
                if arr.size == cf.size:
                    net = round(float((arr - COST_ONE_WAY_BP * 1e-4 * cf).mean()) * 1e4, 1)
                else:
                    net = None
            print(f"  {name:<40}{s.get('mean_bp', 0):>9.1f}{s.get('t', 0):>7.2f}"
                  f"{net if net is not None else 0:>8.1f}{s.get('sd_bp', 0):>8.0f}"
                  f"{s.get('sharpe_ann', 0):>7.2f}{s.get('ann_pct', 0):>7.1f}  {s.get('halves')}")
        print(f"  [OV-1] mean(_book - (i1+gap/2)) = "
              f"{cell.get('identity_book_minus_i1_plus_halfgap_bp')} bp   "
              f"(raw gap {cell.get('raw_gap_bp')}bp, raw i1 {cell.get('raw_i1_bp')}bp)")
        h20 = cell["hold20"]
        ne = h20["n"] / 20.0 if h20.get("n") else 0
        if ne:
            t_eff = h20["mean_bp"] / (h20["sd_bp"] / np.sqrt(ne))
            print(f"  [n_eff] HOLD20 has {h20['n']} overlapping windows -> n_eff={ne:.1f}; "
                  f"t 1.33 as-if-independent becomes {t_eff:.2f}")
        if "book_raw_split_bp" in cell:
            b = cell["book_raw_split_bp"]
            print(f"  [fill] BOOK raw: limit-up-at-close {b['limit_up_at_close']}bp "
                  f"({b['share_limit_up']:.0%} of picks) vs tradable-at-close {b['tradable_at_close']}bp")
        print(f"  [sweep] { {k: v for k, v in sweep.items() if k.startswith('book')} }")
        print(f"  [sweep] { {k: v for k, v in sweep.items() if k.startswith('i1')} }")

    p = Path("analysis/_diag_overlay_capital_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
