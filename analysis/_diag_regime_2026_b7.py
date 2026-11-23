"""季节性 + 超跌反弹赔率检验（2016-2026）。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_diag_regime_2026"
d = pl.read_parquet(OUT / "daily_metrics.parquet").sort("date")
d = d.with_columns(
    pl.col("date").dt.year().alias("y"),
    pl.col("date").dt.month().alias("m"),
)

# 1) 12 月 / 1 月等权收益（历史季节性）
mon = (d.group_by(["y", "m"]).agg(
    ((pl.col("ew_ret") + 1).product() - 1).alias("r"),
    pl.col("ew_ret").mean().alias("dmean"),
    pl.len().alias("n")).sort(["y", "m"]))
print("=== 12 月 / 1 月 等权收益（%）===")
for mm in (12, 1, 2):
    sub = mon.filter(pl.col("m") == mm)
    vals = [round(x * 100, 2) for x in sub["r"].to_list()]
    pos = sum(1 for x in vals if x > 0)
    print(f"月份 {mm:>2}: {vals}  中位 {sorted(vals)[len(vals)//2]:>6.2f}  胜率 {pos}/{len(vals)}")

# 2) 12 月风格：小盘 vs 大盘（用市值分层表）
mp = pl.read_parquet(OUT / "mcap_month2.parquet")
mp = mp.with_columns(pl.col("m").str.slice(0, 4).cast(pl.Int32).alias("y"),
                     pl.col("m").str.slice(5, 2).cast(pl.Int32).alias("m"))
print("\n=== 12 月 小盘-大盘 差（Q1-Q5，%）===")
for r in mp.filter(pl.col("m") == 12).sort("y").to_dicts():
    print(f"  {r['y']}: Q1 {r['Q1_micro']*100:>7.2f}  Q5 {r['Q5_mega']*100:>7.2f}  差 {(r['Q1_micro']-r['Q5_mega'])*100:>7.2f}")

# 3) 全 A 等权回撤 -> 未来 20/60 日收益
d = d.with_columns((pl.col("ew_ret").fill_null(0) + 1).cum_prod().alias("ew_idx"))
d = d.with_columns(pl.col("ew_idx").rolling_max(60).alias("hi60"))
d = d.with_columns((pl.col("ew_idx") / pl.col("hi60") - 1).alias("dd60"))
d = d.with_columns(
    (pl.col("ew_idx").shift(-20) / pl.col("ew_idx") - 1).alias("f20"),
    (pl.col("ew_idx").shift(-60) / pl.col("ew_idx") - 1).alias("f60"),
)
print("\n=== 等权60日回撤分档 -> 未来收益 ===")
bins = [(-1.0, -0.30), (-0.30, -0.20), (-0.20, -0.10), (-0.10, 0.0)]
for lo, hi in bins:
    s = d.filter((pl.col("dd60") > lo) & (pl.col("dd60") <= hi))
    if s.height == 0:
        continue
    print(f"  回撤 {lo*100:>5.0f}~{hi*100:>5.0f}%: n={s.height:>4}  "
          f"未来20日均值 {s['f20'].mean()*100:>6.2f}% 中位 {s['f20'].median()*100:>6.2f}% 胜率 {(s['f20']>0).mean()*100:>5.1f}%  "
          f"未来60日均值 {s['f60'].mean()*100:>7.2f}%")

# 4) 低位区（当前 3584）之后 60 日
cur = d.tail(1)
print(f"\n当前: ew 回撤 {cur['dd60'][0]*100:.2f}%  ew_idx {cur['ew_idx'][0]:.4f}")

# 5) 12 月单月：全 A 等权在 12 月的日胜率
print("\n=== 12 月逐日胜率（等权）===")
for y in range(2016, 2026):
    s = d.filter((pl.col("y") == y) & (pl.col("m") == 12))
    if s.height:
        print(f"  {y}-12: {(s['ew_ret']>0).mean()*100:>5.1f}% 收益 {((s['ew_ret']+1).product()-1)*100:>7.2f}%")
