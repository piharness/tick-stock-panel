"""第六批：量价关系的年度位移 + 分布形状 + 集中度历史。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_diag_regime_2026"
d = pl.read_parquet(OUT / "daily_metrics.parquet").sort("date")
d = d.with_columns(pl.col("date").dt.year().alias("y"))

y = (d.group_by("y").agg(
    pl.len().alias("d"),
    ((pl.col("ew_ret") + 1).product() - 1).alias("ew"),
    (pl.col("amt").mean() / 1e8).alias("amt"),
    pl.col("med_to").mean().alias("to"),
    pl.col("xs_std").mean().alias("std"),
    pl.col("breadth").mean().alias("brd"),
    pl.col("cr10").mean().alias("cr10"),
    pl.col("up_like").mean().alias("ul"),
    pl.col("dn_like").mean().alias("dl"),
).sort("y"))

print("=== 年度量价结构 ===")
print("year   EW%     amt亿    to%    xstd%  brd%  cr10%  UL%   DL%   | log(amt)~EW")
import math
for r in y.to_dicts():
    print(f"{r['y']}  {r['ew']*100:>7.2f} {r['amt']:>8.0f} {r['to']:>6.2f} {r['std']*100:>5.2f} "
          f"{r['brd']*100:>5.1f} {r['cr10']*100:>5.1f} {r['ul']*100:>5.2f} {r['dl']*100:>5.2f}")

# 成交额增速 vs 收益
print("\n=== 成交额同比 vs 当年等权收益 ===")
prev = None
for r in y.to_dicts():
    if prev:
        g = r["amt"] / prev - 1
        print(f"{r['y']}  amt {g*100:>7.1f}%   EW {r['ew']*100:>7.2f}%")
    prev = r["amt"]

# 2026 年内：成交额与后续 20 日收益（是否"放量即顶"）
px = d.filter(pl.col("date") >= dt.date(2026, 1, 1)).sort("date")
px = px.with_columns(
    (pl.col("ew_ret") + 1).cum_prod().alias("ew_idx"),
    (pl.col("amt").rolling_mean(20) / pl.col("amt").shift(20).rolling_mean(20) - 1).alias("amt_yoy"),
)
px = px.with_columns(
    (pl.col("ew_idx").shift(-20) / pl.col("ew_idx") - 1).alias("fwd20")
)
sub = px.drop_nulls(["amt_yoy", "fwd20"])
print(f"\n2026 放量 vs 未来20日收益: n={sub.height}")
if sub.height > 5:
    import statistics
    xs = sub["amt_yoy"].to_list()
    ys = sub["fwd20"].to_list()
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((a-mx)*(b-my) for a, b in zip(xs, ys)) / len(xs)
    sx = (sum((a-mx)**2 for a in xs)/len(xs))**0.5
    sy = (sum((b-my)**2 for b in ys)/len(ys))**0.5
    print(f"  corr = {cov/(sx*sy):.3f}  (放量→后续收益)")

# 2026 各月个股收益标准差（离散度）与等权收益
print("\n=== 月度：离散度 vs 收益 ===")
mm = (px.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m")).group_by("m").agg(
    ((pl.col("ew_ret") + 1).product() - 1).alias("ew"),
    pl.col("xs_std").mean().alias("std"),
    pl.col("breadth").mean().alias("brd")).sort("m"))
for r in mm.to_dicts():
    print(f"{r['m']}  ew {r['ew']*100:>7.2f}%  xstd {r['std']*100:.2f}%  brd {r['brd']*100:.1f}%")
