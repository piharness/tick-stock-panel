"""策略池 API（后端持久化）。

策略池勾选列表从 localStorage 迁移到后端，提供读写与历史可回查。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import strategy_pool

router = APIRouter(prefix="/api/strategy-pool", tags=["strategy-pool"])


class PoolUpdate(BaseModel):
    ids: list[str] = Field(default_factory=list)
    reason: str = ""


@router.get("")
def get_pool() -> dict:
    return strategy_pool.get()


@router.get("/history")
def get_history(limit: int = Query(50, ge=1, le=500)) -> dict:
    return {"items": strategy_pool.get_history(limit)}


@router.put("")
def put_pool(body: PoolUpdate) -> dict:
    try:
        return strategy_pool.set_pool(body.ids, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/add")
def add_to_pool(body: PoolUpdate) -> dict:
    return strategy_pool.add(body.ids, reason=body.reason)


@router.post("/remove")
def remove_from_pool(body: PoolUpdate) -> dict:
    return strategy_pool.remove(body.ids, reason=body.reason)


@router.post("/reorder")
def reorder_pool(body: PoolUpdate) -> dict:
    try:
        return strategy_pool.reorder(body.ids, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
