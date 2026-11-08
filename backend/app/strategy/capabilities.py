"""策略执行后端能力表 — API、前端与运行时共用的唯一事实来源。

能力项统一为 ``{"supported": bool, "reason": str}``:

- ``supported=False`` 时 ``reason`` 必为可展示的中文原因;
- ``supported=True`` 时 ``reason`` 可为空; 非空表示该能力附带约束
  (例如 matrix_native 的叠加条件只支持数值字段)。

保存期门控与运行时 fail-closed 必须都读取本表, 不在各调用点重复硬编码
后端集合, 避免新增后端时漏掉某一处。
"""
from __future__ import annotations

from typing import Any

FEATURES = ("monitor", "overlay", "walkforward", "paper")

# 明确不支持某能力的执行后端。未列出的后端默认支持全部能力。
_UNSUPPORTED: dict[str, dict[str, str]] = {
    "composite": {
        "monitor": "叠加策略暂不支持实时监控",
        "overlay": "叠加策略暂不支持叠加条件，请对子策略单独配置",
        "walkforward": "步进优化暂不支持叠加策略",
        "paper": "叠加策略暂不支持模拟盘跟单",
    },
    "minute_filter": {
        "monitor": "分钟策略暂不支持实时监控",
        "overlay": "分钟策略暂不支持叠加条件（分钟帧不含 enriched 因子列）",
        "walkforward": "步进优化暂不支持分钟策略",
        "paper": "分钟策略暂不支持模拟盘跟单",
    },
}

# matrix_native 的叠加条件走 float 矩阵, 字符串扩展列无法进入矩阵。
_MATRIX_OVERLAY_REASON = "matrix_native 策略仅支持数值字段"

_MISSING_STRATEGY = {"supported": False, "reason": "策略不存在或已删除"}


def strategy_capabilities(execution_backend: str | None) -> dict[str, dict[str, Any]]:
    """返回某执行后端的完整能力表 (四个能力项均存在)。"""
    backend = str(execution_backend or "polars_expr")
    unsupported = _UNSUPPORTED.get(backend, {})
    result: dict[str, dict[str, Any]] = {}
    for feature in FEATURES:
        reason = unsupported.get(feature, "")
        result[feature] = {"supported": not reason, "reason": reason}
    if backend == "matrix_native":
        result["overlay"] = {"supported": True, "reason": _MATRIX_OVERLAY_REASON}
    return result


def strategy_capability(execution_backend: str | None, feature: str) -> dict[str, Any]:
    """返回单个能力项; feature 非法时抛 ValueError。"""
    if feature not in FEATURES:
        raise ValueError(f"unknown strategy capability: {feature}")
    return strategy_capabilities(execution_backend)[feature]


def resolve_monitor_capability(engine: Any, strategy_id: str | None) -> dict[str, Any]:
    """按策略 id 解析实时监控能力。

    引擎缺失、策略不存在或查询异常时一律 fail-closed 返回不可运行,
    避免列表把死规则显示成可运行。
    """
    if engine is None:
        return {"supported": False, "reason": "策略引擎未初始化"}
    sid = str(strategy_id or "")
    if not sid:
        return dict(_MISSING_STRATEGY)
    try:
        strategy = engine.get(sid)
    except Exception:  # noqa: BLE001 — 引擎查询异常同样按不可用处理
        return dict(_MISSING_STRATEGY)
    if strategy is None:
        return dict(_MISSING_STRATEGY)
    return strategy_capability(getattr(strategy, "execution_backend", None), "monitor")
