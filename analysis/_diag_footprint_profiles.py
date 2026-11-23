"""报告用：关键指标的十分位超额、赢家画像、窗口内/前对照。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"

KEYS = ["turn20", "log_amt20", "log_amihud", "ret5", "limit_up_cnt20",
        "i_sf_20", "i_entropy_20", "i_ret_ac1_20", "range_vol_ratio", "i_day_range_20"]


def z(s):
    return (s.rank(pct=True) - 0.5) * 2


def main():
    p = pd.read_parquet(OUT / "panel.parquet")
    p = p[p["ok"] == 1].copy()
    p["date"] = pd.to_datetime(p["date"])
    it = pd.read_parquet(OUT / "intraday_daily.parquet").sort_values(["symbol", "date"])
    it["date"] = pd.to_datetime(it["date"])
    for f in ["sf", "entropy", "ret_ac1", "day_range"]:
        it["i_" + f + "_20"] = it.groupby("symbol", sort=False)[f].transform(
            lambda s: s.rolling(20, min_periods=15).mean())
    p = p.merge(it[["symbol", "date"] + [c for c in it.columns if c.startswith("i_")]],
                on=["symbol", "date"], how="left")
    keys = [k for k in KEYS if k in p.columns]

    dec = {k: [[] for _ in range(10)] for k in keys}
    win_prof = {k: [] for k in keys}
    during = []
    for d, sub in p.groupby("date", sort=False):
        if len(sub) < 300:
            continue
        ex = sub["ex20_mb"].clip(sub["ex20_mb"].quantile(0.005),
                                sub["ex20_mb"].quantile(0.995)).to_numpy()
        win = ex >= np.nanquantile(ex, 0.9)
        for k in keys:
            v = sub[k]
            m = v.notna().to_numpy()
            if m.sum() < 200:
                continue
            b = pd.qcut(pd.Series(v[m]).rank(method="first"), 10, labels=False)
            gm = np.bincount(b, weights=ex[m], minlength=10) / np.maximum(
                np.bincount(b, minlength=10), 1)
            for i in range(10):
                dec[k][i].append(gm[i])
            # 同市值×波动格内赢家 z 差
            sz = pd.qcut(z(sub["log_mcap"]), 5, labels=False, duplicates="drop")
            vl = pd.qcut(z(sub["vol20"]), 5, labels=False, duplicates="drop")
            cell = pd.Series(list(zip(sz, vl)))
            vv = z(sub[k]).to_numpy()
            diffs, ws = [], []
            for _, idx in cell.groupby(cell).groups.items():
                idx = np.asarray(idx, dtype=int)
                w = win[idx]
                if w.sum() < 5 or (~w).sum() < 5:
                    continue
                if np.isfinite(vv[idx]).sum() < 10:
                    continue
                diffs.append(np.nanmean(vv[idx][w]) - np.nanmean(vv[idx][~w]))
                ws.append(w.sum())
            if diffs:
                win_prof[k].append(float(np.average(diffs, weights=ws)))
        # 窗口内 vs 前后
        zt = z(sub["turn20"]).to_numpy()
        ztf = z(sub["turn20_fwd"]).to_numpy()
        zv = z(sub["vol20"]).to_numpy()
        zvf = z(sub["vol20_fwd"]).to_numpy()
        during.append((zt[win].mean(), ztf[win].mean(), zv[win].mean(), zvf[win].mean(),
                       zt.mean(), ztf.mean()))

    out = {"deciles_bp": {}, "winner_z": {}, "during": {}}
    for k in keys:
        arr = np.array(dec[k], float)
        out["deciles_bp"][k] = [float(np.nanmean(arr[i]) * 1e4) for i in range(10)]
        out["winner_z"][k] = float(np.nanmean(win_prof[k]))
    dw = pd.DataFrame(during, columns=["turn_pre", "turn_dur", "vol_pre", "vol_dur",
                                       "all_turn_pre", "all_turn_dur"])
    out["during"] = {
        c: {"mean": float(dw[c].mean()), "t": float(dw[c].mean() / (dw[c].std() / np.sqrt(len(dw))))}
        for c in dw.columns
    }
    json.dump(out, open(OUT / "profiles.json", "w"), ensure_ascii=False, indent=1)
    for k in keys:
        print(k, [round(x, 0) for x in out["deciles_bp"][k]], "winz", round(out["winner_z"][k], 3))
    print(out["during"])


if __name__ == "__main__":
    main()
