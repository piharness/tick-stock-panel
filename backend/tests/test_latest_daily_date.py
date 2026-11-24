"""回归测试: 「最新日K日期」不得枚举全部分区。

回归动机（2026-11-24）：`KlineRepository.latest_daily_date()` 走
`list_partition_dates(kline_daily)`，月分区布局下要对 590 个分区逐个
`scan_parquet` 读 `date` 列，容器内实测 **19.0s/次**。

它挂在 `/api/kline/minute` 的日期解析上：不传 `date` 时（图表默认）先问
「最新日K是哪天」，于是单只股票分时请求要 19s，而显式传 `date` 只要 0.17s。
同一个函数还被 `/api/kline/minute-batch`、盘前判断等多条路径共用。

判据: 把慢函数换成抛异常，仍要给出正确日期 —— 且与分区真值一致。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import trading_days as td
from app.tickflow import repository as repo_mod
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture(autouse=True)
def _clear_days_cache():
    td._DAYS_CACHE.clear()
    yield
    td._DAYS_CACHE.clear()


@pytest.fixture
def repo(tmp_path: Path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def _write_daily(tmp_path: Path, days: list[str]) -> None:
    """按真实布局写：月分区 date=YYYY-MM/part.parquet，同月多日一个文件。"""
    by_month: dict[str, list[str]] = {}
    for d in days:
        by_month.setdefault(d[:7], []).append(d)
    for month, items in by_month.items():
        part = tmp_path / "kline_daily" / f"date={month}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["600001.SH"] * len(items),
                "date": [date.fromisoformat(x) for x in items],
                "close": [10.0] * len(items),
            }
        ).write_parquet(part / "part.parquet")


def test_latest_daily_date_matches_partition_truth(tmp_path: Path, repo):
    _write_daily(tmp_path, ["2026-10-30", "2026-11-02", "2026-11-03"])
    assert repo.latest_daily_date() == date(2026, 11, 3)


def test_latest_daily_date_does_not_enumerate_all_partitions(tmp_path: Path, repo, monkeypatch):
    _write_daily(tmp_path, ["2026-10-30", "2026-11-02"])

    def boom(*_a, **_k):
        raise AssertionError("最新日K日期不得逐个分区 scan_parquet")

    monkeypatch.setattr(repo_mod, "list_partition_dates", boom)
    assert repo.latest_daily_date() == date(2026, 11, 2)


def test_latest_daily_date_none_when_empty(tmp_path: Path, repo):
    assert repo.latest_daily_date() is None
