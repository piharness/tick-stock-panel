"""分钟 K 全量回补驱动 — 八路并行, 每路写独立 staging, 最后由合并阶段统一落正式分区。

为什么分片而不是直接调 sync_and_persist_minute:
  1. tdx 是逐标的翻页 (99 交易日 ≈ 30 页/只), 单只实测 9.06s, 全市场串行 **14 小时**。
     八路并行实测线性 (8 只 13s), 降到 1.8 小时。
  2. 但 `_write_minute_partition` 是「读旧→concat→unique→原子写」, 多进程同时写同一天
     会互相覆盖 (各自读到旧版再写, 后写的赢)。所以每路只写自己的 staging 目录,
     合并阶段串行调 `_write_minute_partition` —— 质量守卫 (越界副本隔离) 在合并时生效。

用法 (容器内):
  /app/.venv/bin/python /tmp/_minute_backfill.py --shard 0 --shards 8
  /app/.venv/bin/python /tmp/_minute_backfill.py --merge
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
log = logging.getLogger("minute_backfill")

from app.config import settings  # noqa: E402
from app.plugins.tdx.provider import TdxProvider  # noqa: E402

DATA = Path(settings.data_dir)
STAGING = DATA / ".minute_backfill"
MINUTE_DIR = DATA / "kline_minute"
BATCH = 20
WINDOW_DAYS = 150  # 覆盖 tdx 实测深度 99 交易日


def _universe() -> list[str]:
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet", columns=["symbol"])
    return sorted(inst["symbol"].to_list())


def existing_dates() -> list[str]:
    if not MINUTE_DIR.exists():
        return []
    return sorted(d.name[5:] for d in MINUTE_DIR.iterdir() if d.is_dir() and d.name.startswith("date="))


def run_shard(index: int, shards: int, from_date: str | None, to_date: str | None) -> int:
    syms = _universe()
    mine = [s for i, s in enumerate(syms) if i % shards == index]
    have = existing_dates()
    # 默认口径: 补「已有分区最早日」之前的缺口。
    # 传 --from-date/--to-date 时改为补该区间内的残缺天(如 2026-10-08~10-30 只有 39 只)。
    cutoff = have[0] if have else None
    lo = date.fromisoformat(from_date) if from_date else None
    hi = date.fromisoformat(to_date) if to_date else None
    log.info(
        "shard %d/%d: %d 只 (全市场 %d), 已有最早 %s, 目标区间 %s ~ %s",
        index, shards, len(mine), len(syms), cutoff, lo, hi,
    )

    out_root = STAGING / f"shard-{index:02d}"
    provider = TdxProvider()
    # 窗口起点决定翻页数: 只补近端 17 天时从 10-07 起算, 页数从 33 降到 8, 单只 ~3s。
    start = datetime.combine(lo, datetime.min.time()) - timedelta(days=1) if lo else datetime.now() - timedelta(days=WINDOW_DAYS)
    t0 = time.time()
    written = 0
    for i in range(0, len(mine), BATCH):
        batch = mine[i : i + BATCH]
        try:
            df = provider.get_minute(batch, start_time=start, end_time=None)
        except Exception as e:  # noqa: BLE001 - 单批失败不拖垮整片
            log.warning("batch %d 失败 (%s): %s", i, batch[0], e)
            continue
        if df.is_empty():
            continue
        df = df.with_columns(pl.col("datetime").dt.date().alias("_d"))
        if lo is not None:
            df = df.filter(pl.col("_d") >= lo)
        if hi is not None:
            df = df.filter(pl.col("_d") <= hi)
        if lo is None and cutoff:
            df = df.filter(pl.col("_d") < date.fromisoformat(cutoff))
        if df.is_empty():
            continue
        for part in df.partition_by("_d", maintain_order=True):
            d = part["_d"][0].isoformat()
            part = part.drop("_d")
            target = out_root / f"date={d}"
            target.mkdir(parents=True, exist_ok=True)
            part.write_parquet(target / f"part-{i:05d}.parquet")
            written += part.height
        done = min(i + BATCH, len(mine))
        rate = done / max(time.time() - t0, 1e-9)
        log.info(
            "  %d/%d 只 %.1f 行/秒 已落 %d 行 ETA %.0f 分钟",
            done, len(mine), rate, written, (len(mine) - done) / max(rate, 1e-9) / 60,
        )
    log.info("shard %d 完成: %d 行, 耗时 %.0f 分钟", index, written, (time.time() - t0) / 60)
    return written


def run_merge() -> int:
    """staging 各分片按天合并 → 正式分区(串行, 质量守卫生效)。"""
    from app.services.kline_sync import _write_minute_partition

    shards = sorted([d for d in STAGING.iterdir() if d.is_dir() and d.name.startswith("shard-")]) if STAGING.exists() else []
    if not shards:
        log.error("没有 staging 分片可合并")
        return 1
    days: dict[str, list[Path]] = {}
    for sh in shards:
        for d in sh.iterdir():
            if d.is_dir() and d.name.startswith("date="):
                days.setdefault(d.name[5:], []).extend(sorted(d.glob("*.parquet")))
    log.info("待合并 %d 天, 来自 %d 个分片", len(days), len(shards))

    total = 0
    for day in sorted(days):
        files = days[day]
        df = pl.read_parquet(files)
        before = df.height
        n = _write_minute_partition(df, MINUTE_DIR, origin="backfill")
        total += n
        log.info("  %s 读 %d 行 → 落 %d 行 (隔离 %d)", day, before, n, before - n)
    log.info("合并完成: 落盘 %d 行", total)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int)
    ap.add_argument("--shards", type=int, default=8)
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--from-date", help="目标区间下界(含); 同时作为翻页窗口起点")
    ap.add_argument("--to-date", help="目标区间上界(含)")
    a = ap.parse_args()
    if a.merge:
        run_merge()
        return 0
    if a.shard is None:
        ap.error("需要 --shard 或 --merge")
    run_shard(a.shard, a.shards, a.from_date, a.to_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
