"""诊断：换手≥25% 在 H1 上的 +65bp（t=7.1）是真信号还是口径产物。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl

spec = importlib.util.spec_from_file_location(
    "s2p", Path(__file__).with_name("_diag_signal_to_product.py")
)
s2p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2p)

df = s2p.add_excess(s2p.build(s2p.load_daily()))
df = df.with_columns((pl.col("turnover_rate") >= 25).alias("t"))

print("turnover 分位 50/90/95/99/99.5:",
      df.select([pl.col("turnover_rate").quantile(q).alias(f"q{q}") for q in (0.5, 0.9, 0.95, 0.99, 0.995)]).row(0))

sub = df.filter(pl.col("t"))
print("触发数", sub.height, "r1均值", round(sub["r1"].mean(), 5), "中位", round(sub["r1"].median(), 5))
print("x1(全市场超额) bp", round(sub["x1"].mean() * 1e4, 1),
      "| c1(同涨幅箱) bp", round(sub["c1"].mean() * 1e4, 1))
print("r1 分位 1/5/50/95/99:",
      sub.select([pl.col("r1").quantile(q).alias(f"q{q}") for q in (0.01, 0.05, 0.5, 0.95, 0.99)]).row(0))

print("\n按涨幅箱（后 10 箱）:")
print(
    df.filter(pl.col("t"))
    .group_by("bucket")
    .agg(pl.len().alias("n"),
         (pl.col("r1").mean() * 1e4).alias("r1_bp"),
         (pl.col("c1").mean() * 1e4).alias("c1_bp"),
         (pl.col("turnover_rate").mean()).alias("tr"))
    .sort("bucket").tail(10)
)

sub2 = df.filter(pl.col("t") & (pl.col("consecutive_limit_ups") == 0))
print("\n非涨停子集 n", sub2.height, "r1_bp", round(sub2["r1"].mean() * 1e4, 1),
      "c1_bp", round(sub2["c1"].mean() * 1e4, 1))

# 一字板/无法买入检查：T+1 开盘即涨停
df = df.with_columns(
    pl.when(pl.col("next_open_pct") >= pl.col("lp") - 0.005).then(1).otherwise(0).alias("next_open_at_limit")
)
print("\nT+1 开盘即涨停比例：全样本", round(df["next_open_at_limit"].mean(), 4),
      "换手≥25%", round(df.filter(pl.col("t"))["next_open_at_limit"].mean(), 4))
sub3 = df.filter(pl.col("t") & (pl.col("next_open_at_limit") == 0))
print("剔除 T+1 开盘即涨停后: n", sub3.height,
      "r1_bp", round(sub3["r1"].mean() * 1e4, 1),
      "c1_bp", round(sub3["c1"].mean() * 1e4, 1))

# 按日聚合的 H1 序列，看是否集中在少数天
d = (sub.select(["date", "c1"]).drop_nulls()
     .group_by("date").agg(pl.col("c1").mean().alias("v"), pl.len().alias("k")).sort("date"))
print("\nH1 逐日序列: days", d.height, "mean_bp", round(d["v"].mean() * 1e4, 1),
      "sd_bp", round(d["v"].std() * 1e4, 1), "正天占比", round((d["v"] > 0).mean(), 3))
top = d.sort("v", descending=True)
print("最好5天合计 bp", round(top["v"].head(5).sum() * 1e4, 1),
      "| 剔除最好5天后 mean_bp", round(top.tail(top.height - 5)["v"].mean() * 1e4, 1))
