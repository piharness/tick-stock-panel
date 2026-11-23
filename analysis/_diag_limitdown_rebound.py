"""跌停撬板日尾盘买入、次日开盘卖出的赔率（2016-2026，10% 跌停板）。

口径：
  - 撬板日 = 当日最低价触及跌停（low/prev-1 <= -9.5%）且收盘明显高于最低
    （close/low-1 >= 0.5%），即盘中被打开、未封死。
  - 买入 = 当日收盘价（尾盘跟进的保守近似）
  - 卖出 = 次日开盘价
  - 排除跨停牌（gap > 7 天）
"""
from __future__ import annotations

import os
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
files = []
for e in os.scandir(DATA / "kline_daily_enriched"):
    if e.is_dir() and e.name.startswith("date=") and e.name[5:] >= "2016-01-01":
        files.append(str(Path(e.path) / "part.parquet"))
files.sort()
print("files", len(files))

d = pl.read_parquet(files, columns=["symbol", "date", "open", "low", "close", "raw_close", "amount"])
d = d.filter((pl.col("raw_close") > 0) & (pl.col("close") > 0) & (pl.col("low") > 0)).sort(["symbol", "date"])

d = d.with_columns(
    pl.col("close").shift(1).over("symbol").alias("prev"),
    pl.col("date").shift(1).over("symbol").alias("prev_d"),
    pl.col("open").shift(-1).over("symbol").alias("nopen"),
    pl.col("low").shift(-1).over("symbol").alias("nlow"),
    pl.col("date").shift(-1).over("symbol").alias("next_d"),
)
d = d.with_columns(
    ((pl.col("date") - pl.col("prev_d")).dt.total_days() <= 7).alias("ok_prev"),
    ((pl.col("next_d") - pl.col("date")).dt.total_days() <= 7).alias("ok_next"),
)
d = d.filter(pl.col("ok_prev") & pl.col("ok_next") & pl.col("prev").is_not_null())

d = d.with_columns(
    (pl.col("close") / pl.col("prev") - 1).alias("ret"),
    (pl.col("low") / pl.col("prev") - 1).alias("low_ret"),
    (pl.col("close") / pl.col("low") - 1).alias("rebound"),
    (pl.col("nopen") / pl.col("close") - 1).alias("next_open_ret"),
)

# 撬板日：盘中触及 10% 跌停，收盘拉离最低
q = d.filter(
    (pl.col("low_ret") <= -0.095)
    & (pl.col("rebound") >= 0.005)
    & (pl.col("close") > 0)
    & (pl.col("nopen") > 0)
)
q = q.with_columns((pl.col("ret") <= -0.095).alias("prev_limit_down"))
print("撬板日样本", q.height)

def stats(frame: pl.DataFrame, label: str) -> None:
    if frame.height == 0:
        print(label, "n=0")
        return
    r = frame["next_open_ret"]
    print(
        f"{label:<28} n={frame.height:>5}  次日开盘均值 {r.mean()*100:>7.2f}%  "
        f"中位 {r.median()*100:>7.2f}%  胜率 {(r>0).mean()*100:>5.1f}%  "
        f"p10 {r.quantile(0.1)*100:>7.2f}%  p90 {r.quantile(0.9)*100:>7.2f}%  "
        f"次日一字跌停 {(frame['next_open_ret']<=-0.095).mean()*100:>5.1f}%"
    )

print("\n=== 全部撬板日：尾盘买、次日开盘卖 ===")
stats(q, "全部")
stats(q.filter(pl.col("prev_limit_down")), "前一日也是跌停")
stats(q.filter(~pl.col("prev_limit_down")), "前一日非跌停")

# 连续跌停天数 >=2 后的撬板
q2 = q.with_columns((pl.col("ret") <= -0.095).cast(pl.Int32).alias("ld"))
# 用窗口算连续跌停：简化用 prev 也跌停
print("\n=== 按年份 ===")
qy = q.with_columns(pl.col("date").dt.year().alias("y"))
for r in qy.group_by("y").agg(
    pl.len().alias("n"),
    (pl.col("next_open_ret").mean() * 100).alias("mean"),
    (pl.col("next_open_ret").median() * 100).alias("med"),
    ((pl.col("next_open_ret") > 0).mean() * 100).alias("win"),
    ((pl.col("next_open_ret") <= -0.095).mean() * 100).alias("locked"),
).sort("y").iter_rows(named=True):
    print(f"{r['y']}  n={r['n']:>4}  均值 {r['mean']:>6.2f}%  中位 {r['med']:>6.2f}%  胜率 {r['win']:>5.1f}%  次日一字跌停 {r['locked']:>5.1f}%")

# 只看价格 <2 元的撬板（与华幸同域）
print("\n=== 低价股（收盘 <2 元）撬板日 ===")
stats(q.filter(pl.col("close") < 2.0), "收盘<2元 全部")
stats(q.filter((pl.col("close") < 2.0) & pl.col("prev_limit_down")), "收盘<2元 + 前一日跌停")

# 2026 年
print("\n=== 2026 年 ===")
stats(q.filter(pl.col("date").dt.year() == 2026), "2026 全部")
