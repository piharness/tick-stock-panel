"""回归测试: 指数兜底行情只扫尾部日K分区。

回归动机（2026-11-24）：`/api/intraday/indices` 在实时缓存为空时走
`_fallback_index_quotes_from_daily`，对 `kline_index_daily` 视图做
`row_number() OVER (PARTITION BY symbol ORDER BY date DESC)`。视图是
`read_parquet('<dir>/**/*.parquet')`，DuckDB 必须先把 257 个分区文件的 footer
全读一遍才能裁剪 —— 容器内实测 **2.16s/次**，而整张表只有 6.1MB、四只指数。
该端点一周被轮询 3782 次。

判据两面:
  1. **答案不变** —— 尾部窗口与全表窗口必须给出同一组 (symbol, 最近两根收盘)；
  2. **不扫全表** —— 送给 DuckDB 的文件列表只能是尾部分区。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import polars as pl

from app.api.intraday import _index_daily_tail_source


def _write_partitions(data: Path, days: int) -> list[date]:
    """造 `days` 个日分区，每天两只指数。返回写入的日期（升序）。"""
    out: list[date] = []
    day = date(2026, 10, 1)
    while len(out) < days:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    for i, d in enumerate(out):
        part = data / "kline_index_daily" / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["000001.SH", "399001.SZ"],
                "date": [d, d],
                "close": [3000.0 + i, 12000.0 + i * 2],
            }
        ).write_parquet(part / "part.parquet")
    return out


class _Repo:
    """只需要 `store.data_dir` 的最小替身。"""

    def __init__(self, data: Path) -> None:
        self.store = type("S", (), {"data_dir": data})()


def _latest_two(con: duckdb.DuckDBPyConnection, source: str) -> list[tuple]:
    return con.execute(
        f"""
        WITH ranked AS (
            SELECT symbol, date, close,
                   row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
            FROM {source}
        )
        SELECT symbol, max(CASE WHEN rn = 1 THEN date END),
               max(CASE WHEN rn = 1 THEN close END),
               max(CASE WHEN rn = 2 THEN close END)
        FROM ranked WHERE rn <= 2 GROUP BY symbol ORDER BY symbol
        """
    ).fetchall()


def test_tail_source_lists_only_tail_partitions(tmp_path: Path):
    _write_partitions(tmp_path, 30)
    source = _index_daily_tail_source(_Repo(tmp_path), keep=6)

    assert source is not None
    listed = [line for line in source.split(",") if "part.parquet" in line]
    assert len(listed) == 6, "只能把尾部分区交给 DuckDB"
    assert "date=2026-11-11" in source or "date=2026-11-12" in source
    assert "date=2026-10-01" not in source, "最早的不能出现在查询源里"


def test_tail_source_gives_same_answer_as_full_table(tmp_path: Path):
    days = _write_partitions(tmp_path, 30)
    con = duckdb.connect(":memory:")
    try:
        full = f"read_parquet('{(tmp_path / 'kline_index_daily').as_posix()}/**/*.parquet')"
        tail = _index_daily_tail_source(_Repo(tmp_path), keep=6)
        assert tail is not None
        assert _latest_two(con, tail) == _latest_two(con, full)
    finally:
        con.close()

    # 尾部窗口确实覆盖了最后两个交易日
    assert days[-1].isoformat() in (_index_daily_tail_source(_Repo(tmp_path)) or "")


def test_tail_source_returns_none_when_table_missing(tmp_path: Path):
    assert _index_daily_tail_source(_Repo(tmp_path)) is None
