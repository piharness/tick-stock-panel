"""2026 年 A 股结构性变化诊断：区分「跌」与「市场生成过程改变」。

判据设计（关键）：
  一个参数算「不可逆变化」必须同时满足
  1) 2026 年值越出 2016-2025 全部年度区间（越界而非分位偏移）
  2) 2026 年内持续性 >= 6 个月同向（非单月尖峰）
  3) 同等跌幅年份（2018 / 2022）未越界 —— 即不是「熊市」的机械后果
"""
from __future__ import annotations

import os
import json
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / "_diag_regime_2026"
OUT.mkdir(exist_ok=True, parents=True)

START_YEAR = 2016


def partition_files(start: str = f"{START_YEAR}-01-01") -> list[str]:
    files = []
    for e in os.scandir(DATA):
        if not e.is_dir():
            continue
        name = e.name
        if not name.startswith("date="):
            continue
        d = name[5:]
        if d >= start:
            files.append(str(Path(e.path) / "part.parquet"))
    files.sort()
    return files


def load() -> pl.DataFrame:
    files = partition_files()
    print(f"[load] {len(files)} partitions")
    df = pl.read_parquet(
        files,
        columns=[
            "symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "raw_close", "turnover_rate", "consecutive_limit_ups", "consecutive_limit_downs",
        ],
    )
    df = df.filter(pl.col("volume") > 0)
    print(f"[load] rows={df.height}")
    return df


def main() -> None:
    df = load()
    df.write_parquet(OUT / "daily_panel.parquet")
    print(df.height, df["date"].min(), df["date"].max())


if __name__ == "__main__":
    main()
