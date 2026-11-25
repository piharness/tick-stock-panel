"""分钟 symbol 级原子落盘: 中断/窗口变化不留半截残渣。

背景 (2026-11-25): 反复中断重跑时, 旧实现 merge-upsert(concat+unique keep=last)
把不同窗口/半截数据混进同一 symbol, 序列残留 gap, 审计 2,893 只 bars≠240。
覆盖语义把 touched symbol 的权威数据整体交给本次写入, 未触及行原样保留。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl

from app.services import kline_sync

_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}


def _df(symbol: str, times: list[datetime]) -> pl.DataFrame:
    n = len(times)
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "datetime": times,
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [1.0] * n,
            "volume": [100.0] * n,
            "amount": [None] * n,
        },
        schema=_SCHEMA,
    )


def test_overwrite_replaces_touched_symbol_rows(tmp_path):
    day = [
        datetime(2026, 11, 25, 9, 31),
        datetime(2026, 11, 25, 9, 32),
        datetime(2026, 11, 25, 9, 33),
    ]
    # 第一次写: A 的 3 根 + B 的 1 根
    kline_sync._write_minute_partition(
        pl.concat([_df("A.SH", day), _df("B.SH", day[:1])]), tmp_path, origin="test",
    )
    # 第二次写: A 只触及 1 根 (模拟窗口变短/重拉), B 不触及
    kline_sync._write_minute_partition(_df("A.SH", day[:1]), tmp_path, origin="test")

    out = pl.read_parquet(tmp_path / "date=2026-11-25" / "part.parquet")
    a = out.filter(pl.col("symbol") == "A.SH")
    b = out.filter(pl.col("symbol") == "B.SH")
    # A 被整体覆盖成 1 根, 旧的 3 根消失 (旧实现会保留 9:32/9:33 两行残渣)
    assert a.height == 1
    # B 未触及, 原样保留
    assert b.height == 1


def test_overwrite_keeps_untouched_symbols_intact(tmp_path):
    day = [datetime(2026, 11, 25, 9, 31), datetime(2026, 11, 25, 9, 32)]
    kline_sync._write_minute_partition(_df("A.SH", day), tmp_path, origin="test")
    kline_sync._write_minute_partition(_df("B.SH", day), tmp_path, origin="test")

    out = pl.read_parquet(tmp_path / "date=2026-11-25" / "part.parquet")
    assert set(out["symbol"].to_list()) == {"A.SH", "B.SH"}
    assert out.height == 4


def test_overwrite_is_per_trade_date(tmp_path):
    d25 = [datetime(2026, 11, 25, 9, 31)]
    d24 = [datetime(2026, 11, 24, 9, 31)]
    kline_sync._write_minute_partition(_df("A.SH", d24 + d25), tmp_path, origin="test")
    # 只重写 11-25, 11-24 不受影响
    kline_sync._write_minute_partition(_df("A.SH", d25), tmp_path, origin="test")

    p24 = pl.read_parquet(tmp_path / "date=2026-11-24" / "part.parquet")
    p25 = pl.read_parquet(tmp_path / "date=2026-11-25" / "part.parquet")
    assert p24.height == 1 and p25.height == 1
