"""预计算席位声誉表 —— 每个营业部截至最新交易日的 rep20/rep10。

rep20 = 该席位过去 20 次「上榜买入」的平均未来 3 日收益（rolling_mean+shift(1)，
严格无前视）。这是接入触点用的离线表，供 citizenship 运行时查询。

输出：data/dragon_tiger_seats/seat_rep.parquet
  dept_code, dept_name, rep20, rep10, n（买入次数）, last_date
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"

pl.Config.set_tbl_rows(30)
pl.Config.set_tbl_width_chars(200)
pl.Config.set_tbl_cols(30)


def main() -> None:
    seats = pl.read_parquet(OUT / "youzi_seats_all.parquet")
    buy = seats.filter((pl.col("side") == "buy") & (pl.col("buy") > 0)).select([
        "symbol", "date", "dept_code", "dept_name", "buy",
    ]).with_columns(pl.col("date").str.to_date())

    # join 日线 close，算 fwd3
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2025-10"]
    kl = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close"])
        .collect().sort(["symbol", "date"])
        .with_columns(pl.col("close").shift(-3).over("symbol").alias("c3"))
    )
    buy = buy.join(kl.select(["symbol", "date", "close", "c3"]), on=["symbol", "date"], how="left")
    buy = buy.with_columns((pl.col("c3") / pl.col("close") - 1).alias("fwd3")).drop_nulls(["fwd3"])

    # 席位级声誉：按 dept 时间序，滚动平均（不含自身）
    buy = buy.sort(["dept_code", "date"]).with_columns([
        pl.col("fwd3").rolling_mean(20).over("dept_code").shift(1).alias("rep20"),
        pl.col("fwd3").rolling_mean(10).over("dept_code").shift(1).alias("rep10"),
    ])

    # 每个席位取最新一条（截至最后交易日的声誉）
    rep = buy.group_by("dept_code").agg([
        pl.col("dept_name").last().alias("dept_name"),
        pl.col("rep20").last().alias("rep20"),
        pl.col("rep10").last().alias("rep10"),
        pl.len().alias("n"),
        pl.col("date").last().alias("last_date"),
    ]).sort("rep20", descending=True, nulls_last=True)

    print(f"席位声誉表 {rep.height} 个营业部", flush=True)
    print("rep20 分布：")
    print(rep.select([pl.col("rep20").mean().alias("mean"),
                      pl.col("rep20").median().alias("med"),
                      pl.col("rep20").quantile(0.75).alias("p75"),
                      pl.col("rep20").quantile(0.9).alias("p90")]))
    print("\nTop 15 高声誉席位：")
    print(rep.head(15).select(["dept_name", "rep20", "rep10", "n"]))

    rep.write_parquet(ROOT / "data/dragon_tiger_seats/seat_rep.parquet")
    print(f"\n写出 data/dragon_tiger_seats/seat_rep.parquet")


if __name__ == "__main__":
    main()
