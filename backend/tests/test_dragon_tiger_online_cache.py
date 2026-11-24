"""回归测试: 当日龙虎榜的进程内缓存。

回归动机（2026-11-24）：历史日走磁盘缓存，但**当日不落盘**（`actual < today` 才写），
所以从收盘到 `pull_latest` 增量任务跑完这段窗口里，每个请求都要实时问 provider：
容器内实测 **158ms/次**，而账本上这个端点一周被调 113 次、页面是 15s 级轮询。

判据：
  1. 同日同目录在 TTL 内只拉一次（否则缓存等于装饰）；
  2. TTL 过期必须重新拉（当日榜单在发布时刻会变，不能永远给旧值）；
  3. 不同日期不共用条目；
  4. 历史日仍走磁盘缓存，不该被这个内存缓存改变行为。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.plugins.fuyao.client import FuyaoError
from app.services import dragon_tiger as dt

SYMBOL_ROWS = [
    {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
     "change": 0.05, "net_value": 1.5e8, "net_rate": 0.01,
     "buy_value": 2e8, "sell_value": 0.5e8, "hot_rank": 3, "range_days": 1,
     "org_net_value": 0.8e8},
]


class _Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def dragon_tiger(self, board_type: str, day: str | None) -> dict:
        self.calls.append((board_type, day))
        return {
            "trade_date": day or "2026-11-24",
            "count": 1,
            "stock_count": 1,
            "stock_items": list(SYMBOL_ROWS),
            "hot_money_items": [],
        }


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    dt._ONLINE_CACHE.clear()
    yield
    dt._ONLINE_CACHE.clear()


def _daily_dirs(data: Path, days: list[str]) -> None:
    for d in days:
        p = data / "kline_daily" / f"date={d}"
        p.mkdir(parents=True, exist_ok=True)
    # kline_daily 是月分区表：日期来自数据列，这里造一个能读出日期的分区
    import polars as pl

    by_month: dict[str, list[str]] = {}
    for d in days:
        by_month.setdefault(d[:7], []).append(d)
    for month, items in by_month.items():
        part = data / "kline_daily" / f"date={month}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": ["600519.SH"] * len(items),
            "date": [date.fromisoformat(x) for x in items],
            "close": [10.0] * len(items),
        }).write_parquet(part / "part.parquet")


def _use(monkeypatch, provider: _Provider) -> _Provider:
    monkeypatch.setattr(dt, "_provider", lambda: provider)
    return provider


def test_same_day_pulls_once_within_ttl(tmp_path: Path, monkeypatch):
    _daily_dirs(tmp_path, ["2026-11-24"])
    provider = _use(monkeypatch, _Provider())

    first = dt.get_dragon_tiger(tmp_path, None)
    second = dt.get_dragon_tiger(tmp_path, None)

    assert first == second
    # 三榜各一次，共 3 次；第二次请求不应再加
    assert len(provider.calls) == 3, f"同日应只拉一轮，实际 {len(provider.calls)} 次"


def test_expired_entry_is_refetched(tmp_path: Path, monkeypatch):
    _daily_dirs(tmp_path, ["2026-11-24"])
    provider = _use(monkeypatch, _Provider())
    dt.get_dragon_tiger(tmp_path, None)
    assert len(provider.calls) == 3

    monkeypatch.setattr(dt._ONLINE_CACHE, "ttl_s", -1.0)
    dt.get_dragon_tiger(tmp_path, None)
    assert len(provider.calls) == 6, "TTL 过期必须重新拉"


def test_different_days_do_not_share_entries(tmp_path: Path, monkeypatch):
    _daily_dirs(tmp_path, ["2026-11-23", "2026-11-24"])
    provider = _use(monkeypatch, _Provider())

    dt.get_dragon_tiger(tmp_path, date(2026, 11, 23))
    dt.get_dragon_tiger(tmp_path, date(2026, 11, 24))

    dates = {d for _b, d in provider.calls}
    assert dates == {"2026-11-23", "2026-11-24"}


def test_source_unavailable_is_cached_too(tmp_path: Path, monkeypatch):
    """未配置 provider 时也不该每次重建注册表——同样走 60s 缓存。"""
    _daily_dirs(tmp_path, ["2026-11-24"])
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return None

    monkeypatch.setattr(dt, "_provider", counted)
    out1 = dt.get_dragon_tiger(tmp_path, None)
    out2 = dt.get_dragon_tiger(tmp_path, None)

    assert calls["n"] == 1, "未配置 provider 也要缓存，别每次重建注册表"
    assert out1 == out2 == {"state": "source_unavailable"}


def test_historical_day_still_uses_disk_cache(tmp_path: Path, monkeypatch):
    """历史日走磁盘缓存，provider 一次都不该被调用。"""
    _daily_dirs(tmp_path, ["2026-11-20", "2026-11-24"])
    cached = {
        "state": "ok",
        "requested_date": "2026-11-20",
        "trade_date": "2026-11-20",
        "all": [], "org": [], "hot_money": [],
    }
    path = dt._cache_path(tmp_path, date(2026, 11, 20))
    path.parent.mkdir(parents=True, exist_ok=True)
    dt._store_cache(path, cached)

    provider = _use(monkeypatch, _Provider())
    out = dt.get_dragon_tiger(tmp_path, date(2026, 11, 20))

    assert out == cached
    assert provider.calls == []
