"""分区 dtype 漂移自检 —— 一个文件写错类型, 整张表的 glob 扫描就永久抛错。

2026-11-13 实测: `kline_daily_enriched/date=2026-11-05/part.parquet` 的 `quote_ts`
是 **double**, 其余 371 个分区 int64 ⇒ `scan_enriched_parquet`(显式传 schema)抛
`SchemaError: Float64 != Int64` ⇒ 读路径 `except` 返回空表 ⇒ 端点静默改从上游拉数
(重启后第一张图 4.8~9.0s、`source=live`、turnover_rate 直接缺失)。
症状在耗时账本上与"上游慢"同形, 只能靠 `日K查询失败` 日志分开。

为什么只查最近 N 个分区: footer 读在 9p 上 ~3.5ms/个, 372 个要 1.3s; 而漂移只
产生于**最近的写入**, 最近 10 个分区 ≈ 100~300ms, 可以放启动路径同步跑。

**只判 dtype**:
- 缺列 → 由 `union_by_name=true` / `missing_columns="insert"` 容忍(记忆 #37), 不算漂移;
- 多列 → 由 `extra_columns="ignore"` 忽略, 不算漂移;
- 整型存成整型而同名契约写 Float64 → 不算漂移: `scan_*_parquet` 的
  `integer_cast="allow-float"` 正是为这种拓宽准备的(`kline_daily.volume` 全表都是
  Int64, 而 `DAILY_STORAGE_SCHEMA` 写 Float64 —— 第一版判据在这里报了 362 条假阳性)。
把这三类也算进来会让判据永远红, 而永远红的判据等于没有。

判读(command line 也是判据的一部分, 必须被执行过)::

    python -m app.services.schema_drift --data-dir data --days 10
    python -m app.services.schema_drift --data-dir data --all      # 全表, 约 1.3s/372 文件
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import polars as pl

from app.parquet import DAILY_STORAGE_SCHEMA, ENRICHED_STORAGE_SCHEMA
from app.tickflow.partitions import iter_partition_dirs, parse_partition_range

logger = logging.getLogger(__name__)

#: 表 → 存储契约。新增一张日表时**必须**在这里登记, 否则它永远不被自检覆盖。
TABLES: dict[str, Mapping[str, pl.DataType]] = {
    "kline_daily": DAILY_STORAGE_SCHEMA,
    "kline_daily_enriched": ENRICHED_STORAGE_SCHEMA,
    "kline_index_enriched": ENRICHED_STORAGE_SCHEMA,
    "kline_etf_enriched": ENRICHED_STORAGE_SCHEMA,
}


@dataclass(frozen=True)
class Drift:
    table: str
    file: str
    column: str
    expected: str
    actual: str

    def describe(self) -> str:
        return f"{self.table}/{Path(self.file).parent.name}/{Path(self.file).name}: {self.column} 期望 {self.expected} 实为 {self.actual}"


_FLOAT_TYPES = (pl.Float32, pl.Float64)
_INT_TYPES = (
    pl.Int8, pl.Int16, pl.Int32, pl.Int64,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
)


def _allowed(expected: pl.DataType, actual: pl.DataType) -> bool:
    """只放行「scan 读得动」的差异。

    `integer_cast="allow-float"` 允许整型列被读成浮点(拓宽无损), 所以
    `kline_daily.volume` 存 Int64 对 Float64 的契约不算漂移 —— 这是扫描选项
    在设计时就接受的差异, 判据若报它就是假阳性。
    """
    return expected in _FLOAT_TYPES and actual in _INT_TYPES


def _recent_dirs(base: Path, days: int | None) -> list[Path]:
    """按分区覆盖区间的**右端**排序取最近 N 个; days=None 时全取。

    不能按目录名排序: `date=2026-11` 字典序在 `date=2026-11-01` 之前(记忆 #25),
    月分区会被挤到最旧的一端, 恰好在检查窗口外。
    """
    keyed: list[tuple[object, Path]] = []
    for d in iter_partition_dirs(base):
        span = parse_partition_range(d.name)
        if span is None:
            continue
        keyed.append((span[1], d))
    keyed.sort(key=lambda kv: kv[0])
    picked = [d for _, d in keyed]
    return picked if days is None else picked[-days:]


def _parquet_files(part: Path) -> list[Path]:
    try:
        with os.scandir(part) as it:
            return [Path(e.path) for e in it if e.name.endswith(".parquet")]
    except OSError:
        return []


def check_recent(
    data_dir: Path,
    *,
    days: int | None = 10,
    tables: Mapping[str, Mapping[str, pl.DataType]] | None = None,
) -> list[Drift]:
    """扫描最近 N 个分区的 parquet footer, 返回 dtype 与契约不符的项。"""
    root = Path(data_dir)
    found: list[Drift] = []
    for table, expected in (tables or TABLES).items():
        base = root / table
        if not base.is_dir():
            continue
        for part in _recent_dirs(base, days):
            for f in _parquet_files(part):
                try:
                    actual = pl.read_parquet_schema(f)
                except Exception as e:  # noqa: BLE001 — 读不了 footer 不阻塞自检
                    logger.debug("schema check skipped %s: %s", f, e)
                    continue
                for column, want in expected.items():
                    got = actual.get(column)
                    if got is not None and got != want and not _allowed(want, got):
                        found.append(
                            Drift(table, str(f), column, str(want), str(got))
                        )
    return found


def describe_drifts(drifts: list[Drift], *, limit: int = 5) -> str:
    head = "; ".join(d.describe() for d in drifts[:limit])
    more = f" …(共 {len(drifts)} 处)" if len(drifts) > limit else ""
    return head + more


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分区 dtype 漂移自检")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--all", action="store_true", help="全表扫描(约 1.3s/372 文件)")
    args = parser.parse_args(argv)

    drifts = check_recent(Path(args.data_dir), days=None if args.all else args.days)
    if not drifts:
        print(f"clean: 无 dtype 漂移 (days={'all' if args.all else args.days})")
        return 0
    print(f"drift: {len(drifts)} 处")
    for d in drifts:
        print("  " + d.describe())
    return 2


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(_main())
