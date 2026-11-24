"""清库任务化契约：单飞拒绝复用、清任务保留自己的记录、终止钩子恢复分钟增量。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import data as data_api
from app.api import pipeline as pipeline_api
from app.services import pipeline_jobs
from app.services.pipeline_jobs import JobStore


def test_submit_job_refuses_to_reuse_an_active_job_for_clear(tmp_path: Path, monkeypatch) -> None:
    """破坏性任务绝不能复用别人的 job_id —— 调用方会以为自己的清库跑了。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_api, "job_store", store)
    active_id, _ = store.create(kind="pipeline")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            pipeline_api._submit_job(
                None, None, lambda _progress: (lambda: {}), kind="clear", allow_reuse=False,
            )
        )

    assert exc_info.value.status_code == 409
    assert active_id in str(exc_info.value.detail)
    # 没有新建任何任务
    assert store.active_id() == active_id


def test_clear_keeps_its_own_job_record_and_cancel_flag(tmp_path: Path, monkeypatch) -> None:
    """清库跑到一半会清空任务历史；自己的记录/active_id/取消标志必须留下。"""
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    store = JobStore(store_dir=tmp_path / "jobs")
    job_id, _ = store.create(kind="clear")
    stale = tmp_path / "jobs" / "deadbeef00.json"
    stale.write_text("{}", encoding="utf-8")

    store.clear(keep=job_id)

    assert store.active_id() == job_id
    assert (store.get(job_id) or {}).get("kind") == "clear"
    assert not stale.exists()
    assert job_id in pipeline_jobs._CANCEL_FLAGS


def test_clear_without_keep_wipes_everything(tmp_path: Path) -> None:
    """没有 keep 时保持旧语义（供别处/测试使用）。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    job_id, _ = store.create()
    store.clear()
    assert store.active_id() is None
    assert store.get(job_id) is None


def test_restore_minute_refresh_hook(monkeypatch) -> None:
    calls: list[str] = []

    class _M:
        def start(self) -> None:
            calls.append("start")

    monkeypatch.setattr(data_api, "_MINUTE_REFRESH_STOPPED", {"job-x": _M()})
    assert data_api.restore_minute_refresh("job-x") is True
    assert calls == ["start"]
    assert data_api.restore_minute_refresh("job-x") is False  # 幂等


def test_run_clear_restores_minute_refresh_even_on_failure(tmp_path: Path, monkeypatch) -> None:
    """清库失败/取消也必须把分钟增量恢复回去（否则它永久停摆）。"""
    calls: list[str] = []
    finished: list[str] = []

    class _M:
        def status(self):
            return {"running": True}

        def stop(self):
            calls.append("stop")

        def start(self):
            calls.append("start")

    class _Repo:
        class store:
            data_dir = tmp_path

        def clear_cache(self):
            pass

        def refresh_cache(self):
            pass

        def rebuild_views(self):
            pass

    state = type("S", (), {"repo": _Repo(), "minute_refresh": _M()})()

    def boom(_state, _progress=None):
        finished.append("impl")
        raise RuntimeError("boom")

    monkeypatch.setattr(data_api, "_clear_data_impl", boom)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})

    with pytest.raises(RuntimeError, match="boom"):
        data_api._run_clear(state, lambda *a, **k: None)

    assert finished == ["impl"]
    assert calls == ["stop", "start"]
    assert data_api._MINUTE_REFRESH_STOPPED == {}
