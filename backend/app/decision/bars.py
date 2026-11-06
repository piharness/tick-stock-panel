"""决策账本用到的行情读取 — 只读、按日分区、不引入第二套数据口径。

口径约束（与 CONTRIBUTING §3.2 一致）：
- 撮合与限价比较用**不复权 raw 价**（真实交易价格）；
- 收益与超额用**复权价**（`kline_daily_enriched.close`），避免除权日假跌幅。

`kline_daily_enriched` 只落 `raw_close / raw_high / raw_low`，没有 `raw_open`。
同一交易日的复权因子对 OHLC 一致，故 `raw_open = open / (close / raw_close)`；
分母缺失（首日、坏行）时返回 None，由调用方判 UNKNOWN，而不是拿复权价冒充原始价。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

STOCK_ENRICHED = "kline_daily_enriched"
INDEX_ENRICHED = "kline_index_enriched"

# 决策账本只碰这几列；表里其它列（换手率、连板数）与本层无关。
_BAR_COLS = ("symbol", "date", "open", "high", "low", "close", "raw_close", "raw_high", "raw_low")


def sessions(data_dir: Path, table: str = "kline_daily") -> list[str]:
    """已落盘的交易日（ISO 字符串，升序）。

    用分区目录名而不是"今天减一天"：节假日、停机、只补了一半的数据都在这里
    如实体现。**没有数据的那天不是交易日**——这是本层对"新鲜度"的唯一判据来源。
    """
    base = Path(data_dir) / table
    if not base.is_dir():
        return []
    out: list[str] = []
    for child in base.iterdir():
        if not child.is_dir() or not child.name.startswith("date="):
            continue
        raw = child.name.split("=", 1)[1]
        try:
            date.fromisoformat(raw)
        except ValueError:
            continue
        out.append(raw)
    return sorted(out)


def latest_session(data_dir: Path, table: str = "kline_daily") -> str | None:
    dates = sessions(data_dir, table)
    return dates[-1] if dates else None


def _day_frame(data_dir: Path, table: str, day: str, symbols: list[str] | None) -> pl.DataFrame:
    part = Path(data_dir) / table / f"date={day}"
    if not part.is_dir():
        return pl.DataFrame()
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    lf = pl.scan_parquet([str(f) for f in files], missing_columns="insert", extra_columns="ignore")
    have = set(lf.collect_schema().names())
    cols = [c for c in _BAR_COLS if c in have]
    lf = lf.select(cols)
    if symbols is not None:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    return lf.collect()


def day_rows(data_dir: Path, day: str, symbols: list[str] | None = None,
             table: str = STOCK_ENRICHED) -> pl.DataFrame:
    """某交易日、若干标的的 bar；缺表/缺分区返回空表（不是异常）。"""
    return _day_frame(Path(data_dir), table, day, symbols)


def bar_map(data_dir: Path, day: str, symbols: list[str] | None = None,
            table: str = STOCK_ENRICHED) -> dict[str, dict]:
    """`{symbol: row}`，供逐标的撮合使用。"""
    df = day_rows(data_dir, day, symbols, table)
    if df.is_empty():
        return {}
    out: dict[str, dict] = {}
    for row in df.iter_rows(named=True):
        out[str(row["symbol"])] = row
    return out


def raw_open(row: dict) -> float | None:
    """从不复权收盘/复权收盘反推不复权开盘；口径不成立时返回 None。"""
    open_ = row.get("open")
    close = row.get("close")
    raw_close = row.get("raw_close")
    try:
        open_f, close_f, raw_close_f = float(open_), float(close), float(raw_close)
    except (TypeError, ValueError):
        return None
    if close_f <= 0 or raw_close_f <= 0 or open_f <= 0:
        return None
    return open_f * raw_close_f / close_f


def adj_factor(row: dict) -> float | None:
    """复权因子 `f = close / raw_close`（当日一致，用于把成交价换算到复权口径）。"""
    try:
        close_f = float(row.get("close"))
        raw_f = float(row.get("raw_close"))
    except (TypeError, ValueError):
        return None
    if close_f <= 0 or raw_f <= 0:
        return None
    return close_f / raw_f


def raw_low(row: dict) -> float | None:
    try:
        return float(row.get("raw_low"))
    except (TypeError, ValueError):
        return None
