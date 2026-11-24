"""回归测试: DuckDB 视图「按需挂载」。

回归动机（2026-11-24）：启动时一次性 `CREATE OR REPLACE VIEW` 23 张视图，容器内
逐张实测合计 **56.5s**，占整个启动（64s）的 **88%**。原因是 DuckDB 的 CREATE VIEW
**当就绑定**：要把 `<dir>/**/*.parquet` 展开并读每个文件的 schema。

而统计全仓 `FROM/JOIN <view>` 后只有 7 个视图被真实使用，其中 `kline_enriched`
仅出现在注释里。也就是说启动期一半以上的时间在挂载没人查询的视图：

    kline_daily_enriched  5316 文件  40.0s  ← 无引用
    kline_daily           5240 文件  14.7s
    kline_5min             536 文件   4.1s  ← 无引用
    kline_etf_* / index_enriched 各 257 文件 各 ~2.0s  ← 无引用

判据：
  1. 启动**一张都不建**（否则等于没改）；
  2. 第一次被 SQL 引用时建出来，且结果正确；
  3. 建过之后不再重复建（热路径只剩一次判空）；
  4. 名字匹配不能串味 —— `kline_daily` 不得把 `kline_enriched` 一起带出来；
  5. 空目录建不出来时**留在待办**，等数据落盘后还能补上。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def store(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path)


@pytest.fixture
def repo(store: DataStore) -> KlineRepository:
    return KlineRepository(store)


def _write_daily(data: Path, rows: int = 2) -> None:
    part = data / "kline_daily" / "date=2026-11"
    part.mkdir(parents=True, exist_ok=True)
    days = [date(2026, 11, 20 + i) for i in range(rows)]
    pl.DataFrame({
        "symbol": ["600519.SH"] * rows,
        "date": days,
        "close": [10.0 + i for i in range(rows)],
    }).write_parquet(part / "part.parquet")


def _created_views(repo: KlineRepository) -> int:
    return repo.execute_one("SELECT count(*) FROM duckdb_views() WHERE NOT internal")[0]


def test_startup_creates_no_views(store: DataStore, repo: KlineRepository):
    assert len(store._pending_views) > 0, "启动应登记待挂载视图"
    assert _created_views(repo) == 0, "启动一张视图都不该建（否则 88% 的启动成本还在）"


def test_view_is_mounted_on_first_reference(store: DataStore, repo: KlineRepository):
    _write_daily(tmp_path_of(store))

    assert repo.execute_one("SELECT count(*) FROM kline_daily")[0] == 2
    assert "kline_daily" not in store._pending_views
    assert _created_views(repo) == 1, "只该建被引用的这一张"


def test_second_query_does_not_remount(store: DataStore, repo: KlineRepository, monkeypatch):
    _write_daily(tmp_path_of(store))
    repo.execute_one("SELECT count(*) FROM kline_daily")

    calls = {"n": 0}
    real = store._create_view

    def counted(sql):
        calls["n"] += 1
        return real(sql)

    monkeypatch.setattr(store, "_create_view", counted)
    repo.execute_one("SELECT count(*) FROM kline_daily")
    repo.execute_one("SELECT max(close) FROM kline_daily")

    assert calls["n"] == 0, "挂过之后不得重复建"


def test_name_matching_does_not_overlap(store: DataStore, repo: KlineRepository):
    """`kline_daily` 与 `kline_enriched` 是两个名字，一个不能带出另一个。"""
    _write_daily(tmp_path_of(store))

    repo.execute_one("SELECT count(*) FROM kline_daily")

    assert "kline_daily" not in store._pending_views
    assert "kline_enriched" in store._pending_views, "只引用 kline_daily 时不该顺手建 enriched"


def test_view_stays_pending_until_data_arrives(store: DataStore, repo: KlineRepository):
    """空目录建不出视图 → 留在待办；数据落盘后同一条 SQL 自动挂上。

    这条同时钉住一个**既有语义**：空目录下查该表本来就会抛 CatalogException
    （改动前启动期建不出来，视图同样不存在）。改动只保证「以后能自己补上」，
    不改变报错本身——所以这里显式断言异常，避免有人把 fail-closed 改成静默空表。
    """
    import duckdb

    with pytest.raises(duckdb.CatalogException):
        repo.execute_one("SELECT count(*) FROM instruments")
    assert "instruments" in store._pending_views, "建不出来就不能从待办摘掉"

    data = tmp_path_of(store)
    (data / "instruments").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["600519.SH"], "name": ["贵州茅台"]}).write_parquet(
        data / "instruments" / "part.parquet"
    )

    assert repo.execute_one("SELECT count(*) FROM instruments")[0] == 1
    assert "instruments" not in store._pending_views


def test_sql_without_view_names_does_nothing(store: DataStore, repo: KlineRepository, monkeypatch):
    _write_daily(tmp_path_of(store))
    calls = {"n": 0}
    real = store._create_view

    def counted(sql):
        calls["n"] += 1
        return real(sql)

    monkeypatch.setattr(store, "_create_view", counted)
    repo.execute_one("SELECT 1")

    assert calls["n"] == 0


def test_refresh_view_clears_pending(store: DataStore, repo: KlineRepository):
    _write_daily(tmp_path_of(store))
    assert "kline_daily" in store._pending_views

    assert repo.refresh_view("kline_daily", str(tmp_path_of(store) / "kline_daily" / "**" / "*.parquet"))
    assert "kline_daily" not in store._pending_views
    assert repo.execute_one("SELECT count(*) FROM kline_daily")[0] == 2


# ── 辅助 ────────────────────────────────────────────────────────────


def tmp_path_of(store: DataStore) -> Path:
    return Path(store.data_dir)


def test_warm_common_views_mounts_only_the_cheap_used_ones(store: DataStore, repo: KlineRepository):
    """后台预热只碰「首屏确定会用且便宜」的那几张，贵的必须留着。

    依据：36 个界面端点全跑一遍后，只有 kline_index_daily(3.75s) + 四张字典表
    被挂载过；kline_daily(14.7s)/kline_enriched(40.0s) 全程没人碰。
    """
    import threading
    import time as _time

    _write_daily(tmp_path_of(store))
    for table in ("instruments", "instruments_index", "instruments_etf", "adj_factor"):
        d = tmp_path_of(store) / table
        d.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": ["600519.SH"]}).write_parquet(d / "part.parquet")

    repo.store.warm_common_views()
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        if not any(n in store._pending_views for n in
                   ("instruments", "instruments_index", "instruments_etf",
                    "adj_factor", "kline_index_daily")):
            break
        threading.Event().wait(0.05)

    for name in ("instruments", "instruments_index", "instruments_etf", "adj_factor"):
        assert name not in store._pending_views, f"{name} 应已被预热挂上"
    assert "kline_daily" in store._pending_views, "贵的视图不得预热"
    assert "kline_enriched" in store._pending_views, "贵的视图不得预热"
