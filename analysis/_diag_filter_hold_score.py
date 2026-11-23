"""持有期 15/20/25 日与「去掉评分」的事件层检验。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"
PANEL = OUT / "replay_daily.parquet"

DESIGN = (pd.Timestamp("2019-01-01"), pd.Timestamp("2021-12-31"))
OOS = (pd.Timestamp("2022-01-01"), pd.Timestamp("2026-11-20"))


def main() -> None:
    df = pd.read_parquet(PANEL)
    df["date"] = pd.to_datetime(df["date"])
    cols = ["open", "close", "entry", "basic_ok", "limit20", "turn_top", "score"]
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index() for c in cols}
    dates = list(w["open"].index)
    op = w["open"].to_numpy(float)
    cl = w["close"].to_numpy(float)
    en = w["entry"].to_numpy(float) == 1
    bok = w["basic_ok"].to_numpy(float) == 1
    lim = w["limit20"].to_numpy(float) > 0
    ttop = w["turn_top"].to_numpy(float) == 1
    score = w["score"].to_numpy(float)
    syms = list(w["open"].columns)

    rows = []
    for t in range(60, len(dates) - 26):
        base = np.flatnonzero(en[t] & bok[t])
        if len(base) < 10:
            continue
        order = np.argsort(np.argsort(score[t, base], kind="stable"), kind="stable")
        q = np.clip((order * 5 // len(base)), 0, 4)
        qmap = np.full(len(syms), -1, dtype=int)
        qmap[base] = q
        cand = base[~(lim[t, base] | ttop[t, base])]
        if len(cand) == 0:
            continue
        for h in (15, 20, 25):
            ret = cl[t + h] / op[t + 1] - 1
            ok = bok[t] & np.isfinite(ret)
            if ok.sum() < 50:
                continue
            bench = np.nanmean(ret[ok])
            ex = ret - bench
            for a in cand:
                if not np.isfinite(ex[a]):
                    continue
                rows.append((dates[t], h, int(qmap[a]), float(ex[a])))
    e = pd.DataFrame(rows, columns=["date", "h", "q", "ex"])
    out = {}
    for name, (lo, hi) in [("design", DESIGN), ("oos", OOS)]:
        x = e[(e.date >= lo) & (e.date <= hi)]
        out[name] = {}
        for h in (15, 20, 25):
            xh = x[x.h == h]
            out[name][h] = {}
            for g, mask in [("all", np.ones(len(xh), bool))] + [
                (f"Q{i}", xh.q == i) for i in range(5)
            ]:
                z = xh[mask]
                daily = z.groupby("date").ex.mean()
                out[name][h][g] = {
                    "n": int(len(z)),
                    "mean_bp": float(z.ex.mean() * 1e4) if len(z) else None,
                    "daily_bp": float(daily.mean() * 1e4) if len(daily) else None,
                    "win": float((z.ex > 0).mean()) if len(z) else None,
                    "t": float(daily.mean() / (daily.std() / np.sqrt(len(daily))))
                    if len(daily) > 2 else None,
                }
    json.dump(out, open(OUT / "hold_score_event.json", "w"), ensure_ascii=False, indent=1)
    for period in ("design", "oos"):
        print("\n==", period)
        for h in (15, 20, 25):
            print("h", h, {g: (round(v["mean_bp"], 0), round(v["t"], 2))
                           for g, v in out[period][h].items()})


if __name__ == "__main__":
    main()
