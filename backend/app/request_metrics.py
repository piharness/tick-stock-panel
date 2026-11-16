"""请求耗时账本 — 416 个端点此前没有任何一条耗时记录。

为什么是纯 ASGI 而不是 ``@app.middleware("http")``:

- ``BaseHTTPMiddleware`` 会在 response 外面再套一层 task group。本仓已有认证
  中间件走这条路, 再加一层是每个请求的净开销;
- SSE (``/api/events``、``/api/intraday/stream``) 经 BaseHTTPMiddleware 时要穿过
  额外的事件循环跳转, 长期连接上更容易踩取消/背压问题。
  本模块只观测 ``http.response.start`` 并原样透传 ``send``, 不缓冲、不改流。

落盘 ``<data_dir>/logs/requests-YYYY-MM-DD.jsonl``, 每行一个 JSON:

    {"ts": "2026-11-13T15:47:01.123+08:00", "method": "GET",
     "route": "/api/kline/daily", "path": "/api/kline/daily",
     "status": 200, "ms": 28.4, "bytes": 10234, "stream": false}

``route`` 取 Starlette 匹配到的**模板**(``/api/backtest/{job_id}/status``), 不是
原始路径 —— 否则 job_id 进路径会让"按路由聚合"退化成按 ID 聚合, 分布没有意义。
匹配失败(404)时才回落到 ``path``。

判读::

    python -m app.request_metrics --date 2026-11-13 --top 30

关闭: 环境变量 ``TSP_REQUEST_METRICS=0``。

**为什么是缓冲写而不是直接写**: 容器里 ``data/logs`` 在 9p 挂载上, 实测单行
``open+write+close`` **p50 1.90ms、最长 109ms**(2026-11-13 容器内实测)。而本仓
热端点只要 3~5ms —— 同步写等于给被测对象加 40%~200% 的开销, 还把事件循环卡住,
量出来的 p99 是被自己污染的。故: ``record()`` 只做一次 list.append, 由后台线程
每 2s 或每 200 行落盘一次。代价是进程被 kill -9 时最多丢 2s 遥测(可接受)。
"""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import threading
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 只记 API 面。静态资源(前端 dist)是浏览器缓存的事, 混进来会淹没分布。
_RECORD_PREFIXES = ("/api/",)
_RECORD_EXACT = ("/health",)

_ENABLED = os.environ.get("TSP_REQUEST_METRICS", "1").strip().lower() not in {
    "0", "false", "off", "no",
}

_override_dir: Path | None = None  # 测试注入; None 时用 settings.data_dir
_streaming_mark = "text/event-stream"

# 缓冲: {日期: [行]}。按日期分桶, 跨零点时两天的行不会写进同一个文件。
_pending: dict[str, list[str]] = {}
_pending_lock = threading.Lock()
_flush_now = threading.Event()
_flusher: threading.Thread | None = None
_FLUSH_INTERVAL_S = 2.0
_FLUSH_AT_LINES = 200
# 缓冲上限: 刷盘持续失败时不能无限吃内存(丢掉的是遥测, 不是业务数据)。
_BUFFER_MAX_LINES = 5000


def configure(*, enabled: bool | None = None, directory: Path | None = None) -> None:
    """测试/诊断用开关。生产不需要调用。"""
    global _ENABLED, _override_dir
    if enabled is not None:
        _ENABLED = enabled
    _override_dir = directory


def _log_dir() -> Path:
    if _override_dir is not None:
        return _override_dir
    from app.config import settings

    return Path(settings.data_dir) / "logs"


def _should_record(path: str) -> bool:
    return path in _RECORD_EXACT or any(path.startswith(p) for p in _RECORD_PREFIXES)


def _route_template(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return str(scope.get("path") or "")


def _ensure_flusher() -> None:
    """首个请求时启动刷盘线程(不放在 import 期: 纯 import 不许起线程)。"""
    global _flusher
    if _flusher is not None:
        return
    with _pending_lock:
        if _flusher is not None:
            return
        _flusher = threading.Thread(
            target=_flush_loop, name="request-metrics-flush", daemon=True,
        )
        _flusher.start()


def _flush_loop() -> None:
    while True:
        _flush_now.wait(_FLUSH_INTERVAL_S)
        _flush_now.clear()
        flush()


def flush() -> None:
    """把缓冲落盘。平时由后台线程触发; 测试与退出路径显式调用。"""
    global _pending
    with _pending_lock:
        if not _pending:
            return
        batch, _pending = _pending, {}
    for day, lines in batch.items():
        if not lines:
            continue
        try:
            directory = _log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f"requests-{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except Exception as e:  # noqa: BLE001 — 账本失败不得影响响应, 也不能堆积
            logger.debug("request metrics flush skipped (%s): %s", day, e)


atexit.register(flush)


def record(
    *,
    method: str,
    route: str,
    path: str,
    status: int,
    duration_ms: float,
    bytes_out: int = 0,
    stream: bool = False,
) -> None:
    """入缓冲。任何异常都吞掉 —— 计时器不许把请求弄红。"""
    if not _ENABLED:
        return
    try:
        now = datetime.now().astimezone()
        line = json.dumps(
            {
                "ts": now.isoformat(timespec="milliseconds"),
                "method": method,
                "route": route,
                "path": path,
                "status": status,
                "ms": round(duration_ms, 3),
                "bytes": bytes_out,
                "stream": stream,
            },
            ensure_ascii=False,
        )
        day = now.strftime("%Y-%m-%d")
        over = False
        with _pending_lock:
            bucket = _pending.setdefault(day, [])
            if sum(len(v) for v in _pending.values()) < _BUFFER_MAX_LINES:
                bucket.append(line)
                over = len(bucket) >= _FLUSH_AT_LINES
        if over:
            _flush_now.set()
        _ensure_flusher()
    except Exception as e:  # noqa: BLE001 — 账本失败不得影响响应
        logger.debug("request metrics record skipped: %s", e)


class RequestMetricsMiddleware:
    """纯 ASGI: 记录耗时/状态/响应字节, 不缓冲流。"""

    def __init__(self, app):  # noqa: ANN001 — ASGI callable
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = str(scope.get("path") or "")
        if not _should_record(path):
            return await self.app(scope, receive, send)

        started = perf_counter()
        state: dict[str, Any] = {"status": 0, "bytes": 0, "stream": False}

        async def _send(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                state["status"] = int(message.get("status", 0))
                for raw_key, raw_val in message.get("headers") or []:
                    if raw_key.lower() == b"content-type":
                        ctype = raw_val.decode("latin-1", "ignore").lower()
                        state["stream"] = ctype.startswith(_streaming_mark)
                        break
            elif message["type"] == "http.response.body":
                state["bytes"] += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            # route 模板由 Router 在匹配后写回 scope, 只能在这之后读。
            record(
                method=str(scope.get("method") or ""),
                route=_route_template(scope),
                path=path,
                status=state["status"],
                duration_ms=(perf_counter() - started) * 1000.0,
                bytes_out=state["bytes"],
                stream=state["stream"],
            )


# ---------------------------------------------------------------- 判读


def _percentile(sorted_values: list[float], q: float) -> float:
    """nearest-rank; 空表由调用方拦。"""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[idx]


def summarize(
    rows: Iterable[dict[str, Any]], *, top: int = 0,
) -> list[dict[str, Any]]:
    """按 (method, route) 聚合出 p50/p95/p99。流式端点单列, 不算进 p99。

    返回按 p95 降序的列表; ``top>0`` 时截断。
    """
    buckets: dict[tuple[str, str], list[float]] = {}
    errors: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("stream"):
            key = (str(row.get("method")), str(row.get("route")))
            buckets.setdefault(("STREAM", key[1]), []).append(float(row.get("ms") or 0.0))
            continue
        key = (str(row.get("method")), str(row.get("route")))
        buckets.setdefault(key, []).append(float(row.get("ms") or 0.0))
        if int(row.get("status") or 0) >= 500:
            errors[key] = errors.get(key, 0) + 1

    out: list[dict[str, Any]] = []
    for (method, route), values in buckets.items():
        values.sort()
        total = len(values)
        out.append(
            {
                "method": method,
                "route": route,
                "n": total,
                "p50": round(_percentile(values, 0.50), 1),
                "p95": round(_percentile(values, 0.95), 1),
                "p99": round(_percentile(values, 0.99), 1),
                "max": round(values[-1], 1),
                "err5xx": errors.get((method, route), 0),
            }
        )
    out.sort(key=lambda r: (-r["p95"], -r["n"]))
    return out[:top] if top > 0 else out


def load_day(day: str, directory: Path | None = None) -> list[dict[str, Any]]:
    target = (directory or _log_dir()) / f"requests-{day}.jsonl"
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with target.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return rows


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="按路由打印请求耗时分布")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args(argv)

    rows = load_day(args.date)
    if not rows:
        print(f"no metrics for {args.date}")
        return 2
    table = summarize(rows, top=args.top)
    print(f"{'method':7s} {'n':>6s} {'p50':>8s} {'p95':>8s} {'p99':>8s} {'max':>8s} {'5xx':>4s}  route")
    for r in table:
        print(
            f"{r['method']:7s} {r['n']:6d} {r['p50']:8.1f} {r['p95']:8.1f} "
            f"{r['p99']:8.1f} {r['max']:8.1f} {r['err5xx']:4d}  {r['route']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
