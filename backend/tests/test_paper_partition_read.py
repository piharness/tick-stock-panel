"""模拟盘取价必须按分区定位，不许扫全树。

回归动机（2026-11-20）：`read_daily_bar` / `_prev_close` / `_index_close` /
`api._last_close` 四处都是
`scan_daily_parquet(<table>/**/*.parquet).filter(symbol & date)`。
`kline_daily` 迁到月分区后有 246 个月分区 + 344 个旧日分区，polars 展开 glob 时要
逐个打开文件读 footer（容器内全表 schema 16.8s），**一次成交结算只需要一个分区**。

判据不是计时（CI 上没有稳定阈值），而是**这次调用碰到了哪些文件**：
把 `scan_daily_parquet` 换成记录实参的探针，断言它只拿目标月的分区。
旧实现传的是 `.../kline_daily/**/*.parquet` 这个 glob 字符串，一测就红。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.strategy import paper

SYM = "600519.SH"
OTHER = "000001.SZ"


def _frame(symbol: str, rows: list[tuple[date, float, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol] * len(rows),
        "date": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [max(r[1], r[2]) for r in rows],
        "low": [min(r[1], r[2]) for r in rows],
        "close": [r[2] for r in rows],
        "volume": [10_000.0] * len(rows),
        "amount": [r[2] * 10_000.0 for r in rows],
    })


def _write_month(data: Path, label: str, frames: list[pl.DataFrame]) -> None:
    out = data / "kline_daily" / f"date={label}"
    out.mkdir(parents=True, exist_ok=True)
    pl.concat(frames).write_parquet(out / "part.parquet")


def _probe(monkeypatch) -> list[str]:
    """记录每次 scan 收到的文件/glob 实参。"""
    seen: list[str] = []
    real = paper.scan_daily_parquet

    def spy(src, **kw):
        seen.extend([str(s) for s in src] if isinstance(src, list) else [str(src)])
        return real(src, **kw)

    monkeypatch.setattr(paper, "scan_daily_parquet", spy)
    return seen


def test_read_daily_bar_touches_only_the_target_month(tmp_path: Path, monkeypatch):
    """单日取价只读该日所在月的分区。"""
    data = tmp_path / "data"
    _write_month(data, "2026-10", [_frame(SYM, [(date(2026, 10, 30), 10.0, 10.0)])])
    _write_month(data, "2026-11", [
        _frame(SYM, [(date(2026, 11, 2), 11.0, 11.5), (date(2026, 11, 3), 11.6, 11.8)])
    ])

    seen = _probe(monkeypatch)
    bar = paper.read_daily_bar(data, SYM, "stock", "2026-11-03")

    assert bar == {"open": 11.6, "close": 11.8}
    assert seen, "至少应读一个分区"
    assert all("date=2026-11" in s for s in seen), f"读到了目标月之外的文件: {seen}"


def test_prev_close_crosses_month_partition(tmp_path: Path):
    """前收：同月取前一日，跨月退回上个月分区。"""
    data = tmp_path / "data"
    _write_month(data, "2026-10", [
        _frame(SYM, [(date(2026, 10, 29), 9.0, 9.5), (date(2026, 10, 30), 9.8, 10.0)])
    ])
    _write_month(data, "2026-11", [
        _frame(SYM, [(date(2026, 11, 2), 11.0, 11.0), (date(2026, 11, 3), 11.6, 11.8)])
    ])

    assert paper._prev_close(data, SYM, "stock", "2026-11-03") == pytest.approx(11.0)
    # 11-02 的前一日在 10 月分区里
    assert paper._prev_close(data, SYM, "stock", "2026-11-02") == pytest.approx(10.0)


def test_prev_close_falls_back_when_symbol_suspended(tmp_path: Path):
    """本月分区里没有这只票（停牌）时，必须退回上个月分区，而不是返回 None。"""
    data = tmp_path / "data"
    _write_month(data, "2026-10", [
        _frame(SYM, [(date(2026, 10, 29), 9.0, 9.5), (date(2026, 10, 30), 9.8, 10.0)])
    ])
    # 11 月只有另一只票
    _write_month(data, "2026-11", [
        _frame(OTHER, [(date(2026, 11, 2), 20.0, 20.0), (date(2026, 11, 3), 20.0, 21.0)])
    ])

    assert paper._prev_close(data, SYM, "stock", "2026-11-05") == pytest.approx(10.0)
    # 该票在 11 月确实没有 bar
    assert paper.read_daily_bar(data, SYM, "stock", "2026-11-03") is None


def test_missing_day_directories_do_not_leak_neighbour_month(tmp_path: Path):
    """只查当天：目标月里没有这一天就返回 None，不能拿别的日期顶替。"""
    data = tmp_path / "data"
    _write_month(data, "2026-11", [_frame(SYM, [(date(2026, 11, 3), 11.6, 11.8)])])

    assert paper.read_daily_bar(data, SYM, "stock", "2026-11-03") is not None
    assert paper.read_daily_bar(data, SYM, "stock", "2026-11-04") is None
    assert paper.read_daily_bar(data, SYM, "stock", "2026-12-01") is None
