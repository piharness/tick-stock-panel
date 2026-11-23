"""A 股代码变更映射（历史代码 → 当前代码）。

为什么需要
----------
通达信/本地日线把换码标的的整段历史**重挂到新码**（300114 中航电测 → 302132 中航成飞），
而 baostock 分钟按**当时代码**给。两个数据源对同一实体用不同代码，任何按 `symbol`
当 identity 的 join 都会静默漏票或双计——这是与「退市缺口」（幸存者偏差）正交的
另一条轴：不是「票不在」，是「同一家票有两个代码」。

本模块只含【已核实】的换码。全量换码名单待 baostock 解封后用 `query_stock_basic`
的 `outDate`/`ipoDate` 补齐——不要凭名称手补（名称带「退」是退市、改名不换码、
换码 ≠ 退市）。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(r"D:/DSH/tick-stock-panel")
ALIAS_FILE = ROOT / "data" / "backfill_bs_minute" / "code_aliases.json"

_forward: dict[str, str] | None = None
_reverse: dict[str, list[str]] | None = None


def _load() -> dict[str, str]:
    global _forward, _reverse
    if _forward is None:
        fwd: dict[str, str] = {}
        rev: dict[str, list[str]] = {}
        if ALIAS_FILE.exists():
            for a in json.loads(ALIAS_FILE.read_text(encoding="utf-8")).get("aliases", []):
                fwd[a["from"]] = a["to"]
                rev.setdefault(a["to"], []).append(a["from"])
        _forward, _reverse = fwd, rev
    return _forward


def resolve(symbol: str) -> str:
    """历史代码 → 当前代码（无映射原样返回）。"""
    return _load().get(symbol, symbol)


def alias_map() -> dict[str, str]:
    """{历史代码: 当前代码} 全表，供 polars `replace` 一次性替换。"""
    return dict(_load())


def historical_codes(symbol: str) -> list[str]:
    """当前代码曾用过的历史代码（反向；含自身）。"""
    _load()
    return _reverse.get(symbol, [])
