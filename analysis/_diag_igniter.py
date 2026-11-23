"""用 1min 线扒点火者 —— 涨停票的「点火 → 封板」路径。

点火者身份匿名（任何公开数据都扒不到「谁」），但行为能扒：1min 线的
「点火时刻」（第一次放量拉升）与「封板时刻」之间的分钟数 = 点火的主动程度。

验证：点火 → 封板 越快（主动点火），次日溢价是否越高（点火质量有 alpha）。
对比：一字板（竞价点火，点火分钟=0）vs 盘中快速点火 vs 慢速点火。

数据：data/research/l1_open_depth（730 只活跃票，1min，316 天，7,168 涨停事件）。
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)


def main() -> None:
    curves = pl.read_parquet(ROOT / "data/research/l1_open_depth/curves.parquet")
    syms = curves["symbol"].unique().to_list()

    # 日线：拿涨停事件 + 前收 + 次日开盘/收盘
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if "2025-06-06" <= f.split("date=")[1][:10] <= "2026-10-12"]
    kl = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "open", "close", "consecutive_limit_ups"])
        .filter(pl.col("symbol").is_in(syms))
        .collect().sort(["symbol", "date"])
    )
    kl = kl.with_columns([
        pl.col("close").shift(1).over("symbol").alias("prev_close"),
        pl.col("open").shift(-1).over("symbol").alias("next_open"),
        pl.col("close").shift(-1).over("symbol").alias("next_close"),
    ])
    lu = kl.filter(pl.col("consecutive_limit_ups") >= 1).select([
        "symbol", "date", "close", "prev_close", "next_open", "next_close",
    ])
    # 次日溢价：开盘溢价（R0）= next_open/close-1，收盘=next_close/close-1
    lu = lu.with_columns([
        (pl.col("next_open") / pl.col("close") - 1).alias("r0"),
        (pl.col("next_close") / pl.col("close") - 1).alias("r1"),
    ]).drop_nulls(["prev_close", "r0"])
    print(f"涨停事件 {lu.height:,} 个", flush=True)

    # 1min：找点火时刻（第一次 close 涨幅>=2%）和封板时刻（第一次 close 达到涨停价）
    c = curves.with_columns(pl.col("date").str.to_date()).sort(["symbol", "date", "k"])
    # join 涨停价（日线 close）+ 前收
    c = c.join(lu.select(["symbol", "date", "close", "prev_close"]), on=["symbol", "date"], how="inner")
    c = c.with_columns([
        (pl.col("close") / pl.col("prev_close") - 1).alias("ret_from_prev"),
        (pl.col("close") >= pl.col("close_right")).alias("is_limit"),  # close_right = 日线涨停价
    ])
    # 点火时刻 = 第一根 ret_from_prev >= 2% 的 bar；封板时刻 = 第一根 is_limit 的 bar
    ignite = c.filter(pl.col("ret_from_prev") >= 0.02).group_by(["symbol", "date"], maintain_order=True).first() \
        .select(["symbol", "date", pl.col("k").alias("k_ignite")])
    seal = c.filter(pl.col("is_limit")).group_by(["symbol", "date"], maintain_order=True).first() \
        .select(["symbol", "date", pl.col("k").alias("k_seal")])
    # 一字板 = 开盘即涨停（k=0 或 1 就 is_limit）
    first_limit = c.filter(pl.col("is_limit")).group_by(["symbol", "date"], maintain_order=True).first() \
        .select(["symbol", "date", pl.col("k").alias("k_first_limit")])

    ev = lu.join(ignite, on=["symbol", "date"], how="left") \
        .join(seal, on=["symbol", "date"], how="left") \
        .join(first_limit, on=["symbol", "date"], how="left")
    ev = ev.with_columns([
        (pl.col("k_seal") - pl.col("k_ignite")).alias("ignite_to_seal"),
        (pl.col("k_first_limit") <= 2).alias("yiziban"),  # 前 2 根 bar 内封板 = 一字/秒板
    ])

    print("\n=== 点火 → 封板 分钟数分布 ===")
    for q in [0.1, 0.25, 0.5, 0.75, 0.9]:
        print(f"  p{int(q*100)} = {ev['ignite_to_seal'].quantile(q):.0f} 分钟")

    # 分档：一字板 / 快速点火(<30min) / 中速(30-120) / 慢速(>120)
    ev = ev.with_columns(
        pl.when(pl.col("yiziban")).then(pl.lit("一字/秒板"))
        .when(pl.col("ignite_to_seal") < 30).then(pl.lit("快速点火<30min"))
        .when(pl.col("ignite_to_seal") < 120).then(pl.lit("中速30-120min"))
        .otherwise(pl.lit("慢速>120min")).alias("ignite_type")
    )
    g = ev.group_by("ignite_type").agg([
        pl.len().alias("n"),
        (pl.col("r0").mean() * 1e4).alias("r0_bp"),
        (pl.col("r1").mean() * 1e4).alias("r1_bp"),
        (pl.col("r1").median() * 1e4).alias("r1_med_bp"),
    ]).sort("r0_bp", descending=True)
    print("\n=== 点火速度 → 次日溢价 ===")
    print(g)

    ev.write_parquet(OUT / "youzi_ignite.parquet")
    print(f"\n写出 {OUT / 'youzi_ignite.parquet'}")


if __name__ == "__main__":
    main()
