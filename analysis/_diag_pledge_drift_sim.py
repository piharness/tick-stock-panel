# -*- coding: utf-8 -*-
"""信号效果模拟：pledge_drift 作为多-空对冲组合的逐日前向回放。

评估日（2027-03-31）前无新数据，用已有全样本做「无前视的逐日滚动回放」，
度量信号的真实使用效果——不是一次性 design/holdout 的截面 d 值，而是：
    每个质押公告日，按冻结规格（月度截面 rank 三分位）把事件分到 Q0/Q1/Q2；
    从次日开盘起，做多 Q2（浅跌/涨）、做空 Q0（深跌），各持 60 日；
    逐日聚合多-空对冲收益，滚成净值，算年化/Sharpe/MDD/年度收益/成本敏感性。

诚实边界：
    - A 股个股融券受限，「做空 Q0」是信息量度量，不是可直接执行的做空；
      可执行口径是「回避」（§8 已算静态 exc60），本脚本另给「多头 Q2 - 空头 Q0」的相对净值。
    - 全样本已看过（弱 OOS），这是符号稳定性/形态模拟，不是干净样本外。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from _diag_pledge_drift import load_daily, nw_t

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
EV = OUT / "pledge_drift_events.parquet"

H = 61  # 持有 60 个交易日（入场次日开盘 → t+61 收盘），每日收益 61 段


def daily_paths(O, C, ti, ci, H=H):
    """每个事件入场后 H 日的每日收益矩阵 (n_ev × H)。第 0 段 = 开盘→当日收盘。"""
    n_ev = len(ti)
    daily = np.full((n_ev, H), np.nan, dtype=np.float32)
    for t in np.unique(ti):
        sel = np.nonzero(ti == t)[0]
        idx = ci[sel]
        if t + H + 1 > C.shape[0]:
            continue
        path = np.empty((H + 1, len(sel)), dtype=np.float64)
        path[0] = O[t + 1, idx]
        path[1:] = C[t + 1:t + H + 1, idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            d = path[1:] / np.maximum(path[:-1], 1e-9) - 1.0
        daily[sel] = d.T
    return daily


def main():
    print("[1/4] load ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    O = op_df.to_numpy(np.float32)
    C = cl_df.to_numpy(np.float32)
    n = C.shape[0]

    ev = pd.read_parquet(EV)
    ev["symbol"] = ev["symbol"].astype(str)
    ti = ev["tidx"].to_numpy()
    ci = ev["idx"].to_numpy()
    drift = ev["drift"].to_numpy()
    ym = ev["ym"].to_numpy()
    n_ev = len(ev)

    print(f"      事件 {n_ev:,}", flush=True)

    # 冻结规格：月度截面 rank 三分位
    q = np.full(n_ev, -1, dtype=int)
    for m in np.unique(ym):
        sel = np.nonzero(ym == m)[0]
        r = pd.Series(drift[sel]).rank(method="first").to_numpy()
        q[sel] = np.floor(r / len(sel) * 3).astype(int).clip(0, 2)

    print("[2/4] 每日收益路径 ...", flush=True)
    daily = daily_paths(O, C, ti, ci)  # n_ev × 61
    gidx = ti[:, None] + 1 + np.arange(H)[None, :]  # 全局日索引
    valid = gidx < n

    # 逐日聚合：多头 Q2、空头 Q0，等权
    long_pnl = np.zeros(n)
    long_n = np.zeros(n)
    short_pnl = np.zeros(n)
    short_n = np.zeros(n)
    lmask = (q[:, None] == 2) & valid
    smask = (q[:, None] == 0) & valid
    np.add.at(long_pnl, gidx[lmask], daily[lmask])
    np.add.at(long_n, gidx[lmask], 1)
    np.add.at(short_pnl, gidx[smask], daily[smask])
    np.add.at(short_n, gidx[smask], 1)

    long_r = np.where(long_n > 0, long_pnl / np.maximum(long_n, 1), 0.0)
    short_r = np.where(short_n > 0, short_pnl / np.maximum(short_n, 1), 0.0)
    hedged = long_r - short_r  # 多-空对冲（深跌跑输 ⇒ 空头亏负 ⇒ 对冲为正）

    # 只在「同时有多头和空头持仓」的交易日计入对冲（前 60 日 warm-up 与末尾尾部）
    both = (long_n > 0) & (short_n > 0)
    h_ser = pd.Series(hedged, index=dates)
    h_ser[~both] = np.nan

    print("[3/4] 组合指标 ...", flush=True)
    def stats(r: pd.Series):
        r = r.dropna()
        if len(r) < 30:
            return None
        nav = (1 + r).cumprod()
        peak = nav.cummax()
        dd = nav / peak - 1
        ann = float(nav.iloc[-1] ** (250 / len(r)) - 1) if nav.iloc[-1] > 0 else -1.0
        vol = float(r.std() * np.sqrt(250))
        sharpe = float(ann / vol) if vol > 0 else np.nan
        return {
            "days": int(len(r)),
            "cum": round(float(nav.iloc[-1] - 1), 4),
            "ann": round(ann, 4),
            "vol": round(vol, 4),
            "sharpe": round(sharpe, 3),
            "mdd": round(float(dd.min()), 4),
            "positive_days": round(float((r > 0).mean()), 3),
        }

    # 年度收益（对冲）
    yearly = []
    for yy, g in h_ser.groupby(h_ser.index.year):
        g = g.dropna()
        if len(g) < 20:
            continue
        yearly.append({"year": int(yy), "ret": round(float((1 + g).prod() - 1), 4), "days": int(len(g))})

    # 年化换手：多-空双边，等权，每事件 60 日 → 用真实「每日新进/出」近似
    # 简化且可解释：每事件持 60 日 ⇒ 组合平均每 60 日周转一次，双边年化换手 ≈ 2×250/60
    annual_turn = 2 * 250 / 60

    # 成本敏感性：每笔双边成本 c bp，年化净收益 = 毛年化 - annual_turn × c
    gross_ann = stats(h_ser)["ann"] if stats(h_ser) else np.nan
    cost_grid = []
    for c in [0, 5, 10, 15, 20, 30, 50]:
        net = gross_ann - annual_turn * c / 10000.0
        cost_grid.append({"bid_ask_bp": c, "net_ann": round(float(net), 4)})

    summary = {
        "sample": {"events": n_ev, "months": int(ev["ym"].nunique()),
                   "start": str(ev["date"].min().date()), "end": str(ev["date"].max().date())},
        "hedged": stats(h_ser),
        "long_Q2": stats(pd.Series(long_r, index=dates)[long_n > 0]),
        "short_Q0": stats(pd.Series(short_r, index=dates)[short_n > 0]),
        "yearly_hedged": yearly,
        "annual_turn_both_sides": round(annual_turn, 2),
        "cost_sensitivity": cost_grid,
        "note": (
            "做多 Q2（浅跌/涨质押股）、做空 Q0（深跌质押股），各持 60 日，等权，t+1 开盘入场。"
            "A 股个股融券受限：『做空 Q0』是信息量度量；可执行口径是『回避深跌』（§8 静态 exc60 = −468bp）。"
        ),
    }
    (OUT / "pledge_drift_sim.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 净值 CSV（供画图）
    nav_df = pd.DataFrame({
        "date": dates,
        "hedged_nav": (1 + h_ser.fillna(0)).cumprod(),
        "long_Q2_nav": (1 + pd.Series(long_r, index=dates)).cumprod(),
        "short_Q0_nav": (1 + pd.Series(short_r, index=dates)).cumprod(),
        "active": both.astype(int),
    })
    nav_df.to_csv(OUT / "pledge_drift_sim_nav.csv", index=False)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
