"""事件表覆盖读数：空洞连段、声明范围、以及「0 行」与「判不出」的分界。

这些用例只用合成日历与合成的 `adj_factor/all.parquet`，不碰真实行情目录：
读数函数是纯函数（表 + 日历 → 读数），真实数据的值会随同步漂移，钉它没有意义。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.decision import bars

SYM = "000001.SZ"


def _table(tmp_path: Path, rows: list[tuple[str, str]]) -> None:
    """写一张最小的除权事件表：`(symbol, trade_date)` 列表。"""
    path = tmp_path / "adj_factor"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "trade_date": [date.fromisoformat(r[1]) for r in rows],
        "ex_factor": [1.01] * len(rows),
    }).write_parquet(path / "all.parquet")


CAL = ["2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13"]


def test_gap_runs_are_cut_by_sessions_not_by_calendar_distance(tmp_path: Path):
    """两个空洞之间夹着一个有事件的日子 → 两段，不是一段。

    03-10 与 03-12 只差两个自然日，若按「日期接近就并段」切，会并成一段两天；
    按交易日切则是两个各自一天的空洞。同一份 21 个空档，这两种切法给出
    「9 段最长 6 天」与「12 段最长 5 天」两套数——段长是读数，切法是定义，
    定义必须跟着数据一起走，否则读的人无法还原。
    """
    _table(tmp_path, [(SYM, "2026-03-09"), (SYM, "2026-03-11"), (SYM, "2026-03-13")])
    cov = bars.ex_coverage(tmp_path, CAL)
    assert cov is not None
    assert cov["gap_runs"] == [["2026-03-10", "2026-03-10"], ["2026-03-12", "2026-03-12"]]
    assert cov["gap_sessions"] == 2
    assert cov["gap_max_run"] == 1


def test_gap_runs_merge_only_across_adjacent_sessions(tmp_path: Path):
    """连续两个交易日都没有事件 → 一个两天的段。"""
    _table(tmp_path, [(SYM, "2026-03-09"), (SYM, "2026-03-12"), (SYM, "2026-03-13")])
    cov = bars.ex_coverage(tmp_path, CAL)
    assert cov is not None
    assert cov["gap_runs"] == [["2026-03-10", "2026-03-11"]]
    assert cov["gap_max_run"] == 2


def test_days_before_the_table_start_are_not_counted_as_gaps(tmp_path: Path):
    """表首晚于日历首：那两天归 `endpoints_span_calendar` 管，不再另外记成空洞。

    否则同一个事实被数两遍（「表没盖住」既进端点判据又进空洞计数），而两处的
    修法完全不同——前者要去补表，后者要去补同步。这里表从 03-11 起，所以
    03-09 / 03-10 不是空洞；03-12 是。
    """
    _table(tmp_path, [(SYM, "2026-03-11"), (SYM, "2026-03-13")])
    cov = bars.ex_coverage(tmp_path, CAL)
    assert cov is not None
    assert cov["ex_table_first"] == "2026-03-11"
    assert cov["gap_runs"] == [["2026-03-12", "2026-03-12"]]
    assert cov["gap_sessions"] == 1


def test_future_dated_rows_are_counted_but_are_not_a_gap(tmp_path: Path):
    """晚于日历末日的行单独计数，不从空洞里减——它是另一个方向的事实。"""
    _table(tmp_path, [(SYM, d) for d in CAL] + [(SYM, "2026-03-20"), ("000002.SZ", "2026-03-25")])
    cov = bars.ex_coverage(tmp_path, CAL)
    assert cov is not None
    assert cov["gap_sessions"] == 0
    assert cov["future_dated_rows"] == 2
    assert cov["ex_rows"] == 7


def test_a_missing_table_is_unjudgeable_not_an_empty_one(tmp_path: Path):
    """缺表返回 None。空表也返回 None：它的表首表末不存在，读数无从谈起。"""
    assert bars.ex_coverage(tmp_path, CAL) is None
    _table(tmp_path, [])
    assert bars.ex_coverage(tmp_path, CAL) is None


def test_an_empty_calendar_is_unjudgeable(tmp_path: Path):
    """没有日历就没有「洞」这个概念。返回 0 会被读成「一个洞都没有」。"""
    _table(tmp_path, [(SYM, "2026-03-09")])
    assert bars.ex_coverage(tmp_path, []) is None


def test_window_readings_keep_zero_and_unjudgeable_apart(tmp_path: Path):
    """窗口判得出而没有事件 → 0；窗口判不出 → None。两者不能都写成 0。"""
    _table(tmp_path, [(SYM, "2026-03-05"), (SYM, "2026-03-13")])

    # 窗口在表的声明范围内、该标的没有事件 → 0（「确实没有除权」）。
    none_events = bars.ex_window_readings(tmp_path, SYM, "2026-03-09", "2026-03-11")
    assert none_events == {"ex_table_first": "2026-03-05",
                           "ex_table_last": "2026-03-13",
                           "window_ex_events": 0}

    # 同一窗口，表里根本没有的标的 → 仍是 0，不是 None：判的是窗口，不是标的。
    other = bars.ex_window_readings(tmp_path, "000002.SZ", "2026-03-09", "2026-03-11")
    assert other["window_ex_events"] == 0

    # 窗口晚于表末：判不出，行数必须是 None。
    late = bars.ex_window_readings(tmp_path, SYM, "2026-03-12", "2026-03-20")
    assert late["window_ex_events"] is None
    assert late["ex_table_last"] == "2026-03-13"

    # 窗口整体早于表首：等下去也不会变（表不向后生长），但行数同样不可知。
    early = bars.ex_window_readings(tmp_path, SYM, "2026-03-01", "2026-03-04")
    assert early["window_ex_events"] is None

    # 缺表：端点与行数都判不出。
    empty_dir = tmp_path / "nowhere"
    empty_dir.mkdir()
    missing = bars.ex_window_readings(empty_dir, SYM, "2026-03-09", "2026-03-11")
    assert missing == {"ex_table_first": None, "ex_table_last": None,
                       "window_ex_events": None}
