"""ONE-OFF：资金面季节性（年末 / 春节）值多少，以及锁定期权的价格。

数据：511990 万份收益（摊余成本法的短端利率镜像，已被平滑，因此是**下界**估计）。
口径：日收益率 = 万份收益 / 持有天数 / 10000；年化 = 日收益率 × 365。
      周末/节假日的万份收益是合并发放的，必须按间隔天数摊回，否则年化会虚高 3 倍。

三条读数：
  A 季节曲线：相对「年末最后交易日」「春节」的日均年化，按偏移天分组
  B 窗口溢价：从 T-k 起买 28 天，锁定的平均年化 − 当年平均年化 = 你能多赚多少 bp
  C 期权成本：锁 28 天会错过哪些腿 1 信号（含 2024-02 集群的反例）

运行：python analysis/_probe_money_seasonality.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch_dividend as D  # noqa: E402

CNY = {  # 春节（正月初一）
    2013: "2013-02-10", 2014: "2014-01-31", 2015: "2015-02-19", 2016: "2016-02-08",
    2017: "2017-01-28", 2018: "2018-02-16", 2019: "2019-02-05", 2020: "2020-01-25",
    2021: "2021-02-12", 2022: "2022-02-01", 2023: "2023-01-22", 2024: "2024-02-10",
    2025: "2025-01-29", 2026: "2026-02-17", 2027: "2027-02-06",
}


def daily_yield() -> pd.Series:
    inc = D.fetch("511990")["income"]
    inc = inc.sort_index()
    gap = pd.Series(inc.index, index=inc.index).diff().dt.days.fillna(1).clip(lower=1)
    return (inc / gap / 10000 * 365).dropna()


def main() -> None:
    y = daily_yield()
    print(f"511990 年化序列 {y.index[0].date()} → {y.index[-1].date()}  N={len(y)}"
          f"  全期均值 {y.mean():.2%}")

    yr_avg = y.groupby(y.index.year).mean()
    print("\n逐年平均年化：")
    print((yr_avg * 100).round(2).to_string())

    # A 春节前后
    print("\nA 春节前后（按自然日偏移，跨年取平均，pct 年化）")
    rows = []
    for off in range(-40, 41, 5):
        vals = []
        for yy, cny in CNY.items():
            t = pd.Timestamp(cny) + pd.Timedelta(days=off)
            seg = y.loc[(y.index >= t - pd.Timedelta(days=2)) & (y.index <= t + pd.Timedelta(days=2))]
            if len(seg):
                vals.append(seg.mean())
        if vals:
            rows.append({"偏移": off, "年化": round(float(np.mean(vals)) * 100, 2), "N年": len(vals)})
    print(pd.DataFrame(rows).to_string(index=False))

    # A2 年末
    print("\nA2 年末最后交易日前后（pct 年化）")
    rows = []
    for off in range(-30, 31, 5):
        vals = []
        for yy in range(2013, 2027):
            t = pd.Timestamp(f"{yy}-12-31") + pd.Timedelta(days=off)
            seg = y.loc[(y.index >= t - pd.Timedelta(days=2)) & (y.index <= t + pd.Timedelta(days=2))]
            if len(seg):
                vals.append(seg.mean())
        if vals:
            rows.append({"偏移": off, "年化": round(float(np.mean(vals)) * 100, 2), "N年": len(vals)})
    print(pd.DataFrame(rows).to_string(index=False))

    # B 28 天窗口溢价
    print("\nB 从 T-k 起锁 28 天：锁定年化 − 当年平均（bp），按年看稳定性")
    for label, anchor in (("春节", "cny"), ("年末", "year")):
        out = {}
        for k in (0, 7, 14, 21, 28):
            prem = []
            for yy in range(2013, 2027):
                t = (pd.Timestamp(CNY[yy]) - pd.Timedelta(days=k)) if anchor == "cny" \
                    else (pd.Timestamp(f"{yy}-12-31") - pd.Timedelta(days=k))
                seg = y.loc[(y.index >= t) & (y.index < t + pd.Timedelta(days=28))]
                if len(seg) >= 15:
                    prem.append(seg.mean() - yr_avg.get(t.year, np.nan))
            prem = np.array([p for p in prem if np.isfinite(p)])
            out[k] = (prem.mean() * 1e4, (prem > 0).mean(), len(prem))
        print(f"  {label}: " + "  ".join(f"T-{k}: {v[0]:+.0f}bp(正{v[1]:.0%},N={v[2]})" for k, v in out.items()))

    # C 期权成本
    print("\nC 锁 28 天的期权成本：腿 1 信号落在锁定窗内的次数")
    trades = [
        ("2021-11-10", "化工", -0.0393), ("2022-02-28", "医药", -0.0476),
        ("2022-04-26", "军工", 0.2332), ("2022-07-20", "医药", -0.0625),
        ("2023-12-25", "传媒", -0.0374), ("2023-12-29", "电新", -0.1101),
        ("2024-02-01", "军工", 0.1525), ("2024-02-01", "通信", 0.3653),
        ("2024-02-02", "电子", 0.2601), ("2024-02-05", "有色", 0.2225),
    ]
    for d, name, ret in trades:
        t = pd.Timestamp(d)
        cny = min(CNY.values(), key=lambda c: abs((pd.Timestamp(c) - t).days))
        print(f"  {d} {name:4s} {ret:+.1%} | 距春节 {abs((pd.Timestamp(cny)-t).days):3d} 天 | "
              f"月份 {t.month:2d}")
    feb = [x for x in trades if x[0].startswith("2024-02")]
    print(f"\n  2024-02 集群 4 笔合计 {sum(x[2] for x in feb):+.1%}（1/6 仓 = 账户 "
          f"{sum(x[2] for x in feb)/6:+.1%}）")
    print("  若元旦后买入跨春节的 28 天逆回购（约 01-15 买、02-12 到期）⇒ 这 4 笔全部错过")


if __name__ == "__main__":
    main()
