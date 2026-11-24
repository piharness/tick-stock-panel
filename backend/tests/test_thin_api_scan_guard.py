"""守卫: API 薄层不得对**月分区表**做全表枚举。

## 为什么需要一条**可执行**的规则

2026-11-24 的实测：`list_partition_dates(kline_daily)` 对 590 个月分区逐个
`scan_parquet` 读 `date` 列 = **18,943ms**。它被 31 处调用，其中若干在首屏/
轮询路径上，一次性造成十几个"接口超时"：

    /api/kline/minute        18.9s   （不传 date 时先问「最新日K是哪天」）
    /api/data/status 冷态    25.4s
    /api/decision/score      24.7s
    /api/case-lab/scan       29.0s

CONTRIBUTING §2.3 早就写着 API 层「保持薄层，不放重计算、全量扫描」，但那句话
没有执行机构 —— 写进文档的规则等于靠自觉。本文件把它变成 CI 会红的断言
（CI 执行 `uv run pytest tests -q`，本文件在 `tests/` 下，因此真的会跑）。

## 两条判据

**规则 A（无条件）**：`app/` 下任何位置调用 `list_partition_dates` /
`partition_date_map`，且实参文本里出现月分区表名，即违规。没有白名单 ——
月分区表的交易日集合有唯一缓存取数点 `services.trading_days.kline_trading_days`
（进程内 + 磁盘快照 + 内容指纹，稳态 0.02s），没有理由再自己扫一遍。

**规则 B（棘轮）**：`app/api/**` 里调用这两个函数，必须登记在 `_ALLOWED` 里，
按 (文件名, 外层函数名) 记账。现存 5 处都在**日分区**表上（读目录名即可，
30~140ms），登记为已知债务；**新增调用点会直接失败**，要加就必须在评审里解释
为什么不能走缓存取数点。

## 为什么不按行号记账

行号每天被格式化/插入改动打散，白名单会变成维护负担，然后有人把它整个注释掉。
函数名是稳定的语义单位。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.tickflow.partitions import MONTHLY_DAILY_TABLES

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
SCAN_FUNCS = {"list_partition_dates", "partition_date_map"}

#: (文件名, 外层函数名) —— 每一处都必须写明为什么可以留在 api 层。
_ALLOWED: dict[tuple[str, str], str] = {
    ("data.py", "_safe_aggregate_enriched"): "enriched 是日分区，读目录名，实测 32ms",
    ("data.py", "_partition_table_stats"): "同上的通用实现，调用方只传日分区表",
    ("data.py", "_safe_aggregate_etf_daily"): "ETF 日K 日分区，读目录名",
    ("data.py", "_safe_aggregate_minute"): "分钟K 日分区，读目录名，实测 3ms",
    ("etf_watch.py", "_store_last_session"): "ETF 日K 日分区，单次状态读",
}


def _iter_py(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _calls(tree: ast.AST):
    """产出 (调用节点, 被调函数名, 外层函数名)。"""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(id(child), node.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (
            fn.id if isinstance(fn, ast.Name)
            else fn.attr if isinstance(fn, ast.Attribute)
            else None
        )
        if name in SCAN_FUNCS:
            yield node, name, owner.get(id(node), "<module>")


def _scan_calls():
    """[(相对路径, 行号, 函数名, 函数名, 实参文本)]，供两条规则共用。"""
    out = []
    for path in _iter_py(APP_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, name, owner in _calls(tree):
            arg = ast.unparse(node.args[0]) if node.args else ""
            out.append((path.relative_to(APP_ROOT), node.lineno, owner, name, arg))
    return out


def test_no_monthly_partition_full_scan_anywhere():
    """规则 A：月分区表的全表枚举没有合法用途。"""
    monthly = [re.escape(t) for t in sorted(MONTHLY_DAILY_TABLES)]
    # 按词边界匹配，否则 `kline_daily_enriched` 会被 `kline_daily` 前缀误伤
    patterns = [(t, re.compile(rf"\b{t}\b")) for t in monthly]
    violations = []
    for rel, line, _owner, _name, arg in _scan_calls():
        for table, pattern in patterns:
            # 实参里出现表名即视为指向月分区表；变量名的间接引用由规则 B 兜底
            if pattern.search(arg):
                violations.append(f"{rel}:{line} -> {arg}")

    assert not violations, (
        "月分区表全表枚举已从读路径移除，不要写回来；"
        "要交易日集合请用 services.trading_days.kline_trading_days()。\n"
        + "\n".join(violations)
    )


def test_api_layer_scan_calls_are_ratcheted():
    """规则 B：api 层的新增全表枚举必须显式登记（棘轮，只许变少）。"""
    api_root = APP_ROOT / "api"
    unregistered = []
    seen: set[tuple[str, str]] = set()
    for rel, line, owner, name, arg in _scan_calls():
        if not str(rel).startswith("api"):
            continue
        key = (Path(rel).name, owner)
        seen.add(key)
        if key not in _ALLOWED:
            unregistered.append(f"api/{Path(rel).name}:{line} in {owner}() -> {name}({arg})")

    assert not unregistered, (
        "API 层是薄层（CONTRIBUTING §2.3），不得新增全表枚举。"
        "若确实必要，在 tests/test_thin_api_scan_guard.py 的 _ALLOWED 里登记并写明理由与实测耗时。\n"
        + "\n".join(unregistered)
    )

    stale = sorted(set(_ALLOWED) - seen)
    assert not stale, f"白名单里的调用点已经不存在，请删掉以免掩盖新问题: {stale}"
