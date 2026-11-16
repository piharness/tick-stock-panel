"""补齐指数日线缺口 (2026-11-05 .. 2026-11-12)。

病灶不是"源没数据", 是三个叠加:
  1. daily_pipeline 的指数起点 = 本地最新分区日; quote_service 每天把 8 只
     实时指数快照写进 kline_index_daily ⇒ 起点恒等于今天 ⇒ 永不回补历史;
  2. fuyao 对 asset_type != stock 一律空手而归 (契约如此), 于是回退 TickFlow;
  3. sync_daily_batch 的超时批次只记 failed_out, 调用方没传该出参 ⇒ 整批
     100 只被静默丢弃, 当日仍写回一个"成功"的分区。

本脚本只补数据, 不改调度: 按小批次拉取 + 对失败批次重试 + 落盘后逐日核对
行数, 不达标就非零退出。

用法:
  python analysis/_backfill_index_daily.py --dry-run
  python analysis/_backfill_index_daily.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("TICKFLOW_DATA_DIR", str(ROOT / "data"))

DATA = ROOT / "data"
START = date(2026, 11, 4)
END = date(2026, 11, 13)
TARGET_DAYS = ["2026-11-05", "2026-11-06", "2026-11-09",
               "2026-11-10", "2026-11-11", "2026-11-12"]


def fetch(symbols: list[str], batch_size: int, rounds: int = 3) -> tuple[pl.DataFrame, list[str]]:
    from app.services.kline_sync import sync_daily_batch

    pending = list(symbols)
    frames: list[pl.DataFrame] = []
    for r in range(rounds):
        if not pending:
            break
        fails: list[str] = []
        df = sync_daily_batch(
            pending, batch_size=batch_size, rpm=60,
            start_time=datetime.combine(START, datetime.min.time()),
            end_time=datetime.combine(END, datetime.min.time()),
            failed_out=fails,
        )
        if not df.is_empty():
            frames.append(df)
        print(f"  轮 {r + 1}: 请求 {len(pending)}, 成功 {df.height} 行, 失败 {len(fails)} 只")
        pending = fails
        batch_size = max(20, batch_size // 2)
    out = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
    return out, pending


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--asset", choices=["index", "etf"], default="index")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("需要 --dry-run 或 --apply")

    inst_name = "instruments_index" if args.asset == "index" else "instruments_etf"
    inst = pl.read_parquet(DATA / inst_name / f"{inst_name}.parquet")
    symbols = sorted(set(inst["symbol"].to_list()))
    print(f"指数维表 {len(symbols)} 只, 窗口 {START}..{END}")

    df, pending = fetch(symbols, args.batch)
    if df.is_empty():
        print("!! 一行都没取到")
        return 2
    days = sorted({str(x) for x in df["date"].unique()})
    print(f"\n取到 {df.height} 行 / {df['symbol'].n_unique()} 只 / {len(days)} 天: {days}")
    print("失败未补:", len(pending), pending[:10])
    per_day = df.group_by("date").len().sort("date")
    print(per_day)

    if args.dry_run:
        print("\n[dry-run] 未写盘")
        return 0

    from app.indicators.pipeline import compute_enriched
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(DATA)
    repo = KlineRepository(store)
    days_to_write = [d for d in days if d in TARGET_DAYS] or days
    sub = df.filter(pl.col("date").is_in([date.fromisoformat(d) for d in days_to_write]))
    enriched = compute_enriched(sub, factors=None, instruments=None)
    if args.asset == "index":
        repo.append_index_daily(sub)
        repo.append_index_enriched(enriched)
        tbl = "kline_index_daily"
    else:
        repo.append_etf_daily(sub)
        repo.append_etf_enriched(enriched)
        tbl = "kline_etf_daily"
    print(f"\n[apply] {args.asset} 写入 {sub.height} 行, 覆盖 {days_to_write}")

    # 落盘核对
    print("\n== 逐日核对 ==")
    bad = 0
    for d in TARGET_DAYS:
        p = DATA / tbl / f"date={d}"
        n = 0
        if p.exists():
            for f in p.glob("*.parquet"):
                n += pl.read_parquet(f).height
        flag = "OK " if n >= 590 else "LOW"
        if n < 590:
            bad += 1
        print(f"  {flag} {d} rows={n}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
