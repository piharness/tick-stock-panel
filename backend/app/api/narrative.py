"""叙事账本 API — 薄壳 + TTL 缓存。

装配全部在 `app.services.narrative_ledger`（纯函数 + 各块独立降级）；
本层只做进程内缓存，避免每次进页面重扫 enriched 分区。

缓存 300s：五个块的数据都是日频（收盘后不变），冷启动约 3.5s、命中约 5ms。
两融是 T+1 才到，5 分钟足够覆盖人工刷新；重算后想立刻看到，重启进程或等一次过期即可。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, Request

from app.services import narrative_ledger

router = APIRouter(prefix="/api/narrative", tags=["narrative"])

_CACHE_TTL = 300.0
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0
_lock = threading.Lock()


def invalidate_narrative_cache() -> None:
    global _cache, _cache_ts
    with _lock:
        _cache = None
        _cache_ts = 0.0


@router.get("/ledger")
def get_ledger(request: Request):
    """当日叙事账本：温度 / 主线 / 资金四路 / 指数波动 / 词频。

    每块自带 as_of（四个源时滞不同）；块内失败只让该块 `ok=false`，
    整页仍返回 —— 缺数要说缺，不能因为两融没更新就白屏。
    """
    global _cache, _cache_ts
    now = time.time()
    with _lock:
        if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache
    payload = narrative_ledger.build(request.app.state.repo)
    with _lock:
        _cache = payload
        _cache_ts = time.time()
    return payload
