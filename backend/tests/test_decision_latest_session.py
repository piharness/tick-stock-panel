"""`latest_session` 只读尾部分区 —— 但必须给对日期。

它曾经是 `sessions()[-1]`：对月分区表要 `list_partition_dates()`，608 个分区
逐个读 `date` 列，容器内实测 18.36s。这个数字落在 `/api/decision/overview`
首屏上，且 `gates.evaluate()` 与 `check_k4_fresh()` 各调一次 ⇒ 页面 65s。

优化的**唯一**允许的代价是少读分区，不是少看见日期。所以下面两条是主线：

1. 结尾那个 `date=YYYY-MM` 月分区必须真读 —— 它在字符串序上排在
   `date=YYYY-MM-13` 之前，但数据可能最晚（声明区间到月末）；
2. 停的条件是「月份早于当前最优」，不是「读了 N 个」——否则某个月只有
   月分区时会提前停，答案退回上个月。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.decision import bars

TABLE = "kline_daily"


def _day_frame(days: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["000001.SZ"] * len(days),
            "date": days,
            "close": [10.0] * len(days),
            "raw_close": [10.0] * len(days),
        }
    )


def _write(part_dir, days: list[str]) -> None:
    part_dir.mkdir(parents=True, exist_ok=True)
    _day_frame(days).write_parquet(part_dir / "part-0.parquet")


def test_reads_trailing_month_partition(tmp_path):
    """月分区里的最晚日必须赢过同日分区 —— 这是目录名排序的盲区。"""
    _write(tmp_path / TABLE / "date=2026-10-30", ["2026-10-30"])
    _write(tmp_path / TABLE / "date=2026-11", ["2026-11-02", "2026-11-20"])
    _write(tmp_path / TABLE / "date=2026-11-13", ["2026-11-13"])

    assert bars.latest_session(tmp_path, TABLE) == "2026-11-20"


def test_agrees_with_full_enumeration(tmp_path):
    """与 `sessions()[-1]` 逐位相同 —— 两个函数不许给出两个「最新交易日」。"""
    _write(tmp_path / TABLE / "date=2026-10", ["2026-10-08", "2026-10-30"])
    _write(tmp_path / TABLE / "date=2026-11", ["2026-11-02", "2026-11-13"])
    _write(tmp_path / TABLE / "date=2026-11-12", ["2026-11-12"])
    _write(tmp_path / TABLE / "date=2026-11-13", ["2026-11-13"])

    assert bars.latest_session(tmp_path, TABLE) == bars.sessions(tmp_path, TABLE)[-1]


def test_partial_trailing_month_falls_back_to_previous_month(tmp_path):
    """末月只有月分区、且数据没到月末：停的条件不能把答案卡在上个月。"""
    _write(tmp_path / TABLE / "date=2026-09", ["2026-09-30"])
    _write(tmp_path / TABLE / "date=2026-10", ["2026-10-09"])

    assert bars.latest_session(tmp_path, TABLE) == "2026-10-09"


def test_does_not_enumerate_history(tmp_path, monkeypatch):
    """分区数与解析次数解耦：50 个历史月分区只允许读最后 1–2 个。"""
    for year, month in [(2020, 1), (2021, 6), (2022, 6), (2023, 6), (2024, 6)]:
        _write(tmp_path / TABLE / f"date={year}-{month:02d}", [f"{year}-{month:02d}-09"])
    _write(tmp_path / TABLE / "date=2026-11", ["2026-11-13"])

    calls: list[str] = []
    original = bars.partition_dates

    def _counted(part):
        calls.append(part.name)
        return original(part)

    monkeypatch.setattr(bars, "partition_dates", _counted)

    assert bars.latest_session(tmp_path, TABLE) == "2026-11-13"
    assert len(calls) <= 3, f"解析了 {len(calls)} 个分区，尾部扫描没有停下来：{calls}"


def test_missing_table_returns_none(tmp_path):
    assert bars.latest_session(tmp_path, TABLE) is None
    assert bars.latest_session(tmp_path / "nope", TABLE) is None


def test_empty_day_partition_matches_sessions(tmp_path):
    """空日分区仍算那一天 —— 与 `sessions()` 的既有语义一致。

    目录名是日分区的日期来源（零 parquet 扫描），所以「目录在、文件没落」会被
    读成「那天有数据」。这是既有语义，不在本次优化里改：`K4` 的新鲜度判定
    依赖同一分支，两边必须同时改，否则会造出第二种「最新交易日」。
    """
    _write(tmp_path / TABLE / "date=2026-11-13", ["2026-11-13"])
    (tmp_path / TABLE / "date=2026-11-16").mkdir(parents=True)

    assert bars.latest_session(tmp_path, TABLE) == "2026-11-16"
    assert bars.latest_session(tmp_path, TABLE) == bars.sessions(tmp_path, TABLE)[-1]
