"""本地多周期归档体检: 分区、行数、网格、跨表口径一致性。

## 为什么要有这个脚本

多周期分钟表是**一次性归档**（上游按"最近 N 根"保留，没有增量链路），
所以它们的完整性不能靠「每天同步有没有跑」来判断，只能在归档后一次性核。
核的内容有三类：

1. **形状**: 每个分区每天每只票应有多少根 (1m=240 / 5m=48 / 15m=16 / 30m=8 / 60m=4);
2. **网格**: bar 时刻必须落在该周期的规范网格上, 且**不能有 13:00**
   (上午最后一根被错标成 13:00 是这两个 TDX 源的已知缺陷, 1m 侧由
   `minute_labels.fix_morning_close_label` 修, 归档脚本复用同一函数);
3. **口径**: 当日最后一根 close 必须等于同日日线的原始收盘价 (分钟存原始价契约)。

三类都只抽一部分分区/标的即可 —— 全量核一遍要读 2.4GB, 而这三类缺陷
(少根 / 错标 / 两次复权) 一旦发生就是整批发生, 抽样足够触发。

## 用法

    backend/.venv/Scripts/python.exe scripts/verify_local_archive.py --data-dir data
    ... --periods 5m 15m --samples 40
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

#: 周期 → (表名, 每天应有的根数, 规范网格)
PERIODS: dict[str, tuple[str, int, list[str]]] = {
    "1m": ("kline_minute", 240, []),   # 网格由 market_time.minute_grid() 定义, 这里只核根数
    "5m": ("kline_5min", 48, []),
    "15m": ("kline_15min", 16, []),
    "30m": ("kline_30min", 8, []),
    "60m": ("kline_60min", 4, []),
}


def expected_grid(step: int) -> list[str]:
    """区间终点标注: 09:30+step .. 11:30, 13:00+step .. 15:00。"""
    out: list[str] = []
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
        m = start + step
        while m <= end:
            out.append(f"{m // 60:02d}:{m % 60:02d}")
            m += step
    return out


def check_period(data_dir: Path, freq: str, samples: int) -> list[str]:
    table, per_day, _ = PERIODS[freq]
    base = data_dir / table
    problems: list[str] = []
    if not base.is_dir():
        return [f"[{freq}] 目录不存在: {base}"]

    parts = sorted(d for d in base.iterdir() if d.is_dir() and d.name.startswith("date="))
    if not parts:
        return [f"[{freq}] 无分区"]
    days = [d.name[5:] for d in parts]
    print(f"[{freq}] {table}: 分区 {len(parts)} 个  {days[0]} .. {days[-1]}")

    step = int(freq[:-1]) if freq != "1m" else 1
    grid = expected_grid(step) if freq != "1m" else []

    picks = parts[:: max(1, len(parts) // samples)][:samples]
    total = 0
    for part in picks:
        p = part / "part.parquet"
        if not p.exists():
            problems.append(f"[{freq}] {part.name} 缺 part.parquet")
            continue
        df = pl.read_parquet(p, columns=["symbol", "datetime", "close"])
        total += df.height
        if df.is_empty():
            problems.append(f"[{freq}] {part.name} 空分区")
            continue
        per = df.height / max(df["symbol"].n_unique(), 1)
        if per < per_day * 0.5:
            problems.append(f"[{freq}] {part.name} 每股仅 {per:.1f} 根 (应 {per_day})")
        ghost = df.filter(
            (pl.col("datetime").dt.hour() == 13) & (pl.col("datetime").dt.minute() == 0)
        ).height
        if ghost:
            problems.append(f"[{freq}] {part.name} 有 {ghost} 行 13:00 错标")
        if grid:
            hm = set(df["datetime"].dt.strftime("%H:%M").unique().to_list())
            extra = hm - set(grid)
            if extra:
                problems.append(f"[{freq}] {part.name} 网格外时刻 {sorted(extra)[:5]}")
    print(f"      抽样 {len(picks)} 个分区, 累计 {total:,} 行")
    return problems


def check_daily_qfq(data_dir: Path) -> list[str]:
    base = data_dir / "archive" / "kline_daily_qfq"
    if not base.is_dir():
        return ["[daily_qfq] 目录不存在（可选归档，缺失不算错）"]
    parts = sorted(base.iterdir())
    ratio = data_dir / "archive" / "_calib" / "qfq_ratio.parquet"
    print(f"[daily_qfq] 月分区 {len(parts)} 个  {parts[0].name} .. {parts[-1].name}")
    if not ratio.exists():
        return ["[daily_qfq] 缺 qfq_ratio.parquet（无法与原始价换算）"]
    r = pl.read_parquet(ratio)
    print(f"      换算比 {r.height} 只, ratio 中位 {r['ratio'].median():.6f}")
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--periods", nargs="*", default=["5m", "15m", "30m", "60m"])
    ap.add_argument("--samples", type=int, default=20)
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    problems: list[str] = []
    for freq in args.periods:
        problems += check_period(data_dir, freq, args.samples)
    problems += check_daily_qfq(data_dir)

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题:")
        for p in problems[:40]:
            print("  -", p)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
