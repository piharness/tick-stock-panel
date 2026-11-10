"""Round 5: passive open-auction limit entry -- is the missing money in the bid, not the pick?

Context (rounds 1-4, same 367-day panel):
  * skewness is the arithmetic of stops, not a property of the market (audit P2);
  * no legal order type clears R4-A: best legal gross is S5 +44.5bp (t 1.83), net +24.5;
  * the only cell that clears multiplicity is S0 open[d+1]->close[d+1] (t 4.71),
    which A-share T+1 forbids;
  * winners systematically gap DOWN into the open (S3 -28.9bp, t -4.12) and the
    post-gap intraday rebound is positive and non-monotone in gap depth.

Every test so far took the entry price as given: pay the open, eat the gap.
This script changes the object from "which names" to "at what price do I agree
to own them". A limit buy placed before the 09:15-09:25 call auction fills iff
the auction clears at or below the limit; the trader keeps the option not to
trade. That option is the only asymmetry a T+1, long-only, 20bp-cost account
can hold without forecasting direction.

Pre-registered rules (written before the first run):
  PR-1 parity    : the market-on-open reconstruction of S5 must reproduce
                   +44.5bp / t 1.83 on the matched benchmark. Else stop.
  PR-2 gate      : "harvestable" requires matched active t >= 2.8
                   (Bonferroni over 5 discounts x 2 exits = 10 cells) and net > 0.
  PR-3 selection : random-20 baskets under the same limit rule must earn less than
                   half the picks' active return; otherwise the gain is execution,
                   not selection, and must be reported as such.
  PR-4 stability : the two calendar halves must share a sign with ratio in [0.5, 2].
  PR-5 drag      : fill rate is a first-class output; a rule that trades on 5% of
                   days cannot be annualised as if it traded every day.

Exit is close[d+2] (legal: bought at open[d+1], first legal sale is d+2).
The illegal close[d+1] leg is reported separately as a reference, never as money.

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
DISCOUNTS = (0.01, 0.02, 0.03, 0.05)
N_PLACEBO = 100
N_COST_BP = 20.0


def ret_grid(mats: dict, kind: str) -> np.ndarray:
    """Forward return grid; row d uses only prices from d+1 onward (entry at open[d+1])."""
    o, c = mats["open"], mats["close"]
    out = np.full(o.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        if kind == "c2":       # open[d+1] -> close[d+2]   legal
            r = c[2:] / o[1:-1] - 1.0
        elif kind == "o2":     # open[d+1] -> open[d+2]   legal
            r = o[2:] / o[1:-1] - 1.0
        elif kind == "c1":     # open[d+1] -> close[d+1]  illegal, reference only
            r = c[1:] / o[1:] - 1.0
        else:
            raise ValueError(kind)
    out[: r.shape[0]] = r
    return out


def stat(x: np.ndarray, held: int = 2) -> dict:
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 8:
        return {"n": n}
    n_eff = max(n / held, 1.0)
    mean_bp = float(x.mean()) * 1e4
    se = float(x.std(ddof=0)) * 1e4 / np.sqrt(n_eff)
    return {
        "n": n,
        "n_eff": round(n_eff, 1),
        "mean_bp": round(mean_bp, 1),
        "sd_bp": round(float(x.std(ddof=0)) * 1e4, 1),
        "se_bp": round(se, 1),
        "t": round(mean_bp / se, 2) if se else None,
        "hit": round(float((x > 0).mean()), 3),
        "p5_bp": round(float(np.percentile(x, 5)) * 1e4, 1),
        "p50_bp": round(float(np.percentile(x, 50)) * 1e4, 1),
        "p95_bp": round(float(np.percentile(x, 95)) * 1e4, 1),
    }


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    tradable = (
        np.isfinite(mats["close"]) & (mats["close"] > 0)
        & np.isfinite(mats["open"]) & (mats["open"] > 0)
    )
    print(f"panel dates={T} symbols={S}")

    picks_rows = ap.pick_daily(df, "mom20", False).select("date", "symbols").to_dicts()
    cohorts = []
    for r in picks_rows:
        i0 = date_index[str(r["date"])]
        if i0 + 2 >= T:
            continue
        cohorts.append((i0, np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)))
    print(f"cohorts={len(cohorts)}")

    F2 = ret_grid(mats, "c2")
    F1 = ret_grid(mats, "c1")
    FO = ret_grid(mats, "o2")

    out: dict = {"seed": SEED, "n_cohorts": len(cohorts), "discounts": {}, "prereg": {
        "PR-1_parity_s5": "+44.5bp / t 1.83",
        "PR-2_gate": "matched active t >= 2.8 and net > 0",
        "PR-3_selection": "placebo active < 0.5 x picks active",
        "PR-4_stability": "halves same sign, ratio in [0.5, 2]",
        "PR-5_drag": "report fill rate",
    }}

    def cohort_view(i0: int, k: float | None):
        """Return pool, deciles, fill mask, peer fill means for one cohort."""
        c0 = mats["close"][i0]
        pool = tradable[i0] & np.isfinite(F2[i0]) & np.isfinite(turn[i0]) & (c0 > 0)
        if pool.sum() < 200:
            return None
        if k is None:
            fill = pool.copy()
        else:
            lim = c0 * (1.0 - k)
            nxt = i0 + 1
            fill = pool & tradable[nxt] & (mats["open"][nxt] <= lim) & (lim > 0)
        pool_idx = np.flatnonzero(pool)
        r = F2[i0][pool_idx]
        tv = turn[i0][pool_idx]
        order = np.argsort(tv, kind="stable")
        rk = np.empty(pool_idx.size, dtype=np.int64)
        rk[order] = np.arange(pool_idx.size)
        dec = np.minimum(N_DECILE - 1, (rk * N_DECILE) // pool_idx.size)
        dec_full = np.full(S, -1, dtype=np.int64)
        dec_full[pool_idx] = dec
        f = fill[pool_idx]
        pm = np.full(N_DECILE, np.nan)
        for d in range(N_DECILE):
            m = (dec == d) & f
            if m.sum() >= 3:
                pm[d] = float(r[m].mean())
        return {"pool_idx": pool_idx, "dec": dec, "dec_full": dec_full,
                "fill": fill, "pm": pm, "r": r}

    # ---------------- parity + market-on-open baseline (k=None) ----------------
    print("\n############ [PARITY] S5 = open[d+1] -> close[d+2], matched benchmark")
    base_active, base_all = [], []
    for i0, sel in cohorts:
        v = cohort_view(i0, None)
        if v is None:
            continue
        good = sel[v["fill"][sel]]
        if good.size < 5:
            continue
        r_sel = F2[i0][good]
        dsel = v["dec_full"][good]
        cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
        w = cnt / cnt.sum()
        ok = np.isfinite(v["pm"])
        if not ok.any():
            continue
        ww = np.where(ok, w, 0.0)
        if ww.sum() <= 0:
            continue
        bm = float(np.dot(ww / ww.sum(), np.nan_to_num(v["pm"])))
        base_active.append(float(r_sel.mean()) - bm)
        base_all.append(float(r_sel.mean()))
    s_base = stat(np.array(base_active))
    s_base_all = stat(np.array(base_all))
    out["parity_s5"] = {"matched": s_base, "raw": s_base_all}
    print(f"  matched active = {s_base['mean_bp']:+.1f}bp  t={s_base['t']:+.2f}  n={s_base['n']}")
    print(f"  raw              = {s_base_all['mean_bp']:+.1f}bp  t={s_base_all['t']:+.2f}")
    if not (abs(s_base["mean_bp"] - 44.5) <= 2.0 and abs(s_base["t"] - 1.83) <= 0.2):
        print("  [PR-1 FAIL] reconstruction of S5 does not reproduce; stopping before any new claim.")
        out["pr1_pass"] = False
        Path("analysis/_diag_passive_open_result.json").write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        return
    out["pr1_pass"] = True
    print("  [PR-1 PASS]")

    # ---------------- passive limit ladder ----------------
    for k in DISCOUNTS:
        label = f"lim{-k*100:.0f}%"
        print(f"\n############ [PASSIVE] {label}  (fill iff open[d+1] <= close[d]*(1-{k:.2f}))")
        act, raw, fills, filled_ret, unfilled_ret, picks_n = [], [], [], [], [], []
        placebo_act = []
        illegaL_ref = []
        for i0, sel in cohorts:
            v = cohort_view(i0, k)
            if v is None:
                continue
            good = sel[v["fill"][sel]]
            picks_n.append(float(sel.size))
            fills.append(float(good.size) / max(1, sel.size))
            if good.size < 3:
                continue
            r_sel = F2[i0][good]
            dsel = v["dec_full"][good]
            cnt = np.bincount(dsel[dsel >= 0], minlength=N_DECILE).astype(float)
            if cnt.sum() <= 0:
                continue
            w = cnt / cnt.sum()
            ok = np.isfinite(v["pm"])
            ww = np.where(ok, w, 0.0)
            if ww.sum() <= 0:
                continue
            bm = float(np.dot(ww / ww.sum(), np.nan_to_num(v["pm"])))
            act.append(float(r_sel.mean()) - bm)
            raw.append(float(r_sel.mean()))
            filled_ret.extend(r_sel.tolist())
            illegaL_ref.append(float(np.nanmean(F1[i0][good])))
            # unfilled picks: what did patience cost (or save)?
            miss = sel[~v["fill"][sel]]
            if miss.size:
                m = F2[i0][miss]
                m = m[np.isfinite(m)]
                if m.size:
                    unfilled_ret.extend(m.tolist())
            # placebo: random 20 from the same pool, same limit rule
            pool_fill = v["pool_idx"][v["fill"][v["pool_idx"]]
                                      & np.isfinite(F2[i0][v["pool_idx"]])]
            if pool_fill.size >= 20:
                rng = np.random.default_rng(SEED + 7919 * int(k * 100) + i0)
                for _ in range(N_PLACEBO):
                    d20 = rng.choice(pool_fill, size=20, replace=False)
                    w20 = np.bincount(v["dec_full"][d20], minlength=N_DECILE).astype(float)
                    if w20.sum() <= 0:
                        continue
                    w20 = w20 / w20.sum()
                    ww2 = np.where(ok, w20, 0.0)
                    if ww2.sum() <= 0:
                        continue
                    bm2 = float(np.dot(ww2 / ww2.sum(), np.nan_to_num(v["pm"])))
                    placebo_act.append(float(np.mean(F2[i0][d20])) - bm2)

        s_act = stat(np.array(act))
        s_raw = stat(np.array(raw))
        s_pl = stat(np.array(placebo_act), held=2) if placebo_act else {}
        halves = np.array_split(np.array(act), 2) if len(act) >= 16 else []
        h = [stat(x) for x in halves]
        # pooled payoff shape of the filled picks (per trade, not per cohort)
        fr = np.array(filled_ret)
        ur = np.array(unfilled_ret)
        shape = {
            "n_trades": int(fr.size),
            "mean_bp": round(float(fr.mean()) * 1e4, 1) if fr.size else None,
            "sd_bp": round(float(fr.std(ddof=0)) * 1e4, 1) if fr.size else None,
            "hit": round(float((fr > 0).mean()), 3) if fr.size else None,
            "p5_bp": round(float(np.percentile(fr, 5)) * 1e4, 1) if fr.size else None,
            "p50_bp": round(float(np.percentile(fr, 50)) * 1e4, 1) if fr.size else None,
            "p95_bp": round(float(np.percentile(fr, 95)) * 1e4, 1) if fr.size else None,
            "p99_bp": round(float(np.percentile(fr, 99)) * 1e4, 1) if fr.size else None,
            "unfilled_mean_bp": round(float(ur.mean()) * 1e4, 1) if ur.size else None,
            "unfilled_n": int(ur.size),
        }
        fill_rate = float(np.mean(fills)) if fills else 0.0
        pr2 = (s_act.get("t") or 0) >= 2.8 and (s_act.get("mean_bp", -999) - N_COST_BP) > 0
        pr3 = bool(s_pl) and s_pl.get("mean_bp", 1e9) < 0.5 * s_act.get("mean_bp", -1e9)
        pr4 = bool(len(h) == 2 and h[0].get("mean_bp") and h[1].get("mean_bp")
                   and np.sign(h[0]["mean_bp"]) == np.sign(h[1]["mean_bp"])
                   and 0.5 <= abs(h[1]["mean_bp"] / h[0]["mean_bp"]) <= 2.0)
        out["discounts"][label] = {
            "fill_rate": round(fill_rate, 3),
            "active_matched": s_act, "active_raw": s_raw, "placebo": s_pl,
            "halves": h, "payoff_shape": shape,
            "illegal_close_d1_mean_bp": round(float(np.mean(illegaL_ref)) * 1e4, 1) if illegaL_ref else None,
            "net_bp": round(s_act.get("mean_bp", 0) - N_COST_BP, 1) if s_act else None,
            "PR2_gate": bool(pr2), "PR3_selection": bool(pr3), "PR4_stability": bool(pr4),
        }
        print(f"  fill rate = {fill_rate:.1%}   sizes {np.mean(picks_n):.1f} -> {np.mean(picks_n)*fill_rate:.1f}")
        print(f"  active matched = {s_act.get('mean_bp'):+.1f}bp  t={s_act.get('t'):+.2f}  "
              f"n={s_act.get('n')}  net={out['discounts'][label]['net_bp']:+.1f}")
        print(f"  raw            = {s_raw.get('mean_bp'):+.1f}bp  t={s_raw.get('t'):+.2f}")
        if s_pl:
            print(f"  placebo(rand20) = {s_pl.get('mean_bp'):+.1f}bp  t={s_pl.get('t'):+.2f}  "
                  f"n={s_pl.get('n')}")
        if h:
            print(f"  halves = {h[0].get('mean_bp'):+.1f} / {h[1].get('mean_bp'):+.1f}")
        print(f"  filled trades: n={shape['n_trades']} hit={shape['hit']} "
              f"p5/p50/p95/p99={shape['p5_bp']}/{shape['p50_bp']}/{shape['p95_bp']}/{shape['p99_bp']} "
              f"| unfilled mean={shape['unfilled_mean_bp']}")
        print(f"  PR2={pr2}  PR3={pr3}  PR4={pr4}  illegal c1 ref={out['discounts'][label]['illegal_close_d1_mean_bp']:+.1f}bp")

    p = Path("analysis/_diag_passive_open_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
