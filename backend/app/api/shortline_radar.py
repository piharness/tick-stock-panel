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

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.api.kline import _client_accepts_gzip
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


def _radar_response(request: Request, as_of: str | None) -> Response:
    """预序列化 + 按客户端能力选压缩形态。

    载荷是**全市场 ~5000 只**的分类结果（实测 5.3MB → gzip 1.1MB）。dict 缓存
    命中后计算是 0ms，但每次请求仍要 dumps 84ms + gzip 68ms；这一段已经由
    `citizenship.get_citizenship_serialized` 按 (data_dir, as_of) 缓存住，
    这里只做「选哪一份字节」。

    不返回 dict 让 FastAPI 再序列化一遍：那会把刚省掉的 84ms 又付回去。
    """
    target = _parse_date(as_of)
    raw, packed = citizenship.get_citizenship_serialized(_data_dir(request), target)
    if _client_accepts_gzip(request):
        return Response(
            content=packed,
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
        )
    return Response(content=raw, media_type="application/json")


@router.get("")
def get_shortline_radar(
    request: Request,
    as_of: str | None = Query(default=None, description="交易日（YYYY-MM-DD），默认取最新"),
):
    return _radar_response(request, as_of)


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
        # 重点关注（锁仓横盘）：独立口径，与观察池并列。页面据此渲染条件与实测读数，
        # 不抄第二份常量。
        "lock": {
            "turn60_target": citizenship.LOCK_TURN60_TARGET,
            "turn60_tol": citizenship.LOCK_TURN60_TOL,
            "ret20_target": citizenship.LOCK_RET20_TARGET,
            "ret20_tol": citizenship.LOCK_RET20_TOL,
            "lock_ratio": citizenship.LOCK_RATIO,
            "lock_max_capital": citizenship.LOCK_MAX_CAPITAL,
            "min_amount60": citizenship.LOCK_MIN_AMOUNT60,
            "recognition_cut": citizenship.LOCK_RECOGNITION_CUT,
            "readout": citizenship.LOCK_READOUT,
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
