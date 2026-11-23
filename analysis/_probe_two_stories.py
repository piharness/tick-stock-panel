"""探针 3：看两只票的「故事」——000587 *ST金洲 与 600340 *ST华幸。

目的不是下结论，而是确认本地数据是否足以把「故事」还原成可对照的路径：
戴帽区间、区间内涨跌、区间长度、结局。
"""
from __future__ import annotations

import glob
from datetime import date

import polars as pl

QFQ = "data/archive/kline_daily_qfq"


def load_all(symbol: str) -> pl.DataFrame:
    parts = []
    for p in sorted(glob.glob(f"{QFQ}/month=*/*.parquet")):
        sub = pl.read_parquet(p)
        sub = sub.filter(pl.col("symbol") == symbol)
        if not sub.is_empty():
            parts.append(sub)
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="diagonal_relaxed").sort("date").unique(subset=["date"], keep="last")


def profile(symbol: str, label: str) -> None:
    d = load_all(symbol)
    if d.is_empty():
        print(f"{label} {symbol}: 无数据")
        return
    d = d.with_columns((pl.col("close") / pl.col("close").shift(1) - 1).alias("r"))
    d = d.with_columns(pl.col("r").abs().alias("ar"))
    m = (
        d.group_by(pl.col("date").dt.strftime("%Y-%m"))
        .agg([pl.len().alias("n"), pl.col("ar").max().alias("armax"), pl.col("close").last().alias("close")])
        .sort("date")
    )
    # 5% 指纹月：armax <= 0.055 且 armax >= 0.045（存在 5% 级封板且无 10% 级波动）
    m = m.with_columns(
        ((pl.col("armax") <= 0.055) & (pl.col("armax") >= 0.045)).alias("st_like")
    )
    st_months = m.filter(pl.col("st_like"))["date"].to_list()
    print(f"\n=== {label} {symbol} 行数={d.height} {d['date'].min()}~{d['date'].max()} ===")
    print(f"5% 指纹月数={len(st_months)} / {m.height}")
    print("指纹月:", ",".join(st_months))
    # 按年看涨跌
    y = d.group_by(pl.col("date").dt.year().alias("y")).agg(
        [pl.col("close").first().alias("c0"), pl.col("close").last().alias("c1"), pl.len().alias("n")]
    ).sort("y")
    y = y.with_columns((pl.col("c1") / pl.col("c0") - 1).alias("ret")).filter(pl.col("n") > 60)
    print(y.tail(18).select(["y", "c0", "c1", "ret"]).rows())
    print("最近 10 个交易日:", d.tail(10).select(["date", "close", "r"]).rows())


if __name__ == "__main__":
    profile("000587.SZ", "*ST金洲")
    profile("600340.SH", "*ST华幸")
