"""数据完整性验收 —— 一次跑完, 给出 PASS/FAIL 与退出码。

每条判据都对应一次真实事故, 不是"看起来合理":

  S1 全表 glob 可读          —— 11-05 的 part-stream-0 少一列, polars 全表 scan
                                直接 SchemaError, 所有全表读路径不可用;
  S2 (symbol,date) 无重复     —— 月分区与日分区并存时 11-12 被读成两份;
  S3 无跨布局重叠            —— 同一交易日只许出现在一个分区里;
  C1 个股/指数/ETF 日覆盖     —— 6/599 的分区"存在且非空", 旧判据全绿;
  C2 enriched 不超出 daily    —— 派生表带指数行会让 shift 窗口吃到指数点位;
  H1 完整性体检 0 issue       —— 判据本身要在修复后能变绿, 否则它是装饰。

用法:
  python analysis/_verify_data_completeness.py
  python analysis/_verify_data_completeness.py --days 10
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
DATA = ROOT / "data"

# 各表每日应达到的标的数下限 (维表量级 × 0.9, 留停牌/退市余量)
FLOORS = {
    "kline_daily": 5000,
    "kline_index_daily": 540,
    "kline_etf_daily": 1500,
    "kline_daily_enriched": 5000,
    "kline_index_enriched": 540,
    "kline_etf_enriched": 1500,
}


def _recent_days(n: int) -> list[date]:
    """最近 n 个「已收盘」交易日。

    起点是**昨天**, 不是今天: 日线要收盘后才有, 把"今天还没开盘"算进窗口会让
    六张表在零点之后全部判 0 只 —— 判据自己制造假红, 比漏报更消耗注意力。
    """
    from analysis.trading_calendar import COVERED, HOLIDAYS

    out: list[date] = []
    d = date.today() - timedelta(days=1)
    while len(out) < n and d > COVERED[0]:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def check_glob() -> tuple[bool, str]:
    try:
        lf = pl.scan_parquet(
            str(DATA / "kline_daily" / "**" / "*.parquet"),
            missing_columns="insert", extra_columns="ignore",
        )
        n = lf.select(pl.len()).collect().item()
        return True, f"{n} 行可读"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


def check_dup() -> tuple[bool, str]:
    lf = pl.scan_parquet(
        str(DATA / "kline_daily" / "**" / "*.parquet"),
        missing_columns="insert", extra_columns="ignore",
    ).select(["symbol", "date"])
    df = lf.collect()
    dup = df.height - df.unique(["symbol", "date"]).height
    return dup == 0, f"rows={df.height} dup={dup}"


def check_layout_overlap() -> tuple[bool, str]:
    """同一交易日被两个分区同时覆盖 = 混合布局没迁完。"""
    from app.tickflow.partitions import partition_dates

    base = DATA / "kline_daily"
    owner: dict[str, str] = {}
    clash: list[str] = []
    for p in sorted(base.glob("date=*")):
        if not p.is_dir():
            continue
        for d in partition_dates(p):
            key = d.isoformat()
            if key in owner and owner[key] != p.name:
                clash.append(f"{key}@{owner[key]}+{p.name}")
            owner[key] = p.name
    return not clash, "无重叠" if not clash else f"{len(clash)} 处: {clash[:5]}"


def check_coverage(days: list[date]) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True
    for table, floor in FLOORS.items():
        base = DATA / table
        if not base.exists():
            lines.append(f"  -- {table}: 表不存在")
            continue
        worst = None
        for d in days:
            from app.tickflow.partitions import locate_partition_dir, partition_dates

            part = locate_partition_dir(base, table, d)
            if not part.is_dir() or d not in partition_dates(part):
                n = 0
            else:
                try:
                    n = (
                        pl.scan_parquet([str(p) for p in part.glob("*.parquet")])
                        .filter(pl.col("date") == d)
                        .select(pl.col("symbol").n_unique())
                        .collect()
                        .item()
                    )
                except Exception:  # noqa: BLE001
                    n = 0
            if worst is None or n < worst[1]:
                worst = (d, n)
        assert worst is not None
        good = worst[1] >= floor
        ok &= good
        lines.append(f"  {'OK ' if good else 'LOW'} {table}: 最差 {worst[0]} = {worst[1]} 只 (下限 {floor})")
    return ok, lines


def check_enriched_subset(days: list[date]) -> tuple[bool, list[str]]:
    from app.tickflow.partitions import locate_partition_dir, partition_dates

    lines: list[str] = []
    ok = True
    for d in days:
        daily_part = locate_partition_dir(DATA / "kline_daily", "kline_daily", d)
        enr_part = locate_partition_dir(DATA / "kline_daily_enriched", "kline_daily_enriched", d)
        if not daily_part.is_dir() or not enr_part.is_dir():
            continue
        if d not in partition_dates(daily_part) or d not in partition_dates(enr_part):
            continue
        a = set(
            pl.scan_parquet([str(p) for p in daily_part.glob("*.parquet")])
            .filter(pl.col("date") == d).select("symbol").collect()["symbol"].to_list()
        )
        b = set(
            pl.scan_parquet([str(p) for p in enr_part.glob("*.parquet")])
            .filter(pl.col("date") == d).select("symbol").collect()["symbol"].to_list()
        )
        extra = b - a
        ok &= not extra
        lines.append(f"  {'OK ' if not extra else 'BAD'} {d}: daily={len(a)} enriched={len(b)} 超出={len(extra)}")
    return ok, lines


def check_integrity(lookback: int) -> tuple[bool, list[str]]:
    from app.services import data_integrity as di

    issues = di.scan_recent_integrity(DATA, lookback_days=lookback, deep=True)
    lines = [f"  issues={len(issues)}"]
    for i in issues[:10]:
        lines.append(f"    {i.day} {i.table} {i.kind} {i.reason[:60]}")
    return not issues, lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    args = ap.parse_args()
    days = _recent_days(args.days)
    print(f"验收窗口: {days[0]} .. {days[-1]} ({len(days)} 个交易日)\n")

    results: list[tuple[str, bool]] = []

    ok, detail = check_glob()
    results.append(("S1 全表 glob 可读", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] S1 全表 glob 可读: {detail}")

    ok, detail = check_dup()
    results.append(("S2 (symbol,date) 无重复", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] S2 键唯一: {detail}")

    ok, detail = check_layout_overlap()
    results.append(("S3 无跨布局重叠", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] S3 布局: {detail}")

    ok, lines = check_coverage(days)
    results.append(("C1 日覆盖达到量级", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] C1 日覆盖:")
    print("\n".join(lines))

    ok, lines = check_enriched_subset(days)
    results.append(("C2 enriched ⊆ daily", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] C2 派生表不越界:")
    print("\n".join(lines))

    ok, lines = check_integrity(max(args.days, 7))
    results.append(("H1 体检 0 issue", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] H1 完整性体检:")
    print("\n".join(lines))

    bad = [n for n, ok in results if not ok]
    print(f"\n合计 {len(results) - len(bad)}/{len(results)} 通过")
    if bad:
        print("未通过:", "、".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
