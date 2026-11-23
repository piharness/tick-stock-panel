"""冻结协议的 2026 跟踪：无评分、等权全部候选、固定 20 日 cohort。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"
PANEL = OUT / "replay_daily.parquet"
SPEC = {
    "version": "2026-11-23",
    "strategy": "trend_breakout",
    "entry": "close>MA60 & close>=60d close high & volume/prev5d avg>=2",
    "filter": "exclude prior 20d limit-up OR turn_chg top decile",
    "score": "none (equal weight all candidates)",
    "hold_days": 20,
    "robust_hold_days": 25,
    "cost": 0.0015,
    "measure": "cohort equal-weight 20d excess vs eligible universe",
    "no_retune": True,
}


def main() -> None:
    df = pd.read_parquet(PANEL)
    df["date"] = pd.to_datetime(df["date"])
    cols = ["open", "close", "entry", "basic_ok", "limit20", "turn_top"]
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index() for c in cols}
    dates = list(w["open"].index)
    op = w["open"].to_numpy(float)
    cl = w["close"].to_numpy(float)
    en = w["entry"].to_numpy(float) == 1
    bok = w["basic_ok"].to_numpy(float) == 1
    lim = w["limit20"].to_numpy(float) > 0
    ttop = w["turn_top"].to_numpy(float) == 1
    syms = list(w["open"].columns)

    rows = []
    open_rows = []
    last_t = len(dates) - 1
    for t in range(60, last_t + 1):
        base = np.flatnonzero(en[t] & bok[t])
        cand = base[~(lim[t, base] | ttop[t, base])]
        if len(cand) == 0:
            continue
        d = dates[t]
        if t + 20 <= last_t:
            ret = cl[t + 20] / op[t + 1] - 1
            ok = bok[t] & np.isfinite(ret)
            bench = np.nanmean(ret[ok]) if ok.sum() >= 50 else np.nan
            ex = ret - bench
            for a in cand:
                if np.isfinite(ex[a]):
                    rows.append((d, syms[a], float(ex[a])))
        else:
            # 未到期 cohort：按最新收盘计浮盈
            if t + 1 > last_t:
                continue
            entry = op[t + 1]
            cur = cl[last_t] / entry - 1
            ok = bok[t] & np.isfinite(cur)
            bench = np.nanmean(cur[ok]) if ok.sum() >= 50 else np.nan
            for a in cand:
                if np.isfinite(cur[a]):
                    open_rows.append((d, syms[a], float(entry[a]), float(cl[last_t, a]),
                                      float(cur[a]), float(bench), float(cur[a] - bench)))
    e = pd.DataFrame(rows, columns=["date", "symbol", "ex"])
    e["month"] = e.date.dt.to_period("M").astype(str)
    monthly = e.groupby("month").agg(
        cohorts=("date", "nunique"),
        avg_candidates=("symbol", lambda s: len(s) / e.loc[s.index, "date"].nunique()),
        mean_bp=("ex", lambda s: s.mean() * 1e4),
        median_bp=("ex", lambda s: s.median() * 1e4),
        hit=("ex", lambda s: (s > 0).mean()),
    ).reset_index()
    # 按日聚合的 t（月度内）
    daily = e.groupby(["month", "date"]).ex.mean().reset_index()
    tvals = daily.groupby("month").ex.agg(["mean", "std", "count"]).reset_index()
    tvals["t"] = tvals["mean"] / (tvals["std"] / np.sqrt(tvals["count"]))
    tvals["mean_bp"] = tvals["mean"] * 1e4
    monthly = monthly.merge(tvals[["month", "t", "mean_bp"]].rename(columns={"mean_bp": "daily_mean_bp"}),
                            on="month", how="left")
    monthly.to_csv(OUT / "track_2026_monthly.csv", index=False)
    pd.DataFrame(open_rows, columns=["signal_date", "symbol", "entry_open", "last_close",
                                     "ret_since_entry", "bench", "excess"]).to_csv(
        OUT / "track_2026_open.csv", index=False)

    # 2026 汇总
    y26 = e[e.date >= "2026-01-01"]
    daily26 = y26.groupby("date").ex.mean()
    summary = {
        "as_of": str(dates[last_t])[:10],
        "cohort_days_2026": int(daily26.shape[0]),
        "avg_candidates": float(y26.groupby("date").size().mean()) if len(y26) else None,
        "mean_ex_bp": float(y26.ex.mean() * 1e4) if len(y26) else None,
        "daily_mean_bp": float(daily26.mean() * 1e4) if len(daily26) else None,
        "hit": float((y26.ex > 0).mean()) if len(y26) else None,
        "t": float(daily26.mean() / (daily26.std() / np.sqrt(len(daily26))))
        if len(daily26) > 2 else None,
        "open_cohorts": int(len(open_rows)),
        "spec": SPEC,
    }
    json.dump(summary, open(OUT / "track_2026_summary.json", "w"), ensure_ascii=False, indent=1)
    json.dump(SPEC, open(OUT / "frozen_spec.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(monthly.to_string(index=False))


if __name__ == "__main__":
    main()
