"""第一批：横截面结构指标（2016-2026 日频 + 年度聚合）。"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_diag_regime_2026"

df = pl.read_parquet(OUT / "daily_panel.parquet").sort(["symbol", "date"])

# 前复权日收益（日历相邻）
df = df.with_columns(
    pl.col("close").shift(1).over("symbol").alias("prev_close"),
    pl.col("date").shift(1).over("symbol").alias("prev_date"),
)
df = df.with_columns((pl.col("close") / pl.col("prev_close") - 1).alias("ret"))
# 剔除跨停牌
df = df.with_columns(
    (pl.col("date") - pl.col("prev_date")).dt.total_days().alias("gap_days")
)
df = df.with_columns(
    pl.when(pl.col("gap_days") <= 7).then(pl.col("ret")).otherwise(None).alias("ret")
)

# 当日是否触及涨停/跌停（用前复权 close 判定 9.8% 以上）
df = df.with_columns(
    (pl.col("ret") >= 0.098).alias("up_limit_like"),
    (pl.col("ret") <= -0.098).alias("dn_limit_like"),
    (pl.col("turnover_rate")).alias("to"),
)

daily = (
    df.group_by("date")
    .agg(
        pl.len().alias("n"),
        pl.col("ret").mean().alias("ew_ret"),
        pl.col("ret").median().alias("med_ret"),
        pl.col("ret").std().alias("xs_std"),
        pl.col("ret").skew().alias("xs_skew"),
        pl.col("ret").quantile(0.10).alias("p10"),
        pl.col("ret").quantile(0.90).alias("p90"),
        pl.col("up_limit_like").mean().alias("up_share"),
        pl.col("dn_limit_like").mean().alias("dn_share"),
        (pl.col("ret") > 0).mean().alias("breadth"),
        pl.col("amount").sum().alias("total_amount"),
        pl.col("to").median().alias("med_turnover"),
        pl.col("to").quantile(0.90).alias("p90_turnover"),
        pl.col("consecutive_limit_ups").max().alias("max_streak"),
        (pl.col("consecutive_limit_ups") >= 2).sum().alias("n_streak2"),
    )
    .sort("date")
)

# 成交额集中度：每日前 10% 成交额占全市场比重 + HHI
conc = (
    df.filter(pl.col("amount") > 0)
    .with_columns(
        pl.col("amount").rank(descending=True).alias("rk"),
        pl.len().over("date").alias("nn"),
    )
    .with_columns((pl.col("rk") <= pl.col("nn") * 0.1).alias("top10"))
    .group_by("date")
    .agg(
        (pl.col("amount").filter(pl.col("top10")).sum() / pl.col("amount").sum()).alias("cr10_amt"),
        ((pl.col("amount") / pl.col("amount").sum()) ** 2).sum().alias("hhi_amt"),
        pl.col("amount").median().alias("med_amount"),
    )
)

daily = daily.join(conc, on="date", how="left")

# 指数
idx = pl.read_parquet(ROOT / "data" / "backfill_20261117" / "index_daily_full.parquet")
keep = {
    "000001.SH": "sse",
    "399001.SZ": "szse",
    "399006.SZ": "chinext",
    "000300.SH": "hs300",
    "000905.SH": "csi500",
    "000852.SH": "csi1000",
    "932000.CSI": "csi2000",
    "399303.SZ": "guozheng2000",
}
idx = idx.filter(pl.col("symbol").is_in(list(keep))).with_columns(
    pl.col("symbol").replace_strict(keep).alias("k")
)
idxw = idx.pivot(values="close", index="date", on="k").sort("date")
daily = daily.join(idxw, on="date", how="left")

daily.write_parquet(OUT / "daily_metrics.parquet")

# 年度聚合
yearly = (
    daily.with_columns(pl.col("date").dt.year().alias("y"))
    .group_by("y")
    .agg(
        pl.len().alias("days"),
        pl.col("ew_ret").sum().alias("ew_cum"),
        pl.col("xs_std").mean().alias("xs_std_mean"),
        pl.col("xs_skew").mean().alias("xs_skew_mean"),
        pl.col("breadth").mean().alias("breadth_mean"),
        pl.col("up_share").mean().alias("up_share_mean"),
        pl.col("dn_share").mean().alias("dn_share_mean"),
        pl.col("cr10_amt").mean().alias("cr10_mean"),
        pl.col("hhi_amt").mean().alias("hhi_mean"),
        pl.col("med_turnover").mean().alias("med_turnover_mean"),
        pl.col("total_amount").mean().alias("amt_mean_yi"),
        pl.col("max_streak").max().alias("max_streak"),
        pl.col("n_streak2").mean().alias("n_streak2_mean"),
    )
    .sort("y")
)

# 指数年度收益
iy = (
    daily.with_columns(pl.col("date").dt.year().alias("y"))
    .group_by("y")
    .agg([pl.col(c).drop_nulls().last().alias(c) for c in ["sse", "hs300", "csi500", "csi1000", "chinext"]])
    .sort("y")
)
# 用年初/年末 close 算收益
for c in ["sse", "hs300", "csi500", "csi1000", "chinext"]:
    pass

print("=== 年度横截面结构 ===")
with pl.Config(tbl_rows=30, tbl_cols=30, fmt_str_lengths=20, float_precision=4):
    print(yearly)

print("\n=== 指数年末点位 ===")
with pl.Config(tbl_rows=30, float_precision=2):
    print(iy)
