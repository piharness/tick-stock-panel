"""策略池持久化服务。

策略池(勾选列表)从纯前端 localStorage 迁移到后端 ``data/user_data/strategy_pool.json``,
每次变更追加一条历史记录到 ``data/user_data/strategy_pool_history/``, 实现跨设备一致与可回查。

存储契约:
- 本体: ``{"ids": [...], "updated_at": "...", "revision": <int>}``
- 历史: ``YYYYMMDD-HHMMSS-<uuid>.json``, 每条含 before/after/action/at/reason

与 watchlist 服务同一套约定: 模块级 RLock + 版本号 + os.replace 原子写。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_REVISION = 0
_SEQ = 0  # 历史记录单调序号, 保证同一秒内多条记录排序稳定(时间戳只到秒)

_ACTION_LABELS = {
    "set": "覆盖策略池",
    "add": "加入策略",
    "remove": "移出策略",
    "reorder": "重排策略池",
    "capture_pass": "自动捕获 PASS 入池",
}


def _path() -> Path:
    p = settings.data_dir / "user_data" / "strategy_pool.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _history_dir() -> Path:
    p = settings.data_dir / "user_data" / "strategy_pool_history"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {"ids": [], "updated_at": None, "revision": 0}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("策略池配置损坏，请检查 strategy_pool.json") from exc
    if not isinstance(raw, dict):
        raise ValueError("策略池配置格式不正确")
    ids = raw.get("ids", [])
    if not isinstance(ids, list):
        raise ValueError("策略池 ids 格式不正确")
    # 去重保序(兼容历史脏数据), 但不落盘(读时规整, 写时才真正修正)
    seen: set[str] = set()
    deduped: list[str] = []
    for sid in ids:
        s = str(sid)
        if s and s not in seen:
            seen.add(s)
            deduped.append(s)
    return {
        "ids": deduped,
        "updated_at": raw.get("updated_at"),
        "revision": int(raw.get("revision", 0) or 0),
    }


def _write(ids: list[str], action: str, *, before: list[str] | None = None,
           reason: str = "") -> dict:
    """原子写本体 + 追加历史。返回写后的状态 dict。"""
    global _REVISION
    p = _path()
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    prev = _read()
    prev_ids = before if before is not None else prev["ids"]
    next_rev = prev["revision"] + 1

    payload = {"ids": ids, "updated_at": now_iso, "revision": next_rev}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)

    _append_history(
        action=action,
        before=prev_ids,
        after=ids,
        at=now_iso,
        reason=reason,
    )
    _REVISION = next_rev
    return payload


def _append_history(*, action: str, before: list[str], after: list[str],
                    at: str, reason: str = "") -> None:
    global _SEQ
    hdir = _history_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _SEQ += 1
    fname = f"{stamp}-{_SEQ:06d}-{uuid.uuid4().hex[:8]}.json"
    record = {
        "action": action,
        "action_label": _ACTION_LABELS.get(action, action),
        "before": before,
        "after": after,
        "at": at,
        "seq": _SEQ,
        "reason": reason,
    }
    (hdir / fname).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def revision() -> int:
    return _REVISION


def get() -> dict:
    """读取策略池当前状态。"""
    with _LOCK:
        return _read()


def get_history(limit: int = 50) -> list[dict]:
    """按时间倒序列出历史记录(新在前)。

    排序以记录内的 ``at`` 时间戳为准(文件名里的秒级时间戳 + uuid 在同一秒内
    无法保证稳定顺序); 缺 ``at`` 时回退文件名, 保证稳定且语义正确。
    """
    hdir = _history_dir()
    records: list[dict] = []
    for f in hdir.glob("*.json"):
        try:
            records.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    # 新在前: 优先按单调 seq, 缺 seq(旧数据)回退 at 时间戳
    records.sort(key=lambda r: (r.get("seq", 0), r.get("at", "")), reverse=True)
    return records[:limit]


def set_pool(ids: list[str], *, reason: str = "") -> dict:
    """整体覆盖策略池(等价于前端「保存策略池」)。"""
    with _LOCK:
        return _write(ids, "set", reason=reason)


def add(ids: list[str], *, reason: str = "", action: str = "add") -> dict:
    """把若干策略加入池(保序去重)。capture PASS 入池时 action='capture_pass'。"""
    with _LOCK:
        current = _read()["ids"]
        next_ids = list(current)
        for sid in ids:
            s = str(sid)
            if s and s not in next_ids:
                next_ids.append(s)
        if next_ids == current:
            return _read()
        return _write(next_ids, action, before=current, reason=reason)


def remove(ids: list[str], *, reason: str = "") -> dict:
    """把若干策略移出池。"""
    with _LOCK:
        current = _read()["ids"]
        drop = {str(s) for s in ids}
        next_ids = [s for s in current if s not in drop]
        if next_ids == current:
            return _read()
        return _write(next_ids, "remove", before=current, reason=reason)


def reorder(ids: list[str], *, reason: str = "") -> dict:
    """重排策略池(ids 须与当前池同集)。"""
    with _LOCK:
        current = _read()["ids"]
        normalized = [str(s) for s in ids]
        if set(normalized) != set(current):
            raise ValueError("重排的 ids 必须与当前策略池一致")
        if normalized == current:
            return _read()
        return _write(normalized, "reorder", before=current, reason=reason)
