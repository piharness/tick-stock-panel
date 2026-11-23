"""数据维护 API —— 一个入口，两条链。

GET  /api/maintenance      行情 / 手册两条链的数据日、是否落后、当前任务
POST /api/maintenance/run  落后才启动；全最新时立即返回 skipped，不创建空任务

任务提交复用 app.api.pipeline._submit_job：单飞、重任务槽、暂停实时行情、
缓存失效都已在那一处实现，这里不复制第二套并发控制。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from app.api.pipeline import _submit_job
from app.config import settings
from app.services import maintenance

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


def _data_dir(request: Request) -> Path:
    override = getattr(request.app.state, "data_dir", None)
    return Path(override) if override else Path(settings.data_dir)


@router.get("")
def get_maintenance(request: Request) -> dict:
    return maintenance.status(_data_dir(request))


@router.post("/run")
async def run_maintenance(request: Request) -> dict:
    data_dir = _data_dir(request)
    st = maintenance.status(data_dir)
    # 全最新就不建任务：空任务会占住单飞槽，让真正的更新排不上。
    if not st["stale"]:
        return {"skipped": True, "status": st}

    repo = request.app.state.repo
    capset = request.app.state.capabilities
    quote_service = getattr(request.app.state, "quote_service", None)
    submitted = await _submit_job(
        repo,
        quote_service,
        lambda progress: (lambda: maintenance.run_all(repo, capset, data_dir, progress)),
    )
    return {**submitted, "status": st}
