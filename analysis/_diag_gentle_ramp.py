"""第 5 日路径分类器：识别温和早期 ramp，第 6 日开盘入场，持有 15 日。

信号（d 日收盘可知）：
  cum5   = 过去 5 日累计超额（相对 basic_ok 等权）
  mono5  = 5 日路径单调性
  eff5   = cum5 / 5 日绝对超额之和
  dd5    = 5 日路径回撤 / cum5
  snr5   = cum5 / (5 日超额 std * sqrt(5))
规则：cum5 0~3%、mono5>=0.5、eff5>=0.3、dd5>=-0.6、1<=snr5<2。
入场 d+1 开盘，出场 d+15 收盘（15 个交易日）。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"
PANEL = OUT / "replay_daily.parquet"


def zpath(R, mkt, d):
    ex = R[d - 4:d + 1] - mkt[d - 4:d + 1, None]
    cum = np.cumsum(ex, axis=0)
    cum5 = cum[4]
    day = np.arange(1, 6, dtype=float)
    x = day - day.mean()
    y = cum - cum.mean(axis=0)
    mono = (x @ y) / np.sqrt((x @ x) * (y * y).sum(axis=0) + 1e-12)
    eff = cum5 / (np.abs(ex).sum(axis=0) + 1e-12)
    run = np.maximum.accumulate(cum, axis=0)
    dd = ((cum - run) / (np.abs(cum5) + 1e-9)).min(axis=0)
    snr = cum5 / (ex.std(axis=0) * np.sqrt(5) + 1e-9)
    return cum5, mono, eff, dd, snr


def main() -> None:
    df = pd.read_parquet(PANEL)
    df["date"] = pd.to_datetime(df["date"])
    cols = ["open", "close", "ret", "basic_ok", "amount"]
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index() for c in cols}
    dates = list(w["open"].index)
    op = w["open"].to_numpy(float)
    cl = w["close"].to_numpy(float)
    R = w["ret"].to_numpy(float)
    B = w["basic_ok"].to_numpy(float) == 1
    A = w["amount"].to_numpy(float)
    syms = list(w["open"].columns)
    mkt = np.array([np.nanmean(R[i, B[i]]) if B[i].sum() > 50 else 0.0 for i in range(len(dates))])

    rows = []
    for d in range(5, len(dates) - 15):
        if B[d].sum() < 300 or not np.isfinite(op[d + 1]).any():
            continue
        cum5, mono, eff, dd, snr = zpath(R, mkt, d)
        # 基础规则
        sig = (B[d] & np.isfinite(cum5) & (cum5 > 0) & (cum5 <= 0.03)
               & (mono >= 0.5) & (eff >= 0.3) & (dd >= -0.6)
               & (snr >= 1.0) & (snr < 2.0))
        if sig.sum() == 0:
            continue
        fwd = cl[d + 15] / op[d + 1] - 1
        bench = np.nanmean(fwd[B[d] & np.isfinite(fwd)])
        ex = fwd - bench
        for a in np.flatnonzero(sig):
            if not np.isfinite(ex[a]):
                continue
            rows.append((dates[d], syms[a], cum5[a], mono[a], eff[a], dd[a], snr[a],
                         fwd[a], ex[a], A[d, a]))
    e = pd.DataFrame(rows, columns=[
        "date", "symbol", "cum5", "mono5", "eff5", "dd5", "snr5", "ret15", "ex15", "amount"
    ])
    e["year"] = e.date.dt.year
    e.to_parquet(OUT / "gentle_ramp_signals.parquet")

    # 按日聚合（每日等权 cohort）
    daily = e.groupby("date").agg(n=("ex15", "size"), ex=("ex15", "mean"),
                                  amt=("amount", "median")).reset_index()
    daily["year"] = daily.date.dt.year
    # NW(15) t
    x = daily.ex.to_numpy(float)
    mu = x.mean()
    ee = x - mu
    s = ee @ ee / len(x)
    for l in range(1, 16):
        g = ee[l:] @ ee[:-l] / len(x)
        s += 2 * (1 - l / 16) * g
    t = mu / np.sqrt(s / len(x))

    def yr(g):
        return g.groupby("year").apply(
            lambda y: pd.Series({"days": len(y), "avg_n": y.n.mean(),
                                 "ex_bp": y.ex.mean() * 1e4,
                                 "hit": (y.ex > 0).mean()}), include_groups=False)

    summary = {
        "signals": int(len(e)),
        "days": int(len(daily)),
        "avg_signals_per_day": float(daily.n.mean()),
        "median_amount_yi": float(e.amount.median() / 1e8),
        "gross_bp": float(mu * 1e4),
        "net_15bp_bp": float((mu - 0.0015) * 1e4),
        "hit": float((x > 0).mean()),
        "t_nw15": float(t),
        "ann_gross_if_always_in": float(mu * (252 / 15)),
        "ann_net_15bp": float((mu - 0.0015) * (252 / 15)),
        "yearly": yr(daily).reset_index().to_dict("records"),
        "cost_curve_bp": {str(c): float((mu - c) * 1e4) for c in [0.0015, 0.003, 0.005, 0.01]},
    }
    json.dump(summary, open(OUT / "gentle_ramp_summary.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(yr(daily).round(1).to_string())


if __name__ == "__main__":
    main()
