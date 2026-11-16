"""分区裁剪: 把「整表 glob」缩成「区间触及的分区文件」, 结果必须与全扫逐行相等。

为什么要做: 单票查询的代价是**每个分区一次 footer 读**。9p 上实测 372 文件全扫
1.63s; 裁到 178 个 0.68s、81 个 0.32s。真实数据上已用 5 个区间验过逐行相等
(270/120/30/5/1 天, 178/81/23/5/2 个文件, rows 1424/648/184/40/16 全等)。

这里的单元测试只钉**边界语义**, 因为少读一个分区 = 静默丢行, 比慢更贵:

1. 日分区: 只取区间内的;
2. 月分区: 与该月有交集就整份纳入(记忆 #25: 月分区名不是日期), 越界行交给 date 过滤;
3. 区间内没有分区: 返回**空列表**而不是回退全扫 —— 正确答案就是空;
4. 目录读不到(表不存在): 返回 None, 调用方回退 glob, 行为与裁剪前完全一致;
5. 认不出的目录名: 纳入, 不猜。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository

TABLE = "kline_daily_enriched"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    from app.services import daily_health

    monkeypatch.setattr(daily_health, "assert_usable", lambda *a, **k: None)
    return KlineRepository(DataStore(tmp_path))


def _write(root, dirname: str, rows: list[tuple[str, dt.date]]) -> None:
    d = root / TABLE / dirname
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "open": [1.0] * len(rows), "high": [1.0] * len(rows),
            "low": [1.0] * len(rows), "close": [1.0] * len(rows),
            "volume": [1.0] * len(rows), "amount": [1.0] * len(rows),
            "quote_ts": pl.Series([1] * len(rows), dtype=pl.Int64),
        }
    ).write_parquet(d / "part.parquet")


def _names(files):
    return sorted(f.rsplit("/", 2)[-2] if "/" in f else f for f in files)


def test_daily_partitions_are_limited_to_the_range(repo, tmp_path):
    for day in ("2026-11-01", "2026-11-05", "2026-11-10", "2026-11-20"):
        _write(tmp_path, f"date={day}", [("000001.SZ", dt.date.fromisoformat(day))])

    files = repo._partition_files_in_range(TABLE, dt.date(2026, 11, 1), dt.date(2026, 11, 10))
    assert len(files) == 3
    assert {Path(f).parent.name for f in files} == {
        "date=2026-11-01", "date=2026-11-05", "date=2026-11-10",
    }


def test_month_partition_is_included_when_month_intersects(repo, tmp_path):
    _write(tmp_path, "date=2026-11", [
        ("000001.SZ", dt.date(2026, 11, 5)),
        ("000001.SZ", dt.date(2026, 11, 20)),
    ])
    files = repo._partition_files_in_range(TABLE, dt.date(2026, 11, 1), dt.date(2026, 11, 10))
    assert len(files) == 1                              # 整月纳入
    # 越界行由 date 过滤兜底: 只返回 11-05
    df = repo.get_daily("000001.SZ", dt.date(2026, 11, 1), dt.date(2026, 11, 10), columns=["date"])
    assert df["date"].to_list() == [dt.date(2026, 11, 5)]


def test_range_without_partitions_returns_empty_list_not_none(repo, tmp_path):
    _write(tmp_path, "date=2026-11-05", [("000001.SZ", dt.date(2026, 11, 5))])
    files = repo._partition_files_in_range(TABLE, dt.date(2027, 1, 1), dt.date(2027, 1, 5))
    assert files == []          # 空列表 ⇒ 下游直接得空, 不回退全扫
    assert repo.get_daily("000001.SZ", dt.date(2027, 1, 1), dt.date(2027, 1, 5)).is_empty()
    assert repo.take_scan_error() is None    # 空区间不是"读坏了"


def test_missing_table_falls_back_to_glob(repo):
    assert repo._partition_files_in_range("no_such_table", dt.date(2026, 11, 1), dt.date(2026, 11, 2)) is None


def test_unknown_dirname_is_included_not_dropped(repo, tmp_path):
    _write(tmp_path, "date=2026-11-05", [("000001.SZ", dt.date(2026, 11, 5))])
    _write(tmp_path, "date=broken", [("000002.SZ", dt.date(2026, 11, 5))])
    files = repo._partition_files_in_range(TABLE, dt.date(2026, 11, 5), dt.date(2026, 11, 5))
    assert len(files) == 2      # 认不出就纳入: 多读一份只是慢, 少读一份是丢行
