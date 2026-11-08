"""日K分区布局助手。

2026-11 起股票日K (kline_daily) 改按月分区 (date=YYYY-MM), 其余日K表
(index/etf)、enriched、分钟K维持按日分区 (date=YYYY-MM-DD)。

设计约束:
- 写侧对 kline_daily 永远写月分区; 其余表写日分区, 行为不变。
- 读侧助手双布局兼容: 日分区目录名即日期; 月分区目录名只承诺月份,
  真实覆盖日期必须读分区数据的 date 列 (交易日 ≠ 月内全部自然日,
  且月初/月末分区内可能只有部分交易日的行)。
- 混合布局只在迁移窗口短暂存在, 读侧容忍; 写侧不会制造混合, 但迁移
  必须先于新代码的第一次写入, 否则同一天的行会同时落在新旧两个分区,
  glob 查询会把 (symbol, date) 读成两份。
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

# 仅股票日K迁月分区; index/etf 日K、enriched、分钟K保持日分区。
MONTHLY_DAILY_TABLES = frozenset({"kline_daily"})


def is_monthly_daily_table(table: str) -> bool:
    return table in MONTHLY_DAILY_TABLES


def month_label(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def partition_dirname(table: str, day: date) -> str:
    """某表某个交易日所在的分区目录名。写侧与"定位某日分区"的读侧共用。"""
    if is_monthly_daily_table(table):
        return f"date={month_label(day)}"
    return f"date={day.isoformat()}"


def locate_partition_dir(base: Path, table: str, day: date) -> Path:
    """定位某日数据实际所在的分区目录 (读侧专用, 双布局兼容)。

    优先新布局; 目录缺席时回退旧布局 (迁移窗口/旧 fixture)。两者都不存在
    时返回新布局路径 — 写侧据此建月分区, 不复活日分区。
    """
    preferred = base / partition_dirname(table, day)
    if preferred.is_dir():
        return preferred
    legacy_name = f"date={day.isoformat()}" if is_monthly_daily_table(table) else f"date={month_label(day)}"
    legacy = base / legacy_name
    if legacy.is_dir():
        return legacy
    return preferred


def parse_partition_range(name: str) -> tuple[date, date] | None:
    """解析分区目录名 → 覆盖的日期区间。

    "date=2026-11-08" → (该日, 该日); "date=2026-11" → (当月第一天, 当月
    最后一天)。其余 (非 date= 前缀、格式损坏) 返回 None。
    """
    if not name.startswith("date="):
        return None
    value = name[5:]
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return d, d
        year_s, month_s = value.split("-")
        if len(year_s) != 4 or len(month_s) != 2:
            return None
        year, month = int(year_s), int(month_s)
        if not 1 <= month <= 12:
            return None
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        return first, last
    except ValueError:
        return None


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def partition_dates(part: Path) -> set[date]:
    """单个分区目录覆盖的真实交易日集合。

    日分区从目录名直读 (零 parquet 扫描); 月分区读数据的 date 列,
    目录名不承诺月内每个交易日都有行。
    """
    rng = parse_partition_range(part.name)
    if rng is None:
        return set()
    start, end = rng
    if start == end:
        return {start}
    files = sorted(part.glob("*.parquet"))
    if not files:
        return set()
    out: set[date] = set()
    try:
        frame = (
            pl.scan_parquet([str(p) for p in files])
            .select(pl.col("date").unique())
            .collect()
        )
    except Exception:  # noqa: BLE001
        return set()
    for value in frame["date"].to_list():
        d = _as_date(value)
        if d is not None:
            out.add(d)
    return out


def partition_dates_or_span(part: Path) -> set[date]:
    """分区覆盖的日期；数据不可读或为空时退回目录声明的完整区间。

    与 `partition_dates` 的区别只用于完整性检查：月分区损坏后读不出 date 列，
    如果据此返回空集，调用方会把整张表当成「没有数据」而跳过检查。返回目录
    声明的月份区间是 fail-closed——它会让月内每个候选日都去实读文件并报
    corrupt，而不是把损坏静默成干净。正常运行的可读分区仍返回真实日期。
    """
    dates = partition_dates(part)
    if dates:
        return dates
    rng = parse_partition_range(part.name)
    if rng is None:
        return set()
    start, end = rng
    return {start + timedelta(days=i) for i in range((end - start).days + 1)}


def partition_date_map(base: Path) -> dict[Path, set[date]]:
    """base 下全部分区目录 → 真实日期集合 (月分区读数据, 日分区读目录名)。"""
    out: dict[Path, set[date]] = {}
    if not base.exists():
        return out
    for part in sorted(base.glob("date=*")):
        if part.is_dir():
            dates = partition_dates(part)
            if dates:
                out[part] = dates
    return out


def list_partition_dates(base: Path) -> set[date]:
    """全表真实覆盖的日期集合 (双布局兼容)。"""
    out: set[date] = set()
    for dates in partition_date_map(base).values():
        out |= dates
    return out
