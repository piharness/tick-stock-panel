"""DuckDB 视图挂载的启动成本 — repository 启动路径的回归锁。

背景 (2026-11-08 容器启动 32s 事故): ``DataStore()`` 逐条 ``CREATE VIEW``,
每条都要各自展开一次 ``<dir>/**/*.parquet``。data/ 里 1000+ 个分区文件时,
在 Docker Desktop 的 9p 绑定挂载上单条回源 2~3s, 21 条串行把进程启动阻塞
25.6s (占整个启动的 84%)。

本文件的断言不依赖真实文件系统延迟: 用固定耗时的假 cursor 复现「视图之间
可以并发」这一结构性质 —— 串行实现会得到 max_overlap == 1 且总时长 = N×delay。
改回串行必红。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from app.tickflow.repository import DataStore


class _SleepyCursor:
    """记录同时在飞的 DDL 条数, 并固定耗时 (不依赖真实 IO, 不抖动)。"""

    def __init__(self, tracker: dict, delay: float) -> None:
        self._tracker = tracker
        self._delay = delay

    def execute(self, sql: str, params: list | None = None) -> _SleepyCursor:
        with self._tracker["lock"]:
            self._tracker["cur"] += 1
            self._tracker["max"] = max(self._tracker["max"], self._tracker["cur"])
            self._tracker["sql"].append(sql)
        time.sleep(self._delay)
        with self._tracker["lock"]:
            self._tracker["cur"] -= 1
        return self

    def close(self) -> None:
        pass


def _patch_cursor(monkeypatch, delay: float = 0.05) -> dict:
    tracker: dict = {"lock": threading.Lock(), "cur": 0, "max": 0, "sql": []}
    monkeypatch.setattr(
        DataStore, "_new_cursor", lambda self: _SleepyCursor(tracker, delay)
    )
    return tracker


def test_view_registration_does_not_serialize(tmp_path: Path, monkeypatch) -> None:
    """21 条视图 DDL 必须并发下发, 而不是一条一条排队。"""
    tracker = _patch_cursor(monkeypatch)
    started = time.perf_counter()
    DataStore(tmp_path)
    elapsed = time.perf_counter() - started

    assert len(tracker["sql"]) == 21, "视图清单条数变了: 新增/删除视图请同步本断言"
    assert tracker["max"] > 1, "视图注册又退回串行, 启动会被 N× 回源拖住"
    # 串行最少也要 21×0.05=1.05s; 并发应远低于此。留足余量避免慢机抖动。
    assert elapsed < 21 * 0.05 * 0.6, f"并发没生效: {elapsed:.2f}s"


def test_dead_unified_views_are_not_registered(tmp_path: Path, monkeypatch) -> None:
    """无消费方的全市场合并视图不得出现在启动路径上 (每条都是一次全量 glob)。"""
    tracker = _patch_cursor(monkeypatch, delay=0.0)
    DataStore(tmp_path)
    joined = " ".join(tracker["sql"])
    for name in ("kline_daily_all", "kline_enriched_all", "kline_minute_all", "instruments_all"):
        assert name not in joined, f"{name} 没有消费方, 不该在启动时挂载"
