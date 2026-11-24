"""当前重任务上下文 —— 资源归属的唯一来源。

为什么需要它:
  有些资源由任务创建、却不在 job_store 里记账，只在 ``finally`` 里释放：
  enriched 的 publishing 标记、实时行情的临时暂停、手册链子进程。线程一旦
  卡在阻塞调用里回不来，``finally`` 永远不执行，这些资源就永久泄漏 ——
  2026-11-24 的现场就是「任务已判失败，但发布标记还挂着 / 行情一直暂停」。

  归属必须由**创建资源的那一刻**决定，而不是由释放方猜。任务执行体在
  ``run_with_capacity`` 里进一次上下文，之后这条线程上创建的所有可归属资源
  都自动带上 job_id；terminate 按这个 id 收回。

为什么是 ContextVar 而不是参数:
  创建点分散（enriched 发布 7 处、行情暂停 5 处、手册链 1 处），逐个传参会漏；
  漏一处就少一条可收回的路径。上下文变量只设一次。
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_CURRENT_JOB: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tickflow_current_job", default=None,
)


def current_job_id() -> str | None:
    """当前线程/上下文正在执行的重任务 id；不在任务里返回 None。"""
    return _CURRENT_JOB.get()


@contextmanager
def job_context(job_id: str | None) -> Iterator[None]:
    """把这段上下文里创建的可归属资源记在 job_id 名下（None = 不属于任何任务）。"""
    token = _CURRENT_JOB.set(str(job_id) if job_id else None)
    try:
        yield
    finally:
        _CURRENT_JOB.reset(token)
