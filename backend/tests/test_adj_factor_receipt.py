"""除权因子同步的回执：部分失败必须点名，空返回与失败必须分得开。

这里打的是 `sync_adj_factor` 自己的出口，不是回执读侧——读侧的合同在
`test_decision_receipt.py`。两件事只有在这里能验：`failed_out` 真的被填了，
以及「全批失败」与「全批成功但零行」这两个都返回 `(0, [])` 的结局在回执里
留下了不同的 `outcome`。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.decision import receipt
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

A = "000001.SZ"
B = "000002.SZ"
START = datetime(2024, 1, 1)
END = datetime(2024, 12, 31)
EVENT = {"trade_date": date(2024, 6, 19), "ex_factor": 1.1}


def _capset() -> CapabilitySet:
    return CapabilitySet({
        Cap.ADJ_FACTOR: CapabilityLimits(batch=50, rpm=30),
    })


def _patch(monkeypatch, ex_factors, *, chunk_size: int = 2,
           provider: str = "tickflow") -> None:
    """把 SDK 换成桩，并把分批强制成每批 `chunk_size` 只，好造出「部分失败」。

    同时把 provider 钉住：这台机器上配置的是自定义源，不钉住的话请求会绕过
    SDK 桩、打到真实数据源，测试变成依赖网络与本地偏好的东西。
    """
    monkeypatch.setattr(kline_sync, "preferences",
                        SimpleNamespace(get_adj_factor_provider=lambda: provider))
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda i, rpm: None)
    monkeypatch.setattr(kline_sync, "chunked",
                        lambda syms, n: [list(syms[i:i + chunk_size])
                                         for i in range(0, len(syms), chunk_size)])
    monkeypatch.setattr(kline_sync, "get_client",
                        lambda: SimpleNamespace(klines=SimpleNamespace(ex_factors=ex_factors)))


def _lines(tmp_path, asset_type: str = "stock") -> list[dict]:
    path = receipt.receipt_path(tmp_path, asset_type)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_failed_chunk_is_named_and_the_rest_of_the_batch_still_writes(tmp_path, monkeypatch):
    """一批成功、一批失败 → 回执 outcome=partial，失败的标的被点名。

    这三件事缺一不可：不点名，读侧会把失败标的当成已覆盖（虚假覆盖）；把整批
    标成失败，则成功那批的覆盖也丢了；不写回执，这条失败在日志里过一夜就没了。
    """
    def ex_factors(chunk, **kwargs):
        if chunk == [B]:
            raise RuntimeError("boom")
        return {chunk[0]: [EVENT]}

    _patch(monkeypatch, ex_factors, chunk_size=1)
    repo = KlineRepository(DataStore(tmp_path))
    failed: list[str] = []

    written, _affected = kline_sync.sync_adj_factor(
        [A, B], repo, _capset(), start_time=START, end_time=END,
        failed_out=failed, scope="universe",
    )

    assert written == 1 and failed == [B]
    [row] = _lines(tmp_path)
    assert row["outcome"] == "partial"
    assert row["failed_symbols"] == [B]
    assert row["scope"] == "universe"
    assert row["requested_from"] == "2024-01-01" and row["requested_to"] == "2024-12-31"
    # 表是在这次同步里建起来的，读数按「离开时」记。
    assert row["store_created"] is True
    assert row["store_rows"] == 1
    assert row["store_first"] == "2024-06-19"


def test_all_chunks_failing_and_nothing_to_fetch_do_not_look_alike(tmp_path, monkeypatch):
    """两个结局都返回 (0, [])，只有 `outcome` 分得开。

    「这次一个标的都没拿到」与「这次拿到了，只是这段时间没有除权事件」在返回值
    上完全一样，而前者需要重试、后者什么都不用做。日志里两者也长得一样。
    """
    def always_fail(chunk, **kwargs):
        raise RuntimeError("boom")

    _patch(monkeypatch, always_fail, chunk_size=2)
    failed: list[str] = []
    written, _ = kline_sync.sync_adj_factor(
        [A, B], KlineRepository(DataStore(tmp_path)), _capset(),
        start_time=START, end_time=END, failed_out=failed, scope="universe",
    )
    assert written == 0 and failed == [A, B]
    [row] = _lines(tmp_path)
    assert row["outcome"] == "failed"
    assert row["failed_symbols"] == [A, B]


def test_an_empty_answer_is_a_success_not_a_failure(tmp_path, monkeypatch):
    """批次没失败、provider 返回空 → outcome=ok，failed_symbols 为空。

    空返回是「这段时间没有除权」的答案，不是失败；把它记成失败，会让每只
    没有分红的标的永远背着一条失败记录。
    """
    monkeypatch.setattr(kline_sync, "preferences",
                        SimpleNamespace(get_adj_factor_provider=lambda: "tickflow"))
    _patch(monkeypatch, lambda chunk, **kwargs: {}, chunk_size=2)
    written, _ = kline_sync.sync_adj_factor(
        [A], KlineRepository(DataStore(tmp_path)), _capset(),
        start_time=START, end_time=END, scope="universe",
    )
    assert written == 0
    [row] = _lines(tmp_path)
    assert row["outcome"] == "ok" and row["failed_symbols"] == []
    # 没建表：读数全空，而不是 0。0 行和「没这张表」是两件事。
    assert row["store_created"] is False and row["store_rows"] is None


def test_a_skipped_request_is_recorded_as_skipped(tmp_path, monkeypatch):
    """没有权限时请求没发生：记 skipped，不记 ok。

    否则「跑了但没权限」与「跑成功、零行」在回执里一样，而一个要授权、
    一个什么都不用做。
    """
    monkeypatch.setattr(kline_sync, "preferences",
                        SimpleNamespace(get_adj_factor_provider=lambda: "tickflow"))
    written, _ = kline_sync.sync_adj_factor(
        [A], KlineRepository(DataStore(tmp_path)), SimpleNamespace(has=lambda _: False),
        start_time=START, end_time=END,
    )
    assert written == 0
    [row] = _lines(tmp_path)
    assert row["outcome"] == "skipped"
    assert row["store_rows"] is None


def test_no_request_window_means_no_receipt(tmp_path, monkeypatch):
    """没有区间的回执读不了：区间是它的语义单位，宁可不写。"""
    _patch(monkeypatch, lambda chunk, **kwargs: {chunk[0]: [EVENT]}, chunk_size=2)
    kline_sync.sync_adj_factor([A], KlineRepository(DataStore(tmp_path)), _capset())
    assert not receipt.receipt_path(tmp_path).exists()


def test_a_custom_provider_never_claims_coverage(tmp_path, monkeypatch):
    """自定义源的回执是 `unverified`，读侧不拿它提供任何覆盖。

    理由在 provider 自己的代码里：扶摇把整份 dump 的失败吞成空表
    （`plugins/fuyao/provider.py` 捕获 `FuyaoError` 后 `return 空表`），而空表
    同时就是「这批标的没有除权」的答案。两者同形 ⇒ 逐标的完整性不可验证 ⇒
    唯一诚实的 outcome 是「不知道」。记成 `ok` 的话，一份失败的 dump 会被读成
    「窗口已同步、只是没有事件」，那正是把分红整段吃掉的那条路。
    """
    import polars as pl

    from app.data_providers import custom as custom_sources

    frame = pl.DataFrame({
        "symbol": [A], "trade_date": [date(2024, 6, 19)], "ex_factor": [1.1],
    })

    class _Provider:
        def get_adj_factors(self, symbols, start_time, end_time,
                            asset_type="stock", on_chunk_done=None):
            return frame

    _patch(monkeypatch, lambda chunk, **kw: {}, provider="fuyao")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda n, d: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda n: _Provider())

    written, _ = kline_sync.sync_adj_factor(
        [A, B], KlineRepository(DataStore(tmp_path)), _capset(),
        start_time=START, end_time=END, scope="universe",
    )

    assert written == 1                      # 数据照常入库
    [row] = _lines(tmp_path)
    assert row["outcome"] == "unverified"
    # 关键：这条回执一行覆盖也不给，尽管它 outcome 不是 failed。
    assert receipt.coverage_intervals(tmp_path, A) == []
    assert receipt.covered_through(tmp_path, A, "2024-06-20") is None


def test_an_unknown_scope_is_refused_before_any_request(tmp_path, monkeypatch):
    """拼错的 scope 当场报错，不要带病跑完再在回执里留个读不懂的值。"""
    def ex_factors(chunk, **kwargs):        # pragma: no cover - 不该被调用
        raise AssertionError("scope 非法时不该发起请求")

    _patch(monkeypatch, ex_factors)
    with pytest.raises(ValueError):
        kline_sync.sync_adj_factor(
            [A], KlineRepository(DataStore(tmp_path)), _capset(),
            start_time=START, end_time=END, scope="全市场",
        )


def test_an_etf_fallback_records_the_source_that_actually_answered(tmp_path, monkeypatch):
    """自定义源对 ETF 空返回 → TickFlow 接手 → 回执里的 provider 必须是 TickFlow。

    这一条同时钉住三件事：ETF 回执落在 ETF 文件（跨市场假覆盖由路径挡）、
    透传层没把 scope 丢掉、以及 provider 跟着实际来源改口。记着原来的名字，
    读的人会去错的源上找这次同步。
    """
    import polars as pl

    from app.data_providers import custom as custom_sources

    empty = pl.DataFrame(schema={"symbol": pl.String,
                                 "trade_date": pl.Date,
                                 "ex_factor": pl.Float64})

    class _EmptyETF:
        def get_adj_factors(self, symbols, start_time, end_time,
                            asset_type="stock", on_chunk_done=None):
            return empty

    _patch(monkeypatch, lambda chunk, **kw: {chunk[0]: [EVENT]}, provider="fuyao")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda n, d: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda n: _EmptyETF())

    from app.services import index_sync
    index_sync.sync_etf_adj_factor(
        [A], KlineRepository(DataStore(tmp_path)), _capset(),
        start_time=START, end_time=END, scope="universe",
    )

    [row] = _lines(tmp_path, "etf")
    assert row["asset_type"] == "etf" and row["provider"] == "tickflow"
    assert row["scope"] == "universe"
    assert row["store_created"] is True
    assert not receipt.receipt_path(tmp_path, "stock").exists()
