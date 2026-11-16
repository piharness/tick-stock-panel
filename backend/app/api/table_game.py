"""对手盘模型 API — 扫描层 / 名册 / 账本 / 个股实例。

与「博弈链路」页一一对应：
  GET /api/table-game/scan      今日榜单 × 席位历史 × 明早的分档规则（先扫再深入）
  GET /api/table-game/overview  12 个参与者对象 + 龙虎榜账本 + 信息链
  GET /api/table-game/board     某只票的事件时间轴 + 九条常识的可观测判定

本路由不采集任何新数据，只读 kline_daily_enriched / dragon_tiger / instruments /
financials/metrics 四张本地表。重算入口：`python -m app.services.table_game --scan`。
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import table_game

router = APIRouter(prefix="/api/table-game", tags=["table-game"])


def _parse_date(value: str | None) -> date_cls | None:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"as_of 格式应为 YYYY-MM-DD, 收到: {value}")


@router.get("/scan")
def get_scan(
    request: Request,
    as_of: str | None = Query(default=None),
    limit: int = Query(default=table_game.SCAN_LIMIT, ge=3, le=60),
):
    """今日可扫描层：谁上榜、谁在买、买的人历史上赚不赚、明早按什么分档决定接不接。"""
    target = _parse_date(as_of)
    return table_game.get_scan(request.app.state.repo.store.data_dir, target, limit)


@router.get("/overview")
def get_overview(request: Request, as_of: str | None = Query(default=None)):
    """12 个对象的名册 + 龙虎榜席位账本 + 信息到达链。"""
    target = _parse_date(as_of)
    return table_game.get_overview(request.app.state.repo.store.data_dir, target)


@router.get("/board")
def get_board(
    request: Request,
    symbol: str = Query(..., description="标的代码，如 002086.SZ"),
    as_of: str | None = Query(default=None),
    days: int = Query(default=60, ge=20, le=250),
):
    """个股实例：事件时间轴 + 规则判定。symbol 不存在时 state=not_in_universe。"""
    if not symbol.strip():
        raise HTTPException(400, "symbol 不能为空")
    return table_game.get_board(
        request.app.state.repo.store.data_dir, symbol.strip().upper(), _parse_date(as_of), days
    )