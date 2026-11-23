"""构建个股日线研究面板（一次性缓存），供 `_diag_stock_dip_repair.py` 复用。

口径与 `docs/structural-edge/industry-etf-asymmetry.md` 对齐：
  - 前向收益：T 日收盘可知信号 → **T+1 收盘进场** → T+h 收盘卖（与 ETF 事件研究同口径）
  - 位置：距过去 250 日收盘高点（min_periods=200）的回撤 `dd52`
  - 市值：`amount / (turnover_rate/100)` 的 20 日中位（无未来信息，不用静态股本快照）
  - 波动分位：**当日横截面**分位（与 ETF 的「20 个行业里排第几」同构）

不写进 `data/*.parquet` 通配目录：只落 `data/.scratch/`。
"""

from __future__ import annotations

import glob
import sys
import time
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/.scratch/stock_dip_panel.parquet"
START_YEAR = 2012  # 2012 起，留 250 日预热 → 有效样本从 2013 开始


def build() -> pl.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:4] >= str(START_YEAR)]
    df = (
        pl.scan_parquet(files)
        .select([
            "symbol", "date", "close", "open", "high", "low",
            "amount", "turnover_rate",
            "consecutive_limit_ups", "consecutive_limit_downs",
        ])
        .collect()
        .sort(["symbol", "date"])
    )

    c = pl.col("close")
    eps = 1e-12
    df = df.with_columns([
        (c / c.shift(1).over("symbol") - 1).alias("ret1"),
        (c / c.shift(5).over("symbol") - 1).alias("ret5"),
        (c / c.shift(20).over("symbol") - 1).alias("ret20"),
        (c / c.shift(60).over("symbol") - 1).alias("ret60"),
        c.rolling_max(250, min_periods=200).over("symbol").alias("hi250"),
        pl.col("amount").rolling_mean(20, min_samples=10).over("symbol").alias("amt20"),
    ])
    df = df.with_columns([
        (c / pl.col("hi250") - 1).alias("dd52"),
        (pl.col("amount") / (pl.col("amt20") + eps)).alias("amt_ratio"),
        # 当日流通市值（元）：amount / 换手率；换手率是百分数
        (pl.col("amount") / (pl.col("turnover_rate") / 100 + eps)).alias("fcap"),
    ])
    df = df.with_columns([
        pl.col("fcap").rolling_median(20, min_samples=10).over("symbol").alias("fcap20"),
        (pl.col("ret1").rolling_std(20, min_samples=15).over("symbol")).alias("vol20"),
        # 前向收益：T+1 收盘买，T+1+h 收盘卖
        (c.shift(-6).over("symbol") / c.shift(-1).over("symbol") - 1).alias("fwd5"),
        (c.shift(-21).over("symbol") / c.shift(-1).over("symbol") - 1).alias("fwd20"),
        (c.shift(-11).over("symbol") / c.shift(-1).over("symbol") - 1).alias("fwd10"),
    ])

    # 连续下跌天数：上一次 ret1>=0 距今多少个交易日
    df = df.with_columns(
        pl.when(pl.col("ret1") >= 0)
        .then(pl.int_range(pl.len()).over("symbol"))
        .otherwise(None)
        .alias("_up_idx")
    )
    df = df.with_columns(
        (pl.int_range(pl.len()).over("symbol") - pl.col("_up_idx").forward_fill().over("symbol"))
        .alias("consec_down")
    ).drop("_up_idx")

    # 连续上涨天数（"主力还在"的反面参照）
    df = df.with_columns(
        pl.when(pl.col("ret1") <= 0)
        .then(pl.int_range(pl.len()).over("symbol"))
        .otherwise(None)
        .alias("_dn_idx")
    )
    df = df.with_columns(
        (pl.int_range(pl.len()).over("symbol") - pl.col("_dn_idx").forward_fill().over("symbol"))
        .alias("consec_up")
    ).drop("_dn_idx")

    # 横截面分位（当日全市场）
    for src, dst in [("vol20", "p_vol"), ("fcap20", "p_fcap"), ("amt20", "p_amt")]:
        df = df.with_columns(
            (pl.col(src).rank("average").over("date") / pl.len().over("date")).alias(dst)
        )

    # 同日全市场等权前向收益（超额的分母）
    for h in (5, 10, 20):
        mkt = df.group_by("date").agg(pl.col(f"fwd{h}").mean().alias(f"mkt_fwd{h}"))
        df = df.join(mkt, on="date", how="left").with_columns(
            (pl.col(f"fwd{h}") - pl.col(f"mkt_fwd{h}")).alias(f"exc{h}")
        )

    return df


def main() -> None:
    t = time.time()
    df = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUT)
    print(f"wrote {OUT}  shape={df.shape}  {time.time()-t:.1f}s  {OUT.stat().st_size/1e6:.0f}MB")
    print(df.select(["symbol", "date", "close", "dd52", "consec_down", "vol20",
                     "fwd20", "exc20", "p_vol", "p_fcap"]).tail(3))
    print("date range", df["date"].min(), df["date"].max(), "symbols", df["symbol"].n_unique())


if __name__ == "__main__":
    sys.exit(main())
