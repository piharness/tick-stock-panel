"""第四批（干净版）：市值分组月度复合收益 + 年度因子 + 换手-收益关系。

教训：group_by 聚合收益必须先「日均」再「复合」，直接 sum 得到的是股票-日总和。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_diag_regime_2026"

df = pl.read_parquet(OUT / "daily_panel.parquet").sort(["symbol", "date"])
shares = pl.read_parquet(ROOT / "data" / "backfill_20261117" / "fin_shares.parquet")
shares = (shares.select("symbol", "end_date", "total_shares")
          .filter(pl.col("total_shares") > 0)
          .unique(subset=["symbol", "end_date"], keep="last")
          .sort(["symbol", "end_date"]))

df = df.with_columns(
    pl.col("close").shift(1).over("symbol").alias("prev_close"),
    pl.col("date").shift(1).over("symbol").alias("prev_date"),
    pl.col("symbol").cum_count().over("symbol").alias("bar_no"),
).with_columns(((pl.col("date") - pl.col("prev_date")).dt.total_days() <= 7).alias("adj")
).with_columns(
    pl.when(pl.col("adj")).then(pl.col("close") / pl.col("prev_close") - 1).otherwise(None).alias("ret")
)
df = df.join_asof(shares, left_on="date", right_on="end_date", by="symbol", strategy="backward")
df = df.with_columns(
    (pl.col("raw_close") * pl.col("total_shares")).alias("mcap"),
    pl.when(pl.col("bar_no") > 60).then(pl.col("ret")).otherwise(None).alias("ret_x"),
)
df = df.with_columns(pl.col("mcap").rank().over("date").alias("mrk"), pl.len().over("date").alias("nn"))
df = df.with_columns((pl.col("mrk") / pl.col("nn")).alias("mpct")).with_columns(
    pl.when(pl.col("mpct") <= 0.2).then(pl.lit("Q1_micro"))
    .when(pl.col("mpct") <= 0.4).then(pl.lit("Q2_small"))
    .when(pl.col("mpct") <= 0.6).then(pl.lit("Q3_mid"))
    .when(pl.col("mpct") <= 0.8).then(pl.lit("Q4_large"))
    .otherwise(pl.lit("Q5_mega")).alias("mgrp")
)

cols = ["Q1_micro", "Q2_small", "Q3_mid", "Q4_large", "Q5_mega"]
# 日频组均值 → 月度复合
dgrp = (df.filter(pl.col("ret_x").is_not_null())
        .group_by(["date", "mgrp"]).agg(pl.col("ret_x").mean().alias("r"))
        .pivot(values="r", index="date", on="mgrp").sort("date")
        .with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m")))
mon = (dgrp.group_by("m").agg([((pl.col(c) + 1).product() - 1).alias(c) for c in cols])
       .sort("m"))
mon.write_parquet(OUT / "mcap_month2.parquet")

print("=== 月度市值分组收益（%，月度复合）===")
print("month     " + "  ".join(f"{c:>9}" for c in cols) + "   m-mega")
for r in mon.filter(pl.col("m") >= "2025-06").to_dicts():
    print(f"{r['m']}  " + "  ".join(f"{r[c]*100:>9.2f}" for c in cols)
          + f"  {(r['Q1_micro']-r['Q5_mega'])*100:>8.2f}")

print("\n=== 年度市值分组收益（%，年度复合）===")
y = mon.with_columns(pl.col("m").str.slice(0, 4).alias("y")).group_by("y").agg(
    [((pl.col(c) + 1).product() - 1).alias(c) for c in cols]).sort("y")
print("year  " + "  ".join(f"{c:>9}" for c in cols) + "  micro-mega")
for r in y.to_dicts():
    print(f"{r['y']}  " + "  ".join(f"{r[c]*100:>9.2f}" for c in cols)
          + f"  {(r['Q1_micro']-r['Q5_mega'])*100:>9.2f}")

# 等权 / 指数
d = pl.read_parquet(OUT / "daily_metrics.parquet").sort("date")
ew = d.select("date", "ew_ret", "med_ret", "xs_std", "breadth", "med_to", "cr10", "amt").with_columns(
    pl.col("date").dt.strftime("%Y-%m").alias("m"))
m2 = (ew.group_by("m").agg(
    ((pl.col("ew_ret") + 1).product() - 1).alias("ew"),
    pl.col("xs_std").mean().alias("std"),
    pl.col("breadth").mean().alias("brd"),
    pl.col("med_to").mean().alias("to"),
    pl.col("cr10").mean().alias("cr10"),
    (pl.col("amt").mean() / 1e8).alias("amt"),
).sort("m"))
print("\n=== 全市场月度 ===")
for r in m2.filter(pl.col("m") >= "2025-06").to_dicts():
    print(f"{r['m']} ew={r['ew']*100:>7.2f}% std={r['std']*100:.2f} brd={r['brd']*100:.1f} "
          f"to={r['to']:.2f}% cr10={r['cr10']*100:.1f}% amt={r['amt']:>6.0f}亿")
m2.write_parquet(OUT / "monthly_all.parquet")

# 换手-收益关系：按日横截面「换手率分位 → 次日收益」逐年
df2 = df.with_columns(pl.col("turnover_rate").rank().over("date").alias("trk"))
df2 = df2.with_columns((pl.col("trk") / pl.col("nn")).alias("tpct")).with_columns(
    pl.when(pl.col("tpct") <= 0.2).then(pl.lit("T1_low"))
    .when(pl.col("tpct") <= 0.4).then(pl.lit("T2"))
    .when(pl.col("tpct") <= 0.6).then(pl.lit("T3"))
    .when(pl.col("tpct") <= 0.8).then(pl.lit("T4"))
    .otherwise(pl.lit("T5_high")).alias("tgrp"))
df2 = df2.with_columns(pl.col("ret_x").shift(-1).over("symbol").alias("fwd1"),
                       pl.col("ret_x").shift(-20).over("symbol").alias("fwd20"))
fwd = (df2.filter(pl.col("fwd20").is_not_null())
       .with_columns(pl.col("date").dt.year().alias("y"))
       .group_by(["y", "tgrp"]).agg(pl.col("fwd20").mean().alias("r20"))
       .pivot(values="r20", index="y", on="tgrp").sort("y"))
print("\n=== 换手率分位 → 未来20日收益（%，非重叠日，含重叠偏差）===")
tc = ["T1_low", "T2", "T3", "T4", "T5_high"]
print("year  " + "  ".join(f"{c:>8}" for c in tc) + "   T5-T1")
for r in fwd.to_dicts():
    print(f"{r['y']}  " + "  ".join(f"{(r.get(c) or 0)*100:>8.2f}" for c in tc)
          + f"  {((r.get('T5_high') or 0)-(r.get('T1_low') or 0))*100:>8.2f}")
fwd.write_parquet(OUT / "turnover_fwd.parquet")
