"""回归测试: 任务列表读盘的解析缓存。

背景: `list_recent()` 原先每次调用都对目录里全部 job 文件重读+重新 JSON 解析。
容器内实测 49 个文件(合计 549KB) **105ms/次**(首轮 285ms), 而它出现在:
  - Layout 的常驻轮询(每 15s, 有活跃任务时 2s);
  - `/api/maintenance` —— 每次请求调 2 遍 ⇒ 端点 225ms;
  - `/api/data/status` 的 `_last_finished`。

契约(两面都要守):
  1. 未变化的文件不得重复解析 —— 否则缓存等于没做;
  2. 磁盘内容变化必须立即可见, 判据是文件自身 (mtime_ns, size), 不是时间窗口。
     第 2 条尤其重要: job_store 与宿主脚本共用 data/, 别的进程写入的记录同样
     要对本进程的轮询可见。
"""
from __future__ import annotations

import json
import os

import app.services.pipeline_jobs as pipeline_jobs
from app.services.pipeline_jobs import JobStore


def _write_job(d, jid: str, *, started_at: str, status: str = "succeeded") -> None:
    (d / f"{jid}.json").write_text(
        json.dumps(
            {
                "id": jid,
                "kind": "pipeline",
                "status": status,
                "stage": "done",
                "progress": 100,
                "stage_pct": 100,
                "started_at": started_at,
                "finished_at": started_at,
                "duration_s": 1.0,
                "result": {"daily_days": 1},
                "error": None,
            }
        ),
        encoding="utf-8",
    )


def _counting_loads(monkeypatch) -> dict:
    counter = {"n": 0}
    real = json.loads

    def _counting(text, *args, **kwargs):
        counter["n"] += 1
        return real(text, *args, **kwargs)

    monkeypatch.setattr(pipeline_jobs.json, "loads", _counting)
    return counter


def _store(tmp_path) -> JobStore:
    d = tmp_path / "job_store"
    d.mkdir()
    return JobStore(store_dir=d)


def test_repeat_list_does_not_reparse_unchanged_files(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for i in range(5):
        _write_job(tmp_path / "job_store", f"job{i}", started_at=f"2026-11-2{i}T02:00:00Z")

    # 首次列表建立缓存
    assert len(store.list_recent(20)) == 5
    counter = _counting_loads(monkeypatch)
    assert len(store.list_recent(20)) == 5
    assert counter["n"] == 0, "签名未变的文件不得重复解析"


def test_new_file_is_picked_up(tmp_path):
    store = _store(tmp_path)
    d = tmp_path / "job_store"
    _write_job(d, "old", started_at="2026-11-20T02:00:00Z")
    assert [j["id"] for j in store.list_recent(20)] == ["old"]

    _write_job(d, "new", started_at="2026-11-24T02:00:00Z")
    assert [j["id"] for j in store.list_recent(20)] == ["new", "old"]


def test_rewritten_file_is_reparsed(tmp_path):
    """同一路径被重写(终态落盘): 必须读到新内容, 不能拿缓存里的旧状态。"""
    store = _store(tmp_path)
    d = tmp_path / "job_store"
    _write_job(d, "j", started_at="2026-11-24T02:00:00Z", status="running")
    assert store.list_recent(1)[0]["status"] == "running"

    _write_job(d, "j", started_at="2026-11-24T02:00:00Z", status="succeeded")
    # 同尺寸改写时 mtime 粒度可能不变, 显式推进一档模拟真实落盘
    path = d / "j.json"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert store.list_recent(1)[0]["status"] == "succeeded"


def test_deleted_file_drops_out_of_list_and_cache(tmp_path):
    store = _store(tmp_path)
    d = tmp_path / "job_store"
    _write_job(d, "gone", started_at="2026-11-20T02:00:00Z")
    _write_job(d, "stay", started_at="2026-11-21T02:00:00Z")
    assert len(store.list_recent(20)) == 2

    (d / "gone.json").unlink()
    assert [j["id"] for j in store.list_recent(20)] == ["stay"]
    assert "gone.json" not in store._file_cache, "已删除的文件不得留在解析缓存里"


def test_unparsable_file_does_not_break_list(tmp_path):
    store = _store(tmp_path)
    d = tmp_path / "job_store"
    _write_job(d, "ok", started_at="2026-11-21T02:00:00Z")
    (d / "broken.json").write_text("{not json", encoding="utf-8")

    assert [j["id"] for j in store.list_recent(20)] == ["ok"]
