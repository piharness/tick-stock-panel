"""回归测试: 分钟K「最近日期」不得全表聚合。

回归动机（2026-11-24）：`latest_minute_date` / `latest_minute_date_global`
是 `SELECT max(datetime) FROM kline_minute ...`，在日分区视图上是全表聚合。
536 个分区文件的 footer 全读，容器内实测约 **19s**。它落在两条用户路径上:

  - `/api/kline/minute` 不传 date 时的日期解析（周末/盘前打开个股图必走）；
  - 非交易日回退 (`latest_minute_date_global`)。

判据:
  1. **答案不变** —— 与全表聚合给出同一个日期（含停牌股缺行的情况）；
  2. **不扫全表** —— 把全表聚合替换成抛异常，仍然要给出正确答案。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


def _write_day(base: Path, day: str, symbols: list[str]) -> None:
    part = base / "kline_minute" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    rows = []
    for sym in symbols:
        for hh, mm in ((9, 31), (9, 32)):
            rows.append(
                {
                    "symbol": sym,
                    "datetime": datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00"),
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 100,
                    "amount": 1000.0,
                }
            )
    pl.DataFrame(rows).write_parquet(part / "part.parquet")


@pytest.fixture
def repo(tmp_path: Path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def test_latest_minute_date_ignores_suspended_days(tmp_path: Path, repo):
    """停牌股在最近两天没有行 → 必须回到它真正有数据的那个交易日。"""
    _write_day(tmp_path, "2026-11-19", ["600001.SH", "600002.SH"])
    _write_day(tmp_path, "2026-11-20", ["600001.SH"])
    _write_day(tmp_path, "2026-11-23", ["600001.SH"])

    assert repo.latest_minute_date("600001.SH") == date(2026, 11, 23)
    assert repo.latest_minute_date("600002.SH") == date(2026, 11, 19)
    assert repo.latest_minute_date("999999.SH") is None


def test_latest_minute_date_does_not_full_scan(tmp_path: Path, repo, monkeypatch):
    """全表聚合被禁时仍要出结果 —— 防止有人把 `max(datetime)` 写回来。"""
    _write_day(tmp_path, "2026-11-20", ["600001.SH"])
    _write_day(tmp_path, "2026-11-23", ["600001.SH"])

    def boom(*_a, **_k):
        raise AssertionError("最近分钟日期不得走全表聚合")

    monkeypatch.setattr(repo, "execute_one", boom)
    assert repo.latest_minute_date("600001.SH") == date(2026, 11, 23)


def test_latest_minute_date_global_reads_partition_names(tmp_path: Path, repo, monkeypatch):
    _write_day(tmp_path, "2026-11-19", ["600001.SH"])
    _write_day(tmp_path, "2026-11-23", ["600001.SH"])

    def boom(*_a, **_k):
        raise AssertionError("全市场最近分钟日期不得走全表聚合")

    monkeypatch.setattr(repo, "execute_one", boom)
    assert repo.latest_minute_date_global() == date(2026, 11, 23)


def test_latest_minute_date_global_none_without_partitions(tmp_path: Path, repo):
    assert repo.latest_minute_date_global() is None
