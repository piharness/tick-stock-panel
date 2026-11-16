"""dtype 漂移自检的判据: 只判 dtype, 不判"多列/缺列"。

2026-11-13 的事故形态: 一个分区的 `quote_ts` 写成 double, 其余 int64 ⇒
`scan_enriched_parquet`(显式 schema) 整表抛 `SchemaError` ⇒ 读路径吞成空表 ⇒
端点改从上游拉数。自检要在**启动时**就把它喊出来, 而不是等用户抱怨"第一张图很慢"。

钉住四件事:
1. dtype 不符 → 报出来, 且带表名/分区/列/期望/实际(可直接照着修);
2. 多列 → 不报(`extra_columns="ignore"` 是契约);
3. 缺列 → 不报(`union_by_name` / `missing_columns="insert"` 容忍, 记忆 #37);
4. 月分区按**覆盖区间右端**排序, 不会被字典序挤到检查窗口外(记忆 #25)。
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from app.parquet import ENRICHED_STORAGE_SCHEMA
from app.services.schema_drift import Drift, check_recent, describe_drifts


def _write(root, dirname: str, *, quote_ts: pl.DataType = pl.Int64, extra: bool = False,
           drop: str | None = None) -> None:
    d = root / "kline_daily_enriched" / dirname
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "symbol": ["000001.SZ"], "date": [dt.date(2026, 11, 5)],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0], "raw_close": [1.0],
        "raw_high": [1.0], "raw_low": [1.0], "turnover_rate": [0.5],
        "consecutive_limit_ups": pl.Series([0], dtype=pl.UInt32),
        "consecutive_limit_downs": pl.Series([0], dtype=pl.UInt32),
        "quote_ts": pl.Series([1793874982000], dtype=quote_ts),
    }
    if drop:
        data.pop(drop)
    if extra:
        data["brand_new_col"] = [1]
    pl.DataFrame(data).write_parquet(d / "part.parquet")


def test_dtype_mismatch_is_reported_with_actionable_fields(tmp_path):
    _write(tmp_path, "date=2026-11-05", quote_ts=pl.Float64)
    drifts = check_recent(tmp_path, days=10)

    assert len(drifts) == 1
    d = drifts[0]
    assert isinstance(d, Drift)
    assert d.table == "kline_daily_enriched"
    assert "date=2026-11-05" in d.file
    assert d.column == "quote_ts"
    assert d.expected == "Int64"
    assert d.actual == "Float64"
    assert "quote_ts" in describe_drifts(drifts)


def test_clean_partitions_report_nothing(tmp_path):
    _write(tmp_path, "date=2026-11-05")
    _write(tmp_path, "date=2026-11-06")
    assert check_recent(tmp_path, days=10) == []


def test_extra_column_is_not_a_drift(tmp_path):
    """多列由 extra_columns="ignore" 忽略 —— 把它算漂移会让判据永远红。"""
    _write(tmp_path, "date=2026-11-05", extra=True)
    assert check_recent(tmp_path, days=10) == []


def test_missing_column_is_not_a_drift(tmp_path):
    """缺列由 union_by_name=true 容忍(记忆 #37: 11-12 的 etf 分区少一列)。"""
    _write(tmp_path, "date=2026-11-05", drop="consecutive_limit_ups")
    assert check_recent(tmp_path, days=10) == []


def test_integer_column_against_float_contract_is_not_a_drift(tmp_path):
    """`kline_daily.volume` 全表存 Int64, 而契约写 Float64 —— allow-float 拓宽, 不是漂移。

    第一版判据没放行这条, 在真实数据上报了 362 条假阳性; 永远红的判据等于没有。
    """
    d = tmp_path / "kline_daily" / "date=2026-11-05"
    d.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ"], "date": [dt.date(2026, 11, 5)],
        "volume": pl.Series([100], dtype=pl.Int64),
        "amount": pl.Series([1000], dtype=pl.Int64),
    }).write_parquet(d / "part.parquet")
    assert check_recent(tmp_path, days=10) == []


def test_non_numeric_dtype_still_counts_as_drift(tmp_path):
    """放行的只有 int→float 拓宽; 字符串冒充数值必须报。"""
    d = tmp_path / "kline_daily" / "date=2026-11-05"
    d.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ"], "date": [dt.date(2026, 11, 5)],
        "volume": pl.Series(["100"], dtype=pl.Utf8),
    }).write_parquet(d / "part.parquet")
    drifts = check_recent(tmp_path, days=10)
    assert [d.column for d in drifts] == ["volume"]


def test_month_partition_is_not_pushed_out_of_the_window(tmp_path):
    """月分区名 `date=2026-11` 字典序在 `date=2026-11-01` 之前, 必须按区间右端排序。"""
    for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
        _write(tmp_path, f"date={day}")
    _write(tmp_path, "date=2026-11", quote_ts=pl.Float64)   # 最新月份的漂移

    drifts = check_recent(tmp_path, days=4)   # 窗口只容 4 个分区, 月分区必须在内
    assert [d.column for d in drifts] == ["quote_ts"]


def test_window_limits_work(tmp_path):
    _write(tmp_path, "date=2026-11-01", quote_ts=pl.Float64)   # 老分区
    for day in ("2026-11-10", "2026-11-11", "2026-11-12"):
        _write(tmp_path, f"date={day}")

    assert check_recent(tmp_path, days=2) == []                 # 只看最近两个
    assert len(check_recent(tmp_path, days=None)) == 1          # 全表能看到
