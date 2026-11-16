"""修复 kline_daily 月分区与日分区在边界日的重叠。

背景
----
「向前扩展历史」(`POST /api/kline/extend_history`) 把 2024-08 ~ 2025-05-07
的日K按**月分区** (`date=YYYY-MM`) 落盘。但本地在这之前只有**日分区**
(`date=YYYY-MM-DD`)，最早一天是 2025-05-07。

于是 2025-05-07 这一天同时存在于:
  - 月分区  `data/kline_daily/date=2025-05/part-stream-1.parquet`  (fuyao 拉取, 5356 只)
  - 日分区  `data/kline_daily/date=2025-05-07/part.parquet`        (本地原有, 5357 只)

回测矩阵加载器 `backtest/matrix.py::_scan_matrix_values` 要求 `(date, symbol)`
唯一，读到重复即抛 `ValueError: MarketDataMatrix requires unique date/symbol rows`，
导致启动预热 `_prewarm` 里的 `prewarm_matrix_cache` 失败。

判定
----
日分区是超集: 5357 只，比月分区多一只 `002731.SZ`，且两只集合无「只在月分区」
的票。因此删除月分区里的 05-07 (整个 `part-stream-1.parquet`) 是安全的、无损的；
05-06 (part-stream-0.parquet) 保留 —— 它是本地原来没有的一天。

用法
----
    python analysis/_fix_daily_partition_overlap.py            # 检查并修复(带备份)
    python analysis/_fix_daily_partition_overlap.py --check    # 只检查, 不写

退出码
------
  0  无重叠(已修复或本就干净)
  1  发现重叠并已修复(下次再跑应返回 0)
  2  检查模式下发现重叠(尚未修复)
  3  修复失败(备份失败 / 写入失败)
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DAILY = DATA / "kline_daily"


def _month_dates(month_dir: Path) -> set[date]:
    """月分区目录内 part-stream-*.parquet 覆盖的所有日期。"""
    dates: set[date] = set()
    for p in month_dir.glob("part-stream-*.parquet"):
        df = pl.read_parquet(p, columns=["date"])
        for v in df["date"].to_list():
            dates.add(v if isinstance(v, date) else v.date())
    return dates


def find_overlaps() -> list[tuple[str, date, Path]]:
    """返回 [(月分区名, 重叠日期, 该日所在的 parquet 文件)]。"""
    day_dirs: set[str] = set()
    overlaps: list[tuple[str, date, Path]] = []

    # 先收集所有日分区名 date=YYYY-MM-DD
    for d in DAILY.iterdir():
        if not d.is_dir() or not d.name.startswith("date="):
            continue
        key = d.name[5:]
        if len(key) == 10 and key[4] == "-" and key[7] == "-":
            day_dirs.add(key)

    # 对每个月分区, 检查其中每一天是否有同名日分区 (按文件去重, 不逐行输出)
    for d in sorted(DAILY.iterdir()):
        if not d.is_dir() or not d.name.startswith("date="):
            continue
        key = d.name[5:]
        if not (len(key) == 7 and key[4] == "-"):
            continue
        for p in d.glob("part-stream-*.parquet"):
            df = pl.read_parquet(p, columns=["date"])
            file_dates = {(v if isinstance(v, date) else v.date()).isoformat() for v in df["date"].to_list()}
            for ds in sorted(file_dates & day_dirs):
                overlaps.append((key, date.fromisoformat(ds), p))
    return overlaps


def _backup(month_dir: Path) -> Path:
    """把整个月分区备份到 data/.backup/，带时间戳。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = DATA / ".backup" / f"{month_dir.name}-{ts}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(month_dir, dest)
    return dest


def fix() -> int:
    overlaps = find_overlaps()
    if not overlaps:
        print("OK: kline_daily 无月/日分区重叠")
        return 0

    # 按 (月分区, 日期) 汇总需要从月分区删掉的文件
    by_month: dict[str, set[Path]] = {}
    for month, dd, p in overlaps:
        by_month.setdefault(month, set()).add(p)

    for month, files in sorted(by_month.items()):
        month_dir = DAILY / f"date={month}"
        # 验证: 该文件里除了重叠日之外, 没有其它日期(否则要行级删除, 不能整文件删)
        for p in sorted(files):
            df = pl.read_parquet(p, columns=["date"])
            dates = sorted({(v if isinstance(v, date) else v.date()).isoformat() for v in df["date"].to_list()})
            only_overlap = all(
                any(dd.isoformat() == dd2 for _, dd2, _ in overlaps if dd2.isoformat() in dates)
                for dd in [pl.read_parquet(p, columns=["date"])["date"][0]]
            )
            # 简化: 检查文件内所有日期是否都命中某条重叠记录
            overlap_dates = {dd.isoformat() for _, dd, _ in overlaps}
            if not set(dates) <= overlap_dates:
                print(f"ABORT: {p} 混有非重叠日期 {dates}，不能整文件删，需行级处理", file=sys.stderr)
                return 3

    for month, files in sorted(by_month.items()):
        month_dir = DAILY / f"date={month}"
        backup_dir = _backup(month_dir)
        print(f"backup -> {backup_dir.relative_to(ROOT)}")
        for p in sorted(files):
            p.unlink()
            print(f"deleted {p.relative_to(ROOT)}")

    # 复检
    if find_overlaps():
        print("FAIL: 修复后仍有重叠", file=sys.stderr)
        return 3
    print(f"FIXED: 移除了 {sum(len(fs) for fs in by_month.values())} 个重叠文件")
    return 1


def check() -> int:
    overlaps = find_overlaps()
    if not overlaps:
        print("OK: 无重叠")
        return 0
    for month, dd, p in overlaps:
        print(f"OVERLAP: 月分区 {month} 的 {dd} 在 {p.relative_to(ROOT)} 与日分区重复")
    return 2


ENRICHED = DATA / "kline_daily_enriched"


def find_enriched_dup_partitions() -> list[tuple[str, int, int]]:
    """找 enriched 表里「分区内 (date,symbol) 不唯一」的分区。

    扩展历史重算 enriched 时，把本地已有的边界日 2025-05-07 又富化了一遍，
    与原有数据同写进一个分区 → 同一根 bar 出现两份（amount 末位 / volume 差 1 手）。
    返回 [(分区名, 总行数, 唯一行数)]。
    """
    bad: list[tuple[str, int, int]] = []
    for p in sorted(ENRICHED.glob("date=*/part*.parquet")):
        df = pl.read_parquet(p, columns=["date", "symbol"])
        uniq = df.unique(subset=["date", "symbol"]).height
        if df.height != uniq:
            bad.append((p.parent.name, df.height, uniq))
    return bad


def fix_enriched() -> int:
    bad = find_enriched_dup_partitions()
    if not bad:
        print("OK: enriched 无分区内重复")
        return 0
    for name, total, uniq in bad:
        pdir = ENRICHED / name
        backup = _backup(pdir)
        print(f"backup -> {backup.relative_to(ROOT)}")
        # 分区内可能有多个 part 文件；逐个去重后重写
        for p in sorted(pdir.glob("part*.parquet")):
            df = pl.read_parquet(p)
            before = df.height
            dedup = df.unique(subset=["date", "symbol"], keep="first").sort(["date", "symbol"])
            if dedup.height == before:
                continue
            tmp = p.with_suffix(".dedup.tmp")
            dedup.write_parquet(tmp)
            tmp.replace(p)
            print(f"dedup {p.relative_to(ROOT)}: {before} -> {dedup.height}")
        print(f"FIXED: {name} 去掉 {total - uniq} 行重复")
    if find_enriched_dup_partitions():
        print("FAIL: enriched 去重后仍有重复", file=sys.stderr)
        return 3
    return 1


def check_enriched() -> int:
    bad = find_enriched_dup_partitions()
    if not bad:
        print("OK: enriched 无分区内重复")
        return 0
    for name, total, uniq in bad:
        print(f"ENRICHED-DUP: {name} 内 (date,symbol) 不唯一，{total} 行 -> 应 {uniq}")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不修复")
    ap.add_argument("--enriched", action="store_true", help="只处理 enriched 表的分区内重复")
    ap.add_argument("--daily", action="store_true", help="只处理 kline_daily 的月/日分区重叠")
    args = ap.parse_args()

    if args.check:
        rc = 0
        if args.enriched:
            rc |= check_enriched()
        elif args.daily:
            rc |= check()
        else:
            rc |= check()
            rc |= check_enriched()
        return rc

    rc = 0
    if args.enriched:
        rc |= fix_enriched()
    elif args.daily:
        rc |= fix()
    else:
        rc |= fix()
        rc |= fix_enriched()
    return rc


if __name__ == "__main__":
    sys.exit(main())
