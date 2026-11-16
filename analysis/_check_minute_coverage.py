"""验收: 当日分钟库的覆盖度 / 管道健康 / 价格基准。

## 为什么不是"行数 > 0 且无重复键"

2026-11-12 的 `kline_minute/date=2026-11-12` 有 4,982 行、`(symbol,datetime)` 零重复,
两条旧判据都是绿的 —— 但里面只有 32 只 (`000001.SZ`..`000050.SZ`, 按排序逐字连续),
30 只止于 13:31, 全市场另外 5,539 只是一行都没有。旧判据对这两类失败完全失明。

## 三条判据

  1. 覆盖: `n_symbols >= --min-symbols` 且每只 `count >= --min-bars`
     且 `max(datetime) >= --close-by`。一条同时抓"前缀截断"和"缺尾"。
  2. 管道: 当日日志 0 条 `offset-naive` (naive/aware 比较炸补采) 且
     0 条 `AttributeError` (provider 契约缺失)。
  3. 基准: 逐只调生产端 `_scale_from_amount`, 用它返回的 tol 判红 ——
     这里**不另写阈值**。判据比动手线严会让"生产端正确地不动手"变成假红,
     按"任一红即回滚"就会把对的撤掉。

## 退出码

  0 = 三条全过; 1 = 有判据红; 2 = 当日分区不存在 (没跑, 不是跑坏了)。

用法:
    backend/.venv/Scripts/python.exe analysis/_check_minute_coverage.py
    backend/.venv/Scripts/python.exe analysis/_check_minute_coverage.py --day 2026-11-13
"""

from __future__ import annotations

import argparse
import sys
from datetime import time
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "backend"))

# 判据阈值必须与生产端同源, 不许另写一套 —— 理由见模块 docstring 第 3 条。
from app.plugins.tdx.provider import _scale_from_amount  # noqa: E402

LOG_NAMES = ("backend.log", "backend.log.1")
LOG_PATTERNS = ("offset-naive", "AttributeError")


def check_coverage(
    day: str, min_symbols: int, min_bars: int, close_by: time,
) -> tuple[bool, list[int], pl.DataFrame]:
    """返回 (是否通过, [标的数, 达标数], 逐只指标)。"""
    part = DATA / "kline_minute" / f"date={day}" / "part.parquet"
    if not part.exists():
        return False, [0, 0], pl.DataFrame()
    df = pl.read_parquet(part)
    if df.is_empty():
        return False, [0, 0], pl.DataFrame()
    g = df.group_by("symbol").agg(
        pl.len().alias("n"),
        pl.col("datetime").max().alias("mx"),
    )
    cutoff = close_by.hour * 60 + close_by.minute
    g = g.with_columns(
        ((pl.col("mx").dt.hour() * 60 + pl.col("mx").dt.minute()) >= cutoff).alias("tail_ok")
    )
    ok = g.filter((pl.col("n") >= min_bars) & pl.col("tail_ok"))
    return (
        g.height >= min_symbols and ok.height == g.height,
        [g.height, ok.height],
        g.sort("n"),
    )


def check_logs(day: str, since: str | None = None) -> tuple[dict[str, int], list[str]]:
    """只统计当日行; 轮转文件一起看, 但按行首日期过滤, 不吃前几天的旧账。

    since (ISO 秒级 'YYYY-MM-DD HH:MM:SS') 进一步切掉更早的行: 当天修好之后,
    修复前的旧失败还在同一份日志里, 不切就永远红 —— 判据会变成"证明今天曾经
    坏过"而不是"现在是不是好的"。默认 None = 看全天。
    """
    counts = {p: 0 for p in LOG_PATTERNS}
    samples: list[str] = []
    for name in LOG_NAMES:
        path = DATA / "logs" / name
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith(day):
                    continue
                # 修复当天的旧失败仍留在日志里 -> 不切时刻, 这条判据当天永远红,
                # 修好了也看不出来。--since 是给"改完之后再验一次"用的。
                if since is not None and line[:19] < since:
                    continue
                for pat in LOG_PATTERNS:
                    if pat in line:
                        counts[pat] += 1
                        if len(samples) < 3:
                            samples.append(line.strip()[:150])
    return counts, samples


def check_basis(df: pl.DataFrame) -> tuple[list[tuple], list[tuple]]:
    """逐只反读缩放系数, 用生产端的 tol 判定。返回 (全部行, 红行)。"""
    rows: list[tuple] = []
    for (sym,), sub in df.group_by("symbol"):
        est = _scale_from_amount(sub.sort("datetime").to_dicts())
        if est is None:
            rows.append((sym, None, None, sub.height))
            continue
        scale, tol, n = est
        rows.append((sym, abs(scale - 1.0), tol, n))
    rows.sort(key=lambda r: (-(r[1] or 0.0)))
    reds = [r for r in rows if r[1] is not None and r[1] > r[2]]
    return rows, reds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="交易日 (默认当日, 由分区名反推最近一天)")
    ap.add_argument("--min-symbols", type=int, default=200)
    ap.add_argument("--min-bars", type=int, default=200)
    ap.add_argument("--close-by", default="14:57", help="每只最后一根 bar 的最晚下限")
    ap.add_argument(
        "--since", default=None,
        help="只统计该时刻之后的日志行 (ISO 'YYYY-MM-DD HH:MM:SS'), 用于修复后复验",
    )
    args = ap.parse_args()

    days = sorted(p.name.split("=")[1] for p in (DATA / "kline_minute").glob("date=*"))
    if not days:
        print("分钟库没有任何分区")
        return 2
    day = args.day or days[-1]
    hh, mm = (int(x) for x in args.close_by.split(":"))
    close_by = time(hh, mm)

    part = DATA / "kline_minute" / f"date={day}" / "part.parquet"
    if not part.exists():
        print(f"分区不存在: date={day} (当日没跑, 不是跑坏了)")
        return 2

    df = pl.read_parquet(part)
    cov_ok, (n_sym, n_ok), g = check_coverage(day, args.min_symbols, args.min_bars, close_by)
    log_counts, log_samples = check_logs(day, args.since)
    basis_rows, basis_reds = check_basis(df)

    print(f"== 分钟库验收: {day} ==")
    print(f"  分区 {part.name}: {df.height:,} 行 / {n_sym} 只")

    print("\n-- 1. 覆盖度 --")
    print(f"  n_symbols={n_sym} (门 {args.min_symbols})  "
          f"每只 count>={args.min_bars} 且末尾>={args.close_by}: {n_ok}/{n_sym}")
    if n_sym < args.min_symbols:
        print(f"  ** 红: 只有 {n_sym} 只, 低于 {args.min_symbols} —— 前缀截断 **")
    if n_ok < n_sym:
        bad = g.filter((pl.col("n") < args.min_bars) | ~pl.col("tail_ok"))
        worst = bad.head(5)
        print(f"  ** 红: {bad.height} 只不达标, 最差 5 只:")
        for r in worst.iter_rows(named=True):
            print(f"     {r['symbol']}  n={r['n']}  末根={r['mx']}")

    print("\n-- 2. 管道 (当日日志%s) --" % (f", since {args.since}" if args.since else ""))
    for pat in LOG_PATTERNS:
        print(f"  {pat}: {log_counts[pat]} 条")
    for s in log_samples:
        print(f"     {s}")
    log_ok = all(v == 0 for v in log_counts.values())

    print("\n-- 3. 价格基准 (生产端同源 tol) --")
    print("  symbol      shift_bp   tol_bp    n   判定")
    for sym, shift, tol, n in basis_rows[:5]:
        if shift is None:
            print(f"  {sym}       --        --      {n:4d}  样本不足(不判)")
        else:
            verdict = "红" if shift > tol else "ok"
            print(f"  {sym}  {shift * 1e4:8.2f}  {tol * 1e4:8.2f}  {n:4d}   {verdict}")
    if len(basis_rows) > 5:
        print(f"  ... 共 {len(basis_rows)} 只, 只列偏离最大的 5 只")
    print(f"  红: {len(basis_reds)}/{len(basis_rows)}")
    for sym, shift, tol, n in basis_reds[:5]:
        print(f"     {sym}  shift={shift * 1e4:.2f}bp > tol={tol * 1e4:.2f}bp (n={n})")

    passed = cov_ok and log_ok and not basis_reds
    print(f"\n== 结论: {'通过' if passed else '不通过'} ==")
    if not passed:
        failed = []
        if not cov_ok:
            failed.append("覆盖度")
        if not log_ok:
            failed.append("管道")
        if basis_reds:
            failed.append("基准")
        print(f"  未过: {', '.join(failed)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
