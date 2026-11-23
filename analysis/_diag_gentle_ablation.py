"""温和 ramp 分类器的消融、tranche 组合、容量与成本。"""
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

    sets = ["gentle", "cum5_only", "mono_only", "snr_band", "all"]
    rec = {s: [] for s in sets}
    sig_rows = []
    for d in range(5, len(dates) - 15):
        if B[d].sum() < 300:
            continue
        cum5, mono, eff, dd, snr = zpath(R, mkt, d)
        base = B[d] & np.isfinite(cum5)
        masks = {
            "gentle": base & (cum5 > 0) & (cum5 <= 0.03) & (mono >= 0.5)
            & (eff >= 0.3) & (dd >= -0.6) & (snr >= 1) & (snr < 2),
            "cum5_only": base & (cum5 > 0) & (cum5 <= 0.03),
            "mono_only": base & (mono >= 0.5),
            "snr_band": base & (snr >= 1) & (snr < 2),
            "all": base,
        }
        fwd = cl[d + 15] / op[d + 1] - 1
        ok = B[d] & np.isfinite(fwd)
        bench = np.nanmean(fwd[ok])
        ex = fwd - bench
        for s, m in masks.items():
            m = m & np.isfinite(ex)
            if m.sum() >= 5:
                rec[s].append((dates[d], float(ex[m].mean()), int(m.sum())))
        m = masks["gentle"] & np.isfinite(ex)
        for a in np.flatnonzero(m):
            sig_rows.append((dates[d], syms[a], fwd[a], ex[a], A[d, a]))
    out = {}
    for s in sets:
        x = pd.DataFrame(rec[s], columns=["date", "ex", "n"])
        x["year"] = x.date.dt.year
        mu = x.ex.mean()
        ee = (x.ex - mu).to_numpy(float)
        var = ee @ ee / len(x)
        for l in range(1, 16):
            var += 2 * (1 - l / 16) * (ee[l:] @ ee[:-l] / len(x))
        out[s] = {
            "days": int(len(x)), "avg_n": float(x.n.mean()),
            "gross_bp": float(mu * 1e4), "hit": float((x.ex > 0).mean()),
            "t": float(mu / np.sqrt(var / len(x))),
            "yearly_bp": {int(k): float(v * 1e4) for k, v in x.groupby("year").ex.mean().items()},
        }
    json.dump(out, open(OUT / "gentle_ablation.json", "w"), ensure_ascii=False, indent=1)
    print(pd.DataFrame(out).T[["days", "avg_n", "gross_bp", "hit", "t"]].round(2).to_string())

    # tranche 组合：15 个 cohort，每个 1/15 资金，cohort 内等权、买入持有 15 日
    sig = pd.DataFrame(sig_rows, columns=["date", "symbol", "fwd", "ex", "amount"])
    sidx = {x: i for i, x in enumerate(syms)}
    by = {d: g for d, g in sig.groupby("date")}
    port_ret = np.zeros(len(dates))
    for j, d in enumerate(dates):
        if d not in by or j + 15 >= len(dates):
            continue
        idx = [sidx[x] for x in by[d].symbol if x in sidx]
        if not idx:
            continue
        cd = np.nanmean(R[j + 1:j + 16][:, idx], axis=1)  # 15 日 cohort 日收益
        # 首日按 d+1 开盘买入：去掉隔夜 gap
        cd[0] = np.nanmean(cl[j + 1, idx] / op[j + 1, idx] - 1)
        for k in range(15):
            port_ret[j + 1 + k] += cd[k] / 15
        port_ret[j + 1] -= 0.00075 / 15
        port_ret[j + 15] -= 0.00075 / 15
    port_ret = pd.Series(port_ret, index=dates)
    bench = pd.Series(mkt, index=dates)
    ex = port_ret - bench
    nav = (1 + port_ret).cumprod()
    g = out["gentle"]["gross_bp"] / 1e4
    out2 = {
        "ann_ret": float(nav.iloc[-1] ** (252 / len(nav)) - 1),
        "event_ann_gross": float(g * 252 / 15),
        "event_ann_net_15bp": float((g - 0.0015) * 252 / 15),
        "ann_excess_note": "daily basic_ok benchmark is a changing universe; do not use ann_excess vs it",
        "max_dd": float((nav / nav.cummax() - 1).min()),
        "avg_signals": float(sig.groupby("date").size().mean()),
        "median_amount_yi": float(sig.amount.median() / 1e8),
        "cost_curve_ann_event": {str(c): float((g - c) * 252 / 15) for c in [0.0015, 0.003, 0.005, 0.01]},
        "capacity_aum_yi_at_5pct": float(0.05 * sig.amount.median() * sig.groupby("date").size().mean() * 15 / 1e8),
        "capacity_aum_yi_at_10pct": float(0.10 * sig.amount.median() * sig.groupby("date").size().mean() * 15 / 1e8),
    }
    json.dump(out2, open(OUT / "gentle_portfolio.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(out2, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
