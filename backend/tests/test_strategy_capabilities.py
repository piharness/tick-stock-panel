"""策略能力表与门控 — 保存期、读取时标记、运行时 fail-closed。

契约:
- 能力项统一为 {supported, reason}; composite/minute_filter 不支持
  monitor/overlay/walkforward/paper;
- matrix_native 的 overlay 支持但仅限数值字段;
- 监控列表读取时派生 runnable/unsupported_reason, 不回写规则文件;
- 保存只拦「新建」与「停用 → 启用」, 已存在规则仍可改名/改绑/停用;
- 运行时按能力表跳过, 不依赖调用点各自硬编码后端集合。
"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import monitor_rules as monitor_rules_api
from app.api.strategy import _strategy_detail, _validate_overlay_filter
from app.strategy import monitor_rules
from app.strategy.capabilities import (
    resolve_monitor_capability,
    strategy_capabilities,
    strategy_capability,
)
from app.strategy.monitor import MonitorRuleEngine


def _strategy(backend: str, sid: str = "s1") -> SimpleNamespace:
    return SimpleNamespace(
        meta={"id": sid, "name": sid, "params": []},
        source="custom",
        execution_backend=backend,
        basic_filter={},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        take_profit=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
    )


class _StrategyEngine:
    def __init__(self, backends: dict[str, str]):
        self._backends = backends

    def get(self, sid: str):
        backend = self._backends.get(sid)
        if backend is None:
            raise ValueError(f"unknown strategy: {sid}")
        return _strategy(backend, sid)

    def validate_context(self, strategy, context) -> None:
        return None


def _request(tmp_path, backends: dict[str, str] | None = None):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(
        repo=repo,
        strategy_engine=_StrategyEngine(backends or {}),
        monitor_engine=None,
        capabilities=None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _rule(rid: str, sid: str, *, enabled: bool = True) -> dict:
    return monitor_rules.normalize(
        {
            "id": rid,
            "name": rid,
            "type": "strategy",
            "strategy_id": sid,
            "scope": "all",
            "enabled": enabled,
            "notify_events": ["pool_entry"],
        }
    )


def _post(request, rid: str, sid: str, *, enabled: bool = True, name: str | None = None):
    model = monitor_rules_api.RuleModel(
        id=rid,
        name=name or rid,
        type="strategy",
        strategy_id=sid,
        scope="all",
        enabled=enabled,
        notify_events=["pool_entry"],
    )
    return monitor_rules_api.save_rule(model, request)


# ── 能力表 ──────────────────────────────────────────────

def test_supported_backends_expose_all_capabilities() -> None:
    for backend in ("polars_expr", "matrix_native", "python_history_legacy"):
        caps = strategy_capabilities(backend)
        assert set(caps) == {"monitor", "overlay", "walkforward", "paper"}
        assert all(caps[key]["supported"] for key in caps)


def test_matrix_overlay_is_supported_with_numeric_constraint() -> None:
    overlay = strategy_capability("matrix_native", "overlay")
    assert overlay["supported"] is True
    assert "数值字段" in overlay["reason"]


@pytest.mark.parametrize("backend", ["composite", "minute_filter"])
def test_composite_and_minute_are_unsupported_everywhere(backend: str) -> None:
    caps = strategy_capabilities(backend)
    for feature in ("monitor", "overlay", "walkforward", "paper"):
        assert caps[feature]["supported"] is False
        assert caps[feature]["reason"]


def test_unknown_capability_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown strategy capability"):
        strategy_capability("polars_expr", "nope")


# ── 策略详情 ────────────────────────────────────────────

def test_strategy_detail_includes_capabilities() -> None:
    detail = _strategy_detail(_strategy("composite"))
    assert detail["capabilities"]["monitor"]["supported"] is False
    assert detail["capabilities"]["overlay"]["supported"] is False
    assert detail["capabilities"]["walkforward"]["supported"] is False
    assert detail["capabilities"]["paper"]["supported"] is False

    matrix_detail = _strategy_detail(_strategy("matrix_native"))
    assert matrix_detail["capabilities"]["overlay"] == {
        "supported": True,
        "reason": "matrix_native 策略仅支持数值字段",
    }


# ── 叠加条件保存校验 ────────────────────────────────────

@pytest.mark.parametrize("backend", ["composite", "minute_filter"])
def test_overlay_empty_list_is_equivalent_to_none(backend: str) -> None:
    strategy = SimpleNamespace(execution_backend=backend)
    _validate_overlay_filter(strategy, {})
    _validate_overlay_filter(strategy, {"overlay_filter": None})
    _validate_overlay_filter(strategy, {"overlay_filter": []})


def test_overlay_matrix_rejects_string_fields_at_save(monkeypatch) -> None:
    from app.factors import ext_factors

    monkeypatch.setattr(ext_factors, "ext_string_fields", lambda: frozenset({"ext_x_name"}))
    with pytest.raises(HTTPException, match="字符串字段"):
        _validate_overlay_filter(
            SimpleNamespace(execution_backend="matrix_native"),
            {
                "overlay_filter": [
                    {"left": "ext_x_name", "op": "contains", "right": "AI"}
                ]
            },
        )


# ── 监控列表读取时标记 ──────────────────────────────────

def test_list_rules_marks_unsupported_and_missing_strategies(tmp_path) -> None:
    monitor_rules.save_one(tmp_path, _rule("r_comp", "comp"))
    monitor_rules.save_one(tmp_path, _rule("r_ok", "ok"))
    monitor_rules.save_one(tmp_path, _rule("r_ghost", "ghost"))

    listed = monitor_rules_api.list_rules(
        _request(tmp_path, {"comp": "composite", "ok": "polars_expr"})
    )
    by_id = {rule["id"]: rule for rule in listed["rules"]}

    assert by_id["r_comp"]["runnable"] is False
    assert "暂不支持实时监控" in by_id["r_comp"]["unsupported_reason"]
    assert by_id["r_ok"]["runnable"] is True
    assert "unsupported_reason" not in by_id["r_ok"]
    assert by_id["r_ghost"]["runnable"] is False
    assert "不存在" in by_id["r_ghost"]["unsupported_reason"]

    # 读取时标记是派生值, 不得写回规则文件, 也不改变启用状态
    saved = monitor_rules.load_one(tmp_path, "r_comp")
    assert "runnable" not in saved
    assert "unsupported_reason" not in saved
    assert saved["enabled"] is True


def test_monitor_rules_validate_does_not_gate_capabilities() -> None:
    """旧配置/备份导入只做结构校验; 能力门控在 API 保存路径。"""
    monitor_rules.validate(_rule("r_comp", "comp"))


def test_resolve_monitor_capability_fails_closed_without_engine() -> None:
    assert resolve_monitor_capability(None, "s1") == {
        "supported": False,
        "reason": "策略引擎未初始化",
    }


# ── 保存门控 ────────────────────────────────────────────

def test_new_unsupported_monitor_rule_is_blocked(tmp_path) -> None:
    request = _request(tmp_path, {"comp": "composite"})
    for enabled in (True, False):
        with pytest.raises(HTTPException) as exc_info:
            _post(request, "r_new", "comp", enabled=enabled)
        assert exc_info.value.status_code == 400
        assert "暂不支持实时监控" in exc_info.value.detail


def test_existing_unsupported_rule_can_rename_disable_and_rebind(tmp_path) -> None:
    monitor_rules.save_one(tmp_path, _rule("r_old", "comp", enabled=True))
    request = _request(
        tmp_path, {"comp": "composite", "minute": "minute_filter"}
    )

    # 已启用 → 改名/改绑仍允许 (不因升级把用户锁死)
    _post(request, "r_old", "minute", enabled=True, name="改名后")
    saved = monitor_rules.load_one(tmp_path, "r_old")
    assert saved["name"] == "改名后"
    assert saved["strategy_id"] == "minute"

    # 已启用 → 停用允许
    _post(request, "r_old", "minute", enabled=False, name="改名后")
    assert monitor_rules.load_one(tmp_path, "r_old")["enabled"] is False

    # 停用 → 启用被拦
    with pytest.raises(HTTPException) as exc_info:
        _post(request, "r_old", "minute", enabled=True, name="改名后")
    assert exc_info.value.status_code == 400


def test_supported_strategy_monitor_rule_can_be_created(tmp_path) -> None:
    request = _request(tmp_path, {"ok": "polars_expr"})
    response = _post(request, "r_ok", "ok")
    assert response["ok"] is True
    assert monitor_rules.load_one(tmp_path, "r_ok")["enabled"] is True


# ── 运行时 fail-closed ──────────────────────────────────

@pytest.mark.parametrize("backend", ["composite", "minute_filter"])
def test_monitor_runtime_skips_unsupported_backend(backend: str) -> None:
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(
        SimpleNamespace(get=lambda _sid: SimpleNamespace(execution_backend=backend))
    )
    rule = {"id": "r1", "strategy_id": "s1", "asset_type": "stock"}
    assert engine._match_strategy(pl.DataFrame({"symbol": ["A"]}), rule) == []
