"""`TtlCache` 的契约测试 —— 它是两处响应缓存的共同底座，坏了会同时影响两处。

要钉的四件事：
  1. TTL 到期必须重新算（否则页面会长期显示旧结论）；
  2. 有条数上限（否则内存随访问过的标的数无限增长）；
  3. 同键并发只算一次（否则 TTL 到期瞬间 N 个请求各自算一遍）；
  4. 生产者抛异常时**不缓存异常**，等待者退化为自己算 —— 缓存不许变成单点故障。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services.ttl_cache import TtlCache


def test_hit_within_ttl_and_recompute_after(monkeypatch):
    cache = TtlCache(max_entries=4, ttl_s=10.0)
    calls = {"n": 0}
    cache.get_or_compute("k", lambda: calls.__setitem__("n", calls["n"] + 1) or calls["n"])
    cache.get_or_compute("k", lambda: calls.__setitem__("n", calls["n"] + 1) or calls["n"])
    assert calls["n"] == 1

    # 直接把 TTL 拨到过去（不 sleep，避免拖慢测试）
    monkeypatch.setattr(cache, "ttl_s", -1.0)
    cache.get_or_compute("k", lambda: calls.__setitem__("n", calls["n"] + 1) or calls["n"])
    assert calls["n"] == 2


def test_bounded_by_max_entries():
    cache = TtlCache(max_entries=2, ttl_s=60.0)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)

    assert cache.stats()["entries"] == 2
    assert cache.get("a") is None, "最久未使用的一项应被淘汰"
    assert cache.get("c") == 3


def test_single_flight_computes_once():
    cache = TtlCache(max_entries=4, ttl_s=60.0)
    calls = {"n": 0}
    started = threading.Barrier(4)

    def slow_factory():
        calls["n"] += 1
        time.sleep(0.15)
        return "value"

    results: list[str] = []

    def worker():
        started.wait()
        results.append(cache.get_or_compute("k", slow_factory))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1, "同键并发只应计算一次"
    assert results == ["value"] * 4


def test_failure_is_not_cached_and_waiter_recovers():
    cache = TtlCache(max_entries=4, ttl_s=60.0)

    def boom():
        raise RuntimeError("factory failed")

    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", boom)
    assert cache.get("k") is None, "异常不得被缓存"

    assert cache.get_or_compute("k", lambda: "ok") == "ok"


def test_none_value_is_not_cacheable():
    """约定：None 既表示未命中，也不许被当成命中值 —— 否则 get 与 get_or_compute
    会对同一个键给出相反答案（前者说命中、后者说未命中）。"""
    cache = TtlCache(max_entries=4, ttl_s=60.0)
    cache.put("k", None)

    assert cache.get("k") is None
    assert cache.stats()["misses"] == 1
    assert cache.stats()["hits"] == 0


def test_invalidate_and_clear():
    cache = TtlCache(max_entries=4, ttl_s=60.0)
    cache.put("a", 1)
    cache.put("b", 2)

    assert cache.invalidate("a") is True
    assert cache.invalidate("a") is False
    assert cache.get("a") is None

    cache.clear()
    assert cache.stats()["entries"] == 0
