"""席位声誉验证 —— 完全无前视的自算版本。

信号：营业部 D 在 T 日之前的历史「上榜买入后 3 日平均收益」，作为 D 的声誉。
      用 rolling_mean + shift(1) 严格只取 T 之前的事件，不含自身、不含未来。

验证：全市场上榜事件，按「当日买入席位中的最高声誉」分档，看未来 3/5 日
      涨幅与涨停率是否单调。若高声誉席位的票未来显著更好，席位声誉是真信号。

不依赖东财 rise_prob_3d（可能前视），完全从抓到的事件流自算。
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
    seats = pl.read_parquet(OUT / "youzi_seats_all.parquet")
    print(f"营业部明细 {seats.height:,} 行", flush=True)

    # 买入事件（side=buy 且 buy>0）
    buy = seats.filter((pl.col("side") == "buy") & (pl.col("buy") > 0)).select([
        "symbol", "date", "dept_name", "buy", "net",
    ]).with_columns(pl.col("date").str.to_date())
    print(f"买入事件 {buy.height:,} 条，营业部 {buy['dept_name'].n_unique():,}", flush=True)

    # join 日线 close，算未来 3/5 日涨幅
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2025-10"]
    kl = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close", "consecutive_limit_ups"])
        .collect().sort(["symbol", "date"])
    )
    kl = kl.with_columns([
        pl.col("close").shift(-3).over("symbol").alias("c3"),
        pl.col("close").shift(-5).over("symbol").alias("c5"),
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(5).over("symbol") >= 1).alias("f_lu5"),
    ])
    buy = buy.join(kl.select(["symbol", "date", "close", "c3", "c5", "f_lu5"]),
                   on=["symbol", "date"], how="left")
    buy = buy.with_columns([
        (pl.col("c3") / pl.col("close") - 1).alias("fwd3"),
        (pl.col("c5") / pl.col("close") - 1).alias("fwd5"),
    ]).drop_nulls(["fwd3"])

    # 席位声誉：按 dept 时间序，过去 20 个买入事件的 fwd3 均值（shift(1) 排除自身）
    buy = buy.sort(["dept_name", "date"]).with_columns(
        pl.col("fwd3").rolling_mean(20).over("dept_name").shift(1).alias("rep20")
    ).with_columns(
        pl.col("fwd3").rolling_mean(10).over("dept_name").shift(1).alias("rep10")
    )

    # 每个上榜事件（symbol,date）取买入席位中最高声誉
    ev = buy.group_by(["symbol", "date"]).agg([
        pl.col("rep20").max().alias("rep20_max"),
        pl.col("rep20").mean().alias("rep20_mean"),
        pl.col("fwd3").first().alias("fwd3"),
        pl.col("fwd5").first().alias("fwd5"),
        pl.col("f_lu5").first().alias("f_lu5"),
        pl.len().alias("n_seats"),
    ])
    ev = ev.with_columns(pl.col("date").cast(pl.Date))
    ev = ev.filter(pl.col("rep20_max").is_not_null())
    print(f"上榜事件（有声誉）{ev.height:,} 个", flush=True)

    # 分档验证
    ev = ev.with_columns(
        pl.col("rep20_max").qcut(4, labels=["Q1低", "Q2", "Q3", "Q4高"]).alias("q")
    )
    base3 = float(ev["fwd3"].mean() * 1e4)
    base5 = float(ev["fwd5"].mean() * 1e4)
    base_lu = float(ev["f_lu5"].mean() * 1e4)
    print(f"\n基准：fwd3={base3:.1f}bp  fwd5={base5:.1f}bp  f_lu5={base_lu:.1f}bp", flush=True)

    g = ev.group_by("q").agg([
        pl.len().alias("n"),
        (pl.col("fwd3").mean() * 1e4).alias("fwd3_bp"),
        (pl.col("fwd5").mean() * 1e4).alias("fwd5_bp"),
        (pl.col("f_lu5").mean() * 1e4).alias("lu5_bp"),
    ]).with_columns(
        pl.struct(["q"]).map_elements(lambda s: ["Q1低", "Q2", "Q3", "Q4高"].index(s["q"]),
                                      return_dtype=pl.Int32).alias("_o")
    ).sort("_o").drop("_o")
    print("\n=== 席位声誉分档 → 未来收益 ===")
    print(g)

    # 单调性：Q4高 是否显著好于 Q1低
    q4 = ev.filter(pl.col("q") == "Q4高")
    q1 = ev.filter(pl.col("q") == "Q1低")
    if q4.height and q1.height:
        import numpy as np
        d3 = float(q4["fwd3"].mean() - q1["fwd3"].mean()) * 1e4
        dlu = float(q4["f_lu5"].mean() - q1["f_lu5"].mean()) * 1e4
        print(f"\nQ4−Q1：fwd3 差 {d3:+.1f}bp，f_lu5 差 {dlu:+.1f}bp")

    ev.write_parquet(OUT / "youzi_seat_rep.parquet")
    print(f"\n写出 {OUT / 'youzi_seat_rep.parquet'}")


if __name__ == "__main__":
    main()
