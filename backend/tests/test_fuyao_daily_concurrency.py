"""扶摇逐票日K并发: 并发不改变结果集, 也不放大上游请求速率。

背景: 旧实现 `for symbol in batch: 拉一只 + sleep 0.12` 是 5,892 只 × 158ms ≈ 16 分钟,
且与请求的日期跨度无关(拉 1 天 == 拉 1 年) —— 瓶颈是「标的数 × 固定间隔」。
改成 8 路并发后必须守住两件事:
  1. 结果集/落盘语义不变(每批 50 只, 逐批 yield);
  2. 全局请求速率不上涨(并发只重叠等待, 不能把 6.3 req/s 放大成 50 req/s)。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime

import polars as pl

from app.plugins.fuyao import provider as fp


def _bars(day: date = date(2020, 1, 2)) -> list[dict]:
    return [{
        "date_ms": fp._ms_of_date(day),
        "open_price": 10.0, "high_price": 11.0, "low_price": 9.0,
        "close_price": 10.5, "volume": 100_000, "turnover": 1_000_000.0,
    }]


def _provider(monkeypatch, *, rate_per_s: float = 0):
    """走 api 分支的 provider: 有多年 dump 就走 dump, 这里必须让它不存在。"""
    p = fp.FuyaoProvider()
    monkeypatch.setattr(p, "_daily_dump_info", lambda: None)
    p._hist_gate = fp._GlobalRateGate(rate_per_s)
    return p


def test_iter_daily_runs_symbols_concurrently_and_keeps_every_symbol(monkeypatch):
    p = _provider(monkeypatch)
    peak = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fake_hist(symbol: str, start_d: date, end_d: date) -> list[dict]:
        with lock:
            peak["cur"] += 1
            peak["max"] = max(peak["max"], peak["cur"])
        time.sleep(0.05)
        with lock:
            peak["cur"] -= 1
        return _bars()

    monkeypatch.setattr(p, "_historical_bars", fake_hist)
    symbols = [f"{600000 + i:06d}.SH" for i in range(50)]

    chunks = list(p.iter_daily(symbols, datetime(2020, 1, 1), datetime(2020, 3, 1)))

    stored = pl.concat([c for c in chunks if not c.is_empty()])
    assert set(stored["symbol"].to_list()) == set(symbols)
    # 串行实现峰值为 1; 只断言「不止一个在飞」, 不锁死具体路数(线程调度有抖动)
    assert peak["max"] > 1


def test_single_symbol_failure_does_not_drop_the_rest_of_the_batch(monkeypatch):
    p = _provider(monkeypatch)

    def fake_hist(symbol: str, start_d: date, end_d: date) -> list[dict]:
        if symbol == "600001.SH":
            raise RuntimeError("boom")
        return _bars()

    monkeypatch.setattr(p, "_historical_bars", fake_hist)
    symbols = [f"{600000 + i:06d}.SH" for i in range(50)]

    chunks = list(p.iter_daily(symbols, datetime(2020, 1, 1), datetime(2020, 3, 1)))

    stored = pl.concat([c for c in chunks if not c.is_empty()])
    got = set(stored["symbol"].to_list())
    assert len(got) == 49
    assert "600001.SH" not in got


def test_rate_gate_spaces_slots_globally():
    """50/s ⇒ 每槽 20ms; 5 次领牌(首张不睡)至少跨 4 个间隔, 留 30% 抖动余量。"""
    gate = fp._GlobalRateGate(50.0)
    t0 = time.monotonic()
    for _ in range(5):
        gate.wait()
    assert time.monotonic() - t0 >= 0.055


def test_rate_gate_zero_means_no_throttle():
    gate = fp._GlobalRateGate(0)
    t0 = time.monotonic()
    for _ in range(50):
        gate.wait()
    assert time.monotonic() - t0 < 0.05


def test_rate_gate_is_shared_across_threads():
    """同一 provider 的并发工作体共享速率: 8 线程各领 4 张牌 = 32 张, 速率 100/s ⇒ ≥0.31s。"""
    gate = fp._GlobalRateGate(100.0)
    started = threading.Barrier(8)

    def worker() -> None:
        started.wait()
        for _ in range(4):
            gate.wait()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 32 张牌 / 100 per s = 0.31s; 允许 25% 余量
    assert time.monotonic() - t0 >= 0.23


def test_429_is_retried_instead_of_dropped(monkeypatch):
    """实测: 25 req/s 会撞 429。旧实现遇错即 break ⇒ 该只永久缺失且任务仍报成功。"""
    p = _provider(monkeypatch)
    monkeypatch.setattr(fp, "_HIST_429_BACKOFF_S", (0.01, 0.02))
    calls = {"n": 0}

    class _Client:
        def historical_kline(self, symbol, s, e, adjust="none"):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise fp.FuyaoError("HTTP 429: /api/a-share/prices/historical")
            return _bars()

    monkeypatch.setattr(p, "_get_client", lambda: _Client())

    out = p._historical_bars("600000.SH", date(2020, 1, 1), date(2020, 1, 2))

    assert calls["n"] == 3  # 2 次重试后成功
    assert len(out) == 1


def test_429_gives_up_after_retry_budget(monkeypatch):
    p = _provider(monkeypatch)
    monkeypatch.setattr(fp, "_HIST_429_BACKOFF_S", (0.01, 0.02))
    calls = {"n": 0}

    class _Client:
        def historical_kline(self, *a, **kw):
            calls["n"] += 1
            raise fp.FuyaoError("HTTP 429: /api/a-share/prices/historical")

    monkeypatch.setattr(p, "_get_client", lambda: _Client())

    out = p._historical_bars("600000.SH", date(2020, 1, 1), date(2020, 1, 2))

    assert calls["n"] == fp._HIST_429_RETRIES + 1
    assert out == []


def test_unknown_symbol_is_not_retried(monkeypatch):
    """1002(退市/换码旧码)是确定性结果: 重试只会把 304 只放大成 912 次无谓请求。"""
    p = _provider(monkeypatch)
    calls = {"n": 0}

    class _Client:
        def historical_kline(self, *a, **kw):
            calls["n"] += 1
            raise fp.FuyaoError("扶摇接口错误 code=1002: Unknown thscode: 301139.SZ")

    monkeypatch.setattr(p, "_get_client", lambda: _Client())

    out = p._historical_bars("301139.SZ", date(2020, 1, 1), date(2020, 1, 2))

    assert calls["n"] == 1
    assert out == []
