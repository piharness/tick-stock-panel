"""第三批（修正）：市值分层 + YTD 分布 + 指数背离。"""
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
mon = (df.filter(pl.col("ret_x").is_not_null())
       .with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m"))
       .group_by(["m", "mgrp"]).agg(pl.col("ret_x").sum().alias("r"), pl.len().alias("n"))
       .pivot(values="r", index="m", on="mgrp").sort("m"))
mon.write_parquet(OUT / "mcap_month.parquet")

print("=== 年度市值分组累计（日收益求和，%）===")
g = mon.with_columns(pl.col("m").str.slice(0, 4).alias("y")).group_by("y").agg(
    [pl.col(c).sum().alias(c) for c in cols]).sort("y")
print("year  " + "  ".join(f"{c:>9}" for c in cols) + "  micro-mega")
for r in g.to_dicts():
    print(f"{r['y']}  " + "  ".join(f"{r[c]*100:>9.2f}" for c in cols)
          + f"  {(r['Q1_micro']-r['Q5_mega'])*100:>9.2f}")

# YTD 个股收益（2026）
d0 = dt.date(2026, 1, 1)
first = df.filter(pl.col("date") >= d0)
px = (first.group_by("symbol")
      .agg(pl.col("close").sort_by("date").first().alias("c0"),
           pl.col("close").sort_by("date").last().alias("c1"),
           pl.col("mcap").median().alias("mcap_med"),
           pl.col("bar_no").min().alias("b0"))
      .with_columns(((pl.col("c1") / pl.col("c0")) - 1).alias("ytd")))
px = px.join(df.filter(pl.col("date") == df["date"].max()).select("symbol", "mgrp"),
             on="symbol", how="left")
px.write_parquet(OUT / "ytd_2026.parquet")

print(f"\n=== 2026 YTD 个股收益（n={px.height}）===")
for x in [0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]:
    print(f"  p{int(x*100):>2} = {px['ytd'].quantile(x)*100:>8.2f}%")
print(f"  mean = {px['ytd'].mean()*100:.2f}%  pos = {(px['ytd']>0).mean()*100:.1f}%")
print("\n按期末市值组：")
print(px.group_by("mgrp").agg(pl.len().alias("n"), (pl.col("ytd").median() * 100).alias("med%"),
                              (pl.col("ytd").mean() * 100).alias("mean%"),
                              ((pl.col("ytd") > 0).mean() * 100).alias("pos%")).sort("mgrp"))

# 指数 vs 等权：2026 年内累计
idx = pl.read_parquet(ROOT / "data" / "backfill_20261117" / "index_daily_full.parquet")
keep = {"000001.SH": "sse", "000300.SH": "hs300", "000905.SH": "csi500",
        "000852.SH": "csi1000", "399006.SZ": "chinext", "932000.CSI": "csi2000"}
ix = (idx.filter(pl.col("symbol").is_in(list(keep)))
      .with_columns(pl.col("symbol").replace_strict(keep).alias("k"))
      .pivot(values="close", index="date", on="k").sort("date")
      .filter(pl.col("date") >= d0))
base = ix.row(0, named=True)
print("\n=== 2026 年内指数 vs 等权（%）===")
ew_cum = df.filter(pl.col("date") >= d0).group_by("date").agg(pl.col("ret_x").mean().alias("ew")).sort("date")
ew_cum = ew_cum.with_columns((pl.col("ew").fill_null(0) + 1).cum_prod().alias("ew_idx"))
tot_ew = (ew_cum["ew_idx"][-1] - 1) * 100
print(f"等权（剔除次新）: {tot_ew:.2f}%")
for c in ["sse", "hs300", "csi500", "csi1000", "chinext", "csi2000"]:
    if c in ix.columns:
        last = ix[c].drop_nulls()[-1]
        print(f"{c:>8}: {(last/base[c]-1)*100:>8.2f}%   (末值 {last:.2f})")
