"""发布标记按 job 收回：卡死任务不能把后一轮永远挡在 "another publication is active" 外面。

2026-11-24 的链路：任务被 reap → run slot / limiter 容量都强放 → 新任务仍写不了
enriched，因为 publishing 标记的 publication_id 还在 `_ACTIVE_PUBLICATIONS` 里，
`_publication_claim_is_running` 直接短路成 True —— 心跳超时也不生效，只能重启进程。
修法：标记写 owner_job，terminate 时按所有权收回（摘活跃对象 + 换新 generation 写 ready）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import polars as pl
import pytest

from app import enriched_generation as eg
from app.services.pipeline_jobs import JobStore


def _marker(data_dir: Path, asset: str = "stock") -> dict:
    return json.loads(
        (data_dir / f".matrix_generation_{asset}.json").read_text(encoding="utf-8")
    )


def _owned(job_id: str, data_dir: Path, asset: str = "stock") -> eg.EnrichedPublication:
    with eg.publication_owned_by(job_id):
        publication = eg.EnrichedPublication(data_dir, asset, recover=True)
    publication.begin()
    return publication


def test_publishing_marker_records_owner_job(tmp_path: Path) -> None:
    publication = _owned("job-a", tmp_path)
    try:
        payload = _marker(tmp_path)
        assert payload["state"] == "publishing"
        assert payload["owner_job"] == "job-a"
    finally:
        publication.abandon()


def test_publication_outside_a_job_has_no_owner(tmp_path: Path) -> None:
    publication = eg.EnrichedPublication(tmp_path, "stock", recover=True)
    publication.begin()
    try:
        assert "owner_job" not in _marker(tmp_path)
    finally:
        publication.abandon()


def test_abandon_for_job_releases_marker_and_blocks_the_old_writer(tmp_path: Path) -> None:
    publication = _owned("job-a", tmp_path)
    old_generation = _marker(tmp_path)["generation"]

    assert eg.abandon_publication_for_job(tmp_path, "job-a") == ["stock"]

    payload = _marker(tmp_path)
    assert payload["state"] == "ready"
    assert payload["generation"] != old_generation  # 换代：按代缓存整体失效
    assert "publication_id" not in payload and "owner_job" not in payload

    # 僵尸线程恢复后的下一次写入不能再落盘
    out = tmp_path / "kline_daily_enriched" / "date=2026-11-24" / "part.parquet"
    with pytest.raises(eg.EnrichedGenerationUnavailableError):
        publication.write_parquet(pl.DataFrame({"symbol": ["a"]}), out)
    assert not out.exists()

    # 迟到的 abandon 不得覆盖新标记（它按 publication_id 校验所有权）
    publication.abandon()
    assert _marker(tmp_path)["generation"] == payload["generation"]


def test_abandon_for_another_job_leaves_the_marker_alone(tmp_path: Path) -> None:
    publication = _owned("job-b", tmp_path)
    try:
        assert eg.abandon_publication_for_job(tmp_path, "job-a") == []
        payload = _marker(tmp_path)
        assert payload["state"] == "publishing"
        assert payload["owner_job"] == "job-b"
    finally:
        publication.abandon()


def test_abandon_ignores_unowned_publication(tmp_path: Path) -> None:
    publication = eg.EnrichedPublication(tmp_path, "stock", recover=True)
    publication.begin()
    try:
        assert eg.abandon_publication_for_job(tmp_path, "job-a") == []
        assert _marker(tmp_path)["state"] == "publishing"
    finally:
        publication.abandon()


def test_abandon_does_not_hang_when_the_writer_lock_is_held(tmp_path: Path) -> None:
    """terminate 的收回必须有上界：僵尸若正卡在写盘（持进程内锁），不许把请求线程拖住。"""
    publication = _owned("job-a", tmp_path)
    lock = eg._writer_lock(tmp_path, "stock")
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with lock:
            holding.set()
            release.wait(3)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(1)
        started = time.monotonic()
        assert eg.abandon_publication_for_job(tmp_path, "job-a", budget_s=0.2) == []
        assert time.monotonic() - started < 1.5
        assert _marker(tmp_path)["state"] == "publishing"  # 没收回，也没误写
    finally:
        release.set()
        holder.join(3)

    # 锁释放后可以正常收回
    assert eg.abandon_publication_for_job(tmp_path, "job-a") == ["stock"]
    assert _marker(tmp_path)["state"] == "ready"
    publication.abandon()


def test_terminal_job_releases_its_publication(tmp_path: Path) -> None:
    """端到端：JobStore.terminate 之后标记必须已经可读，不用等 600s 心跳超时。"""
    data_dir = tmp_path / "data"
    store = JobStore(store_dir=data_dir / "job_store")
    job_id, is_new = store.create()
    assert is_new

    publication = _owned(job_id, data_dir)
    assert _marker(data_dir)["state"] == "publishing"

    store.terminate(job_id, "cancelled while publishing")

    payload = _marker(data_dir)
    assert payload["state"] == "ready"
    # 再收回一次是幂等的
    assert eg.abandon_publication_for_job(data_dir, job_id) == []
    publication.abandon()
