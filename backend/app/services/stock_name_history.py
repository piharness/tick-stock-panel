"""简称轨迹 —— 从本地龙虎榜快照里读出「这只票什么时候叫什么」。

为什么需要它：个股分析页能画 2024-09 起的日 K，但 `instruments` 维表只有
**当前**简称（实测 600340.SH → `*ST华幸`）。看两年图形的人会问「我印象里它
叫华夏幸福」，而页面只有一个今天的名字，等于把「改过名」这件事抹掉了。

本地唯一带历史简称的表是龙虎榜快照 `data/dragon_tiger/date=*.json`（每份
含当日上榜股票的 `name`）。它稀疏（只在上榜日有观测），所以本模块**不猜
改名日**，只输出两件可证伪的事实：

  - 某个简称在哪些日期被看到过（observations）
  - 相邻两个不同简称之间，改名发生在 [前一个简称最后见到, 后一个简称首次见到]

`*ST华幸` 首次见到 2026-01-14、`华夏幸福` 最后见到 2025-12-02 ⇒ 改名落在
这 43 天里。写「2026-01-01 改名」是编的，写「43 天窗口内改名」是可查的。

口径纪律：
  - 来源只认本地快照，不联网；快照缺失就返回空，不退回「当前维表名」冒充历史。
  - 索引按快照目录的 (文件数, 最新 mtime) 失效，不做定时器：新增一天快照
    必然改变文件数，历史快照不可变（见 services/dragon_tiger.py 的契约）。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date as date_cls
from pathlib import Path

logger = logging.getLogger(__name__)

_BOARD_KEYS = ("all", "org")
_HOT_KEY = "hot_money"

# 索引缓存: (data_dir, n_files, max_mtime) → {symbol: [(date_str, name), ...]}
_CACHE: dict[str, tuple[tuple[int, float], dict[str, list[tuple[str, str]]]]] = {}
_CACHE_LOCK = threading.Lock()


def _iter_snapshot_paths(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("date=*.json") if p.is_file())


def _collect_symbol_names(payload: dict, out: dict[str, list[tuple[str, str]]], day: str) -> None:
    """把一份快照里 (symbol → name) 追加进索引；同 symbol 同日只记一次。"""
    seen: set[str] = set()

    def take(items: object) -> None:
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            sym = it.get("thscode")
            name = it.get("name")
            if not isinstance(sym, str) or not sym or not isinstance(name, str) or not name:
                continue
            if sym in seen:
                continue
            seen.add(sym)
            out.setdefault(sym, []).append((day, name))

    for key in _BOARD_KEYS:
        block = payload.get(key)
        if isinstance(block, dict):
            take(block.get("stock_items"))
    hot = payload.get(_HOT_KEY)
    if isinstance(hot, dict):
        for seat in hot.get("hot_money_items") or []:
            if isinstance(seat, dict):
                take(seat.get("rows"))


def _build_index(base: Path) -> dict[str, list[tuple[str, str]]]:
    index: dict[str, list[tuple[str, str]]] = {}
    paths = _iter_snapshot_paths(base)
    for path in paths:
        day = path.stem.split("=", 1)[-1]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            _collect_symbol_names(payload, index, day)
    for obs in index.values():
        obs.sort()
    return index


def _index_for(data_dir: Path) -> dict[str, list[tuple[str, str]]]:
    base = data_dir / "dragon_tiger"
    paths = _iter_snapshot_paths(base)
    if not paths:
        return {}
    try:
        max_mtime = max(p.stat().st_mtime for p in paths)
    except OSError:
        max_mtime = 0.0
    sig = (len(paths), max_mtime)
    key = str(base)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    index = _build_index(base)
    with _CACHE_LOCK:
        _CACHE[key] = (sig, index)
    return index


def _bands(obs: list[tuple[str, str]]) -> list[dict]:
    """连续同名观测归并成一段。"""
    bands: list[dict] = []
    for day, name in obs:
        if bands and bands[-1]["name"] == name:
            bands[-1]["last_seen"] = day
            bands[-1]["n"] += 1
        else:
            bands.append({"name": name, "first_seen": day, "last_seen": day, "n": 1})
    return bands


def name_history(data_dir: Path, symbol: str, current_name: str | None = None) -> dict:
    """某只票的简称观测轨迹。

    返回:
      observations: [{date, name}]        逐次观测（升序）
      bands:        [{name, first_seen, last_seen, n}]
      changes:      [{from, to, after, before, gap_days}]  改名区间 (after, before]
      renamed:      bool                 出现 ≥2 个不同简称
      current:      维表当前简称（独立来源，不参与合并）
      source:       "dragon_tiger_snapshots"
      snapshots:    参与统计的快照份数
    """
    sym = (symbol or "").strip().upper()
    index = _index_for(data_dir)
    obs = index.get(sym, [])
    bands = _bands(obs)

    changes: list[dict] = []
    for prev, nxt in zip(bands, bands[1:]):
        gap = None
        try:
            gap = (date_cls.fromisoformat(nxt["first_seen"]) - date_cls.fromisoformat(prev["last_seen"])).days
        except ValueError:
            gap = None
        changes.append({
            "from": prev["name"],
            "to": nxt["name"],
            "after": prev["last_seen"],
            "before": nxt["first_seen"],
            "gap_days": gap,
        })

    return {
        "symbol": sym,
        "observations": [{"date": d, "name": n} for d, n in obs],
        "bands": bands,
        "changes": changes,
        "renamed": len({b["name"] for b in bands}) > 1,
        "current": current_name,
        "source": "dragon_tiger_snapshots",
        "snapshots": len(_iter_snapshot_paths(data_dir / "dragon_tiger")),
    }
