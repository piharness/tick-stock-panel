"""全量历史回补 — 日K(2005→今)、复权因子、enriched 重算、覆盖报告。

背景:本地日K 只有 2024-08-12 起(546 交易日 × 5580 只),而 fuyao 源实测可回溯到
2005-01-04(21.9 年)。所有研究/回测窗口因此被压在 1 年以内。

设计要点:
  - 幂等:重复跑同一区间,落盘走 `unique(symbol,date)` 去重(见 _persist_daily_chunks)。
  - 可续:按 `--from/--to` 切片,一片失败只丢一片,不影响已落盘部分。
  - 走既有契约:复用 kline_sync 的 staging + 月分区合并,不自己写分区命名。
  - 分批目的不是限流(provider 内部已 0.12s/标的节流),而是限制单次失败的影响面。

用法(容器内 /app 为工作目录):
  /app/.venv/bin/python /tmp/_backfill.py --phase daily --from 2005 --to 2016
  /app/.venv/bin/python /tmp/_backfill.py --phase adj
  /app/.venv/bin/python /tmp/_backfill.py --phase enriched
  /app/.venv/bin/python /tmp/_backfill.py --phase report
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime

import polars as pl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("backfill")

from app.config import settings  # noqa: E402
from app.services import kline_sync  # noqa: E402
from app.services.extend_history import _resolve_universe  # noqa: E402
from app.tickflow.capabilities import CapabilitySet  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

DATA = settings.data_dir


def _repo() -> KlineRepository:
    return KlineRepository(DataStore())


def _cb(cur: int, tot: int) -> None:
    if cur == tot or cur % 20 == 0:
        log.info("  进度 %d/%d (%.0f%%)", cur, tot, 100.0 * cur / max(tot, 1))


def phase_daily(repo: KlineRepository, start: date, end: date) -> int:
    universe = _resolve_universe(None, repo)
    log.info("标的池 %d 只, 区间 %s ~ %s", len(universe), start, end)
    t0 = time.time()
    written = kline_sync.sync_and_persist_daily_batch(
        universe,
        repo,
        CapabilitySet(),
        start_date=datetime.combine(start, datetime.min.time()),
        end_date=datetime.combine(end, datetime.min.time()),
        on_chunk_done=_cb,
    )
    log.info("日K写入 %d 行, 耗时 %.1fs", written, time.time() - t0)
    return written


def _patch_adj_closes_from_local() -> None:
    """让因子配价走本地 kline_daily, 而不是逐只打接口。

    provider 原实现只认 fuyao 的日K dump(2016-11 起),2005-2016 的事件因拿不到
    前收盘价而逐只回退,5580 只实测 3 小时。本地 kline_daily 已含 2005-2026 全部
    收盘价,且其 2016-11 之后的部分正是用同一份 dump 落的 —— 同源同列,等价。
    只替换「取价来源」,推导公式仍由 provider 的 get_adj_factors 执行。

    必须 patch 类而不是实例: custom 源对外是 GenericHTTPProvider 包装,真正的
    FuyaoProvider 在其内部,实例属性改不到转发后的实现。
    """
    import polars as pl
    from app.plugins.fuyao.provider import FuyaoProvider, _PREV_CLOSE_BACKDAYS

    def _patched(self, bounds):  # noqa: ARG001
        win = bounds.select(
            "symbol",
            (pl.col("first_ex") - pl.duration(days=_PREV_CLOSE_BACKDAYS)).alias("lo"),
            "last_ex",
        )
        local = (
            pl.scan_parquet(
                DATA / "kline_daily" / "**" / "*.parquet", extra_columns="ignore"
            )
            .select("symbol", "date", "close")
            .join(win.lazy(), on="symbol", how="inner")
            .filter((pl.col("date") >= pl.col("lo")) & (pl.col("date") <= pl.col("last_ex")))
            .collect(engine="streaming")
        )
        out: dict[str, dict[date, float]] = {}
        for sub in local.partition_by("symbol", maintain_order=True):
            out[sub["symbol"][0]] = dict(
                zip(sub["date"].to_list(), sub["close"].to_list(), strict=True)
            )
        log.info("本地日K配价: %d/%d 标的", len(out), bounds.height)
        return out

    FuyaoProvider._closes_from_dumps = _patched  # noqa: SLF001 - 只替换取价来源


def phase_adj(repo: KlineRepository, start: date, end: date) -> int:
    universe = _resolve_universe(None, repo)
    log.info("除权因子 %d 只, 区间 %s ~ %s", len(universe), start, end)
    _patch_adj_closes_from_local()
    t0 = time.time()
    written, affected = kline_sync.sync_adj_factor(
        universe,
        repo,
        CapabilitySet(),
        start_time=datetime.combine(start, datetime.min.time()),
        end_time=datetime.combine(end, datetime.min.time()),
        on_chunk_done=_cb,
        scope="universe",
    )
    log.info("因子写入 %d 行, 影响 %d 只, 耗时 %.1fs", written, len(affected), time.time() - t0)
    return written


def phase_enriched(full: bool) -> int:
    from app.indicators.pipeline import run_pipeline

    t0 = time.time()
    n = run_pipeline(new_dates_only=not full)
    log.info("enriched %s 完成: %d 行, 耗时 %.1fs", "全量" if full else "仅新增日期", n, time.time() - t0)
    return n


def phase_report() -> None:
    """覆盖率矩阵:daily 按年统计交易日/标的/行数 + enriched 覆盖。"""
    d = DATA / "kline_daily"
    t0 = time.time()
    df = (
        pl.scan_parquet(d / "**" / "*.parquet", extra_columns="ignore")
        .select(["date", "symbol"])
        .collect(engine="streaming")
    )
    log.info("kline_daily 扫描 %d 行, %.1fs", df.height, time.time() - t0)
    g = (
        df.group_by(pl.col("date").dt.year().alias("year"))
        .agg(
            pl.len().alias("rows"),
            pl.col("date").n_unique().alias("days"),
            pl.col("symbol").n_unique().alias("symbols"),
            pl.col("date").min().alias("d0"),
            pl.col("date").max().alias("d1"),
        )
        .sort("year")
    )
    print(g.to_pandas().to_string(index=False))
    total_days = df["date"].n_unique()
    log.info("合计: %d 行 / %d 交易日 / %d 标的", df.height, total_days, df["symbol"].n_unique())

    e = DATA / "kline_daily_enriched"
    if e.exists():
        ed = pl.scan_parquet(e / "**" / "*.parquet", extra_columns="ignore").select("date").collect()
        log.info("enriched: %d 行 / %d 交易日", ed.height, ed["date"].n_unique())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["daily", "adj", "enriched", "report"])
    ap.add_argument("--from", dest="start", default="2005-01-01")
    ap.add_argument("--to", dest="end", default=date.today().isoformat())
    ap.add_argument("--full", action="store_true", help="enriched 全量重算(覆盖既有日期)")
    a = ap.parse_args()

    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end)

    if a.phase == "report":
        phase_report()
        return 0
    if a.phase == "enriched":
        phase_enriched(a.full)
        return 0

    repo = _repo()
    if a.phase == "daily":
        phase_daily(repo, start, end)
    else:
        phase_adj(repo, start, end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
