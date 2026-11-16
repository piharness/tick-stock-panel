"""把自定义源的全量分钟取数放到子进程 —— 让原生传输层冻不住 API。

## 为什么必须出进程 (2026-11-13 实测)

`minute_refresh` 打开后 8 秒内整个 HTTP 面超时（`/` 静态页也超时，容器内 urllib
同样超时），日志时间戳停住，只有 SDK 自己的线程还在打 `[W] hq`。同一轮取数
（472 只）单独耗时 **134.8s**。A/B：

| 跑在哪 | API 响应 |
|---|---|
| 主进程内（minute_refresh 开启） | 8s 内全部超时 |
| 独立进程（`docker exec`） | 全程 0.094s，100s 无波动 |

即「整段原生调用期间解释器不可用」。无论根因是 PyO3 未 `allow_threads`（持 GIL）
还是原生层阻塞，唯一稳的隔离手段都是进程边界 —— 线程做不到。

## 边界怎么切

**子进程只取数，不落盘。** 落盘仍由主进程在 `repo._write_lock` 下做：
`minute_refresh` 里写明「无互斥的两个写者并发替换同一分区时, 后写者以过期基底
覆盖丢整批行」——`threading.Lock` 跨进程无效，把写也搬出去就等于把这个洞打开。

取数结果经 IPC 用 Arrow IPC 传回（一轮 4 万行、7 列，几 MB）。

## 超时

原生调用没有超时语义，父进程必须自己兜。超时 → terminate 子进程并按空轮处理
（调用方已有空轮计数与告警），绝不无限等。
"""
from __future__ import annotations

import io
import logging
import multiprocessing as mp
import time
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

#: 单轮取数上限 (秒)。实测 (2026-11-13):
#:   09:xx 一批 472 只 = 135s;  14:2x 一批 50 只 = 21.5s (≈430ms/只)
#: tdx 的 `get_intraday_batch` 每轮只取 `_SCAN_BATCH=500` 只并推进游标,
#: 因此单次调用的规模是 500 只、按当前速度约 215s —— 300s 只剩 1.4 倍余量,
#: 实测两次都在 300s 被杀、整轮 0 行。取 600s ≈ 2.8 倍余量。
FETCH_TIMEOUT_S = 600.0

#: 子进程启动 + 原生库加载的宽限。
_STARTUP_GRACE_S = 60.0


def encode_frame(df: pl.DataFrame | None) -> bytes:
    """取数结果 → Arrow IPC 字节 (子进程侧)。"""
    buf = io.BytesIO()
    (df if df is not None else pl.DataFrame()).write_ipc(buf)
    return buf.getvalue()


def decode_payload(payload: dict[str, Any] | None) -> tuple[pl.DataFrame, int]:
    """子进程回传 → (df, requests)。失败/损坏一律退回空帧。"""
    if not payload or not payload.get("ok"):
        return pl.DataFrame(), 0
    try:
        return pl.read_ipc(io.BytesIO(payload["ipc"])), int(payload.get("requests") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("分钟取数子进程回传解码失败: %s", exc)
        return pl.DataFrame(), 0


def _child_fetch(
    conn,
    kind: str,
    provider_name: str,
    symbols: list[str] | None,
) -> None:
    """子进程入口: 只调用 provider, 不碰仓库、不落盘。"""
    try:
        from app.services import kline_sync

        provider, _use_tickflow, err = kline_sync._resolve_full_minute_provider(provider_name)
        if provider is None or err is not None:
            conn.send({"ok": False, "error": f"provider unavailable: {err}"})
            return
        if kind == "custom_latest":
            result = kline_sync.fetch_intraday_custom_latest(provider, provider_name)
            if result is None:
                conn.send({"ok": False, "error": "increment unsupported"})
                return
            df, requests = result
        elif kind == "custom_batch":
            df, requests = kline_sync.fetch_intraday_custom_batch(
                provider, provider_name, symbols or [],
            )
        else:
            conn.send({"ok": False, "error": f"unknown kind: {kind}"})
            return

        conn.send({"ok": True, "requests": int(requests), "ipc": encode_frame(df)})
    except BaseException as exc:  # noqa: BLE001 — 原生 panic 是 BaseException 子类
        try:
            conn.send({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        except Exception:  # noqa: BLE001 — 管道已断时不再二次抛错
            pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def fetch_via_child(
    kind: str,
    provider_name: str,
    symbols: list[str] | None = None,
    *,
    timeout_s: float = FETCH_TIMEOUT_S,
) -> tuple[pl.DataFrame, int]:
    """在子进程里跑一次取数, 返回 (df, requests)。失败/超时返回空帧。

    失败不抛: 调用方（`MinuteRefreshService._round`）已有「空轮计数 + 告警」的
    语义, 这里抛错只会把一次网络抖动升级成服务异常。
    """
    context = mp.get_context("spawn")
    parent_conn, child_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_fetch,
        args=(child_conn, kind, provider_name, symbols),
        daemon=True,
    )
    started = time.monotonic()
    try:
        process.start()
    except BaseException:
        parent_conn.close()
        child_conn.close()
        raise

    payload: dict[str, Any] | None = None
    try:
        child_conn.close()  # 父进程不持写端, 子进程退出时 poll 才会返回
        process.join(timeout=timeout_s)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
            logger.warning(
                "分钟取数子进程超时 %.0fs (kind=%s, provider=%s), 已终止并按空轮处理",
                timeout_s, kind, provider_name,
            )
            return pl.DataFrame(), 0
        if parent_conn.poll(timeout=1.0):
            payload = parent_conn.recv()
    except (EOFError, OSError) as exc:
        logger.warning("分钟取数子进程通信失败 (kind=%s): %s", kind, exc)
        return pl.DataFrame(), 0
    finally:
        parent_conn.close()
        if process.is_alive():
            process.terminate()

    if not payload or not payload.get("ok"):
        logger.warning(
            "分钟取数子进程失败 (kind=%s, provider=%s, %.1fs): %s",
            kind, provider_name, time.monotonic() - started,
            (payload or {}).get("error", "no payload"),
        )
        return pl.DataFrame(), 0

    return decode_payload(payload)
