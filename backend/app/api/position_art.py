"""仓位艺术 API —— 一条可执行的仓位规则的当日读数与验收。

GET /api/position-art?confirm_days=10  → 状态、今日建议、规则对比、验收、净值
GET /api/position-art/tail            → 构成轴（换手×市值的 25 格左尾）

装配在 `app.services.position_art`（纯函数 + 缓存），这里只做参数校验与数据根选择。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import settings
from app.services import position_art as pa

router = APIRouter(prefix="/api/position-art", tags=["position-art"])


def _data_dir(request: Request) -> Path:
    override = getattr(request.app.state, "data_dir", None)
    return Path(override) if override else Path(settings.data_dir)


@router.get("")
def get_position_art(
    request: Request,
    confirm_days: int = Query(default=pa.CONFIRM_DEFAULT, ge=0, le=60),
) -> dict:
    if confirm_days not in pa.CONFIRM_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"confirm_days 只接受 {list(pa.CONFIRM_CHOICES)}，收到 {confirm_days}",
        )
    return pa.policy_payload(_data_dir(request), confirm_days)


@router.get("/tail")
def get_position_art_tail(request: Request) -> dict:
    return pa.tail_payload(_data_dir(request))
