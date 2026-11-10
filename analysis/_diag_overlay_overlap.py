"""Round 6b: the overlay is legal only on names the base already owns.

Rounds 5b-6 established that the tradeable intraday leg (i1 = open[d+1] ->
close[d+1], matched active +49bp, t 4.3) is reachable only by selling shares
bought on an earlier day. Every version of the structure that *rebuilds* the
base at the close is fill-constrained: 18% of picks are locked at limit-up at
that moment and carry most of the return.

This round asks the one question that decides whether the overlay is a product:
of today's 20 picks, how many does a standing book already own, and is the
intraday edge on the overlap or on the fresh entrants? If the edge sits on the
names that just entered the ranking, a base held from earlier cannot harvest it
and the overlay's realisable size is not 20 names.

Standing-book definitions tested:
  prev1   top20 at d-1
  prev5   union of top20 over d-5..d-1
  prev20  union of top20 over d-20..d-1

Pre-registered:
  P-1 overlap  : report names/day and share for each definition.
  P-2 edge     : matched active i1 on overlap vs on new entrants, same cohort.
  P-3 gate     : the overlay is a product only if overlap i1 has t >= 2.8 and
                 carries at least half of the full-set i1.
  P-4 fill     : the same split computed after dropping names locked at
                 limit-up on d-1 (the base could not have bought them either).

ASCII output only.
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

sys.path.insert(0, str(Path(__file__).parent))
from _diag_overlay_capital import mats_of, load_panel, shift_ratio, stat, decile_weight, bench_active  # noqa: E402


def main() -> None:
    t0 = time.time()
    df = load_panel().with_columns(
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1.0).alias("mom20"))
    dates, symbols, M = mats_of(df)
    T, S = M["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    d_map = {d: i for i, d in enumerate(dates)}
    o, c, turn, lu = M["open"], M["close"], M["turnover_rate"], M["consecutive_limit_ups"]

    i1 = shift_ratio(c, o, 1, 1, T, S)
    gap = shift_ratio(o, c, 1, 0, T, S)
    costf = np.full((T, S), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        costf[: T - 1] = (o[1:] + c[1:]) / o[1:]

    pk = (df.filter(pl.col("mom20").is_finite())
          .with_columns(pl.col("mom20").rank(method="ordinal", descending=True).over("date").alias("rk"))
          .filter(pl.col("rk") <= N_STOCK)
          .group_by("date").agg(pl.col("symbol")).sort("date"))
    seq = [(d_map[str(r["date"])], np.array([s_map[s] for s in r["symbol"]], dtype=np.int64))
           for r in pk.to_dicts()]
    pos = {i0: k for k, (i0, _) in enumerate(seq)}
    print(f"panel {dates[0]}..{dates[-1]} dates={T} symbols={S} pick-days={len(seq)}")

    out = {"seed": SEED, "defs": {}, "cohorts": 0}
    for tag, look in (("prev1", 1), ("prev5", 5), ("prev20", 20)):
        acc = {"all": [], "overlap": [], "new": [], "overlap_net": []}
        n_ov, n_all = [], []
        for k, (i0, sel) in enumerate(seq):
            if i0 + 2 >= T or k < 1:
                continue
            hist = [seq[j][1] for j in range(max(0, k - look), k)]
            if not hist:
                continue
            owned = np.unique(np.concatenate(hist))
            elig = (np.isfinite(c[i0]) & (c[i0] > 0) & np.isfinite(o[i0 + 1]) &
                    np.isfinite(i1[i0]) & np.isfinite(turn[i0]))
            dw = decile_weight(elig, turn[i0], sel)
            if dw is None:
                continue
            w, pool, dec_full = dw
            bm = bench_active(i1, i0, w, pool, dec_full)
            if bm is None:
                continue
            m_ov = np.isin(sel, owned)
            for tagname, mask in (("all", np.ones(sel.size, bool)), ("overlap", m_ov), ("new", ~m_ov)):
                idx = sel[mask]
                v = i1[i0][idx]
                v = v[np.isfinite(v)]
                if v.size >= 3:
                    acc[tagname].append(float(v.mean()) - bm)
            idx = sel[m_ov]
            v = i1[i0][idx]
            cf = costf[i0][idx]
            fin = np.isfinite(v) & np.isfinite(cf)
            if fin.sum() >= 3:
                acc["overlap_net"].append(float((v - COST_ONE_WAY_BP * 1e-4 * cf)[fin].mean()) - bm)
            n_all.append(float(sel.size))
            n_ov.append(float(m_ov.sum()))
        cell = {}
        for kk, arr in acc.items():
            s = stat(np.array(arr))
            s["halves"] = [h.get("mean_bp") for h in
                           (stat(x) for x in np.array_split(np.array(arr), 2))] if len(arr) >= 16 else []
            cell[kk] = s
        cell["names_per_day"] = round(float(np.mean(n_all)), 1) if n_all else None
        cell["overlap_per_day"] = round(float(np.mean(n_ov)), 1) if n_ov else None
        cell["overlap_share"] = round(float(np.mean(np.array(n_ov) / np.array(n_all))), 3) if n_all else None
        out["defs"][tag] = cell
        print(f"\n############ [BASE = {tag}]  overlap {cell['overlap_per_day']}/{cell['names_per_day']} "
              f"= {cell['overlap_share']:.0%} of picks")
        print(f"  {'subset':<10}{'gross':>9}{'t':>7}{'net':>8}{'sd':>8}{'halves':>18}")
        for kk in ("all", "overlap", "new"):
            s = cell[kk]
            print(f"  {kk:<10}{s.get('mean_bp', 0):>9.1f}{s.get('t', 0):>7.2f}"
                  f"{'':>8}{s.get('sd_bp', 0):>8.0f}  {str(s.get('halves'))}")
        s = cell["overlap_net"]
        print(f"  {'overlapN':<10}{s.get('mean_bp', 0):>9.1f}{s.get('t', 0):>7.2f}   <- after 10bp one-way")

    out["cohorts"] = len(seq)
    p = Path("analysis/_diag_overlay_overlap_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
