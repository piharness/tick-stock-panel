"""一次性迁移: kline_daily 日分区 (date=YYYY-MM-DD) → 月分区 (date=YYYY-MM)。

背景: kline_daily 按月分区后, DuckDB 视图 glob 的文件数从 367 降到 ~121,
9p 挂载上每条查询从 ~3.9s 降到 ~1.2s; 历史回填 (2428 个交易日) 只有月分区
可行 (日分区回填 = 2428 文件, 每条查询 +25s, 实测见工作区日志 2026-11-08)。

用法 (先停容器, 迁移完成后再 `docker compose up -d --build` 换新代码):
    python scripts/migrate_daily_to_monthly.py --data-dir ../data
    python scripts/migrate_daily_to_monthly.py --data-dir <副本目录> --dry-run

流程: 逐日分区读入 → 按 (symbol, date) 去重合并进月分区 (已有月分区时
日分区数据胜出, 它是最后一手) → 全部写完并逐月核对 (symbol, date) 集合
完全一致后才删除日分区。中断安全: 不删源目录即可安全重跑, 幂等。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.tickflow.partitions import parse_partition_range  # noqa: E402


def _read_partition(part: Path) -> pl.DataFrame:
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(p) for p in files], how="diagonal_relaxed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True, help="数据目录 (含 kline_daily)")
    ap.add_argument("--table", default="kline_daily")
    ap.add_argument("--dry-run", action="store_true", help="只报告计划, 不写入不删除")
    args = ap.parse_args()

    base = Path(args.data_dir) / args.table
    if not base.exists():
        print(f"[migrate] 目录不存在: {base}")
        return 2

    day_dirs: list[Path] = []
    for part in sorted(base.glob("date=*")):
        rng = parse_partition_range(part.name)
        if rng is not None and rng[0] == rng[1]:
            day_dirs.append(part)

    if not day_dirs:
        print("[migrate] 没有日分区, 无需迁移")
        return 0

    # 逐日分区读入, 按月归组
    by_month: dict[str, list[tuple[Path, pl.DataFrame]]] = defaultdict(list)
    total_rows = 0
    for part in day_dirs:
        df = _read_partition(part)
        if df.is_empty():
            print(f"[migrate] 跳过空日分区: {part.name}")
            continue
        month = df.select(pl.col("date").dt.strftime("%Y-%m")).to_series()[0]
        by_month[month].append((part, df))
        total_rows += df.height
    months = sorted(by_month)
    print(
        f"[migrate] 计划: {len(day_dirs)} 个日分区 / {total_rows} 行 → {len(months)} 个月分区 "
        f"({months[0]} … {months[-1]})"
    )
    if args.dry_run:
        for month in months:
            rows = sum(df.height for _, df in by_month[month])
            days = [p.name[5:] for p, _ in by_month[month]]
            print(f"  {month}: {rows} 行, {len(days)} 日 ({days[0]}…{days[-1]})")
        print("[migrate] dry-run, 未写入")
        return 0

    # 写月分区: 日分区数据是最后一手, 拼在后面 keep="last"
    for month in months:
        month_dir = base / f"date={month}"
        month_dir.mkdir(parents=True, exist_ok=True)
        existing = _read_partition(month_dir)
        incoming = pl.concat([df for _, df in by_month[month]], how="diagonal_relaxed")
        merged = (
            pl.concat([existing, incoming], how="diagonal_relaxed")
            .unique(subset=["symbol", "date"], keep="last")
            .sort(["symbol", "date"])
        )
        out = month_dir / "part.parquet"
        tmp = month_dir / "part.parquet.tmp"
        merged.write_parquet(tmp)
        tmp.replace(out)

        # 核对: 每个源日分区的 (symbol, date) 集合必须被月分区完整吸收
        month_keys = merged.select("symbol", "date")
        for part, df in by_month[month]:
            want = df.select("symbol", "date").unique()
            missing = want.join(month_keys, on=["symbol", "date"], how="anti")
            if not missing.is_empty():
                print(
                    f"[migrate] 核对失败: {part.name} 有 {missing.height} 行未进入月分区, 中止 (未删除任何源目录)"
                )
                return 1
        print(f"[migrate] {month}: {merged.height} 行 (吸收 {len(by_month[month])} 个日分区)")

    # 全部核对通过才删除源日分区
    removed = 0
    for month in months:
        for part, _ in by_month[month]:
            for f in sorted(part.glob("*.parquet")):
                f.unlink()
            part.rmdir()
            removed += 1
    print(f"[migrate] 完成: 删除 {removed} 个日分区目录, 保留 {len(months)} 个月分区")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
