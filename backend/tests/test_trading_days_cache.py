"""交易日历的缓存必须真的命中，且必须真的失效。

回归动机（2026-11-20）：`kline_trading_days` 对 `kline_daily` 的 246 个月分区逐个
`scan_parquet` 读 `date` 列，容器内 19.5s/次。`get_dragon_tiger` 一次请求调两遍
⇒ 38.5s，顶穿前端 30s 超时。

判据不是「变快了」，而是两件可观测的事：
  1. 数据不变时**不再重扫**（否则缓存等于装饰）；
  2. 新增分区后**必须重扫**（否则缓存会永远返回旧日历，比慢更贵）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.services import trading_days as td


def _make_days(data: Path, labels: list[str]) -> None:
    for label in labels:
        (data / "kline_daily" / f"date={label}").mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _clear_cache():
    td._DAYS_CACHE.clear()
    yield
    td._DAYS_CACHE.clear()


def _spy(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = td.list_partition_dates

    def wrapper(base):
        calls.append(1)
        return real(base)

    monkeypatch.setattr(td, "list_partition_dates", wrapper)
    return calls


def test_repeated_calls_do_not_rescan(tmp_path: Path, monkeypatch):
    _make_days(tmp_path, ["2026-11-18", "2026-11-19"])
    calls = _spy(monkeypatch)

    first = td.kline_trading_days(tmp_path)
    second = td.kline_trading_days(tmp_path)

    assert first == second == [date(2026, 11, 18), date(2026, 11, 19)]
    assert len(calls) == 1, "第二次调用不应重扫"


def test_new_partition_invalidates_cache(tmp_path: Path, monkeypatch):
    _make_days(tmp_path, ["2026-11-18", "2026-11-19"])
    calls = _spy(monkeypatch)

    assert len(td.kline_trading_days(tmp_path)) == 2
    _make_days(tmp_path, ["2026-11-20"])
    after = td.kline_trading_days(tmp_path)

    assert after[-1] == date(2026, 11, 20), "新增分区后必须看到新日期"
    assert len(calls) == 2, "新增分区必须触发重扫"


def test_monthly_partition_mtime_invalidates(tmp_path: Path, monkeypatch):
    """当月继续写入（目录名不变）也要失效 —— 否则缓存会停在月初的日历上。"""
    _make_days(tmp_path, ["2026-10-30"])
    (tmp_path / "kline_daily" / "date=2026-11").mkdir(parents=True)
    calls = _spy(monkeypatch)

    td.kline_trading_days(tmp_path)
    assert len(calls) == 1

    # 只改月分区目录的 mtime，目录名集合不变
    import os

    os.utime(tmp_path / "kline_daily" / "date=2026-11", (1, 2_000_000_000))
    td.kline_trading_days(tmp_path)

    assert len(calls) == 2, "月分区 mtime 变化必须触发重扫"


def test_returned_list_is_a_copy(tmp_path: Path):
    """调用方会就地排序/裁剪，共享同一个 list 会把缓存改坏。"""
    _make_days(tmp_path, ["2026-11-18", "2026-11-19"])

    first = td.kline_trading_days(tmp_path)
    first.sort(reverse=True)
    second = td.kline_trading_days(tmp_path)

    assert second == [date(2026, 11, 18), date(2026, 11, 19)]


def test_missing_table_is_empty_not_error(tmp_path: Path):
    assert td.kline_trading_days(tmp_path) == []


# ── 磁盘快照 ────────────────────────────────────────────────────


def test_snapshot_avoids_rescan_across_restart(tmp_path: Path, monkeypatch):
    """进程重启（内存缓存清空）后应从磁盘快照恢复，不再扫 246 个月分区。

    这是「容器重启后首次打开龙虎榜」那条路径：没有快照就要等 19.5s（或等
    启动预热算完）。
    """
    _make_days(tmp_path, ["2026-11-18", "2026-11-19"])
    calls = _spy(monkeypatch)

    assert td.kline_trading_days(tmp_path) == [date(2026, 11, 18), date(2026, 11, 19)]
    assert len(calls) == 1
    assert td.snapshot_path(tmp_path).exists(), "首次计算后应落盘"

    td._DAYS_CACHE.clear()  # 模拟进程重启：内存没了，磁盘还在
    again = td.kline_trading_days(tmp_path)

    assert again == [date(2026, 11, 18), date(2026, 11, 19)]
    assert len(calls) == 1, "快照命中时不应重扫分区"


def test_snapshot_invalidated_by_new_partition(tmp_path: Path, monkeypatch):
    """快照必须跟着数据失效 —— 否则「快」换来的是「永远返回旧日历」。"""
    _make_days(tmp_path, ["2026-11-18", "2026-11-19"])
    calls = _spy(monkeypatch)
    td.kline_trading_days(tmp_path)

    td._DAYS_CACHE.clear()
    _make_days(tmp_path, ["2026-11-20"])
    after = td.kline_trading_days(tmp_path)

    assert after[-1] == date(2026, 11, 20)
    assert len(calls) == 2, "指纹变了必须重算"


def test_corrupt_snapshot_is_rebuilt_not_fatal(tmp_path: Path, monkeypatch):
    _make_days(tmp_path, ["2026-11-18", "2026-11-19"])
    calls = _spy(monkeypatch)
    td.kline_trading_days(tmp_path)

    snap = td.snapshot_path(tmp_path)
    snap.write_text("{ 这不是 JSON", encoding="utf-8")
    td._DAYS_CACHE.clear()

    days = td.kline_trading_days(tmp_path)
    assert days == [date(2026, 11, 18), date(2026, 11, 19)]
    assert len(calls) == 2, "快照损坏应重算而不是抛错或返回空"
    # 重算后必须覆盖成合法快照，否则每次都要重算
    import json

    assert json.loads(snap.read_text(encoding="utf-8"))["version"] == td.SNAPSHOT_VERSION


def test_snapshot_is_outside_partition_globs(tmp_path: Path):
    """快照不许落进 `kline_daily/` 树里 —— 那会被 `date=*` / `**/*.parquet` 扫到。"""
    _make_days(tmp_path, ["2026-11-19"])
    td.kline_trading_days(tmp_path)

    snap = td.snapshot_path(tmp_path)
    assert "kline_daily" not in snap.relative_to(tmp_path).parts
    assert snap.parent.name == "trading_days"


# ── 写入方通知：增量合并，不重扫 ─────────────────────────────────
#
# 回归动机（2026-11-24）：指纹包含「最新月分区 mtime」，而盘后管道每个交易日
# 都会重写当月分区 ⇒ 指纹必变 ⇒ 19.5s 全表重扫，且落在写入后第一个读请求上。
# 启动预热只能救「进程刚起来」那一次，救不了每天这一次。


def _write_month(data: Path, month: str, days: list[str]) -> None:
    """写真实月分区（part.parquet 里带 date 列），并且**按仓库的写法**落盘。

    必须 tmp + os.replace：指纹盯的是**目录** mtime（新文件进目录会更新它），
    而直接 `write_parquet` 原地改写只动文件 mtime、不动目录 mtime —— 用后者
    构造用例会得到一个指纹看不出变化的假场景，测出来的结论在生产里不成立。
    """
    import os

    import polars as pl

    part = data / "kline_daily" / f"date={month}"
    part.mkdir(parents=True, exist_ok=True)
    out = part / "part.parquet"
    tmp = out.with_name(out.name + ".tmp")
    pl.DataFrame(
        {
            "symbol": ["600001.SH"] * len(days),
            "date": [date.fromisoformat(d) for d in days],
            "close": [10.0] * len(days),
        }
    ).write_parquet(tmp)
    os.replace(tmp, out)


def test_note_partition_dates_avoids_full_rescan(tmp_path: Path, monkeypatch):
    """已预热 → 写入方通知 → 新日期立刻可见且**不重扫**。"""
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    assert td.kline_trading_days(tmp_path) == [date(2026, 11, 19)]

    calls = _spy(monkeypatch)
    _write_month(tmp_path, "2026-11", ["2026-11-19", "2026-11-24"])
    assert td.note_partition_dates(tmp_path, "kline_daily", [date(2026, 11, 24)]) is True

    assert td.kline_trading_days(tmp_path) == [date(2026, 11, 19), date(2026, 11, 24)]
    assert calls == [], "写入方已告知日期，必须命中，不得再全表重扫 19s"


def test_without_note_append_would_rescan(tmp_path: Path, monkeypatch):
    """对照组：不做通知时，当月写入会让指纹失效并触发重扫。

    这条证明「通知」不是装饰 —— 没有它，每天盘后都会付一次 19.5s。
    """
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    assert td.kline_trading_days(tmp_path) == [date(2026, 11, 19)]

    calls = _spy(monkeypatch)
    _write_month(tmp_path, "2026-11", ["2026-11-19", "2026-11-24"])
    days = td.kline_trading_days(tmp_path)

    assert days == [date(2026, 11, 19), date(2026, 11, 24)]
    assert len(calls) == 1, "未通知时必须（也只能）重扫一次，内容仍然正确"


def test_note_without_warm_baseline_is_declined(tmp_path: Path):
    """没有内存底稿就不猜：进程刚起、预热未完成时返回 False，交给正常路径。"""
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    assert td.note_partition_dates(tmp_path, "kline_daily", [date(2026, 11, 24)]) is False


def test_note_ignores_non_monthly_tables(tmp_path: Path):
    """日分区表的日期就是目录名，不需要维护第二份索引。"""
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    td.kline_trading_days(tmp_path)
    assert td.note_partition_dates(tmp_path, "kline_daily_enriched", [date(2026, 11, 24)]) is False


def test_note_with_no_new_days_is_noop(tmp_path: Path):
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    td.kline_trading_days(tmp_path)
    assert td.note_partition_dates(tmp_path, "kline_daily", [date(2026, 11, 19)]) is False


# ── 写入方通知：增量合并，不重扫 ─────────────────────────────────
#
# 回归动机（2026-11-24）：指纹包含「最新月分区 mtime」，而盘后管道每个交易日
# 都会重写当月分区 ⇒ 指纹必变 ⇒ 19.5s 全表重扫，且落在写入后第一个读请求上。
# 启动预热只能救「进程刚起来」那一次，救不了每天这一次。


def _write_month(data: Path, month: str, days: list[str]) -> None:
    """写真实月分区（part.parquet 里带 date 列），并且**按仓库的写法**落盘。

    必须 tmp + os.replace：指纹盯的是**目录** mtime（新文件进目录会更新它），
    而直接 `write_parquet` 原地改写只动文件 mtime、不动目录 mtime —— 用后者
    构造用例会得到一个指纹看不出变化的假场景，测出来的结论在生产里不成立。
    """
    import os

    import polars as pl

    part = data / "kline_daily" / f"date={month}"
    part.mkdir(parents=True, exist_ok=True)
    out = part / "part.parquet"
    tmp = out.with_name(out.name + ".tmp")
    pl.DataFrame(
        {
            "symbol": ["600001.SH"] * len(days),
            "date": [date.fromisoformat(d) for d in days],
            "close": [10.0] * len(days),
        }
    ).write_parquet(tmp)
    os.replace(tmp, out)


def test_note_partition_dates_avoids_full_rescan(tmp_path: Path, monkeypatch):
    """已预热 → 写入方通知 → 新日期立刻可见且**不重扫**。"""
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    assert td.kline_trading_days(tmp_path) == [date(2026, 11, 19)]

    calls = _spy(monkeypatch)
    _write_month(tmp_path, "2026-11", ["2026-11-19", "2026-11-24"])
    assert td.note_partition_dates(tmp_path, "kline_daily", [date(2026, 11, 24)]) is True

    assert td.kline_trading_days(tmp_path) == [date(2026, 11, 19), date(2026, 11, 24)]
    assert calls == [], "写入方已告知日期，必须命中，不得再全表重扫 19s"


def test_without_note_append_would_rescan(tmp_path: Path, monkeypatch):
    """对照组：不做通知时，当月写入会让指纹失效并触发重扫。

    这条证明「通知」不是装饰 —— 没有它，每天盘后都会付一次 19.5s。
    """
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    assert td.kline_trading_days(tmp_path) == [date(2026, 11, 19)]

    calls = _spy(monkeypatch)
    _write_month(tmp_path, "2026-11", ["2026-11-19", "2026-11-24"])
    days = td.kline_trading_days(tmp_path)

    assert days == [date(2026, 11, 19), date(2026, 11, 24)]
    assert len(calls) == 1, "未通知时必须（也只能）重扫一次，内容仍然正确"


def test_note_without_warm_baseline_is_declined(tmp_path: Path):
    """没有内存底稿就不猜：进程刚起、预热未完成时返回 False，交给正常路径。"""
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    assert td.note_partition_dates(tmp_path, "kline_daily", [date(2026, 11, 24)]) is False


def test_note_ignores_non_monthly_tables(tmp_path: Path):
    """日分区表的日期就是目录名，不需要维护第二份索引。"""
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    td.kline_trading_days(tmp_path)
    assert td.note_partition_dates(tmp_path, "kline_daily_enriched", [date(2026, 11, 24)]) is False


def test_note_with_no_new_days_is_noop(tmp_path: Path):
    _write_month(tmp_path, "2026-11", ["2026-11-19"])
    td.kline_trading_days(tmp_path)
    assert td.note_partition_dates(tmp_path, "kline_daily", [date(2026, 11, 19)]) is False
