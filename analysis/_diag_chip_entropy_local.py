"""本地 5min 段（2024-11-07 ~ 2026-11-20）的日内成本带宽度 —— 用于样本外。

与 `_diag_chip_entropy.py` 同一套定义，但数据布局按日分区，故按日聚合。
两段之间没有重叠日（minute_union.SPLIT = 2024-11-07），可直接与 bs 段结果拼成
样本内 / 样本外，不需要去重。

用法:
  python analysis/_diag_chip_entropy_local.py
输出:
  data/.scratch/chip_entropy_local/part.parquet
"""
from __future__ import annotations

import os
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "data" / "kline_5min"
OUT_DIR = ROOT / "data" / ".scratch" / "chip_entropy_local"
START, END = "2024-11-07", "2026-11-20"


def one_day(f: Path) -> pl.DataFrame:
    d = pl.read_parquet(f, columns=["symbol", "datetime", "high", "low", "volume",
                                    "amount"])
    d = d.filter(
        (pl.col("volume") > 0) & (pl.col("amount") > 0)
        & (pl.col("high") >= pl.col("low")) & (pl.col("high") > 0)
    )
    if d.height == 0:
        return pl.DataFrame()
    d = d.with_columns(
        pl.col("datetime").dt.hour().alias("hh"),
        pl.col("datetime").dt.minute().alias("mm"),
        (pl.col("amount") / (pl.col("volume") * 100.0)).alias("bp"),
        (pl.col("high") - pl.col("low")).alias("rng"),
    ).with_columns(
        pl.col("bp").clip(pl.col("low"), pl.col("high")).alias("bp")
    ).with_columns(
        (pl.col("volume") / pl.col("volume").sum().over("symbol")).alias("w"),
        ((pl.col("hh") < 10) | ((pl.col("hh") == 10) & (pl.col("mm") == 0))).alias("is_open"),
        (pl.col("hh") == 14).alias("is_prevclose"),
    )
    g = d.group_by("symbol").agg(
        pl.col("volume").sum().alias("vol"),
        pl.col("amount").sum().alias("amt"),
        (pl.col("volume") * pl.col("bp")).sum().alias("sv"),
        (pl.col("volume") * pl.col("bp") ** 2).sum().alias("sv2"),
        (pl.col("volume") * pl.col("rng") ** 2).sum().alias("sr2"),
        pl.len().alias("nbar"),
        (-(pl.col("w") * pl.col("w").log())).sum().alias("h_raw"),
        pl.col("volume").filter(pl.col("is_open")).sum().alias("v_open"),
        pl.col("volume").filter(pl.col("is_prevclose")).sum().alias("v_close"),
    ).filter(pl.col("nbar") >= 40).with_columns(
        (pl.col("amt") / (pl.col("vol") * 100.0)).alias("vwap"),
        pl.lit(f.parent.name[5:]).alias("date"),
    ).with_columns(
        (pl.col("sv") / pl.col("vol")).alias("m1"),
    ).with_columns(
        ((pl.col("sv2") / pl.col("vol") - pl.col("m1") ** 2).clip(0.0)).alias("var_b"),
        ((pl.col("sr2") / pl.col("vol")).clip(0.0)).alias("var_w"),
    ).with_columns(
        (pl.col("var_b").sqrt() / pl.col("vwap")).alias("ivd_lo"),
        ((pl.col("var_b") + pl.col("var_w")).sqrt() / pl.col("vwap")).alias("ivd_hi"),
        (pl.col("h_raw") / pl.col("nbar").log()).alias("h_time"),
        (pl.col("v_open") / pl.col("vol")).alias("tc_open"),
        (pl.col("v_close") / pl.col("vol")).alias("tc_close"),
    ).with_columns(pl.col("date").str.to_date())
    return g.select(["symbol", "date", "vwap", "ivd_lo", "ivd_hi", "h_time",
                     "tc_open", "tc_close", "nbar", "vol", "amt"])


def main() -> None:
    days = sorted(p[5:] for p in os.listdir(LOCAL) if p.startswith("date="))
    days = [d for d in days if START <= d <= END]
    print(f"本地段 {len(days)} 天：{days[0]} ~ {days[-1]}")
    out = []
    for i, d in enumerate(days):
        t = one_day(LOCAL / f"date={d}" / "part.parquet")
        if not t.is_empty():
            out.append(t)
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(days)}")
    df = pl.concat(out).sort(["symbol", "date"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUT_DIR / "part.parquet")
    print(f"写出 {OUT_DIR/'part.parquet'} rows={df.height:,} "
          f"symbols={df['symbol'].n_unique()} dates={df['date'].n_unique()}")
    print(df.select(["ivd_lo", "ivd_hi", "h_time", "tc_open", "tc_close"]).describe())


if __name__ == "__main__":
    main()
