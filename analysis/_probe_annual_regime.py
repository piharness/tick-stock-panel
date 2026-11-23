"""ONE-OFF：年度环境差异有多大、提前接住有多难、不预测的"灵活"是否有效。

判据（跑之前写死）：
  A 环境差异：逐年各资产收益 + 横截面离散度
  B 可预测性：年度排名持续性、买上年冠军 / 上年垫底 vs 等权
  C1 年度再平衡溢价（无预测的机械规则）
  C2 MA200 状态闸门：切换频率 vs 改善（日线口径）
  C3 12 个月动量 / 反转轮动（最常见的"灵活"方案，月频，4bp/次）
  D 噪音地板：N 个年度二选一的 2^N 全枚举 —— 灵活择时的收益分布有多宽

运行：python analysis/_probe_annual_regime.py
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch_leg2_bond as LB  # noqa: E402

IDX = ROOT / "data/backfill_20261117/index_daily_full.parquet"
ETF = ROOT / "data/backfill_20261117/etf_daily_full.parquet"
H00922 = ROOT / "data/etf_watch/_index_tr/H00922.parquet"
START = "2014-01-01"
COST = 0.0004          # 单边 4bp（ETF 万 0.5 + 半个 tick 近似）


def etf_close(code: str) -> pd.Series:
    k = pd.read_parquet(ETF, columns=["symbol", "date", "close"])
    s = k[k.symbol == f"{code}.SH"].sort_values("date")
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["close"]


def idx_close(code: str) -> pd.Series:
    k = pd.read_parquet(IDX, columns=["symbol", "date", "close"])
    s = k[k.symbol == code].sort_values("date")
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["close"]


def daily_levels() -> pd.DataFrame:
    bond, _ = LB.bond_total_return()
    df = pd.DataFrame({
        "国债511010": bond,
        "沪深300": idx_close("000300.SH"),
        "红利H00922": pd.read_parquet(H00922).set_index("date")["close"].sort_index(),
        "黄金518880": etf_close("518880"),
    }).sort_index()
    df.index = pd.to_datetime(df.index)
    return df.loc[df.index >= START].dropna(how="all")


def perf(r: pd.Series, ann: int = 252) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    return {"cagr": float(eq.iloc[-1] ** (1 / yrs) - 1), "mdd": float(dd.min()),
            "vol": float(r.std() * np.sqrt(ann)), "n": len(r)}


def yearly(m: pd.DataFrame) -> pd.DataFrame:
    return (1 + m).groupby(m.index.year).apply(lambda g: g.prod(min_count=1) - 1)


def main() -> None:
    d = daily_levels()
    m = d.resample("ME").last().pct_change().dropna(how="all")
    y = yearly(m)

    print("=" * 78)
    print("A 逐年收益（%）")
    print((y * 100).round(1).to_string())
    disp = y.max(axis=1) - y.min(axis=1)
    print(f"\n横截面离散度（最好−最差）：均值 {disp.mean():.1%} / 最小 {disp.min():.1%} / 最大 {disp.max():.1%}")

    print("=" * 78)
    print("B 可预测性")
    ry = y.rank(axis=1)
    for lag in (1, 2):
        ps = []
        for i in range(lag, len(ry)):
            a, b = ry.iloc[i - lag], ry.iloc[i]
            ok = a.notna() & b.notna()
            if ok.sum() >= 4:
                ps.append(a[ok].corr(b[ok]))
        ps = np.array([p for p in ps if np.isfinite(p)])
        print(f"  排名持续性 lag{lag}：Pearson(rank) 均值 {ps.mean():+.2f}，正 {int((ps > 0).sum())}/{len(ps)}")
    rows = []
    for i in range(1, len(y)):
        prev, cur = y.iloc[i - 1], y.iloc[i]
        ok = prev.notna() & cur.notna()
        if ok.sum() < 3:
            continue
        p, c = prev[ok], cur[ok]
        rows.append({"年": y.index[i], "冠军": p.idxmax(), "冠军超额": float(c[p.idxmax()] - c.mean()),
                     "垫底": p.idxmin(), "垫底超额": float(c[p.idxmin()] - c.mean())})
    t = pd.DataFrame(rows)
    print(t.assign(**{k: (t[k] * 100).round(1) for k in ("冠军超额", "垫底超额")}).to_string(index=False))
    print(f"  冠军次年超额 {t['冠军超额'].mean():+.2%}（正 {int((t['冠军超额'] > 0).sum())}/{len(t)}）"
          f" | 垫底次年超额 {t['垫底超额'].mean():+.2%}（正 {int((t['垫底超额'] > 0).sum())}/{len(t)}）")

    print("=" * 78)
    print("C1 年度再平衡（不做预测）")
    for e in ("红利H00922", "沪深300"):
        sub = m[[e, "国债511010"]].dropna()
        for w in (0.3, 0.5):
            wts = np.array([w, 1 - w])
            bh = (1 + (sub * wts).sum(axis=1)).cumprod()
            vals, cur = [], wts * 1.0
            for _, r in sub.iterrows():
                cur = cur * (1 + r.to_numpy())
                if r.name.month == 12:
                    cur = cur.sum() * wts
                vals.append(cur.sum())
            reb = pd.Series(vals, index=sub.index)
            a, b = perf(bh.pct_change().dropna(), 12), perf(reb.pct_change().dropna(), 12)
            print(f"  {e:10s} {int(w*100)}/{int((1-w)*100)}  B&H {a['cagr']:+.2%}/{a['mdd']:+.2%} | "
                  f"年再平衡 {b['cagr']:+.2%}/{b['mdd']:+.2%} | Δ {b['cagr']-a['cagr']:+.2%}")

    print("\nC2 MA200 闸门（日线，t 收盘算 t+1 生效）")
    for c in d.columns:
        s = d[c].dropna()
        r = s.pct_change().dropna()
        ma = s.rolling(200).mean()
        pos = (s > ma).astype(float).reindex(r.index).shift(1).fillna(0)
        yrs = (r.index[-1] - r.index[0]).days / 365.25
        sw = int((pos.diff().abs() > 0).sum())
        g = r * pos
        a, b = perf(r), perf(g)
        print(f"  {c:11s} 切换 {sw:3d} 次 = {sw/yrs:4.1f}/年 | B&H {a['cagr']:+.2%}/{a['mdd']:+.2%} | "
              f"MA200 {b['cagr']:+.2%}/{b['mdd']:+.2%} | Δcagr {b['cagr']-a['cagr']:+.2%}")

    print("\nC3 12 个月动量 / 反转轮动（月频，持 1 只，4bp/次）")
    lv = (1 + m).cumprod()
    mom = lv.pct_change(12)
    for label, pick in (("动量(追强)", "max"), ("反转(买弱)", "min")):
        pos, prev, rets = [], None, []
        for dt in m.index:
            sig = mom.loc[dt].dropna() if dt in mom.index else pd.Series(dtype=float)
            name = (sig.idxmax() if pick == "max" else sig.idxmin()) if len(sig) else None
            turn = 0.0 if name == prev else COST
            rets.append(float(m.loc[dt, name]) - turn if name else 0.0)
            pos.append(name)
            prev = name
        r = pd.Series(rets, index=m.index).iloc[12:]
        p = perf(r, 12)
        sw = sum(1 for i in range(1, len(pos)) if pos[i] != pos[i - 1])
        print(f"  {label:8s} {p['cagr']:+.2%}/{p['mdd']:+.2%} 切换 {sw} 次 ({sw/(p['n']/12):.1f}/年)")
    p = perf(m.mean(axis=1).iloc[12:], 12)
    print(f"  {'等权静态':8s} {p['cagr']:+.2%}/{p['mdd']:+.2%}")

    print("=" * 78)
    print("D 噪音地板：每年二选一（红利 or 国债）的全枚举")
    sub = m[["红利H00922", "国债511010"]].dropna()
    yr = yearly(sub).dropna()
    eq, rf = yr["红利H00922"].to_numpy(), yr["国债511010"].to_numpy()
    n = len(yr)
    out = np.array([float(np.prod(1 + np.where(np.array(b) == 1, eq, rf)) ** (1 / n) - 1)
                    for b in itertools.product([0, 1], repeat=n)])
    base = float(np.prod(1 + 0.5 * eq + 0.5 * rf) ** (1 / n) - 1)
    print(f"  {n} 个年度 → {len(out)} 条路径：最好 {out.max():+.2%} / 中位 {np.median(out):+.2%} / "
          f"最差 {out.min():+.2%} / sd {out.std():.2%}")
    print(f"  静态 50/50 {base:+.2%} | 随机年度择时跑赢静态 {(out > base).mean():.1%} | "
          f"跑赢 8% 的路径 {(out > 0.08).mean():.1%}")


if __name__ == "__main__":
    main()
