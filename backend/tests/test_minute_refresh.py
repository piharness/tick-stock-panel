"""盘中分钟增量刷新服务 (minute_refresh) 测试。

覆盖:
- 连续竞价时段判定 (含边界)
- 门控链: 开关关闭 / 能力缺失 / 时段外 / 放行 (自定义源与 TickFlow 统一口径)
- 数据源路由: full_minute 偏好 → 自定义源 (get_intraday_batch / get_intraday_latest /
  get_minute 回退) 或降级 TickFlow; 仅修复轮源 60s 节奏下限
- 单轮: mock 边界层脉冲 + 落盘, 校验状态字段与 universe 来源
- 偏好读写: 默认关闭、间隔 clamp [3, 120]
- API: /minute-refresh/status 无服务时 available=false

不发起真实网络请求: TickFlow 侧边界函数与 _write_minute_partition 均
monkeypatch 替换; 自定义源侧用内存 fake provider 走真实边界包装。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from app.services import minute_refresh, preferences
from app.services.minute_refresh import MinuteRefreshService, _in_continuous_session


def _isolated_prefs(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    return path


class _FakeCapSet:
    def __init__(self, has_intraday_batch: bool):
        self._has = has_intraday_batch

    def has(self, cap) -> bool:
        from app.tickflow.capabilities import Cap

        # 服务门控挂 INTRADAY_UNIVERSE, 修复轮用 INTRADAY_BATCH — 两者同档, 一起授/不授
        return self._has and cap in (Cap.INTRADAY_BATCH, Cap.INTRADAY_UNIVERSE)


class _FakeAppState:
    def __init__(self, has_intraday_batch: bool):
        self.capabilities = _FakeCapSet(has_intraday_batch)


class _FakeRepo:
    def __init__(self, symbols: list[str]):
        import threading
        from pathlib import Path
        self._inst = pl.DataFrame({"symbol": symbols})
        self.store = type("S", (), {"data_dir": Path(".")})()
        # 与真实仓库同契约: 分区读-改-写必须持仓库写锁
        self._write_lock = threading.Lock()

    def get_instruments(self) -> pl.DataFrame:
        return self._inst


# ── 时段判定 ────────────────────────────────────────────────────────


def test_continuous_session_boundaries():
    wk = datetime(2026, 8, 25, 10, 0)  # 周二
    assert _in_continuous_session(wk)
    assert not _in_continuous_session(datetime(2026, 8, 25, 9, 29))
    assert not _in_continuous_session(datetime(2026, 8, 25, 11, 31))   # 午休
    assert _in_continuous_session(datetime(2026, 8, 25, 13, 0))        # 午后恢复
    assert _in_continuous_session(datetime(2026, 8, 25, 15, 0))        # 收盘瞬时
    assert not _in_continuous_session(datetime(2026, 8, 25, 15, 1))
    assert not _in_continuous_session(datetime(2026, 8, 22, 10, 0))    # 周六


# ── 门控链 ──────────────────────────────────────────────────────────


def _svc(tmp_path, monkeypatch, *, enabled=True, capability=True, in_hours=True,
         full_minute_provider="tickflow", custom=None):
    """custom 非 None 时模拟「full_minute 路由到声明该数据集的自定义源」:
    resolver 直接返回 fake provider (注册表在测试环境未加载)。"""
    _isolated_prefs(tmp_path, monkeypatch)
    preferences.save({"minute_refresh_enabled": enabled})
    monkeypatch.setattr(
        preferences, "get_full_minute_data_provider", lambda: full_minute_provider,
    )
    if custom is not None:
        monkeypatch.setattr(
            "app.services.kline_sync._resolve_full_minute_provider",
            lambda name: (custom, False, None),
        )
    svc = MinuteRefreshService(_FakeRepo(["600000.SH"]))
    svc.set_app_state(_FakeAppState(capability))
    monkeypatch.setattr(minute_refresh, "_in_continuous_session", lambda now=None: in_hours)
    # 交易日探针默认未知 (None → 放行): 隔离真实网络探测, holiday 分支在
    # test_trading_day.py 单独覆盖
    from app.services import trading_day
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: None)
    return svc


def test_gate_disabled(tmp_path, monkeypatch):
    assert _svc(tmp_path, monkeypatch, enabled=False)._gate_reason() == "disabled"


def test_gate_full_minute_custom_provider_runs(tmp_path, monkeypatch):
    """全量分钟路由到自定义源: 不再让位 — 能力口径统一 (capset 增广后放行)。"""
    class _P:
        def get_intraday_batch(self, symbols, count=300, asset_type="stock"):
            return pl.DataFrame()
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="myfm", custom=_P())
    assert svc._gate_reason() is None
    assert svc.active_provider() == "myfm"
    assert svc.status()["provider"] == "myfm"


def test_gate_capability_missing(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, capability=False)
    assert svc._gate_reason() == "capability"


def test_gate_outside_trading_hours(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, in_hours=False)
    assert svc._gate_reason() == "outside_trading_hours"


def test_gate_pass(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    assert svc._gate_reason() is None
    assert svc.capability_ok() and svc.active_provider() == "tickflow"


# ── 单轮 ────────────────────────────────────────────────────────────


def test_run_round_writes_partition_and_updates_status(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    minute_df = pl.DataFrame({
        "symbol": ["600000.SH"],
        "datetime": [datetime(2026, 8, 25, 1, 30)],
        "open": [10.0], "high": [10.5], "low": [9.9], "close": [10.2],
        "volume": [1000.0], "amount": [10200.0],
    })
    calls: dict = {}

    def fake_burst(symbols, capset, *, count=300):
        calls["symbols"] = list(symbols)
        return (minute_df, 1)

    def fake_write(df, minute_dir, **_kw):
        calls["dir"] = minute_dir
        calls["rows"] = df.height
        calls["origin"] = _kw.get("origin")
        return df.height

    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_full_market_burst", fake_burst
    )
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition", fake_write)

    svc._run_round()

    assert calls["symbols"] == ["600000.SH"]
    assert calls["rows"] == 1
    assert calls["origin"] == "refresh", "全量分钟轮的落盘必须带 origin=refresh"
    st = svc.status()
    assert st["rounds"] == 1
    assert st["last_rows"] == 1
    assert st["last_symbols"] == 1
    assert st["last_requests"] == 1
    assert st["last_round_at"] is not None
    assert st["last_error"] is None
    assert st["capability_ok"] is True


def test_run_round_records_error_when_burst_empty(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_full_market_burst",
        lambda symbols, capset, *, count=300: (pl.DataFrame(), 3),
    )
    svc._run_round()
    st = svc.status()
    assert st["rounds"] == 0
    assert "no data" in st["last_error"]
    assert st["last_requests"] == 3


# ── 两段式模式选择: 冷启动全天 → 稳态增量 ───────────────────────────


def _patch_round(monkeypatch, *, lag, inc_df, burst_df):
    calls: dict = {"modes": []}

    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: lag, raising=True,
    )
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_universe_increment",
        lambda *a, **k: (calls["modes"].append("increment"), (inc_df, 1))[1],
    )
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_full_market_burst",
        lambda symbols, capset, *, count=300: (calls["modes"].append("full"), (burst_df, 28))[1],
    )
    monkeypatch.setattr(
        "app.services.kline_sync._write_minute_partition",
        lambda df, minute_dir, **_kw: df.height,
    )
    return calls


def _inc_df():
    return pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "datetime": [datetime(2026, 8, 25, 10, 0)] * 2,
        "open": [10.0] * 2, "high": [10.5] * 2, "low": [9.9] * 2, "close": [10.2] * 2,
        "volume": [1000.0] * 2, "amount": [10200.0] * 2,
    })


def _full_df():
    return _inc_df()


def test_cold_start_no_local_data_uses_full_mode(tmp_path, monkeypatch):
    """当日无数据 (lag=None, 如 10 点冷启动) → 全天修复轮。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(monkeypatch, lag=None, inc_df=_inc_df(), burst_df=_full_df())
    svc._run_round()
    assert calls["modes"] == ["full"]
    st = svc.status()
    assert st["last_mode"] == "full"
    assert st["last_rows"] == 2


def test_healthy_coverage_uses_increment_mode(tmp_path, monkeypatch):
    """当日覆盖新鲜 (lag ≤ 3 分钟) → universe 单请求增量, 不打 burst。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(monkeypatch, lag=0.2, inc_df=_inc_df(), burst_df=_full_df())
    svc._run_round()
    assert calls["modes"] == ["increment"]
    st = svc.status()
    assert st["last_mode"] == "increment"
    assert st["last_requests"] == 1
    assert st["last_symbols"] == 2
    assert st["last_rows"] == 2


def test_stale_coverage_beyond_bar_headroom_falls_back_to_full(tmp_path, monkeypatch):
    """覆盖滞后超过 3 分钟 (超过 universe 3 根余量) → 全天修复轮。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(monkeypatch, lag=5.0, inc_df=_inc_df(), burst_df=_full_df())
    svc._run_round()
    assert calls["modes"] == ["full"]


def test_consecutive_empty_rounds_escalate_to_full(tmp_path, monkeypatch):
    """universe 连续 2 轮空返回 → 第 3 轮自动升级全天修复 (自愈)。"""
    svc = _svc(tmp_path, monkeypatch)
    calls = _patch_round(
        monkeypatch,
        lag=0.2,
        inc_df=pl.DataFrame(),   # 增量恒空 (模拟 universe 端点持续异常)
        burst_df=_full_df(),
    )
    svc._run_round()
    svc._run_round()
    assert calls["modes"] == ["increment", "increment"]
    assert svc.status()["rounds"] == 0
    svc._run_round()
    assert calls["modes"] == ["increment", "increment", "full"]
    assert svc.status()["last_mode"] == "full"


# ── 自定义源 (full_minute 路由) 轮次 ────────────────────────────────


class _FakeCustomProvider:
    """内存 fake: 按 flags 暴露 get_intraday_batch / get_intraday_latest / get_minute。"""

    def __init__(self, *, batch=False, latest=False, minute=False):
        self.calls: dict = {}
        # 未启用的方法置 None (实例属性遮蔽类方法) — getattr 返回 None → 边界
        # 包装按"未实现"处理, 与真实 provider 只暴露部分方法的形状一致
        if not batch:
            self.get_intraday_batch = None
        if not latest:
            self.get_intraday_latest = None
        if not minute:
            self.get_minute = None

    def get_intraday_batch(self, symbols, count=300, asset_type="stock"):
        self.calls["batch"] = list(symbols)
        return _full_df()

    def get_intraday_latest(self, symbols=None, count=3):
        self.calls["latest"] = count
        return _inc_df()

    def get_minute(self, symbols, start_time=None, end_time=None,
                   asset_type="stock", freq="1m", on_chunk_done=None):
        self.calls["minute"] = {
            "symbols": list(symbols),
            "start": start_time, "end": end_time,
        }
        if on_chunk_done:
            on_chunk_done(1, 4)
        return _full_df()


def _patch_write(monkeypatch) -> dict:
    calls = {"rows": None}
    monkeypatch.setattr(
        "app.services.kline_sync._write_minute_partition",
        lambda df, minute_dir, **_kw: calls.update(rows=df.height) or df.height,
    )
    return calls


def _patch_child_fetch(monkeypatch, provider) -> list[tuple[str, list[str]]]:
    """把「子进程取数」换成进程内直调, 保留「用了哪个端点」的断言。

    真实路径把取数放在子进程里（原生传输层会冻住解释器, 见
    `app/services/minute_fetch_proc.py`）; 单元测试无法跨进程观察 provider.calls,
    因此在这里接管边界, 内部仍调用真实包装函数 `fetch_intraday_custom_*` ——
    时区守卫、chunk 计数、批量→get_minute 回退都照旧被验到。
    """
    from app.services import kline_sync

    calls: list[tuple[str, list[str]]] = []

    def _fake(kind, name, symbols=None, **_kwargs):
        calls.append((kind, list(symbols or [])))
        if kind == "custom_batch":
            return kline_sync.fetch_intraday_custom_batch(provider, name, symbols or [])
        latest = kline_sync.fetch_intraday_custom_latest(provider, name)
        return latest if latest is not None else (pl.DataFrame(), 0)

    monkeypatch.setattr(minute_refresh.minute_fetch_proc, "fetch_via_child", _fake)
    return calls


def test_custom_provider_full_round_via_intraday_batch(tmp_path, monkeypatch):
    """修复轮走 provider.get_intraday_batch (真实边界包装含时区守卫)。"""
    provider = _FakeCustomProvider(batch=True)
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="myfm", custom=provider)
    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: None,  # 冷启动 → 全天修复
    )
    write_calls = _patch_write(monkeypatch)
    kinds = _patch_child_fetch(monkeypatch, provider)
    svc._run_round()
    assert kinds == [("custom_batch", ["600000.SH"])]
    assert provider.calls["batch"] == ["600000.SH"]
    assert write_calls["rows"] == 2
    st = svc.status()
    assert st["last_mode"] == "full"
    assert st["provider"] == "myfm"
    assert st["provider_effective"] == "myfm"


def test_custom_provider_increment_round_via_latest(tmp_path, monkeypatch):
    """实现 get_intraday_latest 的源: 覆盖新鲜时走稳态增量, 不打全天批量。"""
    provider = _FakeCustomProvider(batch=True, latest=True)
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="myfm", custom=provider)
    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: 0.2,
    )
    _patch_write(monkeypatch)
    kinds = _patch_child_fetch(monkeypatch, provider)
    svc._run_round()
    assert kinds == [("custom_latest", [])]
    assert provider.calls == {"latest": 3}
    st = svc.status()
    assert st["last_mode"] == "increment"
    assert st["last_requests"] == 1
    assert st["repair_only"] is False


def test_custom_provider_latest_missing_forces_full_with_minute_fallback(tmp_path, monkeypatch):
    """无 get_intraday_latest: 增量轮退化为修复轮, 批量未实现时回退 get_minute
    (当日窗口), 节奏下限抬到 60s。"""
    provider = _FakeCustomProvider(minute=True)
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="myfm", custom=provider)
    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: 0.2,  # 覆盖新鲜 → 本应增量
    )
    _patch_write(monkeypatch)
    kinds = _patch_child_fetch(monkeypatch, provider)
    svc._run_round()
    # 增量退化为修复: 走 get_minute 回退 (chunk 回调统计请求数)
    assert kinds == [("custom_batch", ["600000.SH"])]
    assert "minute" in provider.calls
    assert provider.calls["minute"]["symbols"] == ["600000.SH"]
    assert svc.status()["last_mode"] == "full"
    assert svc.status()["last_requests"] == 4
    assert svc.repair_only() is True
    assert svc._effective_interval() == 60


def test_custom_provider_unresolved_fails_closed(tmp_path, monkeypatch):
    """失效的 full_minute 自定义源不再静默降级并请求 TickFlow。"""
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="not-registered")
    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: 0.2,
    )
    _patch_write(monkeypatch)
    modes: list[str] = []
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_universe_increment",
        lambda *a, **k: (modes.append("tf-inc"), (_inc_df(), 1))[1],
    )
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_full_market_burst",
        lambda symbols, capset, *, count=300: (modes.append("tf-full"), (_full_df(), 28))[1],
    )
    svc._run_round()
    assert modes == []
    st = svc.status()
    assert st["provider_effective"] == "unavailable:not-registered"
    assert "provider unavailable" in st["last_error"]
    assert st["last_requests"] == 0
    assert st["last_symbols"] == 0


def test_unloaded_full_minute_provider_does_not_fall_back_to_tickflow(tmp_path, monkeypatch):
    """偏好仍选自定义源但插件卸载后，不能借旧能力快照发 TickFlow 请求。"""
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="tickflow")
    preferences.save({"full_minute_data_provider": "missing_src"})
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_universe_increment",
        lambda *args, **kwargs: (calls.append("tickflow"), (pl.DataFrame(), 1))[1],
    )
    svc._run_round()
    assert calls == []
    assert svc.status()["provider_effective"] == "unavailable:missing_src"


def test_custom_provider_without_full_minute_dataset_fails_closed(tmp_path, monkeypatch):
    """已注册但未声明 full_minute 的源不能借共享能力集合请求 TickFlow。"""
    from app.data_providers import custom as custom_sources

    svc = _svc(tmp_path, monkeypatch, full_minute_provider="other")
    provider = type("Provider", (), {"config": type("Config", (), {"datasets": {"minute": object()}})()})()
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.kline_sync.fetch_intraday_universe_increment",
        lambda *args, **kwargs: (calls.append("tickflow"), (pl.DataFrame(), 1))[1],
    )
    monkeypatch.setattr(minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes", lambda self: 0.2)

    svc._run_round()

    assert calls == []
    assert svc.status()["provider_effective"] == "unavailable:other"
    assert "provider unavailable" in svc.status()["last_error"]


def test_status_reports_gate_reason_when_stopped(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, enabled=False)
    st = svc.status()
    assert st["enabled"] is False
    assert st["running"] is False
    assert st["gate_reason"] == "disabled"
    assert st["interval_seconds"] == 6


# ── 偏好 ────────────────────────────────────────────────────────────


def test_refresh_preferences_defaults_and_clamp(tmp_path, monkeypatch):
    _isolated_prefs(tmp_path, monkeypatch)
    assert preferences.get_minute_refresh_enabled() is False
    assert preferences.get_minute_refresh_interval() == 6
    preferences.save({"minute_refresh_interval": 1})
    assert preferences.get_minute_refresh_interval() == 3  # 下限
    preferences.save({"minute_refresh_interval": 999})
    assert preferences.get_minute_refresh_interval() == 120  # 上限
    preferences.save({"minute_refresh_interval": 15})
    assert preferences.get_minute_refresh_interval() == 15

def test_realtime_monitor_config_owns_refresh_keys(tmp_path, monkeypatch):
    """盘中增量配置归属实时监控端点 (set_realtime_monitor_config), 并 clamp 到 [3,120]。"""
    _isolated_prefs(tmp_path, monkeypatch)
    saved = preferences.set_realtime_monitor_config({
        "minute_refresh_enabled": True,
        "minute_refresh_interval": 1,   # 越界 → clamp 到下限
    })
    assert saved["minute_refresh_enabled"] is True
    assert saved["minute_refresh_interval"] == 3
    saved = preferences.set_realtime_monitor_config({"minute_refresh_interval": 400})
    assert saved["minute_refresh_interval"] == 120
    saved = preferences.set_realtime_monitor_config({"minute_refresh_interval": 6})
    assert saved["minute_refresh_interval"] == 6


def test_status_endpoint_without_service():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.settings import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.get("/api/settings/minute-refresh/status")
    assert resp.status_code == 200
    assert resp.json() == {"available": False}


def test_gate_reason_logs_only_on_change(tmp_path, monkeypatch, caplog):
    """「开着但不跑」必须能查: 原因变化时落一条, 相同原因不刷屏。"""
    svc = _svc(tmp_path, monkeypatch)
    with caplog.at_level("INFO", logger="app.services.minute_refresh"):
        svc._note_gate("disabled")
        svc._note_gate("disabled")
        svc._note_gate("capability")
        svc._note_gate(None)

    messages = [
        r.getMessage() for r in caplog.records
        if "暂停刷新" in r.getMessage() or "门控放行" in r.getMessage()
    ]
    assert len(messages) == 3, messages
    assert "disabled" in messages[0]
    assert "capability" in messages[1]
    assert "放行" in messages[2]


# --------------------------------------------------------------- 取数池筛选
def _seed_daily(root, rows):
    import polars as pl
    from datetime import date
    part = root / "kline_daily" / "date=2026-11-12"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "date": [r[1] for r in rows],
        "open": [10.0] * len(rows), "high": [10.0] * len(rows),
        "low": [10.0] * len(rows), "close": [10.0] * len(rows),
        "volume": [1.0] * len(rows), "amount": [1.0] * len(rows),
    }).write_parquet(part / "part.parquet")


def test_tradable_symbols_drops_delisted_keeps_fresh_and_extra(tmp_path):
    """退市票 (从无日线) 剔除; 新上市 / 今日已有分钟行 必须保留。"""
    import polars as pl
    from datetime import date

    _seed_daily(tmp_path, [
        ("600000.SH", date(2026, 11, 12)),
        ("600001.SH", date(2026, 10, 20)),   # 一个月前停牌 → 仍在窗口内
    ])
    inst = pl.DataFrame({
        "symbol": ["600000.SH", "600001.SH", "600002.SH", "600003.SH"],
        "listing_date": ["1999-01-01", "1999-01-01", "1999-01-01", "2026-11-10"],
    })

    kept = minute_refresh.tradable_symbols(
        tmp_path, inst, today=date(2026, 11, 13), extra={"600003.SH"},
    )
    # 600002 从无日线且非新上市 → 剔除; 600003 新上市 + 今日已有行 → 保留
    assert kept == ["600000.SH", "600001.SH", "600003.SH"]


def test_tradable_symbols_fails_open_when_daily_unreadable(tmp_path):
    """读不到日线时抛错, 让调用方退回完整维表 —— 绝不返回空池。"""
    import polars as pl
    from datetime import date

    inst = pl.DataFrame({"symbol": ["600000.SH"], "listing_date": ["1999-01-01"]})
    with pytest.raises(RuntimeError):
        minute_refresh.tradable_symbols(tmp_path, inst, today=date(2026, 11, 13))


def test_universe_falls_back_to_instruments_on_scan_error(tmp_path, monkeypatch):
    """筛选异常 → 用完整维表 (少拉票是缺口, 空池是事故)。"""
    import polars as pl
    from datetime import date

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        svc._repo, "get_instruments",
        lambda: pl.DataFrame({"symbol": ["600000.SH", "600002.SH"]}),
    )
    monkeypatch.setattr(minute_refresh, "cn_today", lambda: date(2026, 11, 13))
    monkeypatch.setattr(
        minute_refresh, "tradable_symbols",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert svc._universe() == ["600000.SH", "600002.SH"]


def test_round_passes_whole_pool_in_one_call(tmp_path, monkeypatch):
    """分片只能有一处: provider 内部按游标取 500 只, 这一层不许再切。

    2026-11-13 事故: 在 `minute_refresh` 又按 500 切了一次 ⇒ 一轮变成 12 次
    调用、每次 200s+、整轮 40 分钟且每块撞超时 → 0 行落盘。
    """
    provider = _FakeCustomProvider(batch=True)
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="myfm", custom=provider)
    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: None,
    )
    _patch_write(monkeypatch)
    calls = []
    def _fake(kind, name, symbols=None, **_kwargs):
        calls.append((kind, list(symbols or [])))
        return provider.get_intraday_batch(symbols), 1

    monkeypatch.setattr(minute_refresh.minute_fetch_proc, "fetch_via_child", _fake)
    svc._run_round()
    assert len(calls) == 1, calls
    kind, passed = calls[0]
    assert kind == "custom_batch"
    assert passed == ["600000.SH"]


def test_round_passes_whole_pool_in_one_call(tmp_path, monkeypatch):
    """分片只能有一处: provider 内部按游标取 500 只, 这一层不许再切。

    2026-11-13 事故: 在 `minute_refresh` 又按 500 切了一次 ⇒ 一轮变成 12 次
    调用、每次 200s+、整轮 40 分钟且每块撞超时 → 0 行落盘。
    """
    provider = _FakeCustomProvider(batch=True)
    svc = _svc(tmp_path, monkeypatch, full_minute_provider="myfm", custom=provider)
    monkeypatch.setattr(
        minute_refresh.MinuteRefreshService, "_today_coverage_lag_minutes",
        lambda self: None,
    )
    _patch_write(monkeypatch)
    calls = []
    def _fake(kind, name, symbols=None, **_kwargs):
        calls.append((kind, list(symbols or [])))
        return provider.get_intraday_batch(symbols), 1

    monkeypatch.setattr(minute_refresh.minute_fetch_proc, "fetch_via_child", _fake)
    svc._run_round()
    assert len(calls) == 1, calls
    kind, passed = calls[0]
    assert kind == "custom_batch"
    assert passed == ["600000.SH"]
