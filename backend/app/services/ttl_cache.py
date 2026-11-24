"""进程内有界 TTL 缓存 —— 给「同一个响应被反复算」的读路径用。

## 为什么不是 Redis

2026-11-24 的实测：这批慢的成因是「同一个事实被重复**推导**」和「首次就要付全价」，
不是「同一份数据被重复读」。缓存改变第 2 次及以后，改变不了第 1 次，也改变不了
写路径。而本仓是**单进程**部署（Docker 单容器 / PyInstaller 单 exe），跨进程共享
没有需求 —— 引入外部服务的运维与打包成本换不来收益。详见 `docs/caching-strategy.md`。

## 契约（调用方必须自己保证的三条）

1. **键要覆盖所有会改变结果的维度**，包括资产类型、周期、日期、复权基准；
2. **值是快照**，不许把缓存里的可变对象直接交出去（本类不做深拷贝，交出去的是
   调用方自己 put 进去的那个对象）；
3. **允许陈旧的时间上限由 TTL 表达**；需要立即生效的写入点应显式 `clear()` /
   `invalidate(key)`。TTL 不是"最终一致"的借口，它是给「历史数据可能被修补」
   这类低频变化留的兜底窗口。

## 为什么带 single-flight

TTL 到期的瞬间，N 个并发请求会各自算一遍同一份东西。本仓已有先例：`_get_table_stats`
的重聚合若不加 single-flight，一次 40s 的 DuckDB 聚合会被放大成 N 次。这里的做法是
「同键只让一个线程算，其余等它的结果」，失败的等待者退化为自己算一次（不把异常
传染给所有等待者）。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable


class TtlCache:
    """有界 LRU + TTL + 单飞。线程安全。"""

    def __init__(self, *, max_entries: int = 128, ttl_s: float = 600.0, name: str = "") -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self.name = name or "ttl-cache"
        self._lock = threading.Lock()
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Any, threading.Event] = {}
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # ---------------------------------------------------------------- 基本操作

    def get(self, key: Any) -> Any | None:
        """命中返回值；过期/缺失返回 None（并顺手清掉过期项）。"""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            stamped, value = entry
            if now - stamped > self.ttl_s:
                self._entries.pop(key, None)
                self._misses += 1
                return None
            if value is None:
                # None 不可缓存：`get_or_compute` 用 `is not None` 判命中，若把
                # None 当有效值，两处语义会分叉（get 说命中、get_or_compute 说未命中）。
                # 统一按未命中处理并清掉，统计才不会说谎。
                self._entries.pop(key, None)
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

    def invalidate(self, key: Any) -> bool:
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ---------------------------------------------------------------- 单飞

    def get_or_compute(self, key: Any, factory: Callable[[], Any]) -> Any:
        """命中直接返回；未命中时**同键只算一次**。

        等待者超时或生产者失败时自己算一遍：缓存是加速手段，不许成为单点故障。
        """
        hit = self.get(key)
        if hit is not None:
            return hit

        with self._lock:
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                owner = True
            else:
                owner = False

        if not owner:
            # 等生产者；上限取 TTL —— 超过它说明生产者卡住了，自己算更安全
            event.wait(timeout=self.ttl_s)

        if not owner:
            hit = self.get(key)
            if hit is not None:
                return hit
            return factory()

        try:
            value = factory()
            self.put(key, value)
            return value
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()

    # ---------------------------------------------------------------- 诊断

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "name": self.name,
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "ttl_s": self.ttl_s,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
                "evictions": self._evictions,
            }
