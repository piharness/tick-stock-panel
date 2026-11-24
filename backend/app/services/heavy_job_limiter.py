"""Weighted process-local limiter for memory-heavy jobs."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar, Literal

HeavyJobKind = Literal["normal", "mining", "exclusive"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


# 内存准入 (issue #516): 可用内存低于阈值时重任务不进门, 挂起等待恢复,
# 避免叠加任务把整机拖进 swap。0 = 关闭。等待者以 _MEMORY_POLL_S 周期重查
# (内存恢复不会触发 condition notify, 必须轮询)。
_HEAVY_MIN_AVAILABLE_BYTES = _env_int("TICK_HEAVY_MIN_AVAILABLE_BYTES", 1024**3)
_MEMORY_POLL_S = 5.0


def _memory_admission_ok() -> bool:
    if _HEAVY_MIN_AVAILABLE_BYTES <= 0:
        return True
    from app.services.memory_budget import memory_budget

    return memory_budget().available_bytes >= _HEAVY_MIN_AVAILABLE_BYTES


class HeavyJobLimitTimeoutError(TimeoutError):
    """Raised when a heavy-job slot cannot be acquired before its deadline."""


class HeavyJobCancelledError(RuntimeError):
    """Raised when slot acquisition is cancelled while waiting."""


class HeavyJobLimiter:
    """FIFO weighted capacity; exclusive jobs reserve the entire process budget.

    所有权（owner）: 可选。带 owner 的申请会记在 ``_owners`` 里，供 ``force_release``
    收回 —— 被 reap 的任务线程可能永远卡在阻塞调用里，回不来执行 finally；容量
    不收回的话，后续每次点击都排在「等待其他计算任务完成…」上直到超时，只能重启
    进程。收回后该 owner 迟到的 ``release`` 会被识别为「已收回」并忽略，不会二次
    扣减。
    """

    _WEIGHTS: ClassVar[dict[HeavyJobKind, int]] = {"normal": 1, "mining": 2}

    def __init__(self, capacity: int = 2, *, cancel_poll_interval: float = 0.05) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if cancel_poll_interval <= 0:
            raise ValueError("cancel_poll_interval must be positive")
        self.capacity = capacity
        self._cancel_poll_interval = cancel_poll_interval
        self._used = 0
        self._acquired = {"normal": 0, "mining": 0, "exclusive": 0}
        self._owners: dict[str, tuple[HeavyJobKind, int]] = {}
        self._condition = threading.Condition()
        self._waiters: deque[object] = deque()
        self._local = threading.local()

    @property
    def in_use(self) -> int:
        with self._condition:
            return self._used

    @property
    def available(self) -> int:
        with self._condition:
            return self.capacity - self._used

    @property
    def holders(self) -> list[str]:
        """当前带 owner 的持有者（诊断用）。"""
        with self._condition:
            return list(self._owners)

    def acquire(
        self,
        kind: HeavyJobKind = "normal",
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        owner: str | None = None,
    ) -> bool:
        """Wait for capacity and return ``False`` on cancellation or timeout."""
        weight = self._weight(kind)
        if weight > self.capacity:
            raise ValueError(f"{kind} requires {weight} slots, capacity is {self.capacity}")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if owner is not None and owner in self._owners:
                raise RuntimeError(f"owner {owner!r} 已持有重任务容量")
            ticket = object()
            self._waiters.append(ticket)
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                    if (
                        self._waiters[0] is ticket
                        and self._used + weight <= self.capacity
                        and _memory_admission_ok()
                    ):
                        self._used += weight
                        self._acquired[kind] += 1
                        if owner is not None:
                            self._owners[owner] = (kind, weight)
                        return True

                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    wait_for = remaining
                    if cancel_event is not None:
                        wait_for = self._cancel_poll_interval
                        if remaining is not None:
                            wait_for = min(wait_for, remaining)
                    # 内存准入需要周期性重查 (内存恢复不触发 notify)
                    if wait_for is None or wait_for > _MEMORY_POLL_S:
                        wait_for = _MEMORY_POLL_S
                    self._condition.wait(wait_for)
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    def release(self, kind: HeavyJobKind = "normal", *, owner: str | None = None) -> None:
        """Return capacity previously acquired for ``kind``.

        owner 非空时按 owner 记账：已被 ``force_release`` 收回的，迟到的释放是
        no-op（僵尸线程的 finally 不得再扣一次）。
        """
        with self._condition:
            if owner is not None:
                held = self._owners.pop(owner, None)
                if held is None:
                    return
                kind, weight = held
            else:
                weight = self._weight(kind)
            if self._acquired[kind] == 0:
                raise RuntimeError(f"cannot release unacquired {kind} capacity")
            self._acquired[kind] -= 1
            self._used -= weight
            self._condition.notify_all()

    def force_release(self, owner: str) -> bool:
        """收回指定 owner 的容量（线程卡在阻塞调用里回不来时）。返回是否收回。"""
        with self._condition:
            held = self._owners.pop(owner, None)
            if held is None:
                return False
            kind, weight = held
            self._acquired[kind] = max(0, self._acquired[kind] - 1)
            self._used = max(0, self._used - weight)
            self._condition.notify_all()
            return True

    @contextmanager
    def slot(
        self,
        kind: HeavyJobKind = "normal",
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        owner: str | None = None,
    ) -> Iterator[HeavyJobLimiter]:
        """Reserve capacity in the executing thread, reusing an outer reservation."""
        weight = self._weight(kind)
        held = getattr(self._local, "weight", 0)
        if held:
            if weight > held:
                raise RuntimeError("cannot upgrade a held heavy-job reservation")
            if cancel_event is not None and cancel_event.is_set():
                raise HeavyJobCancelledError(f"{kind} job was cancelled")
            yield self
            return
        acquired = self.acquire(
            kind, timeout=timeout, cancel_event=cancel_event, owner=owner,
        )
        if not acquired:
            if cancel_event is not None and cancel_event.is_set():
                raise HeavyJobCancelledError(f"{kind} job was cancelled while waiting")
            raise HeavyJobLimitTimeoutError(f"timed out waiting for {kind} job capacity")
        try:
            self._local.weight = weight
            yield self
        finally:
            self._local.weight = 0
            self.release(kind, owner=owner)

    def _weight(self, kind: HeavyJobKind) -> int:
        if kind == "exclusive":
            return self.capacity
        try:
            return self._WEIGHTS[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported heavy job kind: {kind!r}") from exc


shared_heavy_job_limiter = HeavyJobLimiter(capacity=2)
# Short alias for entry points that prefer the existing module-singleton naming style.
heavy_job_limiter = shared_heavy_job_limiter
