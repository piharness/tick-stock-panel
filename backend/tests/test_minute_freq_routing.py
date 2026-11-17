"""多周期分钟表的读取路由。

判据不是「freq 参数被接受」，而是**拼出来的分区路径真的指向那张表的那个日期**：
`_minute_partition_path` 从 glob 反推 base（`glob.rsplit("/", 2)[0]`），
多一个层级或少一个 `date=` 都会静默回退到全树扫描 —— 慢，而且读的是别的表。
"""

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.tickflow.repository import MINUTE_FREQ_TABLES, KlineRepository


def _repo(data_dir):
    """绕过 __init__（它要建 DuckDB 与全套目录）只装配路径解析需要的属性。"""
    repo = object.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=data_dir)
    repo._minute_glob = str(data_dir / "kline_minute" / "**" / "*.parquet")
    repo._etf_minute_glob = str(data_dir / "kline_etf_minute" / "**" / "*.parquet")
    repo._minute_freq_globs = {
        freq: str(data_dir / table / "**" / "*.parquet")
        for freq, table in MINUTE_FREQ_TABLES.items()
    }
    return repo


@pytest.mark.parametrize("freq,table", sorted(MINUTE_FREQ_TABLES.items()))
def test_each_freq_resolves_to_its_own_table(tmp_path, freq, table):
    day = date(2026, 11, 13)
    part = tmp_path / table / f"date={day.isoformat()}" / "part.parquet"
    part.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "datetime": [None]}).write_parquet(part)

    repo = _repo(tmp_path)

    assert repo._minute_glob_for("stock", freq) == str(tmp_path / table / "**" / "*.parquet")
    assert repo._minute_partition_path("stock", day, freq) == str(part)


def test_1m_keeps_legacy_stock_and_etf_paths(tmp_path):
    day = date(2026, 11, 13)
    repo = _repo(tmp_path)

    assert repo._minute_glob_for("stock") == str(tmp_path / "kline_minute" / "**" / "*.parquet")
    assert repo._minute_glob_for("etf") == str(tmp_path / "kline_etf_minute" / "**" / "*.parquet")
    # 分区不存在时返回 None（调用方据此回退全树扫描），而不是拼一个假路径
    assert repo._minute_partition_path("stock", day, "5m") is None


def test_etf_has_no_multi_period_table(tmp_path):
    """5m 表只含股票。查 ETF 时仍指向该表（过滤后为空），不虚构一张 etf_5min。"""
    repo = _repo(tmp_path)

    assert repo._minute_glob_for("etf", "5m") == str(tmp_path / "kline_5min" / "**" / "*.parquet")


def test_unknown_freq_is_rejected_loudly(tmp_path):
    """拼错周期必须抛，不能退化成「读 1m 却标成 7m」。"""
    repo = _repo(tmp_path)

    with pytest.raises(ValueError, match="未支持的分钟周期"):
        repo._minute_glob_for("stock", "7m")
