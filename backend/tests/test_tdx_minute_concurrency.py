"""通达信分钟并发: 每线程独占连接 (tdxrs 非线程安全), 页级速率钉在通道限流值。"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import polars as pl

from app.plugins.tdx import provider as tp


def _kept(symbol: str):
    return [{
        "symbol": symbol,
        "datetime": datetime(2026, 11, 25, 9, 31),
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "volume": 1000.0, "amount": 1_000_000.0,
    }]


def _provider(monkeypatch, *, rate_per_s: float = 0):
    p = tp.TdxProvider()
    p._minute_gate = tp._PageGate(rate_per_s)
    # 并行工作体绕过协议解析与日线校准: 这里只验证并发/隔离/速率, 不验证取数语义。
    monkeypatch.setattr(tp, "_minute_rows", lambda sym, rows: _kept(sym))
    monkeypatch.setattr(tp, "_calibrate_by_day", lambda sym, rows: rows)
    return p


def _install_fake_client(monkeypatch, peak: dict, lock: threading.Lock):
    class _FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def bars(self, market, code, category, offset, count):
            with lock:
                peak["cur"] += 1
                peak["max"] = max(peak["max"], peak["cur"])
            time.sleep(0.03)
            with lock:
                peak["cur"] -= 1
            return [{"close": 10.5, "open": 10.0, "high": 11.0, "low": 9.0,
                     "vol": 100000, "amount": 1_000_000.0, "datetime": "2026-11-25 09:31"}]

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(tp, "TdxClient", _FakeClient)
    return _FakeClient


def test_get_minute_runs_concurrently_and_keeps_every_symbol(monkeypatch):
    p = _provider(monkeypatch)
    peak = {"cur": 0, "max": 0}
    lock = threading.Lock()
    fake = _install_fake_client(monkeypatch, peak, lock)
    symbols = [f"{600000 + i:06d}.SH" for i in range(30)]

    df = p.get_minute(symbols, None, None, freq="1m")

    assert isinstance(df, pl.DataFrame)
    assert set(df["symbol"].to_list()) == set(symbols)
    assert peak["max"] > 1          # 串行实现峰值为 1
    # 线程独占连接: 8 路 ⇒ 建连数远小于标的数
    assert peak["max"] <= tp._MINUTE_CONCURRENCY + 1


def test_unmappable_symbol_does_not_drop_the_rest(monkeypatch):
    p = _provider(monkeypatch)
    peak = {"cur": 0, "max": 0}
    _install_fake_client(monkeypatch, peak, threading.Lock())
    real_to_tdx = tp.to_tdx
    monkeypatch.setattr(
        tp, "to_tdx",
        lambda s: None if s == "600001.SH" else real_to_tdx.__call__(s),
    )
    symbols = [f"{600000 + i:06d}.SH" for i in range(30)]

    df = p.get_minute(symbols, None, None, freq="1m")

    got = set(df["symbol"].to_list())
    assert "600001.SH" not in got
    assert len(got) == 29


def test_single_client_failure_does_not_drop_the_batch(monkeypatch):
    p = _provider(monkeypatch)
    real_to_tdx = tp.to_tdx
    monkeypatch.setattr(
        tp, "to_tdx",
        lambda s: real_to_tdx.__call__(s),
    )

    def _boom(self, market, code, start_time=None, **kw):
        if code == "600002":
            raise tp.TdxError("connect failed")
        return [{"close": 10.5, "open": 10.0, "high": 11.0, "low": 9.0,
                 "vol": 100000, "amount": 1_000_000.0, "datetime": "2026-11-25 09:31"}], True

    monkeypatch.setattr(tp.TdxProvider, "_page_bars", _boom)
    symbols = [f"{600000 + i:06d}.SH" for i in range(30)]

    df = p.get_minute(symbols, None, None, freq="1m")

    got = set(df["symbol"].to_list())
    assert "600002.SH" not in got
    assert len(got) == 29


def test_window_filter_applies_in_parallel_path(monkeypatch):
    p = _provider(monkeypatch)
    peak = {"cur": 0, "max": 0}
    _install_fake_client(monkeypatch, peak, threading.Lock())
    symbols = [f"{600000 + i:06d}.SH" for i in range(30)]

    # 内部行恒为 09:31, 窗口从 10:00 起 ⇒ 全部被过滤
    df = p.get_minute(symbols, datetime(2026, 11, 25, 10, 0), None, freq="1m")

    assert df.is_empty()


def test_small_batch_still_returns_all_symbols(monkeypatch):
    """低于并行阈值走串行路径: 历史契约 (逐只翻页) 不因并发改造而改变。"""
    p = _provider(monkeypatch)
    peak = {"cur": 0, "max": 0}
    _install_fake_client(monkeypatch, peak, threading.Lock())
    symbols = [f"{600000 + i:06d}.SH" for i in range(8)]

    df = p.get_minute(symbols, None, None, freq="1m")

    assert set(df["symbol"].to_list()) == set(symbols)


def test_page_gate_spaces_requests():
    gate = tp._PageGate(50.0)
    t0 = time.monotonic()
    for _ in range(5):
        gate.wait()
    assert time.monotonic() - t0 >= 0.055


def test_bad_freq_returns_empty_without_touching_clients(monkeypatch):
    p = _provider(monkeypatch)
    peak = {"cur": 0, "max": 0}
    _install_fake_client(monkeypatch, peak, threading.Lock())

    df = p.get_minute([f"{600000 + i:06d}.SH" for i in range(30)], None, None, freq="5m")

    assert df.is_empty()
    assert peak["cur"] == 0


def test_incomplete_symbol_is_dropped_not_written(monkeypatch):
    """半截(翻页遇 TdxError)的 symbol 不能落盘: symbol 级覆盖会把完整旧数据覆盖成半截。"""
    p = _provider(monkeypatch)
    real_to_tdx = tp.to_tdx
    monkeypatch.setattr(tp, "to_tdx", lambda s: real_to_tdx.__call__(s))

    def _half(self, market, code, start_time=None, **kw):
        if code == "600002":
            return [{"close": 10.5, "open": 10.0, "high": 11.0, "low": 9.0,
                     "vol": 100000, "amount": 1_000_000.0, "datetime": "2026-11-25 09:31"}], False
        return [{"close": 10.5, "open": 10.0, "high": 11.0, "low": 9.0,
                 "vol": 100000, "amount": 1_000_000.0, "datetime": "2026-11-25 09:31"}], True

    monkeypatch.setattr(tp.TdxProvider, "_page_bars", _half)
    symbols = [f"{600000 + i:06d}.SH" for i in range(30)]

    df = p.get_minute(symbols, None, None, freq="1m")

    got = set(df["symbol"].to_list())
    assert "600002.SH" not in got
    assert len(got) == 29
