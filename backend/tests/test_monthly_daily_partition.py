"""kline_daily 月分区布局的行为钉子 (2026-11 迁移)。

每个用例对应一个曾经按 date=YYYY-MM-DD 目录名解析的代码点:
- 写侧: append_daily / flush_live_daily / merge_live_daily_asset
- 修补: replace_daily_rows / daily_repair_universe / validate_daily_repair_target
- 完整性: scan_recent_integrity 缺失日检测 / inspect_daily_partition / 快照判定
- 同步: _persist_daily_chunks staging→commit
- 助手: list_partition_dates / partition_date_map 双布局
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.data_integrity import (
    _partition_snapshot_defects,
    _quote_ts_max_ms,
    inspect_daily_partition,
    scan_recent_integrity,
)
from app.tickflow.partitions import list_partition_dates, partition_date_map
from app.tickflow.repository import DataStore, KlineRepository

# 2026-08-19(周三)~2026-08-21(周五) 工作日; TODAY=2026-08-24(周一)
TODAY = date(2026, 8, 24)
FRIDAY = date(2026, 8, 21)
THURSDAY = date(2026, 8, 20)
MONTH = "2026-08"


def _daily_frame(day: date, symbols: tuple[str, ...], *, quote_ts: int | None = None,
                 close: float = 10.0) -> pl.DataFrame:
    n = len(symbols)
    return pl.DataFrame({
        "symbol": list(symbols),
        "date": [day] * n,
        "open": [10.0] * n,
        "high": [10.1] * n,
        "low": [9.9] * n,
        "close": [close] * n,
        "volume": [100.0] * n,
        "amount": [1000.0] * n,
        "quote_ts": [quote_ts] * n,
    })


def _ts_ms(day: date, t: time) -> int:
    return int(datetime.combine(day, t, tzinfo=CN_TZ).timestamp() * 1000)


@pytest.fixture()
def repo(tmp_path: Path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


# ── 助手: 双布局日期枚举 ────────────────────────────────────────────


def test_list_partition_dates_handles_mixed_layout(tmp_path: Path):
    base = tmp_path / "kline_daily"
    (base / f"date={FRIDAY.isoformat()}").mkdir(parents=True)  # 旧日分区
    month_part = base / f"date={MONTH}"
    month_part.mkdir(parents=True)
    _daily_frame(THURSDAY, ("600001.SH",)).write_parquet(month_part / "part.parquet")
    assert list_partition_dates(base) == {THURSDAY, FRIDAY}


def test_partition_date_map_month_reads_real_dates_not_month_span(tmp_path: Path):
    base = tmp_path / "kline_daily"
    month_part = base / f"date={MONTH}"
    month_part.mkdir(parents=True)
    # 只有一个交易日的行: 日期集合必须是 {THURSDAY}, 不是整月区间
    _daily_frame(THURSDAY, ("600001.SH",)).write_parquet(month_part / "part.parquet")
    dates = partition_date_map(base)[month_part]
    assert dates == {THURSDAY}


# ── 写侧 ───────────────────────────────────────────────────────────


def test_append_daily_writes_month_partition(tmp_path: Path, repo: KlineRepository):
    df = pl.concat([
        _daily_frame(THURSDAY, ("600001.SH", "600002.SH")),
        _daily_frame(FRIDAY, ("600001.SH",)),
    ])
    repo.append_daily(df)
    base = tmp_path / "kline_daily"
    assert (base / f"date={MONTH}" / "part.parquet").exists()
    # 同日再次 append: merge-upsert 以新值为准
    repo.append_daily(_daily_frame(FRIDAY, ("600001.SH",), close=11.0))
    stored = pl.read_parquet(base / f"date={MONTH}" / "part.parquet")
    friday = stored.filter(pl.col("date") == FRIDAY)
    assert friday.height == 1 and friday["close"][0] == 11.0
    assert stored.filter(pl.col("date") == THURSDAY).height == 2


def test_flush_live_daily_replaces_day_rows_inside_month(tmp_path: Path, repo: KlineRepository):
    repo.append_daily(pl.concat([
        _daily_frame(THURSDAY, ("600001.SH",)),
        _daily_frame(FRIDAY, ("600001.SH", "600002.SH")),
    ]))
    # 实时 flush: 当日只剩一只 (另一只被停牌过滤) — 旧行必须整体消失
    repo.flush_live_daily(_daily_frame(FRIDAY, ("600001.SH",), close=12.5))
    stored = pl.read_parquet(tmp_path / "kline_daily" / f"date={MONTH}" / "part.parquet")
    friday = stored.filter(pl.col("date") == FRIDAY)
    assert friday["symbol"].to_list() == ["600001.SH"]
    assert friday["close"][0] == 12.5
    # 其他日期行原样保留
    assert stored.filter(pl.col("date") == THURSDAY).height == 1


def test_merge_live_daily_asset_writes_month_partition(tmp_path: Path, repo: KlineRepository):
    repo.merge_live_daily_asset("stock", _daily_frame(FRIDAY, ("600001.SH",), quote_ts=1))
    assert (tmp_path / "kline_daily" / f"date={MONTH}" / "part.parquet").exists()


# ── 修补路径 ────────────────────────────────────────────────────────


def test_validate_daily_repair_target_accepts_month_layout(tmp_path: Path, repo: KlineRepository):
    (tmp_path / "kline_daily" / f"date={MONTH}").mkdir(parents=True)
    repo.validate_daily_repair_target("stock", FRIDAY)


def test_replace_daily_rows_repairs_single_day_inside_month(tmp_path: Path, repo: KlineRepository):
    repo.append_daily(pl.concat([
        _daily_frame(THURSDAY, ("600001.SH", "600002.SH")),
        _daily_frame(FRIDAY, ("600001.SH", "600002.SH")),
    ]))
    backup = tmp_path / "backup"
    repo.replace_daily_rows(
        "stock", FRIDAY, _daily_frame(FRIDAY, ("600001.SH",), close=9.0),
        symbols=["600001.SH"], backup_dir=backup,
    )
    stored = pl.read_parquet(tmp_path / "kline_daily" / f"date={MONTH}" / "part.parquet")
    friday = stored.filter(pl.col("date") == FRIDAY)
    # 目标日目标股被替换
    assert friday.filter(pl.col("symbol") == "600001.SH")["close"][0] == 9.0
    # 目标日其他股保留; 同股其他交易日保留
    assert friday.filter(pl.col("symbol") == "600002.SH")["close"][0] == 10.0
    assert stored.filter(
        (pl.col("date") == THURSDAY) & (pl.col("symbol") == "600001.SH")
    )["close"][0] == 10.0


def test_daily_repair_universe_reads_month_partition(tmp_path: Path):
    # instruments 必须先于 DataStore 视图注册落盘 (CREATE VIEW 创建即绑定 glob)
    instruments = tmp_path / "instruments"
    instruments.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600001.SH", "600002.SH", "600003.SH"]}).write_parquet(
        instruments / "part.parquet"
    )
    repo = KlineRepository(DataStore(tmp_path))
    base = tmp_path / "kline_daily"
    month_part = base / f"date={MONTH}"
    month_part.mkdir(parents=True)
    _daily_frame(FRIDAY, ("600001.SH", "600002.SH")).write_parquet(month_part / "part.parquet")
    known, complete = repo.daily_repair_universe("stock", FRIDAY)
    assert complete
    assert known == ["600001.SH", "600002.SH", "600003.SH"]


# ── 完整性 ─────────────────────────────────────────────────────────


def _write_month_partition(root: Path, frames: list[pl.DataFrame]) -> Path:
    part = root / "kline_daily" / f"date={MONTH}"
    part.mkdir(parents=True, exist_ok=True)
    pl.concat(frames, how="diagonal_relaxed").write_parquet(part / "part.parquet")
    return part


def test_scan_recent_integrity_finds_missing_day_inside_month(tmp_path: Path):
    # 月分区只含周三/周五: 周四交易日缺失必须被报 missing (目录名月份不能顶替日期)。
    # 周四早于数据最早日会被「首次同步前不推断缺失」豁免, 所以要垫一个周三。
    wednesday = date(2026, 8, 19)
    _write_month_partition(tmp_path, [
        _daily_frame(wednesday, ("600001.SH",)),
        _daily_frame(FRIDAY, ("600001.SH",)),
    ])
    issues = scan_recent_integrity(tmp_path, today=TODAY, tables=("kline_daily",))
    missing = [i for i in issues if i.kind == "missing" and i.table == "kline_daily"]
    assert {i.day for i in missing} == {THURSDAY}


def test_scan_recent_integrity_clean_month_layout(tmp_path: Path):
    _write_month_partition(tmp_path, [
        _daily_frame(THURSDAY, ("600001.SH",)),
        _daily_frame(FRIDAY, ("600001.SH",)),
    ])
    assert scan_recent_integrity(tmp_path, today=TODAY, tables=("kline_daily",)) == []


def test_inspect_daily_partition_deep_clean_on_month(tmp_path: Path):
    _write_month_partition(tmp_path, [
        _daily_frame(THURSDAY, ("600001.SH",)),
        _daily_frame(FRIDAY, ("600001.SH",)),
    ])
    assert inspect_daily_partition(tmp_path, "kline_daily", FRIDAY, deep=True) is None


def test_snapshot_detection_is_day_scoped_inside_month(tmp_path: Path):
    part = _write_month_partition(tmp_path, [
        _daily_frame(THURSDAY, ("600001.SH",)),                    # 前一日权威行
        _daily_frame(FRIDAY, ("600001.SH",)),                      # 周五权威行
        _daily_frame(FRIDAY, ("600002.SH",), quote_ts=_ts_ms(FRIDAY, time(10, 0))),
    ])
    # 周五 quote_ts 最大值必须只看周五行 (周四权威行 quote_ts 为 null)
    assert _quote_ts_max_ms(part, FRIDAY) == _ts_ms(FRIDAY, time(10, 0))
    defects = _partition_snapshot_defects(FRIDAY, part, _ts_ms(FRIDAY, time(10, 0)))
    assert defects == {"600002.SH"}


# ── 同步 staging ───────────────────────────────────────────────────


def test_persist_daily_chunks_commits_into_month_dirs(tmp_path: Path, repo: KlineRepository):
    from app.services.kline_sync import _persist_daily_chunks

    written = _persist_daily_chunks(
        [_daily_frame(THURSDAY, ("600001.SH",)), _daily_frame(FRIDAY, ("600001.SH", "600002.SH"))],
        repo,
    )
    assert written == 3
    base = tmp_path / "kline_daily"
    # 首次提交是 staging 搬运, 落成 part-stream-*.parquet, 还没有 part.parquet
    stored = pl.read_parquet(base / f"date={MONTH}" / "**" / "*.parquet")
    assert stored.height == 3
    assert set(zip(stored["date"].to_list(), stored["symbol"].to_list(), strict=False)) == {
        (THURSDAY, "600001.SH"), (FRIDAY, "600001.SH"), (FRIDAY, "600002.SH"),
    }
    # 二次同步同日不同值: upsert 以新值为准, 且不产生第二份分区
    _persist_daily_chunks([_daily_frame(FRIDAY, ("600001.SH",), close=20.0)], repo)
    stored2 = pl.read_parquet(base / f"date={MONTH}" / "**" / "*.parquet")
    friday = stored2.filter(pl.col("date") == FRIDAY)
    assert friday.height == 2
    assert friday.filter(pl.col("symbol") == "600001.SH")["close"][0] == 20.0
