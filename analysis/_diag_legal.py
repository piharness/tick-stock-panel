"""Round 4, part 1: is there a LEGAL structure, and what does it earn?

Round 3 (docs/asymmetry-round3.md) ended on this sentence:

    "这个边缘 100% 在日内，隔夜原样退回，而 T+1 恰好强迫你持有退回的那一段。
     合法的 24 小时持有，毛收益 -7.6bp（匹配基准 -1.5bp），再扣 20bp 往返成本，
     净约 -22 至 -28bp/次。"

That sentence compares the intraday leg (+55.9bp, matched) against the OVERNIGHT
THAT FOLLOWS THE EXIT (close[d+1] -> open[d+2], -56.9bp, matched) and concludes
the edge sits in a window T+1 forbids. But "T+1 forbids open->close" is not the
same statement as "T+1 forbids the edge". A second legal one-day structure
exists and round 3 never priced it:

    enter close[d], exit close[d+1]     (both legs in a closing auction)

which pays the ENTRY-night gap plus the intraday leg, instead of the intraday
leg minus the EXIT-night gap. Round 3 priced only the open->open variant, whose
return is intraday(d+1) + overnight(d+1->d+2), i.e. it subtracts the wrong
overnight. Because the picks gap DOWN into the open, buying at the prior close
is mechanically worse than buying at the open by exactly that gap -- yet the
close-to-close structure can still be positive if the entry gap it eats is
smaller than the exit gap open->open eats.

So the question here is not "is there an edge" but:

    for each ORDER TYPE that is executable under T+1, what is the active return
    per unit of cost, and what SE belongs to the estimator being used?

Pre-registered rules (written before the ladder was run; disclosure below):

    R4-A  LEGAL HARVEST.  A structure counts as legally harvestable only if
          t >= 2 AND mean - c > 0 with c = 20bp (repo cost convention:
          backend/app/backtest/engine.py fees_pct=2bp, slippage=5bp/side, plus
          5bp stamp on the sell). Applied to the matched-benchmark column.
    R4-B  LOOK-AHEAD.  The same structure with a one-day execution delay (enter
          close[d+1], exit close[d+2], same cohort) must keep the sign and at
          least half the magnitude of the zero-lag version. Otherwise the
          zero-lag number is a closing-auction artifact and S1 is withdrawn.
    R4-C  CONCENTRATION.  Dropping the 5% best cohorts must not flip the sign of
          the best legal structure. If it does, the cell is tail-driven.
    R4-D  DATA.  Mean |adjusted c2c - raw c2c| on pick-days must be < 2bp and the
          fraction of |gap| > 10.5% must be < 0.5%. Otherwise every gap-based
          number in this and the previous round is contaminated and withdrawn.

DISCLOSURE (this is not a blind pre-registration). An exploratory probe
(analysis/_scratch_legal.py) was run BEFORE these rules were written; it showed
that close[d]->close[d+1] is positive on the matched benchmark. So R4-A is a
threshold applied after the fact, not a blind test. Two of the probe's numbers
were also wrong -- it computed the turnover decile on the ENTRY day (a mild
look-ahead) and printed a bogus identity residual -- so the ladder below is a
re-run from the signal-date convention, and the S0 row is checked for parity
against round 3's +55.9bp (see [parity]). R4-B, R4-C and R4-D were not in the
probe and are computed here for the first time.

Estimator unchanged from round 3: a_h(d) = mean_{picks} r - mean_{matched} r,
matched = turnover-decile conditional expectation taken on the signal date.
SE = sd/sqrt(n_eff). Every structure used below has distinct cohort sessions, so
n_eff = n/held with `held` the number of sessions between entry and exit.

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
COST_BP = 20.0
ARMS = [("mom_top20", False), ("mom_bottom20", True)]

# label, session-grid key, session offset from signal date, sessions held, legal
STRUCTURES = [
    ("S0 open[d+1]->close[d+1]", "intra", 1, 1, False),
    ("S1 close[d]->close[d+1]", "c2c1", 1, 1, True),
    ("S1L close[d+1]->close[d+2]", "c2c1", 2, 1, True),
    ("S3 close[d]->open[d+1]", "gap", 1, 1, True),
    ("S2 open[d+1]->open[d+2]", "oo", 1, 1, True),
    ("S5 open[d+1]->close[d+2]", "oc2", 1, 2, True),
    ("S6 close[d]->close[d+2]", "c2c2", 0, 2, True),
    ("S7 close[d]->close[d+5]", "c2c5", 0, 5, True),
    ("S8 close[d]->close[d+20]", "c2c20", 0, 20, True),
]


def ratio_grid(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = num / den - 1.0
    ok = np.isfinite(num) & np.isfinite(den) & (den > 0) & np.isfinite(r)
    return np.where(ok, r, np.nan)


def session_grids(mats: dict) -> dict[str, np.ndarray]:
    """Legs indexed by SESSION s, so that per cell
    (1+gap[s])*(1+intra[s]) == 1+c2c1[s] exactly up to the interaction term."""
    o, c = mats["open"], mats["close"]
    T = o.shape[0]
    prev_c = np.full_like(c, np.nan)
    prev_c[1:] = c[:-1]
    nxt_o = np.full_like(o, np.nan)
    nxt_o[:-1] = o[1:]
    nxt_c = np.full_like(c, np.nan)
    nxt_c[:-1] = c[1:]

    g: dict[str, np.ndarray] = {}
    g["gap"] = ratio_grid(o, prev_c)
    g["intra"] = ratio_grid(c, o)
    g["c2c1"] = ratio_grid(c, prev_c)
    g["oo"] = ratio_grid(nxt_o, o)
    g["oc2"] = ratio_grid(nxt_c, o)
    for h in (2, 5, 20):
        lead = np.full_like(c, np.nan)
        if h < T:
            lead[: T - h] = c[h:]
        g[f"c2c{h}"] = ratio_grid(lead, c)
    return g


def align(grid: np.ndarray, off: int) -> np.ndarray:
    """Re-index a session grid so that row d holds the value for session d+off,
    i.e. the signal-date convention every round has used."""
    T = grid.shape[0]
    out = np.full_like(grid, np.nan)
    if 0 <= off < T:
        out[: T - off] = grid[off:]
    return out


def raw_close_grid(dates: list[str], symbols: list[str]) -> np.ndarray:
    """Unadjusted close on the same axes as calendar_mats. asymmetry_probe's
    loader projects only the adjusted OHLC, so the DQ check reads it here rather
    than widening that shared loader."""
    files = sorted(glob.glob(str(ap.DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    frames = [pl.read_parquet(f, columns=["symbol", "date", "raw_close"]) for f in files]
    src = pl.concat(frames, how="vertical_relaxed")
    d_map = {d: i for i, d in enumerate(dates)}
    s_map = {s: i for i, s in enumerate(symbols)}
    di = np.array([d_map.get(x, -1) for x in src["date"].cast(pl.Utf8).to_list()])
    si = np.array([s_map.get(x, -1) for x in src["symbol"].to_list()])
    keep = (di >= 0) & (si >= 0)
    out = np.full((len(dates), len(symbols)), np.nan)
    out[di[keep], si[keep]] = src["raw_close"].to_numpy().astype(np.float64)[keep]
    return out


def stats(a: np.ndarray, held: int) -> dict:
    a = a[np.isfinite(a)]
    n = a.size
    if n < 20:
        return {"n": int(n)}
    n_eff = n / held
    mean_bp = float(a.mean()) * 1e4
    sd_bp = float(a.std(ddof=0)) * 1e4
    se = sd_bp / np.sqrt(n_eff)
    return {
        "n": int(n),
        "n_eff": round(n_eff, 1),
        "mean_bp": round(mean_bp, 1),
        "median_bp": round(float(np.median(a)) * 1e4, 1),
        "sd_bp": round(sd_bp, 1),
        "se_bp": round(se, 1),
        "t": round(mean_bp / se, 2),
        "net_bp": round(mean_bp - COST_BP, 1),
        "t_net": round((mean_bp - COST_BP) / se, 2),
        "hit_rate": round(float((a > 0).mean()), 3),
    }


def trimmed(a: np.ndarray, frac: float) -> np.ndarray:
    a = a[np.isfinite(a)]
    if a.size < 20:
        return a
    k = int(np.floor(a.size * frac))
    return a if k == 0 else np.sort(a)[: a.size - k]


def active_series(F: np.ndarray, picks_rows: list[dict], s_map: dict, date_index: dict,
                  pool_base: np.ndarray, to20: np.ndarray, T: int, S: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-cohort pick mean minus turnover-decile-matched conditional expectation.

    Deciles and the investable pool are both taken on the SIGNAL date; only the
    return grid F is read at that same row (F is already aligned)."""
    pool_ok = pool_base & np.isfinite(F) & np.isfinite(to20)
    a_uni, a_match = [], []
    for r in picks_rows:
        i0 = date_index[str(r["date"])]
        if i0 >= T:
            continue
        pool = np.flatnonzero(pool_ok[i0])
        if pool.size < N_STOCK:
            continue
        sel_all = np.array([s_map[x] for x in r["symbols"]], dtype=np.int64)
        sel = sel_all[pool_ok[i0][sel_all]]
        if sel.size < 5:
            continue
        vals = to20[i0][pool]
        order = np.argsort(vals, kind="stable")
        rk = np.empty(pool.size, dtype=np.int64)
        rk[order] = np.arange(pool.size)
        dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool.size)
        dec_full = np.full(S, -1, dtype=np.int64)
        dec_full[pool] = dec
        dsel = dec_full[sel]
        cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(np.float64)
        w = cnt / cnt.sum()
        rv = F[i0][pool]
        dmean = np.array(
            [float(np.mean(rv[dec == k])) if (dec == k).any() else 0.0
             for k in range(N_DECILE)]
        )
        m_pick = float(np.mean(F[i0][sel]))
        a_uni.append(m_pick - float(np.mean(rv)))
        a_match.append(m_pick - float(np.dot(w, dmean)))
    return np.array(a_uni), np.array(a_match)


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
    raw_c = raw_close_grid(dates, symbols)
    G = session_grids(mats)
    print(f"panel dates={T} symbols={S}")

    # ---- identity ----
    fin3 = np.isfinite(G["gap"]) & np.isfinite(G["intra"]) & np.isfinite(G["c2c1"])
    resid = (G["gap"] + G["intra"] - G["c2c1"])[fin3]
    inter = (G["gap"] * G["intra"])[fin3]
    err = np.abs(resid + inter)
    print(f"[identity] c2c1-(gap+intra) == -gap*intra ; max|numerical error|="
          f"{err.max():.3e} over {err.size} cells "
          f"(max|gap*intra|={np.abs(inter).max():.3f})")

    out: dict = {"seed": SEED, "cost_bp": COST_BP, "ladder": {}, "dq": {},
                 "identity_max_numerical_error": float(err.max())}

    # ---- R4-D data quality ----
    prev_c = np.full_like(mats["close"], np.nan)
    prev_c[1:] = mats["close"][:-1]
    prev_raw = np.full_like(raw_c, np.nan)
    prev_raw[1:] = raw_c[:-1]
    adj_c2c = ratio_grid(mats["close"], prev_c)
    raw_c2c = ratio_grid(raw_c, prev_raw)
    both = np.isfinite(adj_c2c) & np.isfinite(raw_c2c)
    diff_bp = np.abs(adj_c2c - raw_c2c)[both] * 1e4
    n_gap = int(np.isfinite(G["gap"]).sum())
    band = int((np.isfinite(G["gap"]) & (np.abs(G["gap"]) > 0.105)).sum())
    out["dq"] = {
        "adj_vs_raw_c2c_cells": int(both.sum()),
        "adj_vs_raw_c2c_mean_abs_bp": round(float(diff_bp.mean()), 2),
        "adj_vs_raw_c2c_p99_abs_bp": round(float(np.percentile(diff_bp, 99)), 2),
        "adj_vs_raw_c2c_frac_gt_5bp": round(float((diff_bp > 5).mean()), 5),
        "gap_cells": n_gap,
        "gap_outside_10p5_frac": round(band / max(1, n_gap), 6),
    }
    print(f"[dq] |adjusted-raw c2c| mean={out['dq']['adj_vs_raw_c2c_mean_abs_bp']}bp "
          f"p99={out['dq']['adj_vs_raw_c2c_p99_abs_bp']}bp "
          f"frac>5bp={out['dq']['adj_vs_raw_c2c_frac_gt_5bp']:.4%}")
    print(f"[dq] |gap|>10.5% = {out['dq']['gap_outside_10p5_frac']:.4%} of {n_gap} cells")

    aligned = {}
    for label, key, off, held, legal in STRUCTURES:
        aligned[label] = (align(G[key], off), held, legal)

    picks_cache = {a: ap.pick_daily(df, "mom20", asc).select("date", "symbols").to_dicts()
                   for a, asc in ARMS}

    # ---- the ladder ----
    for arm_label, _ in ARMS:
        out["ladder"][arm_label] = {}
        print(f"\n############ {arm_label}")
        print(f"  {'structure':26s} {'n':>4s} {'act-uni':>16s} {'act-match':>16s} "
              f"{'med':>7s} {'net':>7s} {'trim5%':>8s}")
        for label, _, _, held, legal in STRUCTURES:
            F, held_, legal_ = aligned[label]
            au, am = active_series(F, picks_cache[arm_label], s_map, date_index,
                                   pool_base, to20, T, S)
            su, sm = stats(au, held_), stats(am, held_)
            if "mean_bp" not in sm:
                print(f"  {label:26s} n={sm['n']} too few")
                continue
            tr = stats(trimmed(am, 0.05), held_)
            out["ladder"][arm_label][label] = {
                "legal": legal_, "universe": su, "matched": sm, "matched_trim5pct": tr,
            }
            print(f"  {label:26s} {sm['n']:4d} "
                  f"{su['mean_bp']:+8.1f}(t{su['t']:+5.2f}) "
                  f"{sm['mean_bp']:+8.1f}(t{sm['t']:+5.2f}) "
                  f"{sm['median_bp']:+7.1f} {sm['net_bp']:+7.1f} {tr['mean_bp']:+8.1f}")

    # ---- parity against round 3 ----
    par = {}
    for arm_label, _ in ARMS:
        s0 = out["ladder"][arm_label]["S0 open[d+1]->close[d+1]"]
        par[arm_label] = {"S0_matched_bp": s0["matched"]["mean_bp"],
                          "S0_matched_t": s0["matched"]["t"]}
    out["parity"] = par
    print(f"\n[parity] S0 matched: top20 {par['mom_top20']['S0_matched_bp']:+0.1f}bp "
          f"(t{par['mom_top20']['S0_matched_t']})  <- round 3 reports +55.9bp (t+4.71)")

    # ---- R4-B look-ahead ----
    print("\n############ [R4-B] zero-lag (S1) vs one-day-delayed (S1L), same cohort")
    out["lookahead"] = {}
    for arm_label, _ in ARMS:
        L = out["ladder"][arm_label]
        s1 = L["S1 close[d]->close[d+1]"]["matched"]
        s1l = L["S1L close[d+1]->close[d+2]"]["matched"]
        ratio = s1l["mean_bp"] / s1["mean_bp"] if s1["mean_bp"] else float("nan")
        verdict = "PASS" if (ratio >= 0.5 and s1l["mean_bp"] > 0) else "FAIL"
        print(f"  {arm_label:13s} S1 {s1['mean_bp']:+8.1f}bp (t{s1['t']:+5.2f}) | "
              f"S1L {s1l['mean_bp']:+8.1f}bp (t{s1l['t']:+5.2f}) | "
              f"ratio {ratio:+.2f} -> {verdict}")
        out["lookahead"][arm_label] = {"S1_matched_bp": s1["mean_bp"],
                                      "S1L_matched_bp": s1l["mean_bp"],
                                      "ratio": round(float(ratio), 3), "verdict": verdict}

    # ---- R4-C concentration + split-half on the two 1-day structures ----
    print("\n############ [R4-C] tail concentration and split-half (matched)")
    out["concentration"] = {}
    out["split"] = {}
    for arm_label, _ in ARMS:
        out["concentration"][arm_label] = {}
        out["split"][arm_label] = {}
        for label in ("S0 open[d+1]->close[d+1]", "S1 close[d]->close[d+1]"):
            F, held_, _ = aligned[label]
            _, am = active_series(F, picks_cache[arm_label], s_map, date_index,
                                  pool_base, to20, T, S)
            a = am[np.isfinite(am)]
            tot = float(a.sum())
            srt = np.sort(a)[::-1]
            k = max(1, int(a.size * 0.05))
            share5 = float(srt[:k].sum() / tot) if tot else float("nan")
            st = stats(a, held_)
            half = a.size // 2
            s1h, s2h = stats(a[:half], held_), stats(a[half:], held_)
            print(f"  {arm_label:13s} {label:26s} n={a.size} tot={tot*1e4:+8.0f}bp "
                  f"mean={st['mean_bp']:+7.1f} med={st['median_bp']:+7.1f} "
                  f"hit={st['hit_rate']:.3f} top5%share={share5:+.2f} | "
                  f"1st {s1h['mean_bp']:+7.1f}(t{s1h['t']:+5.2f}) "
                  f"2nd {s2h['mean_bp']:+7.1f}(t{s2h['t']:+5.2f})")
            out["concentration"][arm_label][label] = {
                "n": int(a.size), "total_bp": round(tot * 1e4, 1),
                "mean_bp": st["mean_bp"], "median_bp": st["median_bp"],
                "hit_rate": st["hit_rate"], "top5pct_share": round(share5, 3),
                "t": st["t"],
            }
            out["split"][arm_label][label] = {"first": s1h, "second": s2h}

    p = Path("analysis/_diag_legal_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
