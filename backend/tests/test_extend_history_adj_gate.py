"""补历史的前置闸门 —— 拿不到除权因子时拒绝写入，而不是写一份"看着正常"的错数据。

实测依据（2026-11-14）：本机 `data/capabilities.json` 里 `adj_factor` 这个 key 是
**在**的（limits 全 null），同一份文件的 probe_log 却写着 `✗ adj_factor(无权限)`；
真正调用 `ex_factors` 直接 PermissionError。⇒ 只有"调一次"能定案，声明不可信。

为什么值得单独守：补历史写的是 `adjust="none"` 原始价，缺因子时 enriched 不做
前复权，图上每次送转/分红都是一根假跳空（10 只样本 8 只受影响，最大 67%）。
这类错误的特征正是"看起来完全正常"，所以它必须被拒绝，而不是被记录。
"""
from __future__ import annotations

import pytest

from app.services import extend_history
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


@pytest.fixture(autouse=True)
def _clear_probe_cache(monkeypatch):
    # 默认按「因子源 = tickflow」跑；本机 preferences 里可能配了别的源，
    # 不钉死的话前四条会走 custom-provider 直接放行，测不到闸门本身。
    monkeypatch.setattr(
        "app.services.preferences.get_adj_factor_provider", lambda: "tickflow"
    )
    extend_history._adj_probe_cache = None
    yield
    extend_history._adj_probe_cache = None


class _Capset:
    def __init__(self, caps: list[Cap]) -> None:
        self._caps = set(caps)

    def has(self, cap: Cap) -> bool:
        return cap in self._caps


def test_declared_capability_is_not_trusted(monkeypatch) -> None:
    """声明里有 adj_factor，但真调不通 → 必须判 False（本机就是这个状态）。"""
    class _Klines:
        def ex_factors(self, symbols):
            raise PermissionError("无除权因子查询权限")

    class _Client:
        klines = _Klines()

    monkeypatch.setattr("app.services.kline_sync.get_client", lambda: _Client())
    ok, reason = extend_history.check_adj_factor_source(_Capset([Cap.ADJ_FACTOR]))
    assert ok is False
    assert "PermissionError" in reason


def test_probe_success_allows_extend(monkeypatch) -> None:
    class _Klines:
        def ex_factors(self, symbols):
            return {"ok": True}

    class _Client:
        klines = _Klines()

    monkeypatch.setattr("app.services.kline_sync.get_client", lambda: _Client())
    ok, reason = extend_history.check_adj_factor_source(_Capset([Cap.ADJ_FACTOR]))
    assert ok is True and reason == "probe-ok"


def test_missing_capability_short_circuits_without_network(monkeypatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(
        "app.services.kline_sync.get_client",
        lambda: called.append(1),  # 被调用就会留下痕迹
    )
    ok, reason = extend_history.check_adj_factor_source(_Capset([]))
    assert ok is False and reason == "capability-missing"
    assert called == [], "没这个能力就不该再去打一次网络"


def test_probe_is_cached(monkeypatch) -> None:
    """同一进程里重复点击不该反复打网络（探测结果缓存 1h）。"""
    hits: list[int] = []

    class _Klines:
        def ex_factors(self, symbols):
            hits.append(1)
            raise PermissionError("无权限")

    class _Client:
        klines = _Klines()

    monkeypatch.setattr("app.services.kline_sync.get_client", lambda: _Client())
    for _ in range(3):
        extend_history.check_adj_factor_source(_Capset([Cap.ADJ_FACTOR]))
    assert len(hits) == 1


def test_custom_provider_that_returns_data_is_allowed(monkeypatch) -> None:
    """配了自定义因子源、且它真给得出事件 → 放行（不被 tickflow 权限拦住）。"""
    import polars as pl

    class _Prov:
        def get_adj_factors(self, symbols, start_time=None, end_time=None):
            return pl.DataFrame({"symbol": ["600519.SH"], "ex_factor": [1.01]})

    monkeypatch.setattr(
        "app.services.preferences.get_adj_factor_provider", lambda: "my-provider"
    )
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider", lambda name: _Prov()
    )
    ok, reason = extend_history.check_adj_factor_source(_Capset([]))
    assert ok is True and reason == "provider:my-provider"


def test_custom_provider_returning_nothing_is_refused(monkeypatch) -> None:
    """配了源但给不出事件（没装/没 key/空返回）→ 必须拒绝，不能"配了就算有"。"""
    import polars as pl

    class _Prov:
        def get_adj_factors(self, symbols, start_time=None, end_time=None):
            return pl.DataFrame()

    monkeypatch.setattr(
        "app.services.preferences.get_adj_factor_provider", lambda: "my-provider"
    )
    monkeypatch.setattr(
        "app.data_providers.custom.get_provider", lambda name: _Prov()
    )
    ok, reason = extend_history.check_adj_factor_source(_Capset([]))
    assert ok is False
    assert "未返回任何除权事件" in reason
