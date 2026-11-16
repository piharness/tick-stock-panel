"""#516 内存保护 — /health 可观测性、重任务内存准入、worker RSS 上限配置。"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes as routes_api
from app.services import heavy_job_limiter as hjl


class _VM:
    def __init__(self, available: int):
        self.available = available


# ── /health 内存块 ────────────────────────────────────────────────────


def test_health_reports_memory_snapshot():
    app = FastAPI()
    app.include_router(routes_api.router)
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    mem = body["memory"]
    assert mem["rss_bytes"] > 0
    assert mem["system_total_bytes"] > 0
    assert mem["system_available_bytes"] >= 0
    assert 0 <= mem["system_used_pct"] <= 100


# ── 重任务内存准入 (psutil 在 _memory_admission_ok 内函数级 import,
#    patch 真模块的 virtual_memory) ───────────────────────────────────


def test_memory_admission_blocks_and_recovers(monkeypatch):
    monkeypatch.setattr(hjl, "_HEAVY_MIN_AVAILABLE_BYTES", 1024**3)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM(512 * 1024**2))
    limiter = hjl.HeavyJobLimiter(capacity=2)
    # 内存不足: 有空位也不放行, 超时返回 False
    assert limiter.acquire("normal", timeout=0.2) is False
    # 内存恢复 (轮询重查, 无需 notify): 正常获准
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM(4 * 1024**3))
    assert limiter.acquire("normal", timeout=5.0) is True
    limiter.release("normal")


def test_memory_admission_disabled(monkeypatch):
    monkeypatch.setattr(hjl, "_HEAVY_MIN_AVAILABLE_BYTES", 0)
    limiter = hjl.HeavyJobLimiter(capacity=1)
    assert limiter.acquire("exclusive", timeout=0.1) is True
    limiter.release("exclusive")


def test_memory_gate_keeps_cancel_semantics(monkeypatch):
    monkeypatch.setattr(hjl, "_HEAVY_MIN_AVAILABLE_BYTES", 1024**3)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM(0))
    limiter = hjl.HeavyJobLimiter(capacity=1)
    cancel = threading.Event()
    t = threading.Timer(0.1, cancel.set)
    t.start()
    assert limiter.acquire("normal", timeout=5.0, cancel_event=cancel) is False
    t.join()


# ── worker RSS 上限配置解析 ──────────────────────────────────────────


def test_worker_rss_limit_env_parsing(monkeypatch):
    from app.backtest import worker

    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "0")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 0
    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "2147483648")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 2147483648
    monkeypatch.delenv("TICK_WORKER_RSS_LIMIT_BYTES")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 123
    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "not-a-number")
    assert worker._env_int("TICK_WORKER_RSS_LIMIT_BYTES", 123) == 123


def test_process_rss_bytes_for_invalid_pid():
    from app.backtest.worker import _process_rss_bytes

    assert _process_rss_bytes(None) == 0
    assert _process_rss_bytes(999999999) == 0


# ── 回测 worker RSS 上限: 设置页偏好 + 环境变量解析 ──────────────────


@pytest.fixture()
def _isolated_prefs(tmp_path, monkeypatch):
    """preferences.json 指向临时文件, 避免读写真实 data 目录。"""
    from app.services import preferences

    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


@pytest.fixture()
def _box_12gib(monkeypatch):
    """把容器内存预算固定为 12 GiB, 让上限推导与跑测试的机器无关。

    上限 = 总内存 × 0.6 = 7 GiB。真实机器的 cgroup 值不参与断言 ——
    否则同一条用例在 12 GiB 笔记本和 128 GiB 服务器上会得出不同数字。
    """
    from app.services import memory_budget as mb

    monkeypatch.setattr(
        mb, "memory_budget",
        lambda vm=None: mb.MemoryBudget(total_bytes=12 * 1024**3, available_bytes=8 * 1024**3),
    )
    return 7


def test_backtest_rss_limit_pref_roundtrip_and_clamp(_isolated_prefs, _box_12gib):
    from app.services import preferences

    assert preferences.get_backtest_worker_rss_limit_gib() == 6  # 缺省默认

    assert preferences.set_backtest_worker_rss_limit_gib(5) == 5
    assert preferences.get_backtest_worker_rss_limit_gib() == 5
    assert json.loads(_isolated_prefs.read_text(encoding="utf-8"))[
        "backtest_worker_rss_limit_gib"
    ] == 5

    # 越界钳制: 0 是合法值 (不限制), 负数按 0、超上限按本机容器余量收口
    assert preferences.set_backtest_worker_rss_limit_gib(-3) == 0
    assert preferences.set_backtest_worker_rss_limit_gib(999) == _box_12gib

    # 手改 JSON 出现非法值时自愈回默认
    _isolated_prefs.write_text(
        json.dumps({"backtest_worker_rss_limit_gib": "abc"}), encoding="utf-8"
    )
    preferences._invalidate_cache()
    assert preferences.get_backtest_worker_rss_limit_gib() == 6


def test_rss_limit_from_bigger_box_is_capped_by_container(_isolated_prefs, _box_12gib):
    """从 128 GiB 机器带来的 64 GiB 配置, 在 12 GiB 容器里必须被收紧。

    不收紧的后果不是「worker 超限」, 而是 worker 还没到自己上限、容器先被
    OOM —— 后者会把 API 进程一起带走, 且现场没有任何报错文案。
    """
    _isolated_prefs.write_text(
        json.dumps({"backtest_worker_rss_limit_gib": 64}), encoding="utf-8"
    )
    from app.services import preferences

    preferences._invalidate_cache()
    assert preferences.get_backtest_worker_rss_limit_gib() == _box_12gib


def test_rss_limit_keeps_default_when_budget_unreadable(_isolated_prefs, monkeypatch):
    """探测失败时退回通用上限 64, 不许把合法配置压成 1 GiB。"""
    from app.services import memory_budget as mb
    from app.services import preferences

    def _boom(vm=None):
        raise RuntimeError("no cgroup")

    monkeypatch.setattr(mb, "memory_budget", _boom)
    assert preferences.set_backtest_worker_rss_limit_gib(999) == 64


def test_effective_rss_limit_env_beats_preference(monkeypatch, _isolated_prefs):
    from app.backtest import worker
    from app.services import preferences

    preferences.set_backtest_worker_rss_limit_gib(8)
    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", str(2 * 1024**3))
    assert worker._effective_rss_limit_bytes() == 2 * 1024**3  # 环境变量优先


def test_effective_rss_limit_uses_preference_without_env(monkeypatch, _isolated_prefs):
    from app.backtest import worker
    from app.services import preferences

    monkeypatch.delenv("TICK_WORKER_RSS_LIMIT_BYTES", raising=False)
    preferences.set_backtest_worker_rss_limit_gib(8)
    assert worker._effective_rss_limit_bytes() == 8 * 1024**3


def test_effective_rss_limit_unlimited_and_default(monkeypatch, _isolated_prefs):
    from app.backtest import worker
    from app.services import preferences

    monkeypatch.delenv("TICK_WORKER_RSS_LIMIT_BYTES", raising=False)
    assert worker._effective_rss_limit_bytes() == 6 * 1024**3  # 均未设置 → 默认

    # 偏好 0 = 不限制
    preferences.set_backtest_worker_rss_limit_gib(0)
    assert worker._effective_rss_limit_bytes() == 0

    # 环境变量 0 = 不限制 (保持原 env 语义)
    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "0")
    assert worker._effective_rss_limit_bytes() == 0

    # 环境变量显式设置但非法 → 回退内置默认, 不回落偏好
    monkeypatch.setenv("TICK_WORKER_RSS_LIMIT_BYTES", "not-a-number")
    assert worker._effective_rss_limit_bytes() == 6 * 1024**3


def test_update_backtest_worker_rss_limit_endpoint(_isolated_prefs):
    from app.api.settings import (
        BacktestWorkerRssLimitIn,
        get_preferences,
        update_backtest_worker_rss_limit,
    )
    from app.services import preferences

    result = update_backtest_worker_rss_limit(BacktestWorkerRssLimitIn(gib=8))
    assert result == {"backtest_worker_rss_limit_gib": 8}
    assert preferences.get_backtest_worker_rss_limit_gib() == 8
    # GET /preferences 同步暴露该字段, 供设置页回显
    assert get_preferences()["backtest_worker_rss_limit_gib"] == 8


def test_backtest_worker_rss_limit_request_model_rejects_out_of_range():
    from pydantic import ValidationError

    from app.api.settings import BacktestWorkerRssLimitIn

    with pytest.raises(ValidationError):
        BacktestWorkerRssLimitIn(gib=-1)
    with pytest.raises(ValidationError):
        BacktestWorkerRssLimitIn(gib=65)


def test_worker_checks_memory_configuration_before_spawning(monkeypatch, tmp_path):
    """偏好读取失败时不得遗留一个没有监管的 worker。"""
    from app.backtest import worker

    started = []
    context = SimpleNamespace(
        Queue=object,
        Event=object,
        Process=lambda **kwargs: SimpleNamespace(start=lambda: started.append(True)),
    )
    monkeypatch.setattr(worker.mp, "get_context", lambda _method: context)

    def unavailable():
        raise PermissionError("preferences unavailable")

    monkeypatch.setattr(worker, "_effective_rss_limit_bytes", unavailable)
    with pytest.raises(PermissionError, match="preferences unavailable"):
        worker.run_worker_task({"kind": "backtest", "data_dir": str(tmp_path), "config": {}})
    assert started == []
