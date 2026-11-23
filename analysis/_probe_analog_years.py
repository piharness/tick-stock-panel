"""ONE-OFF：历史类比检验 —— "相似年份"到底存不存在、有没有预测力。

判据：
  A 2026 的最近邻：按四资产年度收益的欧氏距离找历史上最像的年份（只允许用 t 年及以前）
  B 类比法的样本外检验：对每个 t，用 t 的最近邻 s 的 s+1 去预测 t+1，看符号命中率
  C 条件基率：沪深300 跌超 10% 的年份之后一年发生了什么（N 很小，只能当基率）
  D 真正稳定的"相似性"：中证红利价格 vs 沪深300价格，逐年谁跑赢（2008-2026）

运行：python analysis/_probe_analog_years.py
"""
from __future__ import annotations

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


def idx_close(code: str) -> pd.Series:
    k = pd.read_parquet(IDX, columns=["symbol", "date", "close"])
    s = k[k.symbol == code].sort_values("date")
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["close"]


def etf_close(code: str) -> pd.Series:
    k = pd.read_parquet(ETF, columns=["symbol", "date", "close"])
    s = k[k.symbol == f"{code}.SH"].sort_values("date")
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["close"]


def yearly_from(s: pd.Series) -> pd.Series:
    m = s.sort_index().resample("ME").last()
    return (1 + m.pct_change()).groupby(m.index.year).apply(lambda g: g.prod(min_count=1) - 1)


def main() -> None:
    bond, _ = LB.bond_total_return()
    four = pd.DataFrame({
        "国债": yearly_from(bond),
        "沪深300": yearly_from(idx_close("000300.SH")),
        "红利": yearly_from(pd.read_parquet(H00922).set_index("date")["close"].sort_index()),
        "黄金": yearly_from(etf_close("518880")),
    })
    four = four.loc[2014:2026].dropna()
    print("四资产年度收益（%）")
    print((four * 100).round(1).to_string())

    print("\nA 2026 的最近邻（只允许用 2026 之前的年份）")
    cur = four.loc[2026]
    prior = four.loc[:2025]
    dist = ((prior - cur) ** 2).sum(axis=1) ** 0.5
    near = dist.nsmallest(4)
    for yy in near.index:
        nxt = four.loc[yy + 1] if (yy + 1) in four.index else None
        s = "  ".join(f"{k}{v:+.1%}" for k, v in four.loc[yy].items())
        t = ("  ".join(f"{k}{v:+.1%}" for k, v in nxt.items())) if nxt is not None else "（无下一年数据）"
        print(f"  {yy}（距离 {near[yy]:.3f}）当年 {s} → 次年 {t}")
    both = four[(four["沪深300"] < -0.10) & (four["黄金"] < -0.10)]
    print(f"  股、金同年均跌超10%：{list(both.index)} → "
          f"{'仅 2026 一年' if list(both.index) == [2026] else '见上'}")

    print("\nB 类比法样本外检验（预测次年沪深300的符号）")
    hits, preds = [], []
    for yy in range(2015, 2026):
        if yy not in four.index or 2026 == yy:
            continue
        hist = four.loc[: yy - 1]
        if len(hist) < 3:
            continue
        dist = ((hist - four.loc[yy]) ** 2).sum(axis=1) ** 0.5
        s = dist.idxmin()
        if s + 1 > yy - 1 or s + 1 not in four.index or yy + 1 not in four.index:
            continue
        pred = four.loc[s + 1, "沪深300"]
        real = four.loc[yy + 1, "沪深300"]
        hits.append(int(np.sign(pred) == np.sign(real)))
        preds.append((yy, s, pred, real))
    if preds:
        print("  " + " | ".join(f"{y}←{s}: 预测{p:+.0%}/实际{r:+.0%}" for y, s, p, r in preds))
        print(f"  符号命中 {sum(hits)}/{len(hits)} = {np.mean(hits):.0%}")

    print("\nC 条件基率：沪深300 当年跌超 10% 之后")
    for yy in four.index:
        if yy >= 2026:
            continue
        if four.loc[yy, "沪深300"] < -0.10:
            nxt = four.loc[yy + 1] if yy + 1 in four.index and yy + 1 <= 2026 else None
            if nxt is not None:
                print(f"  {yy} {four.loc[yy, '沪深300']:+.1%} → {yy+1} 沪深300 {nxt['沪深300']:+.1%}"
                      f" / 红利 {nxt['红利']:+.1%} / 国债 {nxt['国债']:+.1%}")

    print("\nD 红利价格 vs 沪深300价格：谁跑赢（2008-2026）")
    h = yearly_from(idx_close("000922.SH"))
    b = yearly_from(idx_close("000300.SH"))
    cmp = pd.DataFrame({"红利000922": h, "沪深300": b}).dropna()
    cmp["红利−300"] = cmp["红利000922"] - cmp["沪深300"]
    print((cmp * 100).round(1).to_string())
    win = (cmp["红利−300"] > 0)
    print(f"  红利跑赢 {int(win.sum())}/{len(cmp)} 年 = {win.mean():.0%}，"
          f"年均超额 {cmp['红利−300'].mean():+.2%}，最差 {cmp['红利−300'].min():+.2%}")


if __name__ == "__main__":
    main()
