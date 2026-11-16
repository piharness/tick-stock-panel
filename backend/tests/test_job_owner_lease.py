"""所有者租约: 补录前必须先问「这条 running 还有人在跑吗」。

背景(2026-11-13 实测): 宿主侧脚本 `import app.main` 触发模块级 JobStore 实例化,
把容器里**正在跑**的任务(progress 44)标成「后端重启,任务中断」, 而容器内存里
仍是 running。根因是旧判据只有一句「凡磁盘上的 pending/running 都是我之前的
进程留下的」—— 在「容器与宿主共用 data/」以及「任何脚本 import 都会实例化
job_store」两种情况下都不成立。

新契约:
  - 每条记录带 `owner_instance`; 每个进程一个 `run/<instance>.lease`, mtime 即心跳;
  - 补录只回收**租约缺失或超过 TTL** 的记录;
  - 用 mtime 而不是 pid —— 容器与宿主 pid 命名空间不同, 同一 pid 可能是两个进程。

这里模拟"进程死亡"用 patch `_INSTANCE` + 清掉旧租约, 不靠"同进程第二个实例"
（那在新语义下恰恰是**活着**的所有者, 见 test_second_store_in_same_process...）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

import app.services.pipeline_jobs as pj
from app.services.pipeline_jobs import JobStore


def _die(store: JobStore, monkeypatch, *, lease: bool) -> None:
    """把 store 变成「已退出的进程」, 并把当前进程伪装成新进程。

    lease=True 时保留一个新鲜租约(模拟"另一个活着的进程"), False 时清掉(真死)。
    """
    store._heartbeat_stop.set()  # 心跳线程不该在我们判死之后再交租
    old_lease = store._lease_path
    monkeypatch.setattr(pj, "_INSTANCE", uuid.uuid4().hex[:8])
    if not lease:
        old_lease.unlink(missing_ok=True)


def _read(d, jid: str) -> dict:
    return json.loads((d / f"{jid}.json").read_text("utf-8"))


def test_record_carries_owner_identity(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    disk = _read(d, jid)
    assert disk["owner_instance"] == pj._INSTANCE
    assert disk["owner_pid"] == os.getpid()
    assert store._lease_path.is_file()


def test_second_store_in_same_process_does_not_reap_live_job(tmp_path):
    """两个 store 实例(模块单例 + 某处新建)不得互相判死。

    这正是线上事故的最小复现: 进程还活着, 只是又有一个 JobStore 对象出现。
    """
    d = tmp_path / "jobs"
    live = JobStore(store_dir=d)
    jid, _ = live.create(timeout_s=60)
    live.start(jid)

    second = JobStore(store_dir=d)  # 同一个进程, 同一个 _INSTANCE
    assert second.get(jid)["status"] == "running"


def test_live_foreign_owner_is_not_reaped(tmp_path, monkeypatch):
    """另一个**活着**的进程(租约新鲜)的记录不许动 —— 容器场景就是它。"""
    d = tmp_path / "jobs"
    other = JobStore(store_dir=d)
    jid, _ = other.create(timeout_s=60)
    _die(other, monkeypatch, lease=True)

    newcomer = JobStore(store_dir=d)
    assert newcomer.get(jid)["status"] == "pending"
    assert _read(d, jid)["status"] == "pending"


def test_dead_owner_is_reaped(tmp_path, monkeypatch):
    """租约消失 = 进程已退出 ⇒ 照旧补录, 记录不能永远停在 running。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    jid, _ = dead.create(timeout_s=60)
    dead.start(jid)
    _die(dead, monkeypatch, lease=False)

    revived = JobStore(store_dir=d)
    j = revived.get(jid)
    assert j["status"] == "failed"
    assert "中断" in j["error"]
    assert j["duration_s"] is not None


def test_stale_lease_is_reaped(tmp_path, monkeypatch):
    """租约存在但超过 TTL(进程被 kill -9, 没来得及清租约) ⇒ 仍要补录。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    jid, _ = dead.create(timeout_s=60)
    _die(dead, monkeypatch, lease=True)
    old = time.time() - (pj.OWNER_LEASE_TTL_S + 60)
    os.utime(dead._lease_path, (old, old))

    revived = JobStore(store_dir=d)
    assert revived.get(jid)["status"] == "failed"


def test_legacy_record_without_owner_is_reaped(tmp_path):
    """升级前写入的记录没有 owner_instance: 维持旧规则(立即补录)。

    过渡期唯一风险是「旧容器仍在跑 + 宿主先跑新代码」, 已在模块 docstring 写明。
    """
    d = tmp_path / "jobs"
    d.mkdir(parents=True)
    (d / "legacy0001.json").write_text(
        json.dumps({"id": "legacy0001", "status": "running", "stage": "sync",
                    "progress": 10, "log": [], "started_at": "2026-11-13T06:00:00Z",
                    "last_progress_at": "2026-11-13T06:00:00Z"}),
        encoding="utf-8",
    )
    store = JobStore(store_dir=d)
    assert store.get("legacy0001")["status"] == "failed"


def test_progress_refreshes_lease(tmp_path, monkeypatch):
    """进度回调顺带交租(限频), 否则长阶段无落盘的活任务会被判死。"""
    monkeypatch.setattr(pj, "_LEASE_TOUCH_MIN_INTERVAL_S", 0)
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    old = time.time() - 120
    os.utime(store._lease_path, (old, old))

    store.progress(jid, "sync_daily", 30, "chunk")
    assert time.time() - store._lease_path.stat().st_mtime < 5


def test_terminal_record_untouched_by_reap(tmp_path, monkeypatch):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.succeed(jid, {"rows": 1})
    _die(store, monkeypatch, lease=False)

    revived = JobStore(store_dir=d)
    assert revived.get(jid)["status"] == "succeeded"


# ── 惰性单例: import 不许有磁盘副作用 ────────────────────────────────────

def test_lazy_proxy_defers_store_creation(tmp_path):
    """代理在被用到之前不建目录 —— 这就是"import 不触发补录"的机制。"""
    d = tmp_path / "jobs"
    proxy = pj._LazyJobStore(store_dir=d)
    assert not d.exists()
    assert proxy.active_id() is None   # 首次访问才建
    assert d.exists()


def test_lazy_proxy_delegates_to_one_instance(tmp_path):
    d = tmp_path / "jobs"
    proxy = pj._LazyJobStore(store_dir=d)
    jid, is_new = proxy.create(timeout_s=60)
    assert is_new is True
    assert proxy.get(jid)["status"] == "pending"
    assert proxy._instance() is proxy._instance()


def test_import_has_no_job_store_side_effect(tmp_path):
    """子进程只 import, 不许在 data/ 下建 job_store —— 否则跑测试就会判死活任务。"""
    import subprocess
    import sys

    backend = str(Path(__file__).resolve().parents[1])
    data_dir = tmp_path / "data"
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "PYTHONPATH": backend,
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import app.services.pipeline_jobs"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not (data_dir / "job_store").exists()
