"""分区唯一性判据: 「同一分区内不许两个文件含相同的 (date,symbol)」。

为什么不是「文件数 == 1」: `part-stream-{i}.parquet` 是设计内的分块命名, 一次
入库被切成多块本就该多文件。判据只拦**会双计**的那一支 —— 同一只票同一天被
读两次。

为什么必须落在读取时刻: `l1_cohort` 的全 panel 判据只有两份文件 symbol 高度
重叠时才响; 若多出来的是一整份新快照(symbol 不重叠), 面板会静默多出几百只
而名单照常出表。下面最后一个用例把这个**已知盲区**显式钉住, 防止有人以为
这条守卫已经覆盖了它。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.decision import bars

DAY = date(2026, 11, 5)
TABLE = "kline_daily_enriched"


def _frame(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": symbols,
            "date": [DAY] * len(symbols),
            "close": [10.0] * len(symbols),
            "raw_close": [10.0] * len(symbols),
        }
    )


def _partition(tmp_path, files: dict[str, list[str]]) -> "object":
    part = tmp_path / TABLE / f"date={DAY.isoformat()}"
    part.mkdir(parents=True)
    for name, symbols in files.items():
        _frame(symbols).write_parquet(part / name)
    return tmp_path


def test_chunked_files_are_not_duplicates(tmp_path):
    """分块入库是设计内的: 两个文件、symbol 不相交 ⇒ 正常返回两行。"""
    data_dir = _partition(
        tmp_path, {"part-stream-0.parquet": ["000001.SZ"], "part-stream-1.parquet": ["600000.SH"]}
    )
    df = bars.day_rows(data_dir, DAY.isoformat())
    assert sorted(df["symbol"].to_list()) == ["000001.SZ", "600000.SH"]


def test_same_symbol_in_two_files_raises(tmp_path):
    """staging 残留: 同一只票在 part.parquet 与 part-stream-0.parquet 各一份。"""
    data_dir = _partition(
        tmp_path,
        {"part.parquet": ["000001.SZ", "600000.SH"], "part-stream-0.parquet": ["000001.SZ"]},
    )
    with pytest.raises(bars.DuplicateBarsError) as exc:
        bars.day_rows(data_dir, DAY.isoformat())

    msg = str(exc.value)
    assert "000001.SZ" in msg          # 指出是哪只票, 不是"有重复"
    assert "part-stream-0.parquet" in msg  # 指出是哪份文件, 少一次人工定位
    assert DAY.isoformat() in msg


def test_symbol_subset_filter_still_detects_duplicate(tmp_path):
    """只取一只票时也必须响 —— 下游按 symbol 过滤是最常见的调用方式。"""
    data_dir = _partition(
        tmp_path, {"part.parquet": ["000001.SZ"], "part-stream-0.parquet": ["000001.SZ"]}
    )
    with pytest.raises(bars.DuplicateBarsError):
        bars.day_rows(data_dir, DAY.isoformat(), symbols=["000001.SZ"])


def test_per_file_counts_cover_all_duplicates_not_just_the_sample(tmp_path):
    """消息里只展示 6 只, 但逐文件行数必须按全部重复 symbol 统计。

    这条钉住的是自己踩过的坑: 只数样本会把"两份各含 5570 行的整日文件"
    显示成 "6 行", 读的人会以为文件本身只有 6 行, 从而删错文件。
    """
    symbols = [f"00000{i}.SZ" for i in range(1, 9)]  # 8 只 > 展示上限 6
    data_dir = _partition(tmp_path, {"part.parquet": symbols, "part-copy.parquet": symbols})
    with pytest.raises(bars.DuplicateBarsError) as exc:
        bars.day_rows(data_dir, DAY.isoformat())
    assert "-> 8 行重复" in str(exc.value)
    assert "-> 6 行重复" not in str(exc.value)


def test_missing_partition_returns_empty(tmp_path):
    """缺分区是"没有数据", 不是"数据坏了" —— 不许把口径未定当异常。"""
    df = bars.day_rows(tmp_path, DAY.isoformat())
    assert df.is_empty()


def test_single_row_partition_is_fine(tmp_path):
    data_dir = _partition(tmp_path, {"part.parquet": ["000001.SZ"]})
    assert bars.day_rows(data_dir, DAY.isoformat()).height == 1


def test_disjoint_extra_snapshot_is_not_this_guard_s_business(tmp_path):
    """已知盲区(显式钉住): 多出来的整份快照 symbol 不重叠 ⇒ 唯一性判据不响。

    这不是缺陷复现, 是划界: 该情形要靠"分区 symbol 集合 ⊄ 当日宇宙"那条判据,
    目前没有。谁把这条测试改成 raises, 说明要么同时补了那条判据, 要么把
    "多文件" 误当成了 "重复"。
    """
    data_dir = _partition(
        tmp_path, {"part.parquet": ["000001.SZ"], "part-snapshot.parquet": ["300750.SZ"]}
    )
    df = bars.day_rows(data_dir, DAY.isoformat())
    assert sorted(df["symbol"].to_list()) == ["000001.SZ", "300750.SZ"]
