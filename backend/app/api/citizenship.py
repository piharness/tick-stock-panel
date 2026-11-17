"""市场公民 API —— 全市场多维流动性身份分级与注意力预算。

只读本地 `kline_daily_enriched` 与 `instruments`，计算放在 service，这里只做
参数校验与响应映射。分级结果不写盘、不进缓存持久化（service 内已有进程级缓存）。

接口：
  GET /api/citizenship          全市场公民等级 + 注意力预算
  GET /api/citizenship/levels   等级定义与映射规则（供前端渲染图例/说明）
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import citizenship

router = APIRouter(prefix="/api/citizenship", tags=["citizenship"])


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
def get_citizenship(
    request: Request,
    as_of: str | None = Query(default=None, description="交易日（YYYY-MM-DD），默认取最新"),
):
    target = _parse_date(as_of)
    return citizenship.get_citizenship(_data_dir(request), target)


@router.get("/levels")
def get_levels():
    """等级定义与映射规则。前端据此渲染图例，避免「页面写死一套、后端一套」。"""
    return {
        "order": citizenship.LEVEL_ORDER,
        "labels": citizenship.LEVEL_LABELS,
        "hints": citizenship.LEVEL_HINTS,
        "rules": citizenship.LEVEL_RULES,
        "window_days": citizenship.WINDOW_DAYS,
    }


@router.get("/history")
def get_history(
    request: Request,
    days: int = Query(default=30, ge=1, le=120, description="回看交易日数"),
    end: str | None = Query(default=None, description="截至交易日（YYYY-MM-DD），默认最新"),
):
    """近 N 个交易日的注意力预算时序（等级分布 + 迁移计数），供前端画趋势。"""
    target = _parse_date(end)
    return citizenship.history_summary(_data_dir(request), days=days, end=target)
