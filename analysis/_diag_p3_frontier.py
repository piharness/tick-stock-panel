"""第 3 阶段 · 持有期前沿与条件结构（不改阈值、不新增信号，只测量既有规则）。

三个问题（前两阶段都没回答）：
  P1 前向 15 日超额是「一次跳」还是「持续漂」？若不是持续漂，15 日持有期就是未测量的设计选择，
     而换手 16.8 次/年 × 0.30% = 5.0%/年的成本是 P&L 里第二大项。
  P2 五维条件里哪几维真的在承载信息？在 cum5∈(0,3%] 的条件样本内做条件 IC 与分位，
     检验冻结阈值是否落在真实梯度上（而不是任意切点）。
  P3 这个 8.65%/年、−29.3% 的组合，按 CRRA 确定性等价衡量，是否优于账户现行 7.87%/−6.91%。

复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_frontier.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "p3"
PANEL = ROOT / "data" / ".scratch" / "footprint20d" / "replay_daily.parquet"
NAV = ROOT / "data" / ".scratch" / "p2audit" / "portfolio_nav.csv"
MONTHLY = ROOT / "data" / ".scratch" / "p2audit" / "portfolio_monthly.csv"

KMAX = 25


def nw_t(x: np.ndarray, lag: int = 15) -> tuple[float, float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if x.size < 10:
        return float("nan"), float("nan")
    n = x.size
    mu = x.mean()
    e = x - mu
    v = e @ e / n
    for l in range(1, lag + 1):
        v += 2 * (1 - l / (lag + 1)) * (e[l:] @ e[:-l] / n)
    se = np.sqrt(max(v, 0.0) / n)
    return float(mu), float(mu / se) if se > 0 else float("nan")


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


def load():
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "close", "ret", "basic_ok", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index()
         for c in ["open", "close", "ret", "basic_ok", "amount"]}
    dates = list(w["open"].index)
    op = w["open"].to_numpy(float)
    cl = w["close"].to_numpy(float)
    R = w["ret"].to_numpy(float)
    B = w["basic_ok"].to_numpy(float) == 1
    A = w["amount"].to_numpy(float)
    mkt = np.array([np.nanmean(R[i, B[i]]) if B[i].sum() > 50 else 0.0 for i in range(len(dates))])
    return dates, list(w["open"].columns), op, cl, R, B, A, mkt


def build_signals(dates, B, R, mkt):
    rec = []
    for d in range(5, len(dates) - KMAX - 1):
        if B[d].sum() < 300:
            continue
        cum5, mono, eff, dd, snr = zpath(R, mkt, d)
        sig = (B[d] & np.isfinite(cum5) & (cum5 > 0) & (cum5 <= 0.03)
               & (mono >= 0.5) & (eff >= 0.3) & (dd >= -0.6)
               & (snr >= 1.0) & (snr < 2.0) & np.isfinite(R[d + 1]))
        if sig.any():
            for a in np.flatnonzero(sig):
                rec.append((d, a, cum5[a], mono[a], eff[a], dd[a], snr[a]))
    return pd.DataFrame(rec, columns=["di", "ai", "cum5", "mono5", "eff5", "dd5", "snr5"])


def event_curve(dates, op, cl, B, sig, kmax=KMAX):
    """逐信号：d+1 开盘入场，d+k 收盘出场；基准为同日 basic_ok 等权同窗口。"""
    days = np.sort(sig["di"].unique())
    rows = {k: {"day": [], "n": [], "ex": []} for k in range(1, kmax + 1)}
    for d in days:
        if d + kmax >= len(dates) or not np.isfinite(op[d + 1]).any():
            continue
        base = op[d + 1]
        mat = cl[d + 1:d + 1 + kmax] / base[None, :] - 1.0     # (kmax, n_sym)
        okb = B[d]
        if okb.sum() < 300:
            continue
        for k in range(1, kmax + 1):
            v = mat[k - 1]
            bench = np.nanmean(v[okb])
            a = sig.loc[sig.di == d, "ai"].to_numpy()
            ex = v[a] - bench
            ex = ex[np.isfinite(ex)]
            if ex.size == 0:
                continue
            rows[k]["day"].append(dates[d])
            rows[k]["n"].append(ex.size)
            rows[k]["ex"].append(float(ex.mean()))
    out = []
    for k in range(1, kmax + 1):
        r = rows[k]
        if not r["ex"]:
            continue
        x = np.array(r["ex"])
        mu, t = nw_t(x)
        n = np.array(r["n"])
        mu5, t5 = nw_t(x[n >= 5])
        out.append({"k": k, "days": len(x), "signals": int(n.sum()), "avg_n": float(n.mean()),
                    "ex_bp": mu * 1e4, "t": t, "ex_bp_n5": mu5 * 1e4, "t_n5": t5,
                    "hit": float((x > 0).mean())})
    return pd.DataFrame(out)


def annualized_frontier(curve: pd.DataFrame, costs=(0.0015, 0.0030)) -> pd.DataFrame:
    f = curve.copy()
    for c in costs:
        tag = f"{c*1e4:.0f}bp"
        f[f"net_bp_{tag}"] = f.ex_bp - c * 1e4
        f[f"ann_net_{tag}"] = (f.ex_bp / 1e4 - c) * (252 / f.k)
    return f


def conditional_structure(op, cl, R, B, sig, kmax=15):
    """在 cum5∈(0,3%] 的条件样本内，看其余四维是否还有横截面梯度。"""
    days = np.sort(sig["di"].unique())
    recs = []
    for d in days:
        if d + kmax >= len(dates) or not np.isfinite(op[d + 1]).any():
            continue
        base = op[d + 1]
        v = cl[d + kmax] / base - 1.0
        okb = B[d] & np.isfinite(v)
        if okb.sum() < 300:
            continue
        cum5, mono, eff, dd, snr = zpath(R, mkt, d)
        bucket = (okb & np.isfinite(cum5) & (cum5 > 0) & (cum5 <= 0.03))
        idx = np.flatnonzero(bucket)
        if idx.size < 100:
            continue
        ex = v[idx] - np.nanmean(v[okb])
        recs.append(pd.DataFrame({
            "date": dates[d], "symbol": None, "cum5": cum5[idx], "mono5": mono[idx],
            "eff5": eff[idx], "dd5": dd[idx], "snr5": snr[idx], "ex15": ex,
            "amount": A[d, idx],
        }))
    e = pd.concat(recs, ignore_index=True)
    e = e[np.isfinite(e.ex15)]
    return e


def ic_table(e: pd.DataFrame, feats=("mono5", "eff5", "dd5", "snr5", "cum5")) -> pd.DataFrame:
    """逐日 Spearman IC（在条件样本内），FM 平均 + t。"""
    out = []
    for f in feats:
        ics = []
        for _, g in e.groupby("date"):
            if len(g) < 50:
                continue
            a = g[f].rank()
            b = g.ex15.rank()
            if a.std() == 0 or b.std() == 0:
                continue
            ics.append(np.corrcoef(a, b)[0, 1])
        ics = np.array(ics)
        mu, t = nw_t(ics, 10)
        out.append({"feature": f, "ic": mu, "t": t, "days": ics.size})
    return pd.DataFrame(out)


def bucket_table(e: pd.DataFrame, f: str, bins) -> pd.DataFrame:
    g = e.copy()
    g["b"] = pd.cut(g[f], bins, include_lowest=True)
    r = g.groupby("b", observed=True).agg(n=("ex15", "size"), ex_bp=("ex15", lambda x: x.mean() * 1e4),
                                          hit=("ex15", lambda x: (x > 0).mean())).reset_index()
    r["b"] = r["b"].astype(str)
    return r


def crra_ce(monthly: pd.Series, gamma: float) -> float:
    r = monthly.to_numpy(float)
    r = r[np.isfinite(r)]
    if gamma == 1.0:
        ce_m = np.exp(np.mean(np.log1p(r))) - 1
    else:
        u = np.mean((1 + r) ** (1 - gamma)) / (1 - gamma)
        ce_m = ((u * (1 - gamma)) ** (1 / (1 - gamma))) - 1 if u * (1 - gamma) > 0 else np.nan
    return float((1 + ce_m) ** 12 - 1)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    global dates, A, mkt
    dates, syms, op, cl, R, B, A, mkt = load()
    dates = pd.DatetimeIndex(dates)
    sig = build_signals(dates, B, R, mkt)
    print(f"signals rebuilt: {len(sig)}  days: {sig.di.nunique()}  "
          f"range: {dates[sig.di.min()].date()}→{dates[sig.di.max()].date()}")

    curve = event_curve(dates, op, cl, B, sig)
    front = annualized_frontier(curve)

    # 子样本：曲线形状是否稳定
    subs = {}
    for name, lo, hi in (("2018-2021", "2018-01-01", "2021-12-31"), ("2022-2026", "2022-01-01", "2026-12-31")):
        s2 = sig[(dates[sig.di] >= pd.Timestamp(lo)) & (dates[sig.di] <= pd.Timestamp(hi))]
        c2 = event_curve(dates, op, cl, B, s2)
        subs[name] = c2.set_index("k").ex_bp.round(1).to_dict()

    cond = conditional_structure(op, cl, R, B, sig)
    ics = ic_table(cond)
    buckets = {
        "mono5": bucket_table(cond, "mono5", [-1, 0, 0.3, 0.5, 0.7, 0.9, 1.01]),
        "eff5": bucket_table(cond, "eff5", [-1, 0, 0.2, 0.3, 0.4, 0.6, 1.01]),
        "dd5": bucket_table(cond, "dd5", [-10, -0.8, -0.6, -0.4, -0.2, 0.01]),
        "snr5": bucket_table(cond, "snr5", [-10, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 10]),
        "cum5": bucket_table(cond, "cum5", [-1, 0, 0.005, 0.01, 0.02, 0.03, 0.05, 1]),
    }

    monthly = pd.read_csv(MONTHLY).ret
    ces = {f"gamma{g}": crra_ce(monthly, g) for g in (1.0, 2.0, 3.0, 5.0)}
    nav = pd.read_csv(NAV, index_col=0, parse_dates=True)
    ann = (nav.strategy.iloc[-1] / nav.strategy.iloc[0]) ** (252 / (len(nav) - 1)) - 1

    res = {
        "signals": int(len(sig)), "days": int(sig.di.nunique()),
        "curve": curve.round(4).to_dict("records"),
        "frontier": front.round(4).to_dict("records"),
        "subsample_ex_bp": subs,
        "conditional": {
            "n": int(len(cond)), "days": int(cond.date.nunique()),
            "ic": ics.round(4).to_dict("records"),
            "buckets": {k: v.round(1).to_dict("records") for k, v in buckets.items()},
        },
        "utility": {"ce": ces, "cagr_nav": float(ann), "months": int(len(monthly))},
    }
    json.dump(res, open(OUT / "frontier.json", "w"), ensure_ascii=False, indent=1, default=str)

    pd.set_option("display.width", 200)
    print("\n== 持有期前沿（逐信号，未扣成本）==")
    print(curve.round(1).to_string(index=False))
    print("\n== 年化净超额（always-in 近似）==")
    cols = ["k", "ex_bp", "net_bp_30bp", "ann_net_30bp", "ann_net_15bp"]
    print(front[cols].round(3).to_string(index=False))
    print("\n== 条件样本内 IC（cum5∈(0,3%]，逐日 Spearman）==")
    print(ics.round(4).to_string(index=False))
    for k, v in buckets.items():
        print(f"\n-- {k} 分位 --")
        print(v.to_string(index=False))
    print("\n== 确定性等价（月度，年化）==")
    print(json.dumps({"cagr_nav": ann, **ces}, ensure_ascii=False, indent=1))
    print("\n== 曲线子样本（ex_bp by k）==")
    for k, v in subs.items():
        print(k, v)


if __name__ == "__main__":
    main()
