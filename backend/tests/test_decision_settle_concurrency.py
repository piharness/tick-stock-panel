"""settle() 并发幂等回归 — 读-算-写不串行化会产生重复结算行。

背景（2026-11-06 独立评审 P2）：`settle()` 自称幂等——顺序调用下 `done`
集合阻止重复写。但「读 settlements → 算记录 → append 写盘」三步之间没有锁
时，并发调用都读到同一份空 `done` 集合并各自 append 同 key 行。评审探针
（`.workbuddy/tmp/probe_settle_concurrency.py`）实测：8 线程并发 → 8 行同键
记录，`score()` 把 1 个真实样本计成 8 个，Brier/skill 全部重复计分。
API 是同步端点（线程池执行），UI「结算到期」连点即可触发。

修复 = `settle._SETTLE_LOCK` 把整段读-算-写串行化。本测试用屏障把 8 个线程
压在同一个起跑线上：持锁时第二个及以后的调用在锁内重读 `done` 后空转；
去掉锁它必红（8 行同键）。与 `test_backtest_etf.py` 的缓存踩踏测试同构。
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.decision import ledger
from app.decision import settle as settle_mod
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
BENCH = "000300.SH"
START = date(2026, 1, 5)
N_DAYS = 30
HORIZON = 2
WORKERS = 8


def _seed(tmp_path: Path, n: int = N_DAYS) -> list[date]:
    """单标的、恒涨的合成行情：足以让一张 horizon=2 的卡到期并成交。"""
    days = [START + timedelta(days=i) for i in range(n)]
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame({
        "symbol": [SYM] * n, "date": days,
        "open": [100.0] * n, "high": [110.0] * n, "low": [100.0] * n,
        "close": [110.0] * n, "volume": [1e4] * n, "amount": [1e6] * n,
    }))
    repo.append_enriched(pl.DataFrame({
        "symbol": [SYM] * n, "date": days,
        "open": [100.0] * n, "high": [110.0] * n, "low": [100.0] * n,
        "close": [110.0] * n, "raw_close": [110.0] * n,
        "raw_high": [110.0] * n, "raw_low": [100.0] * n,
        "volume": [1e4] * n, "amount": [1e6] * n,
    }))
    repo.append_index_enriched(pl.DataFrame({
        "symbol": [BENCH] * n, "date": days,
        "open": [4000.0] * n, "high": [4000.0] * n, "low": [4000.0] * n,
        "close": [4000.0] * n, "raw_close": [4000.0] * n,
        "volume": [1e9] * n, "amount": [1e12] * n,
    }))
    return days


def test_concurrent_settle_writes_each_key_once(tmp_path: Path) -> None:
    days = _seed(tmp_path)
    card = ledger.build_card(
        as_of=days[0].isoformat(), horizon_sessions=HORIZON,
        picks=[{"symbol": SYM, "limit_price": 100.0, "p_win": 0.6}],
    )
    ledger.save_card(tmp_path, card)

    barrier = threading.Barrier(WORKERS)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            settle_mod.settle(tmp_path)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, name=f"settle-{i}")
               for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发 settle 抛异常：{errors!r}"
    assert not any(t.is_alive() for t in threads), "有线程未在 60s 内结束"

    rows = ledger.load_settlements(tmp_path)
    keys = [str(r.get("key")) for r in rows]
    assert len(keys) == len(set(keys)), f"重复结算行（锁失效）：{keys}"
    assert len(rows) == 1, f"应恰有 1 行结算，实际 {len(rows)}：{keys}"

    score = settle_mod.score(rows)
    assert score["n"] == 1, "重复行使 score 把 1 个样本计成多个"
