"""Round 4, part 4: re-estimate part 3 (the regime split) under ONE estimator.

Part 3 (_diag_regime.py) splits round 4's cells by a 3-way bucket of
data/regime_history and reports

    strong | range | weak
    in-window excess   +95.10 / +93.63 / +93.45 %   (t 71.02 / 33.52 / 66.99)
    day1 S1 c2c        +37.7  / +55.0  / -3.0  bp   (t  2.08 /  1.58 / -0.12)
    day1 S0 intraday   +64.4  / +73.1  / +38.2  bp  (t  4.03 /  2.31 /  1.83)

Three things in that table are not internally consistent with round 4's own
rules, and this script measures all three instead of arguing about them.

  (1) OVERLAP.  Round 3's R3-C says n_eff = n/held, and part 3 applies it to S8
      (20-day forward), calling t=0.12 "not evidence".  The in-window excess is
      ALSO a 20-day quantity: mom20 at d covers [d-20, d], so adjacent cohorts
      share 19 of 20 sessions.  Part 3 nevertheless prints t=71.02 for it.  By
      the rule applied one line lower, that t is inadmissible.  Here every cell
      gets the same three estimators: the iid t, a Newey-West t (Bartlett,
      lag=min(20, n//5)), and a circular block bootstrap (block 20, B=2000).

  (2) SELECTION.  "strong bucket S1 t=2.08" is the max over 3 buckets of a cell
      that already survived 18 cells in part 1.  Bonferroni is one correction;
      the directly relevant one is a permutation of the bucket labels, which
      keeps the daily series and the bucket sizes and destroys only the regime
      assignment.  Reported: P(max_t over 3 buckets >= observed).

  (3) PERSISTENCE.  A "regime" that flips every 1.4 sessions is a daily breadth
      score, not a regime.  Measured: mean run length and number of runs of the
      3-way bucket.

Plus the cells part 3 never conditioned:

  (4) S5 open[d+1]->close[d+2] is round 4's best NET legal cell (+44.5bp gross,
      net +24.5).  Part 3 split S1 only.
  (5) RIGHT-TAIL WIDTH by bucket.  Part 3 says the in-window excess is "an order
      statistic, regime-invariant by construction".  That is testable: the
      cross-sectional p99.9 of mom20 should be equally invariant.  If tail width
      varies across buckets while the top-20 excess does not, the invariance is a
      coincidence, not a property.

Also counts (6) the lookahead in asymmetry_probe.pick_daily: it requires
`fwd` finite, so a pick is dropped when the name has no close 20 sessions later
(suspension / delisting / panel end).  That is conditioning the signal date on
the future.  The count says whether it can move a number.

Pre-registered rules (written after reading part 3, before running these
estimators -- this is a re-estimation, not a blind confirmation):

    R6-A  ESTIMATOR.  A cell counts as evidence only if the NW(20) t and the
          block-bootstrap 95% CI agree in sign AND |NW t| >= 2.  The iid t is
          printed for parity with parts 1-3 but is not a decision rule.
    R6-B  PERSISTENCE.  The 3-way label is a "regime" only if mean run length
          >= 5 sessions and there are >= 10 runs in every bucket.  Otherwise it
          is reported as a daily breadth score, and part 3's withdrawal of
          "needs a second regime period" is itself withdrawn.
    R6-C  SELECTION.  The strong-bucket S1 claim survives only if the permutation
          p(max over 3 buckets) <= 0.05.
    R6-D  TRANSFER.  The regime split must also be reported for the best NET
          legal cell (S5).  If S5 shows no bucket pattern while S1 does, part 3's
          headline is about a cell round 4 already disqualified (R4-B sign flip,
          R4-C 108% concentration).
    R6-E  ORDER STATISTIC.  "Regime-invariant by construction" survives only if
          p99.9(mom20) varies across buckets by less than 5% relative.

ASCII output only (GBK console).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import _diag_gaprev as dg  # noqa: E402
import _diag_legal as dl  # noqa: E402  (full session-grid set: oo/oc2/c2cN)
import asymmetry_control as ac  # noqa: E402
import asymmetry_probe as ap  # noqa: E402

SEED = 20261109
COST_BP = 20.0
LAG = 20
BLOCK = 20
N_BOOT = 2000
N_PERM = 2000
MIN_N = 30
BUCKETS = ("strong", "range", "weak")
RNG = np.random.default_rng(SEED)

# (label, session-grid key, offset, held, legal)
STRUCTS = [
    ("S0", "intra", 1, 1, False),
    ("S1", "c2c1", 1, 1, True),
    ("S1L", "c2c1", 2, 1, True),
    ("S3", "gap", 1, 1, True),
    ("S2", "oo", 1, 1, True),
    ("S5", "oc2", 1, 2, True),
    ("S6", "c2c2", 0, 2, True),
    ("S7", "c2c5", 0, 5, True),
    ("S8", "c2c20", 0, 20, True),
]


def regime_map() -> dict[str, str]:
    """date -> bucket of the PREVIOUS session's state (same as part 3)."""
    r = pl.read_parquet(ap.DATA / "regime_history" / "part.parquet").select(
        pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"), pl.col("state")
    )
    r = r.with_columns(
        pl.when(pl.col("state").is_in(["strong", "lean_strong"]))
        .then(pl.lit("strong"))
        .when(pl.col("state").is_in(["weak", "lean_weak"]))
        .then(pl.lit("weak"))
        .otherwise(pl.lit("range"))
        .alias("bucket")
    ).sort("date")
    r = r.with_columns(pl.col("bucket").shift(1).alias("b_t1"))
    return {row["date"]: row["b_t1"] for row in r.iter_rows(named=True)
            if row["b_t1"] is not None}


def nw_t(a: np.ndarray, lag: int = LAG) -> dict:
    """Newey-West t for the mean, Bartlett kernel."""
    x = np.asarray(a, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < MIN_N:
        return {"n": int(n)}
    lag_used = min(lag, max(5, n // 5))
    d = x - x.mean()
    s = float((d * d).mean())
    for k in range(1, lag_used + 1):
        w = 1.0 - k / (lag_used + 1.0)
        s += 2.0 * w * float((d[k:] * d[:-k]).mean())
    se = float(np.sqrt(max(s, 0.0) / n))
    m = float(x.mean())
    return {"n": int(n), "lag": int(lag_used), "mean": m, "se": se,
            "t": round(m / se, 2) if se > 0 else None}


def iid_t(a: np.ndarray, held: int = 1) -> dict:
    x = np.asarray(a, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < MIN_N:
        return {"n": int(n)}
    se = float(x.std(ddof=0)) * np.sqrt(held / n)
    m = float(x.mean())
    return {"n": int(n), "mean": m, "se": se,
            "t": round(m / se, 2) if se > 0 else None}


def block_boot(a: np.ndarray, block: int = BLOCK, n_boot: int = N_BOOT) -> dict:
    """Circular moving-block bootstrap of the mean."""
    x = np.asarray(a, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 3 * max(5, block // 2):
        return {"n": int(n)}
    nb = int(np.ceil(n / block))
    off = np.arange(block)
    out = np.empty(n_boot)
    for i in range(n_boot):
        starts = RNG.integers(0, n, nb)
        idx = ((starts[:, None] + off[None, :]) % n).ravel()[:n]
        out[i] = x[idx].mean()
    return {"n": int(n), "mean": float(x.mean()),
            "ci5": float(np.percentile(out, 2.5)),
            "ci95": float(np.percentile(out, 97.5)),
            "p_le0": round(float((out <= 0).mean()), 4)}


def cluster_t(a: np.ndarray, clusters: np.ndarray) -> dict:
    """CR0 t with contiguous-run clusters (for bucket-conditional cells)."""
    x = np.asarray(a, dtype=np.float64)
    ok = np.isfinite(x)
    x, c = x[ok], np.asarray(clusters)[ok]
    n = x.size
    _, inv = np.unique(c, return_inverse=True)
    G = int(np.unique(c).size)
    if n < MIN_N or G < 10:
        return {"n": int(n), "n_clusters": G}
    m = float(x.mean())
    s = np.bincount(inv, weights=x - m)
    var = (G / (G - 1.0)) * float((s * s).sum()) / (n * n)
    se = float(np.sqrt(var))
    return {"n": int(n), "n_clusters": G,
            "t": round(m / se, 2) if se > 0 else None}


def fmt_cell(d_iid: dict, d_nw: dict, d_bb: dict, scale: float = 1e4,
             pct: bool = False) -> str:
    unit = "%" if pct else "bp"
    sc = 100.0 if pct else scale
    if "mean" not in d_nw:
        return f"(n={d_nw['n']} too few)"
    parts = [f"{d_nw['mean'] * sc:+8.2f}{unit}"]
    parts.append(f"iid_t {d_iid['t']:+5.2f}" if d_iid.get("t") is not None else "iid_t  n/a")
    parts.append(f"NW_t {d_nw['t']:+5.2f}" if d_nw.get("t") is not None else "NW_t  n/a")
    if "ci5" in d_bb:
        parts.append(f"CI[{d_bb['ci5'] * sc:+8.2f},{d_bb['ci95'] * sc:+8.2f}]{unit}")
        parts.append(f"P(<=0)={d_bb['p_le0']:.3f}")
    else:
        parts.append("(boot n/a)")
    return "  ".join(parts)


def bucket_run_ids(labels: list[str]) -> np.ndarray:
    """Global contiguous-run id over the cohort sequence."""
    ids = np.zeros(len(labels), dtype=np.int64)
    cur = 0
    for i in range(1, len(labels)):
        if labels[i] != labels[i - 1]:
            cur += 1
        ids[i] = cur
    return ids


def run_lengths(labels: list[str]) -> dict:
    out: dict = {}
    for b in BUCKETS:
        seq = [x == b for x in labels]
        runs = cur = 0
        for v in seq:
            if v:
                cur += 1
            elif cur:
                runs += 1
                cur = 0
        if cur:
            runs += 1
        days = int(sum(seq))
        out[b] = {"days": days, "runs": runs,
                  "mean_run": round(days / runs, 2) if runs else None}
    return out


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
    mom_grid = ac.grid_of(df, "mom20", dates, symbols)
    G = dl.session_grids(mats)
    rm = regime_map()
    print(f"panel dates={T} symbols={S}  regime days mapped={len(rm)}")

    # ---- R6-B: is the bucket a regime or a daily score? ----
    lab_days = [rm[d] for d in dates if d in rm]
    rl = run_lengths(lab_days)
    print("[R6-B] bucket persistence of the mapped calendar:")
    for b in BUCKETS:
        print(f"   {b:6s} days={rl[b]['days']:3d} runs={rl[b]['runs']:3d} "
              f"mean_run={rl[b]['mean_run']}")

    out: dict = {"seed": SEED, "persistence": rl, "arms": {}}

    for arm_label, ascending in [("mom_top20", False), ("mom_bottom20", True)]:
        picks_rows = ap.pick_daily(df, "mom20", ascending).select("date", "symbols").to_dicts()
        cohorts: list[dict] = []
        for r in picks_rows:
            dkey = str(r["date"])[:10]
            i0 = date_index[dkey]
            b = rm.get(dkey)
            if b is None or i0 + 20 >= T:
                continue
            st = dg.cohort_setup(i0, np.array([s_map[x] for x in r["symbols"]], dtype=np.int64),
                                 pool_base, to20, S)
            if st is None:
                continue
            mv = mom_grid[i0][st["pool"]]
            dmean = np.array([
                float(np.nanmean(mv[st["dec"] == k])) if (st["dec"] == k).any() else np.nan
                for k in range(dg.N_DECILE)
            ])
            w_pick = float(np.nanmean(mom_grid[i0][st["sel"]]))
            w_match = float(np.nansum(st["w"] * dmean))
            row = {"i0": i0, "bucket": b, "win": w_pick - w_match, "match": w_match}
            for key, gkey, off, _held, _legal in STRUCTS:
                t = dg.active_triplet(G[gkey][i0 + off], st)
                row[key] = np.nan if t is None else t[0] - t[2]
            cs = mom_grid[i0][np.isfinite(mom_grid[i0])]
            row["cs_p999"] = float(np.percentile(cs, 99.9)) if cs.size > 1000 else np.nan
            cohorts.append(row)

        n = len(cohorts)
        labels = [c["bucket"] for c in cohorts]
        rid = bucket_run_ids(labels)
        print(f"\n############ {arm_label}  cohorts={n}  "
              f"runs={int(np.unique(rid).size)}")
        A: dict = {"n": n, "runs": int(np.unique(rid).size)}

        # ---- pooled: all cells under three estimators ----
        print("  pooled (one estimator for every cell; R6-A):")
        pooled = {}
        for key, _gkey, _off, held, legal in STRUCTS:
            a = np.array([c[key] for c in cohorts], dtype=np.float64)
            d_i, d_n, d_b = iid_t(a, held), nw_t(a), block_boot(a)
            net_t = None
            if "se" in d_n and d_n["se"] > 0:
                net_t = round((d_n["mean"] * 1e4 - COST_BP) / (d_n["se"] * 1e4), 2)
            pooled[key] = {"legal": legal, "held": held, "iid": d_i, "nw": d_n,
                           "bb": d_b, "net_nw_t": net_t}
            print(f"   {key:3s} legal={'Y' if legal else 'N'} " + fmt_cell(d_i, d_n, d_b)
                  + f"  net_NW_t {net_t}")
        a = np.array([c["win"] for c in cohorts], dtype=np.float64)
        d_i, d_n, d_b = iid_t(a, 20), nw_t(a), block_boot(a)
        d_i1 = iid_t(a, 1)
        pooled["win"] = {"iid": d_i, "nw": d_n, "bb": d_b}
        print(f"   WIN      legal=- " + fmt_cell(d_i, d_n, d_b, pct=True)
              + f"   [part 3 printed iid(held=1) t {d_i1['t']:+.2f}]")
        A["pooled"] = pooled

        # ---- per bucket ----
        print("  by bucket (same three estimators + run-cluster t):")
        by_bucket: dict = {}
        for b in BUCKETS:
            idx = [i for i, c in enumerate(cohorts) if c["bucket"] == b]
            nb = len(idx)
            if nb < MIN_N:
                print(f"   {b:6s} n={nb} too few")
                continue
            run_id = rid[idx]
            B: dict = {"n": nb, "runs": int(np.unique(run_id).size)}
            for key in ("win", "S0", "S1", "S3", "S5", "S1L", "S2", "S6", "S8"):
                a = np.array([cohorts[i][key] for i in idx], dtype=np.float64)
                held = 20 if key in ("win", "S8") else 1
                B[key] = {"iid": iid_t(a, held), "nw": nw_t(a), "bb": block_boot(a),
                          "cluster": cluster_t(a, run_id)}
            a = np.array([cohorts[i]["match"] for i in idx], dtype=np.float64)
            B["match_baseline_bp"] = round(float(a.mean()) * 1e4, 1)
            a = np.array([cohorts[i]["cs_p999"] for i in idx], dtype=np.float64)
            B["cs_p999_pct"] = round(float(np.nanmean(a)) * 100, 1)
            by_bucket[b] = B
            print(f"   {b:6s} n={nb:3d} runs={B['runs']}")
            print(f"      win    " + fmt_cell(B["win"]["iid"], B["win"]["nw"], B["win"]["bb"],
                                               pct=True)
                  + f"  cl_t {B['win']['cluster'].get('t')}")
            for key in ("S1", "S0", "S5", "S3"):
                print(f"      {key:4s}   " + fmt_cell(B[key]["iid"], B[key]["nw"], B[key]["bb"])
                      + f"  cl_t {B[key]['cluster'].get('t')}")
            print(f"      match_baseline {B['match_baseline_bp'] / 100:+6.2f}%  "
                  f"cs_p99.9 {B['cs_p999_pct']:+6.1f}%")
        A["by_bucket"] = by_bucket

        # ---- R6-C: permutation calibration of "best of 3 buckets" ----
        if n > 300:
            perm = {"sizes": [int((np.array([BUCKETS.index(c["bucket"]) for c in cohorts]) == k).sum())
                              for k in range(3)], "observed": {}, "p_max": {}}
            arr = np.array([BUCKETS.index(c["bucket"]) for c in cohorts])

            def max_t(x: np.ndarray, lab: np.ndarray) -> float:
                v = []
                for k in range(3):
                    s = x[lab == k]
                    v.append(float(s.mean() / (s.std(ddof=0) / np.sqrt(s.size))))
                return max(v)

            for key in ("S1", "S0", "S5"):
                x = np.array([c[key] for c in cohorts], dtype=np.float64)
                obs = max_t(x, arr)
                perm["observed"][key] = round(obs, 2)
                null = np.empty(N_PERM)
                for i in range(N_PERM):
                    null[i] = max_t(x, RNG.permutation(arr))
                perm["p_max"][key] = round(float((null >= obs).mean()), 4)
                perm[f"null_{key}_p50"] = round(float(np.percentile(null, 50)), 2)
                perm[f"null_{key}_p95"] = round(float(np.percentile(null, 95)), 2)
            A["perm_max_t"] = perm
            print("  [R6-C] max-over-3-buckets permutation (iid t, sizes kept):")
            for key in ("S1", "S0", "S5"):
                print(f"     {key:3s} observed max t={perm['observed'][key]:+5.2f}  "
                      f"P(max>=obs)={perm['p_max'][key]:.3f}  "
                      f"null p50/p95={perm[f'null_{key}_p50']}/{perm[f'null_{key}_p95']}")

        out["arms"][arm_label] = A

    # ---- (6) lookahead in pick_daily's `fwd` filter ----
    fwd = G["c2c20"]
    per_day = []
    for r in ap.pick_daily(df, "mom20", False).select("date", "symbols").to_dicts():
        i0 = date_index[str(r["date"])[:10]]
        row = mom_grid[i0]
        ok = np.isfinite(row)
        top = np.argsort(-np.where(ok, row, -np.inf), kind="stable")[:20]
        per_day.append(int((~np.isfinite(fwd[i0][top])).sum()))
    pd_arr = np.array(per_day)
    look = {"cohorts": int(pd_arr.size),
            "days_any_missing": int((pd_arr > 0).sum()),
            "missing_picks_total": int(pd_arr.sum()),
            "missing_picks_frac": round(float(pd_arr.sum()) / (20 * pd_arr.size), 5)}
    out["pick_fwd_filter"] = look
    print(f"\n[lookahead] pick_daily requires fwd finite: {look['days_any_missing']} of "
          f"{look['cohorts']} signal days lose >=1 name "
          f"({look['missing_picks_total']} picks, "
          f"{look['missing_picks_frac'] * 100:.2f}% of pick slots)")

    p = Path("analysis/_diag_regime2_result.json")
    p.write_text(json.dumps(dg.clean_json(out), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {p}  elapsed={time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
