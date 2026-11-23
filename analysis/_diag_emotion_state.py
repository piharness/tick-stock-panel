"""验证文稿的「状态依赖」核心论断 —— 四维情绪温度计调节打板次日溢价。

文稿第7幕：完全相同的打板信号，主升期(S1) +2.4%，退潮期(S3炸板率48%) −4.8%。
用本地数据可算的两维做状态分档：
  炸板率    = 当日触及涨停但收盘未封住 / (触及涨停总数)
  连板晋级率 = 昨日涨停、今日继续涨停的比例
验证：低炸板率日打板，次日高开溢价是否显著高于高炸板率日。
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)


def main() -> None:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:4] >= "2022"]
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "open", "high", "low", "close",
                 "consecutive_limit_ups", "consecutive_limit_downs"])
        .collect().sort(["symbol", "date"])
    )
    print(f"面板 {df.height:,} 行  {df['date'].min()} ~ {df['date'].max()}", flush=True)

    df = df.with_columns([
        pl.col("close").shift(1).over("symbol").alias("prev_close"),
        pl.col("open").shift(-1).over("symbol").alias("next_open"),
        pl.col("consecutive_limit_ups").shift(-1).over("symbol").alias("next_lu"),
    ])
    # 涨停价（按板块）
    board = (
        pl.when(pl.col("symbol").str.contains("^(300|301|688)"))
        .then(0.20).otherwise(0.10)
    )
    df = df.with_columns(
        (pl.col("prev_close") * (1.0 + board)).round(2).alias("limit_price")
    )
    # 触及涨停 = high >= limit_price；封板 = 收盘涨停
    df = df.with_columns([
        (pl.col("high") >= pl.col("limit_price")).alias("touch_limit"),
        (pl.col("consecutive_limit_ups") >= 1).alias("sealed"),
    ])

    # 逐日市场情绪
    daily = df.filter(pl.col("prev_close").is_not_null()).group_by("date").agg([
        (pl.col("touch_limit") & ~pl.col("sealed")).sum().alias("n_zhaban"),
        pl.col("sealed").sum().alias("n_sealed"),
        # 跌停家数（收盘跌停）
        (pl.col("consecutive_limit_downs") >= 1).sum().alias("n_limitdown"),
    ]).with_columns([
        (pl.col("n_zhaban") / (pl.col("n_zhaban") + pl.col("n_sealed"))).alias("zhaban_rate"),
    ])
    # 连板晋级率：昨日涨停，今日是否继续涨停
    df = df.with_columns(pl.col("consecutive_limit_ups").shift(1).over("symbol").alias("prev_lu"))
    upgrade = df.filter(pl.col("prev_lu") >= 1).group_by("date").agg([
        pl.len().alias("n_yesterday_lu"),
        (pl.col("consecutive_limit_ups") >= 1).sum().alias("n_upgrade"),
    ]).with_columns((pl.col("n_upgrade") / pl.col("n_yesterday_lu")).alias("upgrade_rate"))
    daily = daily.join(upgrade, on="date", how="left")

    # 打板事件 = 收盘涨停，次日高开溢价
    events = df.filter(pl.col("sealed")).select([
        "symbol", "date", "close", "next_open", "next_lu",
    ]).with_columns((pl.col("next_open") / pl.col("close") - 1).alias("r0")).drop_nulls(["r0"])

    events = events.join(daily.select(["date", "zhaban_rate", "upgrade_rate", "n_limitdown"]),
                         on="date", how="left")

    # 按炸板率分档（三档）
    events = events.with_columns(
        pl.col("zhaban_rate").qcut(3, labels=["低炸板", "中", "高炸板"]).alias("state")
    )
    print("\n=== 炸板率状态 → 打板次日高开溢价 ===")
    g = events.group_by("state").agg([
        pl.len().alias("n"),
        pl.col("zhaban_rate").mean().alias("炸板率均值"),
        (pl.col("r0").mean() * 1e4).alias("r0_bp"),
        (pl.col("next_lu").mean() * 1e4).alias("次日涨停率bp"),
    ]).sort("r0_bp", descending=True)
    print(g)

    # 按连板晋级率分档
    events = events.with_columns(
        pl.col("upgrade_rate").qcut(3, labels=["低晋级", "中", "高晋级"]).alias("state2")
    )
    print("\n=== 连板晋级率状态 → 打板次日高开溢价 ===")
    g2 = events.group_by("state2").agg([
        pl.len().alias("n"),
        pl.col("upgrade_rate").mean().alias("晋级率均值"),
        (pl.col("r0").mean() * 1e4).alias("r0_bp"),
        (pl.col("next_lu").mean() * 1e4).alias("次日涨停率bp"),
    ]).sort("r0_bp", descending=True)
    print(g2)

    daily.write_parquet(OUT / "youzi_emotion.parquet")
    events.write_parquet(OUT / "youzi_emotion_events.parquet")
    print(f"\n写出 {OUT / 'youzi_emotion.parquet'}")


if __name__ == "__main__":
    main()
