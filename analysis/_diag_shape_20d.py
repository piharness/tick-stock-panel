"""20 日超额路径的形状分类与价值检验。

形状在 t+1..t+20 的日度超额路径上定义；价值用三件事衡量：
  1) 第 5 日收盘才入场、持到第 20 日的剩余超额（可交易）
  2) 第 21..40 日的延续超额（形状是否可持续）
  3) 牛/熊（等权指数 vs MA60）下的稳健性
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"
PANEL = OUT / "panel.parquet"
DAILY = OUT / "replay_daily.parquet"


def main() -> None:
    p = pd.read_parquet(PANEL)
    p["date"] = pd.to_datetime(p["date"])
    p = p[(p["ok"] == 1) & (p["date"] >= "2019-01-01")].copy()
    d = pd.read_parquet(DAILY)
    d["date"] = pd.to_datetime(d["date"])
    ret = d.pivot(index="date", columns="symbol", values="ret").sort_index()
    ret = ret.loc[:, ~ret.columns.duplicated()]
    dates = list(ret.index)
    R = ret.to_numpy(float)
    syms = list(ret.columns)
    sidx = {s: i for i, s in enumerate(syms)}
    didx = {x: i for i, x in enumerate(dates)}

    # 市场路径：等权 ok 池
    anchors = sorted(p["date"].unique())
    rows = []
    for t in anchors:
        if t not in didx:
            continue
        ti = didx[t]
        if ti + 41 >= len(dates):
            continue
        sub = p[p["date"] == t]
        ids = np.array([sidx[s] for s in sub["symbol"] if s in sidx], dtype=int)
        if len(ids) < 500:
            continue
        # 要求 40 日路径完整
        path = R[ti + 1: ti + 41][:, ids]
        good = np.isfinite(path).all(axis=0)
        ids = ids[good]
        if len(ids) < 300:
            continue
        path = path[:, good]
        mkt = path.mean(axis=1)
        ex = path - mkt[:, None]
        cum = np.cumsum(ex[:20], axis=0)
        total = cum[19]
        next20 = ex[20:40].sum(axis=0)
        # 形状特征（20 日，事后）
        day = np.arange(1, 21, dtype=float)
        xd = day - day.mean()
        yc = cum - cum.mean(axis=0)
        mono = (xd @ yc) / np.sqrt((xd @ xd) * (yc * yc).sum(axis=0) + 1e-12)
        eff = total / (np.abs(ex[:20]).sum(axis=0) + 1e-12)
        # 前 5 日可观测特征（用于实时识别）
        d5 = np.arange(1, 6, dtype=float)
        x5 = d5 - d5.mean()
        y5 = cum[:5] - cum[:5].mean(axis=0)
        mono5 = (x5 @ y5) / np.sqrt((x5 @ x5) * (y5 * y5).sum(axis=0) + 1e-12)
        cum5, cum10 = cum[4], cum[9]
        ex5 = ex[:5]
        eff5 = cum5 / (np.abs(ex5).sum(axis=0) + 1e-12)
        run5 = np.maximum.accumulate(cum[:5], axis=0)
        dd5 = ((cum[:5] - run5) / (np.abs(cum5) + 1e-9)).min(axis=0)
        pos5 = (ex5 > 0).mean(axis=0)
        snr5 = cum5 / (ex5.std(axis=0) * np.sqrt(5) + 1e-9)
        ramp5 = (mono5 >= 0.5) & (eff5 >= 0.3) & (dd5 >= -0.6) & (cum5 > 0) & (snr5 >= 1)
        jump5 = ex5.max(axis=0) / (cum5 + 1e-9) >= 0.5
        runmax = np.maximum.accumulate(cum, axis=0)
        dd = ((cum - runmax) / (np.abs(total) + 1e-9)).min(axis=0)
        front5 = cum[4] / (total + 1e-9)
        back5 = (total - cum[14]) / (total + 1e-9)
        jump = ex.max(axis=0) / (total + 1e-9)
        pos = (ex > 0).mean(axis=0)
        remain5 = total - cum[4]
        remain10 = total - cum[9]
        cum_rem = cum[4:20] - cum[4]
        run_rem = np.maximum.accumulate(cum_rem, axis=0)
        dd_rem = (cum_rem - run_rem).min(axis=0)
        # 形状分类（对赢家；先 jump/front/back，再 V/ramp）
        shape = np.where(jump >= 0.5, "jump",
                 np.where(front5 >= 0.6, "front",
                  np.where(back5 >= 0.6, "back",
                   np.where((cum.min(axis=0) <= -0.3 * np.abs(total)) & (total > 0), "v",
                    np.where((mono >= 0.5) & (dd >= -0.5) & (eff >= 0.3), "ramp", "chop")))))
        for k, a in enumerate(ids):
            rows.append((t, syms[a], total[k], remain5[k], remain10[k], next20[k],
                         mono[k], eff[k], dd[k], front5[k], back5[k], jump[k], pos[k],
                         shape[k], cum5[k], cum10[k], mono5[k], eff5[k], dd5[k],
                         pos5[k], snr5[k], bool(ramp5[k]), bool(jump5[k]), dd_rem[k]))
    e = pd.DataFrame(rows, columns=[
        "date", "symbol", "total", "remain5", "remain10", "next20",
        "mono", "eff", "dd", "front5", "back5", "jump", "pos", "shape",
        "cum5", "cum10", "mono5", "eff5", "dd5", "pos5", "snr5", "ramp5", "jump5", "dd_rem",
    ])
    # 赢家：每个锚日 total top decile
    e["win"] = e.groupby("date")["total"].rank(pct=True) >= 0.9
    w = e[e.win].copy()
    #  regimes
    mkt_ret = ret.mean(axis=1).fillna(0)
    idx = (1 + mkt_ret).cumprod()
    ma60 = idx.rolling(60, min_periods=30).mean()
    regime = pd.DataFrame({"date": idx.index, "bull": idx > ma60}).set_index("date")["bull"]
    w["bull"] = w.date.map(regime)
    # 并入事前指标
    pre = p[["date", "symbol", "turn20", "vol20", "ret20", "limit_up_cnt20",
             "turn_chg", "log_mcap", "log_amihud"]]
    w = w.merge(pre, on=["date", "symbol"], how="left")
    w.to_parquet(OUT / "shape_20d.parquet")
    e.to_parquet(OUT / "shape_20d_all.parquet")

    def agg(x):
        return {
            "n": int(len(x)),
            "total_bp": float(x.total.mean() * 1e4),
            "remain5_bp": float(x.remain5.mean() * 1e4),
            "remain10_bp": float(x.remain10.mean() * 1e4),
            "next20_bp": float(x.next20.mean() * 1e4),
            "next_pos": float((x.next20 > 0).mean()),
            "remain5_pos": float((x.remain5 > 0).mean()),
            "dd": float(x.dd.mean()),
            "mono": float(x.mono.mean()),
            "share": float(len(x) / len(w)),
        }

    out = {"all": {s: agg(x) for s, x in w.groupby("shape")}}
    for reg in (True, False):
        out["bull" if reg else "bear"] = {
            s: agg(x) for s, x in w[w.bull == reg].groupby("shape")
        }
    json.dump(out, open(OUT / "shape_summary.json", "w"), ensure_ascii=False, indent=1)
    for name in ("all", "bull", "bear"):
        print("\n==", name)
        tab = pd.DataFrame(out[name]).T.sort_values("remain5_bp", ascending=False)
        print(tab.round(1).to_string())
    print("\nwinners", len(w), "anchors", w.date.nunique())


if __name__ == "__main__":
    main()
