"""每日名单的定时接线 —— 「盘后自动生成」这句话的唯一证据。

单份名单的测试只能证明「算得对」。它证明不了**它会被算**：
`run_decision_stage` 在管道里被调多少次都不影响「16:40 有没有补一次」。

这一组盯三个不会让别的测试变红的坏法：

1. **闭包晚绑定**：两个时点都报成最后一个 `job_id`，任务列表从此分不清谁失败；
2. **回调签名**：`_run_tracked` 用关键字传 `on_progress`，lambda 不收它就 TypeError，
   而这个任务一天只跑两次——坏了也没人当场看见；
3. **`today=None`**：在节假日/同步失败时补出**上一个交易日**的名单并试图为那天
   发卡。补历史是显式动作（`--backfill`），不该藏在定时任务里。
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.jobs import daily_pipeline as dp


def test_slate_finalize_jobs_are_registered_and_addressable(monkeypatch):
    """两个时点各自可寻址，且回调接得住 `on_progress`。"""
    calls: list[str] = []
    monkeypatch.setattr(dp, "_run_tracked",
                        lambda fn, label: calls.append(label))

    sched = AsyncIOScheduler(timezone="Asia/Shanghai")
    dp._register_slate_finalize_jobs(sched, repo=None)

    jobs = {job.id: job for job in sched.get_jobs()}
    assert set(jobs) == {dp.SLATE_FINALIZE_JOB_ID, dp.SLATE_FINALIZE_RETRY_JOB_ID}
    for job_id, job in jobs.items():
        job.func(on_progress=None)      # 签名不对这里就 TypeError
        assert calls[-1] == job_id, "闭包绑定错了 job_id"
    assert calls == [dp.SLATE_FINALIZE_JOB_ID, dp.SLATE_FINALIZE_RETRY_JOB_ID]


def test_finalize_pins_today_instead_of_the_latest_partition(monkeypatch):
    """补跑必须钉「今天」，不能取「最新分区」。

    取最新分区会在节假日补出一份**昨天**的名单，并顺着 `_l1_picks` 为昨天发卡：
    一张 retroactive 的承诺，日期看起来完全正常。
    """
    seen: dict = {}

    def fake_stage(repo, engine, *, today=None):
        seen["today"] = today
        return {"status": "created", "slate": {"status": "ready", "as_of": "2026-11-13"}}

    monkeypatch.setattr("app.decision.producer.run_decision_stage", fake_stage)
    out = dp._run_slate_finalize(repo=object())

    assert seen["today"] == dp.cn_today()
    assert out["status"] == "created"


def test_finalize_never_raises(monkeypatch):
    """定时任务的异常不得冒到调度器主循环；失败要落成可读的返回。"""

    def boom(*a, **k):
        raise RuntimeError("名单算不出来")

    monkeypatch.setattr("app.decision.producer.run_decision_stage", boom)
    out = dp._run_slate_finalize(repo=object())
    assert out["status"] == "failed"
    assert "名单算不出来" in out["reason"]
