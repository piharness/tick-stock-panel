"""Feasibility attack on the short-horizon result found by analysis/horizon_scale.py.

horizon_scale.py pre-registered a neutral-scaling prediction and the data
rejected it: the per-cohort active return of BOTH 20-day momentum extremes is
positive at h=1..3 and negative at h>=20, with |t| up to ~5 at h=1. Before any
of that is allowed to become a conclusion, three things must be attacked, each
with its own kill condition:

  F1 FILLABILITY.  A-share limit rules. Two layers:
       (a) T+1: shares bought at the open of day d+1 cannot be sold on d+1, so
           the h=1 cell (open[d+1] -> close[d+1]) is not an executable trade at
           any cost. It is reported for shape only.
       (b) a one-price day (high == low) is a locked board: no fill at the open.
           20-day momentum winners are the population most likely to be locked,
           so the edge can be an artifact of buying what nobody could buy.
       Kill condition: the edge at the cheapest EXECUTABLE horizon (h=2) must
       keep |t| >= 2 after locked entries are removed, else the short-horizon
       result is withdrawn.

  F2 BETA.  A 20-stock basket of momentum extremes is not beta-1 versus the
       equal-weighted universe. In a trending sample the whole active return can
       be market exposure. Kill condition: the intercept of
       a_h ~ const + beta * u_h must keep the sign and a quarter of the size.

  F3 CALIBRATION.  nw_var is invalid here: it needs L/n -> 0 and the grid has
       L/n up to 120/287. The reported SE is therefore sd/sqrt(n_eff) (round 2's
       convention), and its true null distribution is measured per horizon by
       pushing zero-signal picks through the identical estimator. Kill
       condition: a cell is only evidence if its placebo-calibrated p (not the
       nominal t) clears 5% after Benjamini-Hochberg over the cells tested in
       this stage.

ASCII output only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import glob

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402
import horizon_scale as hs  # noqa: E402

SEED = 20261109
H_GRID = (1, 2, 3, 5, 10, 20, 40, 60)
N_STOCK = ap.N_STOCK
N_PLACEBO = 200
ARMS = [("mom_bottom20", True), ("mom_top20", False)]


def limit_ups_grid(dates: list[str], symbols: list[str]) -> np.ndarray:
    files = sorted(glob.glob(str(Path("data") / "kline_daily_enriched" / "date=*" / "part.parquet")))
    frames = [
        pl.read_parquet(f, columns=["symbol", "date", "consecutive_limit_ups", "consecutive_limit_downs"])
        for f in files
    ]
    df = pl.concat(frames, how="vertical_relaxed")
    df = df.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
    lu = ac.grid_of(df, "consecutive_limit_ups", dates, symbols)
    ld = ac.grid_of(df, "consecutive_limit_downs", dates, symbols)
    return np.nan_to_num(lu, nan=0.0), np.nan_to_num(ld, nan=0.0)


def bh(pvals: list[float]) -> list[float]:
    n = len(pvals)
    order = np.argsort(pvals)
    q = np.empty(n)
    prev = 1.0
    for rank in range(n - 1, -1, -1):
        i = order[rank]
        prev = min(prev, pvals[i] * n / (rank + 1))
        q[i] = prev
    return list(q)


def placebo_t(F: np.ndarray, rows: list[tuple[int, np.ndarray]], umean: np.ndarray, h: int,
              draws: int, seed0: int) -> np.ndarray:
    """Null distribution of the t statistic under zero signal, same estimator."""
    ts = np.empty(draws)
    for k in range(draws):
        rng = np.random.default_rng(seed0 + k)
        a = np.empty(len(rows))
        for i, (i0, pool) in enumerate(rows):
            sel = rng.choice(pool, size=N_STOCK, replace=False)
            a[i] = float(np.mean(F[i0][sel])) - umean[i]
        ts[k] = a.mean() / (a.std(ddof=0) / np.sqrt(a.size / h))
    return ts


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel())
    dates, symbols, mats = ap.calendar_mats(df)
    T, S = mats["close"].shape
    s_map = {s: i for i, s in enumerate(symbols)}
    date_index = {d: i for i, d in enumerate(dates)}
    turn = ac.grid_of(df, "turnover_rate", dates, symbols)
    tradable = np.isfinite(mats["close"]) & (mats["close"] > 0) & np.isfinite(turn) & (turn > 0)

    # one-price day = locked board = no fill at the open
    hi, lo = mats["high"], mats["low"]
    one_price = np.zeros((T, S), dtype=bool)
    both_finite = np.isfinite(hi) & np.isfinite(lo)
    one_price[both_finite] = np.isclose(hi[both_finite], lo[both_finite], rtol=0.0, atol=1e-9)

    lu, ld = limit_ups_grid(dates, symbols)
    print(f"panel dates={T} symbols={S}  one-price days={int(one_price.sum())} "
          f"({one_price.sum()/max(1,(hi.size)):.1%} of cells)  limit-up cells={int((lu>0).sum())}")

    # entry-day (d+1) fillability: not a locked board on the entry day
    fill_ok = np.zeros((T, S), dtype=bool)
    fill_ok[:-1] = ~one_price[1:] & np.isfinite(mats["open"][1:]) & (mats["open"][1:] > 0)

    F20 = hs.fwd_grid(mats, 20)
    out: dict = {"seed": SEED, "h_grid": list(H_GRID), "variants": {}, "placebo": {},
                 "beta": {}, "limitup_share": {}}

    print("\n############ variant grid: BASE / FILL / FILL+nolimitup")
    rows_log = []
    for arm_label, ascending in ARMS:
        pk = ap.pick_daily(df, "mom20", ascending)
        picks_rows = pk.select("date", "symbols").to_dicts()
        out["variants"][arm_label] = {}
        print(f"\n== {arm_label}")
        for h in H_GRID:
            F = F20 if h == 20 else hs.fwd_grid(mats, h)
            entry_fill = fill_ok
            variants = {
                "BASE": tradable & np.isfinite(F),
                "FILL": tradable & np.isfinite(F) & entry_fill,
                "FILL_nolu": tradable & np.isfinite(F) & entry_fill & (lu == 0),
            }
            out["variants"][arm_label][str(h)] = {}
            for vname, pool_ok in variants.items():
                act = []
                locked_entry = 0
                n_picks = 0
                for r in picks_rows:
                    i0 = date_index[str(r["date"])]
                    if i0 + h >= T:
                        continue
                    sel = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
                    good = sel[pool_ok[i0][sel]]
                    n_picks += sel.size
                    locked_entry += int(sel.size - good.size)
                    if good.size < 5:
                        continue
                    pool = np.flatnonzero(pool_ok[i0])
                    if pool.size < N_STOCK:
                        continue
                    act.append(float(np.mean(F[i0][good]) - np.mean(F[i0][pool])))
                act = np.array(act)
                if act.size < 30:
                    continue
                n = act.size
                n_eff = n / h
                mean_bp = float(act.mean()) * 1e4
                sd_bp = float(act.std(ddof=0)) * 1e4
                se_bp = sd_bp / np.sqrt(n_eff)
                out["variants"][arm_label][str(h)][vname] = {
                    "n": n, "n_eff": round(n_eff, 1), "mean_bp": round(mean_bp, 1),
                    "sd_bp": round(sd_bp, 1), "se_bp": round(se_bp, 1),
                    "t": round(mean_bp / se_bp, 2),
                    "dropped_pick_frac": round(locked_entry / max(1, n_picks), 3),
                }
                rows_log.append((arm_label, h, vname, out["variants"][arm_label][str(h)][vname]))
            b = out["variants"][arm_label][str(h)]
            print(f"  h={h:3d}  BASE t={b['BASE']['t']:+6.2f} (mean {b['BASE']['mean_bp']:+8.1f})"
                  f" | FILL t={b['FILL']['t']:+6.2f} (mean {b['FILL']['mean_bp']:+8.1f},"
                  f" dropped {b['FILL']['dropped_pick_frac']:.1%})"
                  f" | FILL_nolu t={b['FILL_nolu']['t']:+6.2f}")

    # ---------------- F1b: how exposed are the picks to locked boards? -------
    print("\n############ [F1b] picks that closed limit-up on the signal date")
    for arm_label, ascending in ARMS:
        pk = ap.pick_daily(df, "mom20", ascending)
        picks_rows = pk.select("date", "symbols").to_dicts()
        ups, tot = 0, 0
        for r in picks_rows:
            i0 = date_index[str(r["date"])]
            for s in r["symbols"]:
                ups += int(lu[i0][s_map[s]] > 0)
                tot += 1
        out["limitup_share"][arm_label] = round(ups / max(1, tot), 3)
        print(f"  {arm_label}: {ups}/{tot} = {ups/max(1,tot):.1%} of picks closed limit-up on the signal date")

    # ---------------- F2: beta decomposition ---------------------------------
    print("\n############ [F2] active return vs market exposure (a = alpha + beta*u)")
    for arm_label, h in (("mom_top20", 1), ("mom_top20", 2), ("mom_top20", 3),
                         ("mom_top20", 5), ("mom_bottom20", 1), ("mom_bottom20", 2),
                         ("mom_top20", 20)):
        pk = ap.pick_daily(df, "mom20", arm_label == "mom_bottom20")
        picks_rows = pk.select("date", "symbols").to_dicts()
        F = F20 if h == 20 else hs.fwd_grid(mats, h)
        pool_ok = tradable & np.isfinite(F)
        a, u = [], []
        for r in picks_rows:
            i0 = date_index[str(r["date"])]
            if i0 + h >= T:
                continue
            sel = np.array([s_map[s] for s in r["symbols"]], dtype=np.int64)
            good = sel[np.isfinite(F[i0][sel])]
            pool = np.flatnonzero(pool_ok[i0])
            if good.size < 5 or pool.size < N_STOCK:
                continue
            um = float(np.mean(F[i0][pool]))
            u.append(um)
            a.append(float(np.mean(F[i0][good])) - um)
        a, u = np.array(a), np.array(u)
        X = np.column_stack([np.ones(u.size), u])
        coef, *_ = np.linalg.lstsq(X, a, rcond=None)
        resid = a - X @ coef
        # HC0-free: simple OLS SEs are adequate for a decomposition, not a test
        s2 = float(resid @ resid) / max(1, u.size - 2)
        cov = s2 * np.linalg.inv(X.T @ X)
        se = np.sqrt(np.diag(cov))
        alpha_bp, beta = float(coef[0]) * 1e4, float(coef[1])
        alpha_t = float(coef[0] / se[0])
        mkt_ann = float(u.mean()) * 252.0 / h * 100.0
        print(f"  {arm_label:13s} h={h:3d}  mean_a={a.mean()*1e4:+8.1f}bp  beta={beta:+5.2f} "
              f"alpha={alpha_bp:+8.1f}bp (t={alpha_t:+5.2f})  |  market {mkt_ann:+6.1f}%/yr")
        out["beta"][f"{arm_label}|h{h}"] = {
            "mean_a_bp": round(float(a.mean()) * 1e4, 1),
            "beta": round(beta, 2), "beta_t": round(float(coef[1] / se[1]), 2),
            "alpha_bp": round(alpha_bp, 1), "alpha_t": round(alpha_t, 2),
            "market_ann_pct": round(mkt_ann, 1), "n": int(u.size),
        }

    # ---------------- F3: placebo-calibrated p per cell ----------------------
    print(f"\n############ [F3] placebo calibration ({N_PLACEBO} draws per horizon)")
    pk0 = ap.pick_daily(df, "mom20", True)
    rows0 = [date_index[str(r["date"])] for r in pk0.select("date").to_dicts()]
    cells = []
    for h in H_GRID:
        F = F20 if h == 20 else hs.fwd_grid(mats, h)
        pool_ok = tradable & np.isfinite(F) & fill_ok
        i0s, pools, umeans, rows_f = [], [], [], []
        for i0 in rows0:
            if i0 + h >= T:
                continue
            pool = np.flatnonzero(pool_ok[i0])
            if pool.size < N_STOCK:
                continue
            i0s.append(i0)
            pools.append(pool)
            rows_f.append((i0, pool))
            umeans.append(float(np.mean(F[i0][pool])))
        ts = placebo_t(F, rows_f, np.array(umeans), h, N_PLACEBO, SEED + 1000 * h)
        sd_null = float(ts.std())
        out["placebo"][str(h)] = {
            "draws": int(ts.size), "sd_null_t": round(sd_null, 2),
            "p_abs_ge_2": round(float(np.mean(np.abs(ts) >= 2.0)), 3),
        }
        print(f"  h={h:3d} dates={len(i0s):3d} sd(null t)={sd_null:4.2f}  "
              f"P(|t|>=2)={np.mean(np.abs(ts)>=2.0):5.1%}  "
              f"P(|t|>=2.8)={np.mean(np.abs(ts)>=2.8):5.1%}")
        # calibrate each observed cell against this horizon's null
        for arm_label in ("mom_bottom20", "mom_top20"):
            cell = out["variants"][arm_label][str(h)]["FILL"]
            t_obs = cell["t"]
            p_cal = float(np.mean(np.abs(ts) >= abs(t_obs)))
            cells.append({"arm": arm_label, "h": h, "t": t_obs, "p_placebo": p_cal,
                          "p_nominal": round(_p_norm(abs(t_obs)), 5)})
    qs = bh([c["p_placebo"] for c in cells])
    for c, q in zip(cells, qs):
        c["q_bh"] = round(q, 3)
    out["calibrated_cells"] = cells
    print("\n  cell                         t    p_placebo  q_BH")
    for c in sorted(cells, key=lambda x: x["p_placebo"]):
        flag = " <== survives 5%" if c["q_bh"] <= 0.05 else ""
        print(f"  {c['arm']:13s} h={c['h']:3d}  t={c['t']:+6.2f}  p={c['p_placebo']:.3f}  "
              f"q={c['q_bh']:.3f}{flag}")

    p = Path("analysis/_diag_fillable_result.json")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time()-t0:.1f}s")


def _p_norm(z: float) -> float:
    """Two-sided normal p for |z|."""
    import math
    return float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))


if __name__ == "__main__":
    main()
