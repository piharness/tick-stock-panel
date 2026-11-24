"""回归测试: `/api/data/status` 的日K覆盖不得再走全表月分区扫描。

回归动机（2026-11-24）：`_safe_aggregate_daily` 与 `_safe_aggregate_adj_factor`
各自调一次 `list_partition_dates(kline_daily)`。月分区布局下该函数要对每个
分区 `scan_parquet` 读 `date` 列 —— 590 个分区容器内实测 **19.0s/次**，
两个聚合合计 38s，`/status` 冷态单请求 25s+，直接顶穿前端 30s 超时，
数据页表现为「一直在转圈」。

判据（两面）：
  1. **结果不变** —— 覆盖日历必须与 `list_partition_dates` 完全一致，
     提速不许改变口径（否则「已最新」会变成假绿）；
  2. **路径不回退** —— 让慢函数抛异常，聚合仍要给出答案。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.api import data as data_api
from app.services import trading_days as td
from app.tickflow.partitions import list_partition_dates


@pytest.fixture(autouse=True)
def _clear_cache():
    td._DAYS_CACHE.clear()
    yield
    td._DAYS_CACHE.clear()


def _write_month_partition(data: Path, days: list[str]) -> None:
    """写一个月分区（kline_daily 的真实布局：date=YYYY-MM/part.parquet）。"""
    import polars as pl

    month = days[0][:7]
    part = data / "kline_daily" / f"date={month}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH"] * len(days),
            "date": [date.fromisoformat(d) for d in days],
            "close": [10.0] * len(days),
        }
    ).write_parquet(part / "part.parquet")


def _write_day_partition(data: Path, day: str) -> None:
    import polars as pl

    part = data / "kline_daily" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "date": [date.fromisoformat(day)],
            "close": [10.0],
        }
    ).write_parquet(part / "part.parquet")


def test_coverage_matches_partition_dates(tmp_path: Path):
    """混合布局（月分区 + 迁移前的日分区）下，两条路径必须同一集合。"""
    _write_month_partition(tmp_path, ["2026-11-02", "2026-11-03"])
    _write_day_partition(tmp_path, "2026-10-30")

    expected = sorted(list_partition_dates(tmp_path / "kline_daily"))
    got = data_api._kline_coverage_days(tmp_path)

    assert expected == [date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3)]
    assert got == expected, "覆盖日历必须与分区真值一致，提速不得改变口径"


def test_coverage_does_not_scan_monthly_partitions(tmp_path: Path, monkeypatch):
    """慢路径被禁时仍要出结果 —— 防止有人再把 `list_partition_dates` 写回来。"""
    _write_month_partition(tmp_path, ["2026-11-02", "2026-11-03"])

    def boom(*_a, **_k):
        raise AssertionError("/status 的覆盖日历不得全表扫描月分区")

    monkeypatch.setattr(data_api, "list_partition_dates", boom)
    assert data_api._kline_coverage_days(tmp_path) == [date(2026, 11, 2), date(2026, 11, 3)]


def test_coverage_empty_when_table_missing(tmp_path: Path):
    assert data_api._kline_coverage_days(tmp_path) == []
