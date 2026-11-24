"""terminate 必须收回的资源：行情暂停、手册链子进程、执行槽、发布标记。

共同点：都是「任务创建、只在 finally 释放」。线程卡在阻塞调用里回不来时
finally 永不执行 —— 2026-11-24 的现场是任务已判失败、发布标记还挂着、
实时行情一直暂停，只能重启进程。归属统一由 job_context 记录，terminate 收回。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import maintenance as svc_maintenance
from app.services import pipeline_jobs
from app.services.job_context import job_context
from app.services.pipeline_jobs import JobStore
from app.services.quote_service import QuoteService


def test_pause_is_owned_and_counted() -> None:
    """暂停按持有者记账：先退出的那一个不能把仍在运行的那个的暂停解除。"""
    qs = QuoteService()
    qs.pause("job-a")
    qs.pause("job-b")
    assert qs.is_paused()

    qs.resume("job-a")
    assert qs.is_paused()  # job-b 还在跑

    assert qs.force_resume("job-b") is True
    assert not qs.is_paused()
    assert qs.force_resume("job-b") is False


def test_paused_context_uses_current_job() -> None:
    """任务上下文里的 paused() 自动记在 job 名下，terminate 才能按 id 收回。"""
    qs = QuoteService()
    with job_context("job-x"):
        with qs.paused():
            assert qs.is_paused()
    assert not qs.is_paused()


def test_paused_context_without_job_gets_a_unique_token() -> None:
    """普通请求（不在任务里）拿到一次性令牌：并发/嵌套暂停不再互相踩。"""
    qs = QuoteService()
    with qs.paused():
        with qs.paused():
            assert qs.is_paused()
        assert qs.is_paused()  # 内层退出，外层仍在暂停
    assert not qs.is_paused()


def test_terminate_runs_hooks_and_isolates_failures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_jobs, "_TERMINATE_HOOKS", {})
    calls: list[tuple[str, str]] = []
    pipeline_jobs.register_terminate_hook("ok", lambda jid: calls.append(("ok", jid)))

    def boom(_jid: str) -> None:
        raise RuntimeError("hook failed")

    pipeline_jobs.register_terminate_hook("boom", boom)

    store = JobStore(store_dir=tmp_path / "jobs")
    job_id, _ = store.create()
    store.terminate(job_id, "cancelled")

    # 一个钩子炸掉不影响另一个，也不影响 terminate 本身
    assert calls == [("ok", job_id)]
    assert store.get(job_id)["status"] == "failed"


def test_terminate_hook_registry_is_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_jobs, "_TERMINATE_HOOKS", {})
    first = lambda _jid: None  # noqa: E731
    second = lambda _jid: None  # noqa: E731
    pipeline_jobs.register_terminate_hook("quote-pause", first)
    pipeline_jobs.register_terminate_hook("quote-pause", second)
    assert pipeline_jobs._TERMINATE_HOOKS == {"quote-pause": second}


class _FakeChild:
    pid = 4242

    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.killed = False

    def poll(self):
        return None if self.running else 0

    def kill(self) -> None:
        self.killed = True
        self.running = False


def test_kill_handbook_child(monkeypatch) -> None:
    monkeypatch.setattr(svc_maintenance, "_HANDBOOK_CHILDREN", {})
    running = _FakeChild()
    svc_maintenance._HANDBOOK_CHILDREN["job-z"] = running

    assert svc_maintenance.kill_handbook_child("job-z") is True
    assert running.killed is True
    assert "job-z" not in svc_maintenance._HANDBOOK_CHILDREN
    assert svc_maintenance.kill_handbook_child("job-z") is False  # 幂等

    finished = _FakeChild(running=False)
    svc_maintenance._HANDBOOK_CHILDREN["job-w"] = finished
    assert svc_maintenance.kill_handbook_child("job-w") is False
    assert finished.killed is False


@pytest.mark.parametrize("hook_name", ["quote-pause", "handbook-child"])
def test_main_registers_both_terminate_hooks(hook_name: str) -> None:
    """接线由 main 负责：漏注册就等于没修（这里守住名字）。"""
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert f'register_terminate_hook("{hook_name}"' in source
