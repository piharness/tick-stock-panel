"""短线雷达 API —— 全市场博弈角色主场分类 + 属性标签过滤。

对外路径（2026-11-22 更名）：

  GET /api/shortline-radar          主场分类 + 位次标签 + 属性标签
  GET /api/shortline-radar/levels   全部定义与切分规则（前端渲染图例，避免两套口径）

`/api/citizenship` 是旧路径（页面曾叫「市场公民」），保留为**隐藏别名**：
老脚本与书签不 404，但不出现在 OpenAPI 里，新代码只认新路径。

计算内核仍在 `app.services.citizenship`（历史模块名：6 个分析脚本与两篇冻结
研究直接引用它，改模块名会改写研究 provenance）。本层只做参数校验与响应映射。
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import citizenship, shortline_audit

router = APIRouter(prefix="/api/shortline-radar", tags=["shortline-radar"])
legacy_router = APIRouter(prefix="/api/citizenship", include_in_schema=False)


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


def _parse_date(value: str | None) -> date_cls | None:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"as_of 格式应为 YYYY-MM-DD，收到: {value}") from exc


@router.get("")
def get_shortline_radar(
    request: Request,
    as_of: str | None = Query(default=None, description="交易日（YYYY-MM-DD），默认取最新"),
):
    target = _parse_date(as_of)
    return citizenship.get_citizenship(_data_dir(request), target)


@router.get("/levels")
def get_levels():
    """全部定义与切分规则。前端据此渲染图例，避免「页面写死一套、后端一套」。

    `levels` 是降级后的位次标签，`arenas` 是页面主轴，`tags` 是可叠加的过滤条件。
    """
    return {
        "arenas": {
            "order": citizenship.ARENA_ORDER,
            "labels": citizenship.ARENA_LABELS,
            "hints": citizenship.ARENA_HINTS,
            "cut": citizenship.ARENA_CUT,
            "risk": citizenship.ARENA_RISK,
        },
        "tags": {
            "order": citizenship.TAG_ORDER,
            "labels": citizenship.TAG_LABELS,
            "hints": citizenship.TAG_HINTS,
            "cut": citizenship.TAG_CUT,
            "whitehorse_rules": citizenship.WHITEHORSE_RULES,
            "risk": citizenship.TAG_RISK,
        },
        "levels": {
            "order": citizenship.LEVEL_ORDER,
            "labels": citizenship.LEVEL_LABELS,
            "hints": citizenship.LEVEL_HINTS,
            "rules": citizenship.LEVEL_RULES,
        },
        "stages": {
            "order": citizenship.STAGE_ORDER,
            "labels": citizenship.STAGE_LABELS,
            "hints": citizenship.STAGE_HINTS,
        },
        "focus": {
            "top_n": citizenship.FOCUS_TOP_N,
            "min_amount": citizenship.FOCUS_MIN_AMOUNT,
            "heat_cut": citizenship.HEAT_CUT,
            "min_listing_days": citizenship.FOCUS_MIN_LISTING_DAYS,
            "min_ret60": citizenship.FOCUS_MIN_RET60,
        },
        "risk_flags": {
            "order": citizenship.RISK_ORDER,
            "labels": citizenship.RISK_LABELS,
            "rules": citizenship.RISK_RULES,
        },
        "window_days": citizenship.ARENA_WINDOW,
        "stats_window": citizenship.STATS_WINDOW,
        "market_risk": citizenship.MARKET_RISK,
    }


@router.get("/sort-audit")
def get_sort_audit(request: Request):
    """排序体检产出物（预注册 heat-sort-2026-11-24）。

    只读 `data/shortline/sort_audit.json`，不做任何计算与裁决 —— 裁决按
    `docs/structural-edge/prereg-heat-sort.md` 的冻结判据，在登记满 120 个
    交易日后由人执行。产出物由 17:10/20:10 的定时任务或
    `shortline_audit.rebuild()` 刷新。
    """
    art = shortline_audit.load_artifact(_data_dir(request))
    if art is None:
        raise HTTPException(
            404,
            "还没有排序体检产出物（data/shortline/sort_audit.json 不存在）："
            "等 17:10 定时任务，或手工跑 `shortline_audit.rebuild(data_dir)`",
        )
    return art


# 旧路径：兼容老脚本/书签，不进 OpenAPI。
legacy_router.add_api_route("", get_shortline_radar, methods=["GET"], include_in_schema=False)
legacy_router.add_api_route("/levels", get_levels, methods=["GET"], include_in_schema=False)
legacy_router.add_api_route("/sort-audit", get_sort_audit, methods=["GET"], include_in_schema=False)
