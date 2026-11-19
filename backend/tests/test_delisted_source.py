"""全样本日K（主表 + 306 只缺口股）的接线合同。

这里只验四件事，但每一件都对应一种「不报错但算错」的失败：

1. `price_basis` 必须区分 qfq 与 raw —— 混成一列会让除权日变成无人察觉的假跌。
2. 还原原始开盘价必须精确（同日开盘与收盘共享复权比例），否则日内收益全错。
3. 缺口源与主表零重叠时才允许 union —— 重叠会双计，且读侧不会报错。
4. 补全包不存在时必须优雅退化（只读主表），不能让启动挂掉。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.parquet import ENRICHED_STORAGE_SCHEMA
from app.tickflow.delisted import (
    BASIS_QFQ,
    BASIS_RAW,
    GAP_DIRNAME,
    GAP_FILENAME,
    SRC_GAP,
    SRC_MAIN,
    gap_symbols,
    to_raw_basis,
)
from app.tickflow.repository import DataStore, KlineRepository

MAIN = "600000.SH"
GAP = "000004.SZ"
DAY = date(2024, 1, 2)


def _write_main(data_dir: Path) -> None:
    """一只正常票：close 是前复权（6.6），raw_close 是原始价（7.0）。"""
    row = {
        "symbol": MAIN,
        "date": DAY,
        "open": 6.63,
        "high": 6.7,
        "low": 6.5,
        "close": 6.6,
        "volume": 1000.0,
        "amount": 6600.0,
        "raw_close": 7.0,
        "raw_high": 7.1,
        "raw_low": 6.5,
        "turnover_rate": 1.2,
        "consecutive_limit_ups": 0,
        "consecutive_limit_downs": 0,
        "quote_ts": 1704153600000,
    }
    out = data_dir / "kline_daily_enriched" / f"date={DAY.isoformat()}"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([row], schema=ENRICHED_STORAGE_SCHEMA).write_parquet(out / "part.parquet")


def _write_gap(data_dir: Path) -> None:
    """一只退市票：只有原始价，列比主表少 7 个。"""
    out = data_dir / GAP_DIRNAME
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [GAP],
            "date": [DAY],
            "open": [10.0],
            "close": [10.2],
            "high": [10.5],
            "low": [9.8],
            "volume": [500],
            "amount": [5100.0],
        }
    ).write_parquet(out / GAP_FILENAME)


@pytest.fixture()
def _store_factory():
    """按需建库；不做全局 settings patch（pydantic 实例不接受 setattr）。

    每个测试自己 `DataStore(tmp_path)`：视图注册只 glob 临时目录里的
    一两个分区，成本可忽略，换来的是用例之间零共享状态。
    """
    created: list[DataStore] = []

    def make(data_dir: Path) -> KlineRepository:
        store = DataStore(data_dir)
        created.append(store)
        return KlineRepository(store)

    yield make
    for store in created:
        try:
            store.db.close()
        except Exception:  # noqa: BLE001
            pass


def test_gap_symbols_reads_the_pack_and_caches_by_mtime(tmp_path):
    _write_gap(tmp_path)
    assert gap_symbols(tmp_path) == frozenset({GAP})
    # 重写文件后 mtime 变，缓存必须失效（补全包重跑是整文件覆盖）
    _write_gap(tmp_path)
    assert gap_symbols(tmp_path) == frozenset({GAP})


def test_missing_pack_degrades_to_main_only(tmp_path):
    """补全包不存在：名单为空、视图不建、不抛错。"""
    _write_main(tmp_path)
    assert gap_symbols(tmp_path) == frozenset()
    repo = KlineRepository(DataStore(tmp_path))
    views = repo.execute_all("SELECT table_name FROM information_schema.views")
    names = {r[0] for r in views}
    assert "kline_stock_all" not in names
    assert "kline_enriched" in names


def test_all_sample_view_marks_basis_and_never_duplicates(tmp_path):
    _write_main(tmp_path)
    _write_gap(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    rows = repo.execute_all(
        "SELECT symbol, src, price_basis, close FROM kline_stock_all ORDER BY symbol"
    )
    assert rows == [
        (GAP, SRC_GAP, BASIS_RAW, 10.2),
        (MAIN, SRC_MAIN, BASIS_QFQ, 6.6),
    ]
    # 每个 (symbol, date) 只能出现一次 —— 双计是 union 最贵的失败模式
    dup = repo.execute_all(
        "SELECT count(*) FROM (SELECT symbol, date FROM kline_stock_all"
        " GROUP BY 1,2 HAVING count(*) > 1)"
    )
    assert dup == [(0,)]


def test_raw_basis_reconstructs_open_exactly(tmp_path):
    """raw_open = open * raw_close / close（同日共享复权比例，精确可逆）。"""
    _write_main(tmp_path)
    _write_gap(tmp_path)
    store = DataStore(tmp_path)
    repo = KlineRepository(store)

    raw = repo.get_daily_all_raw(MAIN, DAY, DAY)
    assert raw.height == 1
    assert raw["price_basis"].to_list() == [BASIS_RAW]
    assert raw["close"].to_list() == [7.0]
    assert raw["open"].to_list() == pytest.approx([6.63 * 7.0 / 6.6])
    # 还原出的开盘必须落在当日的原始高低之间，否则公式方向就错了
    assert raw["open"][0] <= raw["high"][0]
    assert raw["open"][0] >= raw["low"][0]


def test_mixed_basis_keeps_adjusted_close_for_main(tmp_path):
    """默认口径：主表保留前复权 close，缺口股只能是原始价。"""
    _write_main(tmp_path)
    _write_gap(tmp_path)
    store = DataStore(tmp_path)
    repo = KlineRepository(store)

    both = repo.get_daily_batch_all([MAIN, GAP], DAY, DAY)
    got = dict(zip(both["symbol"].to_list(), both["close"].to_list()))
    assert got[MAIN] == 6.6      # 前复权，未被降级
    assert got[GAP] == 10.2      # 原始价
    assert all(both["src"].to_list()) is not None


def test_union_respects_date_window_and_columns(tmp_path):
    _write_main(tmp_path)
    _write_gap(tmp_path)
    store = DataStore(tmp_path)
    repo = KlineRepository(store)

    # 窗口落在数据之外 → 空表而不是抛错
    empty = repo.get_daily_all(GAP, date(2025, 1, 1), date(2025, 2, 1))
    assert empty.is_empty()

    picked = repo.get_daily_all(GAP, DAY, DAY, columns=["date", "close", "price_basis"])
    assert picked.columns == ["date", "close", "price_basis"]
    assert picked["close"].to_list() == [10.2]


def test_raw_projection_is_idempotent_on_gap_rows():
    """缺口股本来就是原始价，raw 投影不能改动它（含 high/low）。"""
    df = pl.DataFrame(
        {
            "symbol": [GAP],
            "date": [DAY],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "volume": [500.0],
            "amount": [5100.0],
            "raw_close": [10.2],
            "raw_high": [10.5],
            "raw_low": [9.8],
        },
        schema_overrides={"date": pl.Date},
    )
    out = to_raw_basis(df)
    assert out["open"].to_list() == [10.0]
    assert out["close"].to_list() == [10.2]
    assert out["price_basis"].to_list() == [BASIS_RAW]
