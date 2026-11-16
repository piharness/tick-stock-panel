"""分钟取数子进程的契约 —— 冻不住 API、超时能杀、坏了当空轮。

## 背景（实测，2026-11-13）

`minute_refresh` 一开，8 秒内 `/` 与全部 `/api/*` 超时；同一轮 472 只的取数耗时
134.8s。把同样调用放到独立进程，API 全程 0.094s。所以取数必须出进程 —— 线程
做不到（GIL 是解释器级的）。

## 这里验什么

1. 编解码：一轮 4 万行经 Arrow IPC 往返后逐值不变（取数在子进程、落盘在主进程）。
2. 坏输入：未知 kind / provider 不可用 → 空帧 + 0 请求，不抛。
3. 超时：原生调用没有超时语义，父进程必须能杀掉并退回空轮。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.services import minute_fetch_proc


def _frame(rows: int = 3) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [f"60000{i}.SH" for i in range(rows)],
        "datetime": [1_760_000_000_000 + i * 60_000 for i in range(rows)],
        "open": [10.0 + i for i in range(rows)],
        "close": [10.5 + i for i in range(rows)],
        "volume": [1000.0 + i for i in range(rows)],
        "amount": [1_000_000.0 + i for i in range(rows)],
    })


def test_encode_decode_roundtrip_preserves_values():
    """子进程只取数、主进程落盘 ⇒ 回传必须逐值无损。"""
    src = _frame(50)
    df, requests = minute_fetch_proc.decode_payload(
        {"ok": True, "requests": 7, "ipc": minute_fetch_proc.encode_frame(src)}
    )
    assert requests == 7
    assert df.equals(src)


def test_decode_rejects_error_payload():
    df, requests = minute_fetch_proc.decode_payload({"ok": False, "error": "boom"})
    assert df.is_empty() and requests == 0


def test_decode_rejects_corrupt_bytes():
    df, requests = minute_fetch_proc.decode_payload({"ok": True, "requests": 1, "ipc": b"not-ipc"})
    assert df.is_empty() and requests == 0


def test_unknown_kind_returns_empty_not_raise():
    """子进程报错不许升级成服务异常: 调用方已有「空轮计数 + 告警」语义。"""
    df, requests = minute_fetch_proc.fetch_via_child("nope", "tdx", ["600000.SH"])
    assert df.is_empty() and requests == 0


def test_unavailable_provider_returns_empty():
    """源不可用（未声明的自定义源）→ 空轮，不伪装成成功。"""
    df, requests = minute_fetch_proc.fetch_via_child(
        "custom_batch", "definitely-not-a-provider", ["600000.SH"],
    )
    assert df.is_empty() and requests == 0


def test_timeout_terminates_child():
    """原生调用没有超时语义 ⇒ 父进程必须自己兜，并且真的把进程杀掉。"""
    before = {p.pid for p in __import__("multiprocessing").active_children()}
    df, requests = minute_fetch_proc.fetch_via_child(
        "custom_batch", "tdx", ["600000.SH"], timeout_s=0.01,
    )
    assert df.is_empty() and requests == 0
    leaked = [
        p for p in __import__("multiprocessing").active_children()
        if p.pid not in before and p.is_alive()
    ]
    assert not leaked, f"超时后仍留下活着的子进程: {[p.pid for p in leaked]}"


# --------------------------------------------------------------- 子进程内部路由
class _Conn:
    """收集子进程回传的假管道。"""

    def __init__(self) -> None:
        self.payload: dict | None = None

    def send(self, payload: dict) -> None:
        self.payload = payload

    def close(self) -> None:
        pass


class _Provider:
    def __init__(self, *, batch: bool, minute: bool = True) -> None:
        self.calls: list[str] = []
        if not batch:
            self.get_intraday_batch = None  # 与真实 provider 只暴露部分方法的形状一致
        self._minute = minute

    def get_intraday_batch(self, symbols, **_kwargs):
        self.calls.append("batch")
        return _minute_frame()

    def get_minute(self, symbols, **_kwargs):
        self.calls.append("minute")
        if _kwargs.get("on_chunk_done"):
            _kwargs["on_chunk_done"](1, 4)
        return _minute_frame()


def _minute_frame() -> pl.DataFrame:
    from datetime import datetime

    return pl.DataFrame({
        "symbol": ["600000.SH"],
        "datetime": [datetime(2026, 8, 25, 10, 0)],
        "open": [10.0], "high": [10.5], "low": [9.9], "close": [10.2],
        "volume": [1000.0], "amount": [10200.0],
    })


def _child(monkeypatch, provider: _Provider, kind: str, symbols: list[str]):
    from app.services import kline_sync

    monkeypatch.setattr(
        kline_sync, "_resolve_full_minute_provider", lambda name: (provider, False, None),
    )
    conn = _Conn()
    minute_fetch_proc._child_fetch(conn, kind, "tdx", symbols)
    return conn.payload


def test_child_prefers_intraday_batch_over_minute(monkeypatch):
    """源自管批量端点时不走逐标的 get_minute —— 否则一轮会变成 N 个请求。"""
    provider = _Provider(batch=True)
    payload = _child(monkeypatch, provider, "custom_batch", ["600000.SH"])
    assert provider.calls == ["batch"]
    df, requests = minute_fetch_proc.decode_payload(payload)
    assert df.height == 1 and requests == 1


def test_child_falls_back_to_minute_and_counts_chunks(monkeypatch):
    """未实现批量端点 → 回退 get_minute, 请求数按 chunk 回调统计。"""
    provider = _Provider(batch=False)
    payload = _child(monkeypatch, provider, "custom_batch", ["600000.SH"])
    assert provider.calls == ["minute"]
    df, requests = minute_fetch_proc.decode_payload(payload)
    assert df.height == 1 and requests == 4


def test_child_increment_uses_latest_endpoint(monkeypatch):
    provider = _Provider(batch=True)
    provider.get_intraday_latest = lambda symbols=None, count=3: _minute_frame()
    provider.calls.clear()
    payload = _child(monkeypatch, provider, "custom_latest", [])
    df, requests = minute_fetch_proc.decode_payload(payload)
    assert df.height == 1 and requests >= 1


