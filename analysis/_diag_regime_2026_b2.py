"""第二批：月度演化 + 集中度修复 + 指数/等权背离 + 相关性结构。"""
from __future__ import annotations

from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_diag_regime_2026"
df = pl.read_parquet(OUT / "daily_panel.parquet").sort(["symbol", "date"])

df = df.with_columns(
    pl.col("close").shift(1).over("symbol").alias("prev_close"),
    pl.col("date").shift(1).over("symbol").alias("prev_date"),
).with_columns(
    ((pl.col("date") - pl.col("prev_date")).dt.total_days() <= 7).alias("adj")
).with_columns(
    pl.when(pl.col("adj")).then(pl.col("close") / pl.col("prev_close") - 1).otherwise(None).alias("ret")
)

# 成交额集中度（修复：用 when/then 而非 filter）
df = df.with_columns(
    pl.col("amount").rank(descending=True).over("date").alias("rk"),
    pl.len().over("date").alias("nn"),
).with_columns(
    (pl.col("rk") <= (pl.col("nn") * 0.1).ceil()).alias("top10"),
    (pl.col("rk") <= (pl.col("nn") * 0.05).ceil()).alias("top5"),
)

daily = (
    df.group_by("date")
    .agg(
        pl.len().alias("n"),
        pl.col("ret").mean().alias("ew_ret"),
        pl.col("ret").median().alias("med_ret"),
        pl.col("ret").std().alias("xs_std"),
        (pl.col("ret") > 0).mean().alias("breadth"),
        pl.col("amount").sum().alias("amt"),
        pl.col("med_amount_formula").first().alias("_x") if False else pl.col("amount").median().alias("med_amt"),
        pl.col("turnover_rate").median().alias("med_to"),
        pl.col("turnover_rate").quantile(0.9).alias("p90_to"),
        # 涨跌停结构
        (pl.col("ret") >= 0.098).mean().alias("up_like"),
        (pl.col("ret") <= -0.098).mean().alias("dn_like"),
        pl.col("consecutive_limit_ups").max().alias("max_streak"),
        (pl.col("consecutive_limit_ups") >= 3).sum().alias("n_ge3"),
        (pl.col("consecutive_limit_ups") >= 5).sum().alias("n_ge5"),
        # 成交额占比
        (pl.col("amount").filter(pl.col("top10")).sum() / pl.col("amount").sum()).alias("cr10"),
        (pl.col("amount").filter(pl.col("top5")).sum() / pl.col("amount").sum()).alias("cr5"),
        ((pl.col("amount") / pl.col("amount").sum()) ** 2).sum().alias("hhi"),
    )
    .sort("date")
)

# 极端个股占比（尾部厚度）
tail = (
    df.group_by("date")
    .agg(
        (pl.col("ret") <= -0.05).mean().alias("p_le5"),
        (pl.col("ret") <= -0.09).mean().alias("p_le9"),
        (pl.col("ret") >= 0.05).mean().alias("p_ge5"),
        (pl.col("ret") >= 0.09).mean().alias("p_ge9"),
    )
)
daily = daily.join(tail, on="date", how="left")

# 指数
idx = pl.read_parquet(ROOT / "data" / "backfill_20261117" / "index_daily_full.parquet")
keep = {"000001.SH": "sse", "000300.SH": "hs300", "000905.SH": "csi500",
        "000852.SH": "csi1000", "399006.SZ": "chinext", "932000.CSI": "csi2000",
        "399303.SZ": "gz2000", "000016.SH": "sse50"}
idxw = (idx.filter(pl.col("symbol").is_in(list(keep)))
        .with_columns(pl.col("symbol").replace_strict(keep).alias("k"))
        .pivot(values="close", index="date", on="k").sort("date"))
daily = daily.join(idxw, on="date", how="left")
daily.write_parquet(OUT / "daily_metrics.parquet")

# 月度
mon = (
    daily.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m"))
    .group_by("m")
    .agg(
        pl.len().alias("d"),
        pl.col("ew_ret").sum().alias("ew_m"),
        pl.col("med_ret").sum().alias("med_m"),
        pl.col("xs_std").mean().alias("xs_std"),
        pl.col("breadth").mean().alias("breadth"),
        (pl.col("amt").mean() / 1e8).alias("amt_yi"),
        pl.col("med_to").mean().alias("med_to"),
        pl.col("cr10").mean().alias("cr10"),
        pl.col("p_le5").mean().alias("p5"),
        pl.col("p_ge5").mean().alias("g5"),
        pl.col("up_like").mean().alias("up_like"),
        pl.col("dn_like").mean().alias("dn_like"),
        pl.col("max_streak").max().alias("streak"),
        pl.col("sse").last().alias("sse"),
        pl.col("hs300").last().alias("hs300"),
        pl.col("csi1000").last().alias("csi1000"),
        pl.col("chinext").last().alias("chinext"),
    )
    .sort("m")
)
print("=== 月度（2024-01 起）===")
for r in mon.filter(pl.col("m") >= "2024-01").to_dicts():
    print(f"{r['m']} d={r['d']:>2} ew={r['ew_m']*100:>7.2f}% std={r['xs_std']*100:.2f} "
          f"brd={r['breadth']*100:.1f} amt={r['amt_yi']:>7.0f}亿 to={r['med_to']:.2f} "
          f"cr10={r['cr10']*100:.1f}% p5={r['p5']*100:.1f} g5={r['g5']*100:.1f} "
          f"UL={r['up_like']*100:.2f} DL={r['dn_like']*100:.2f} stk={r['streak']} "
          f"| sse={r['sse']:.0f} hs300={r['hs300']:.0f} c1000={r['csi1000']:.0f} cyb={r['chinext']:.0f}")

print("\n=== 年度（全样本）===")
yr = (
    daily.with_columns(pl.col("date").dt.year().alias("y")).group_by("y")
    .agg(pl.len().alias("d"), pl.col("ew_ret").sum().alias("ew"),
         pl.col("xs_std").mean().alias("std"), pl.col("breadth").mean().alias("brd"),
         (pl.col("amt").mean() / 1e8).alias("amt"), pl.col("med_to").mean().alias("to"),
         pl.col("cr10").mean().alias("cr10"), pl.col("p_le5").mean().alias("p5"),
         pl.col("p_ge5").mean().alias("g5"), pl.col("up_like").mean().alias("ul"),
         pl.col("dn_like").mean().alias("dl"), pl.col("n_ge3").mean().alias("ge3"))
    .sort("y")
)
for r in yr.to_dicts():
    print(f"{r['y']} ew={r['ew']*100:>7.2f}% std={r['std']*100:.2f} brd={r['brd']*100:.1f} "
          f"amt={r['amt']:>7.0f} to={r['to']:.2f} cr10={r['cr10']*100:.1f}% "
          f"p5={r['p5']*100:.2f} g5={r['g5']*100:.2f} UL={r['ul']*100:.2f} DL={r['dl']*100:.2f} n3={r['ge3']:.1f}")
