"""回归测试: job 记录跨进程死亡持久化(「数据在、记录丢」补丁)。

背景(用户反馈): 全市场同步 12:11~12:42 成功结束后 0.7s, uvicorn --reload
检测到代码变更杀死 worker, 恰好落在管道完成与 job_store.succeed() 落盘之间
—— 数据已写盘但同步历史无任何记录。旧实现 pending/running 仅存内存、终态才
落盘, 存在整段丢失窗口。

修复后契约:
  - create()/start() 即落盘 pending/running 快照;
  - 下次进程启动把**所有者已死**的遗留记录补标为 failed(中断), finished_at
    取文件 mtime;
  - 终态记录不受补录影响; 终态写入覆盖 running 快照(同一文件)。
均为纯逻辑, 不触网。

注: 「进程死亡」不能再拿「同目录第二个实例」来模拟 —— 2026-11-13 起判据看
owner 租约, 同进程的第二个实例是**活着**的所有者(见 test_job_owner_lease.py)。
"""
from __future__ import annotations

import json
import uuid

import app.services.pipeline_jobs as pipeline_jobs
from app.services.pipeline_jobs import JobStore


def _read_disk(d, jid: str) -> dict:
    return json.loads((d / f"{jid}.json").read_text("utf-8"))


def _simulate_process_death(store: JobStore, monkeypatch) -> None:
    """把 store 变成「已退出的进程」: 停心跳、清租约、换 instance。"""
    store._heartbeat_stop.set()
    store._lease_path.unlink(missing_ok=True)
    monkeypatch.setattr(pipeline_jobs, "_INSTANCE", uuid.uuid4().hex[:8])


# ── 创建/启动即落盘 ──────────────────────────────────────────────────────

def test_create_writes_pending_snapshot_to_disk(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)

    disk = _read_disk(d, jid)
    assert disk["status"] == "pending"
    assert disk["stage"] == "init"


def test_start_updates_disk_snapshot_to_running(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.start(jid)

    disk = _read_disk(d, jid)
    assert disk["status"] == "running"
    assert disk["started_at"] is not None


# ── 进程死亡 → 下次启动补录 ──────────────────────────────────────────────

def test_orphan_running_record_is_reaped_on_next_boot(tmp_path, monkeypatch):
    """核心场景: 进程死在 running(甚至工作已做完但未终态), 记录必须可见。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    jid, _ = dead.create(timeout_s=60)
    dead.start(jid)
    dead.progress(jid, "sync", 50, "halfway")  # 进度只更新内存
    _simulate_process_death(dead, monkeypatch)

    # 新进程 = 同目录新实例 + 旧租约已消失(内存为空, 只有磁盘)
    revived = JobStore(store_dir=d)
    j = revived.get(jid)
    assert j is not None
    assert j["status"] == "failed"
    assert "中断" in j["error"]
    assert j["finished_at"] is not None
    # finished_at 基于文件 mtime(≈ start 时刻), 时长不得虚增为负或巨大
    assert j["duration_s"] is not None
    assert 0 <= j["duration_s"] <= 60
    # 同步历史列表可见
    assert any(x["id"] == jid for x in revived.list_recent())


def test_orphan_pending_record_is_reaped(tmp_path, monkeypatch):
    """进程死在 create() 与 start() 之间: 记录同样可见, 时长为 None。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    jid, _ = dead.create(timeout_s=60)
    # 未 start 即死亡
    _simulate_process_death(dead, monkeypatch)

    revived = JobStore(store_dir=d)
    j = revived.get(jid)
    assert j["status"] == "failed"
    assert j["duration_s"] is None


def test_reap_does_not_touch_terminal_records(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    store.succeed(jid, {"daily_rows": 100})

    revived = JobStore(store_dir=d)
    j = revived.get(jid)
    assert j["status"] == "succeeded"
    assert j["result"] == {"daily_rows": 100}


def test_reap_allows_new_job_after_dead_orphan(tmp_path, monkeypatch):
    """补录后旧 job 已 failed: 新进程 create() 不被死孤儿阻塞(单飞只看内存)。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    old_jid, _ = dead.create(timeout_s=60)
    dead.start(old_jid)
    _simulate_process_death(dead, monkeypatch)

    revived = JobStore(store_dir=d)
    new_jid, is_new = revived.create(timeout_s=60)
    assert is_new is True
    assert new_jid != old_jid


# ── 终态覆盖快照 ─────────────────────────────────────────────────────────

def test_terminal_write_replaces_running_snapshot(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    store.fail(jid, "boom")

    files = list(d.glob("*.json"))
    assert len(files) == 1
    disk = _read_disk(d, jid)
    assert disk["status"] == "failed"
    assert disk["error"] == "boom"
