"""回归测试: 历史日分钟图的整份响应缓存。

回归动机（2026-11-24）：`/api/kline/minute` 端到端实测 210ms，其中真正读盘
（`repo.get_minute`）只有 **18.7ms**，其余分散在标的信息 / 昨收 / 涨跌停价 /
序列化这些各自只有几毫秒的派生查询上。逐个猜不如整体缓存这个单元。

三条边界（错了就会给用户看错图）：
  1. **历史日**缓存：那份数据是快照，只在补数/修数时被重写；
  2. **当日**绝不缓存：盘中每分钟都在变，缓存等于让分时图停在上一轮；
  3. `live=true`（详情页轮询）绝不缓存：它的语义就是"绕过本地拿最新"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.api import kline as kline_api
from app.services import preferences

SYMBOL = "600519.SH"
HISTORY = date(2026, 11, 20)
TODAY = date(2026, 11, 24)


def _session(day: date) -> pl.DataFrame:
    start = datetime(day.year, day.month, day.day, 9, 31)
    return pl.DataFrame({
        "symbol": [SYMBOL] * 240,
        "datetime": [start + timedelta(minutes=i) for i in range(240)],
        "close": [100.0] * 240,
    })


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    monkeypatch.setattr(kline_api, "cn_now", lambda: datetime(2026, 11, 24, 20, 0))
    monkeypatch.setattr(kline_api, "in_continuous_session", lambda: False)
    monkeypatch.setattr(preferences, "get_minute_batch_compress", lambda: False)
    monkeypatch.setattr(kline_api.minute_adjust, "minute_basis_is_raw", lambda _d: False)
    monkeypatch.setattr(kline_api, "_get_stock_info", lambda _r, _s: {"name": "贵州茅台"})
    monkeypatch.setattr(kline_api, "_get_previous_closes", lambda _r, _s, _d, _a: {})
    monkeypatch.setattr(kline_api, "_get_price_limit_info", lambda *_a, **_k: {})
    kline_api._MINUTE_PAYLOAD_CACHE.clear()
    yield
    kline_api._MINUTE_PAYLOAD_CACHE.clear()


def _request() -> MagicMock:
    repo = MagicMock()
    repo.resolve_asset_type.return_value = "stock"
    repo.get_minute.side_effect = lambda _s, day, asset_type="stock": _session(day)
    request = MagicMock()
    request.app.state.repo = repo
    request.app.state.capabilities = MagicMock()
    request.headers = {}
    return request


def _call(request, day: date, live: bool = False):
    return kline_api.get_minute(request, symbol=SYMBOL, trade_date=day, live=live)


def test_history_is_cached():
    request = _request()
    first = _call(request, HISTORY)
    second = _call(request, HISTORY)

    assert request.app.state.repo.get_minute.call_count == 1, "历史日第二次不应再读盘"
    assert first["rows"] == second["rows"]
    assert second["source"] == "local"


def test_today_is_never_cached():
    request = _request()
    _call(request, TODAY)
    _call(request, TODAY)

    assert request.app.state.repo.get_minute.call_count == 2, "当日分钟K每分钟在变，不得缓存"


def test_live_flag_bypasses_cache():
    request = _request()
    _call(request, HISTORY)
    _call(request, HISTORY, live=True)

    assert request.app.state.repo.get_minute.call_count == 2, "live=true 的语义就是绕过缓存"


def test_partial_history_is_cached_too():
    """历史日只有 171 根（不完整）时也走同一条本地分支，同样应命中缓存。"""
    request = _request()
    frame = _session(HISTORY).head(171)
    request.app.state.repo.get_minute.side_effect = lambda *_a, **_k: frame

    first = _call(request, HISTORY)
    second = _call(request, HISTORY)

    assert request.app.state.repo.get_minute.call_count == 1
    assert first["source"] == second["source"] == "local_partial"
    assert second["actual"] == 171


def test_cache_clear_restores_disk_read():
    request = _request()
    _call(request, HISTORY)
    kline_api._MINUTE_PAYLOAD_CACHE.clear()
    _call(request, HISTORY)

    assert request.app.state.repo.get_minute.call_count == 2
