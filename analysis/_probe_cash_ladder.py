"""ONE-OFF：现金腿的形状 —— 短融打底 + 国债按期限分档。

判据（跑之前写死）：
  A 三档资产的全收益序列（短融 / 5Y / 10Y），同窗口
  B 静态前沿：凸组合网格，看收益是否随久期线性、ratio 是否在某档见顶
  C 期限溢价是否可观测、是否有预测力（trailing 12M 超额 → forward 12M 超额）
  D 规则版 vs 静态版（月频调仓，4bp/次），以及 2026-11 末尾的读数

运行：python analysis/_probe_cash_ladder.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch_dividend as D  # noqa: E402
import etf_watch_leg2_rule as LR  # noqa: E402

COST = 0.0004
START = "2020-09-28"


def tr_of(code: str) -> tuple[pd.Series, str]:
    f = D.fetch(code)
    nav, ac = f["nav"], f["ac"]
    ns = nav / nav.iloc[0]
    dv = D.dividends_from_ac(ns, ac)
    if dv is not None and (dv > 0.001).sum() >= 1:
        return D.tr_from_nav_div(ns, dv), f"逐笔还原({int((dv>0.001).sum())}笔)"
    return ac / ac.iloc[0], "AC口径"


def perf(r: pd.Series) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    return {"cagr": float(eq.iloc[-1] ** (1 / yrs) - 1), "mdd": float(dd.min()),
            "worst20": float(eq.pct_change(20).min()), "vol": float(r.std() * np.sqrt(252))}


def main() -> None:
    raw = {}
    for code in ("511360", "511010", "511260"):
        s, how = tr_of(code)
        raw[code] = s
        print(f"{code}: {how}  {s.index[0].date()}→{s.index[-1].date()}  N={len(s)}")

    cal = pd.read_parquet(LR.TR_FILE).set_index("date")["close"].sort_index().index
    df = pd.DataFrame({k: v.reindex(cal).ffill() for k, v in raw.items()}).dropna()
    df = df.loc[df.index >= START]
    r = df.pct_change().dropna()
    print(f"\n共同窗口 {r.index[0].date()}→{r.index[-1].date()}  N={len(r)}")

    print("\nA 单档")
    for c in r.columns:
        p = perf(r[c])
        print(f"  {c}: 年化 {p['cagr']:+.2%} vol {p['vol']:.2%} MDD {p['mdd']:+.2%} "
              f"最差20日 {p['worst20']:+.2%} ratio {abs(p['cagr']/p['mdd']):.2f}")

    print("\nB 静态前沿（短融为底，其余分给 5Y / 10Y；10% 步长）")
    rows = []
    for f5 in np.arange(0, 1.01, 0.1):
        for f10 in np.arange(0, 1.01 - f5 + 1e-9, 0.1):
            fs = 1 - f5 - f10
            w = np.array([fs, f5, f10])
            rr = (r * w).sum(axis=1)
            p = perf(rr)
            rows.append({"短融": round(fs, 1), "5Y": round(f5, 1), "10Y": round(f10, 1),
                         "cagr": p["cagr"], "mdd": p["mdd"], "worst20": p["worst20"],
                         "ratio": abs(p["cagr"] / p["mdd"]) if p["mdd"] else np.nan})
    t = pd.DataFrame(rows)
    best = t.sort_values("ratio", ascending=False).head(6)
    print(best.assign(cagr=lambda d: (d.cagr * 100).round(2), mdd=lambda d: (d.mdd * 100).round(2),
                      worst20=lambda d: (d.worst20 * 100).round(2), ratio=lambda d: d.ratio.round(2)
                      ).to_string(index=False))
    print("\n  回撤预算内收益最高（MDD ≥ −1.5%）：")
    sub = t[t.mdd >= -0.015].sort_values("cagr", ascending=False).head(4)
    print(sub.assign(cagr=lambda d: (d.cagr * 100).round(2), mdd=lambda d: (d.mdd * 100).round(2),
                     ratio=lambda d: d.ratio.round(2)).to_string(index=False))
    print("\n  沿 5Y 单一轴的线性检验（短融:5Y = 1-w : w）")
    for w5 in (0, 0.25, 0.5, 0.75, 1.0):
        rr = r["511360"] * (1 - w5) + r["511010"] * w5
        p = perf(rr)
        print(f"    5Y {w5:.0%}: 年化 {p['cagr']:+.2%} MDD {p['mdd']:+.2%} "
              f"ratio {abs(p['cagr']/p['mdd']):.2f}")

    print("\nC 期限溢价：trailing 12M 超额 → forward 12M 超额（月频）")
    m = (1 + r).resample("ME").prod() - 1
    ex = (1 + r["511010"]).resample("ME").prod() - 1 - ((1 + r["511360"]).resample("ME").prod() - 1)
    tr12 = ex.rolling(12).sum()
    fw12 = ex[::-1].rolling(12).sum()[::-1].shift(-1)
    j = pd.concat([tr12.rename("trailing"), fw12.rename("forward")], axis=1).dropna()
    print(f"  样本 {len(j)} 个月；相关 {j['trailing'].corr(j['forward']):+.2f}")
    if len(j) >= 12:
        j["层"] = pd.qcut(j["trailing"], 3, labels=["低", "中", "高"])
        print(j.groupby("层", observed=True)["forward"].agg(["mean", "count", "min", "max"]).round(4).to_string())
    print(f"  当前读数：近 12M 5Y−短融 超额 {ex.tail(12).sum():+.2%}；"
          f"近 12M 10Y−短融 {(1+r['511260']).resample('ME').prod().sub((1+r['511360']).resample('ME').prod()).tail(12).sum():+.2%}")

    print("\nD 规则版：w_5Y = f(trailing 12M 超额)，单轴，月频，4bp/次")
    for lo, hi in ((0.2, 0.8), (0.3, 0.7)):
        w = (0.5 + (tr12 / 0.04).clip(-1, 1) * 0.3).clip(lo, hi).shift(1).reindex(m.index).ffill().fillna(0.5)
        rets = []
        prev = None
        for dt in m.index:
            ww = float(w.loc[dt]) if dt in w.index else 0.5
            turn = abs(ww - prev) if prev is not None else 0.0
            rets.append(ww * m.loc[dt, "511010"] + (1 - ww) * m.loc[dt, "511360"] - turn * COST)
            prev = ww
        rr = pd.Series(rets, index=m.index)
        p = perf(rr)
        print(f"  带 {lo:.0%}–{hi:.0%}: 年化 {p['cagr']:+.2%} MDD {p['mdd']:+.2%} "
              f"ratio {abs(p['cagr']/p['mdd']):.2f} 平均权重 {rr.index.size and float(w.mean()):.0%}")


if __name__ == "__main__":
    main()
