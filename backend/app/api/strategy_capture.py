"""策略自动捕获 API。

POST 提交社区文本 → 后台线程跑全链 (S1~S5) → 前端轮询 GET 状态。
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import strategy_capture

router = APIRouter(prefix="/api/strategy-capture", tags=["strategy-capture"])
logger = logging.getLogger(__name__)


class CaptureSubmitRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000, description="社区战法文本/描述")
    entry: str = "open_t1"
    exit: str = "close_t2"
    event_type: str = "turnover_shock"


@router.post("")
def submit_capture(req: CaptureSubmitRequest, request: Request) -> dict:
    """提交一次采集, 后台线程执行, 立即返回 uuid。"""
    import uuid as _uuid

    engine = request.app.state.strategy_engine
    repo = request.app.state.repo
    data_dir = request.app.state.repo.store.data_dir
    uid = _uuid.uuid4().hex[:12]

    def _run() -> None:
        try:
            strategy_capture.run_capture(
                data_dir,
                req.text,
                engine=engine,
                repo=repo,
                entry=req.entry,
                exit=req.exit,
                event_type=req.event_type,
                uid=uid,
            )
        except Exception:  # noqa: BLE001 — 采集失败已由 run_capture 内部标 failed
            logger.exception("capture background run raised")

    threading.Thread(target=_run, name="strategy-capture", daemon=True).start()

    return {"status": "submitted", "uuid": uid}


@router.get("")
def list_captures(request: Request, limit: int = 50) -> dict:
    data_dir = request.app.state.repo.store.data_dir
    return {"items": strategy_capture.list_captures(data_dir, limit)}


@router.get("/{uuid}")
def get_capture(uuid: str, request: Request) -> dict:
    data_dir = request.app.state.repo.store.data_dir
    unit = strategy_capture.capture_dir(data_dir) / uuid
    if not unit.is_dir() or not (unit / "meta.json").exists():
        raise HTTPException(status_code=404, detail="采集记录不存在")

    import json

    meta = json.loads((unit / "meta.json").read_text(encoding="utf-8"))
    out = {"meta": meta}
    # 附带 verdict (若有)
    verdict_path = unit / "verdict.json"
    if verdict_path.exists():
        out["verdict"] = json.loads(verdict_path.read_text(encoding="utf-8"))
    # 附带原文 (若有)
    source_path = unit / "source.txt"
    if source_path.exists():
        out["source"] = source_path.read_text(encoding="utf-8")
    return out
