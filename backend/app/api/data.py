"""数据画像 API —— 让前端知道"我们本地有什么数据"。"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.enriched_generation import EnrichedPublication
from app.indicators.pipeline import ENRICHED_COLUMNS
from app.services.pipeline_jobs import job_store
from app.tickflow.partitions import iter_partition_dirs, list_partition_dates, parse_partition_range

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])


class IntegrityScanIn(BaseModel):
    lookback_days: int = Field(default=30, ge=1, le=366)
    asset_types: list[Literal["stock", "etf", "index"]] | None = Field(default=None, min_length=1, max_length=3)


class IntegrityRepairIn(BaseModel):
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def _start_integrity_job(request: Request, operation: Callable, *, repair: bool) -> dict:
    from app.services.pipeline_jobs import (
        JobCancelledError,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )

    job_id, is_new = job_store.create(kind="integrity")
    if not is_new:
        raise HTTPException(status_code=409, detail="已有数据任务正在运行, 请等待完成后再自检或修补")

    def progress(stage, pct, message, **kwargs):
        job_store.progress(job_id, stage, pct, message, **kwargs)

    def run():
        qs = getattr(request.app.state, "quote_service", None)
        with qs.paused() if repair and qs else contextlib.nullcontext():
            return operation(progress)

    def execute():
        try:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务尚未结束, 请稍后重试")
                return
            report = run_with_capacity(job_id, run)
            job_store.succeed(job_id, {"integrity_report": report})
        except JobCancelledError:
            pass
        except Exception as exc:
            logger.exception("日K自检/修补失败: %s", job_id)
            job_store.fail(job_id, str(exc))
        finally:
            release_run_slot(job_id)
            invalidate_data_cache()
            qs = getattr(request.app.state, "quote_service", None)
            notify = getattr(qs, "notify_daily_integrity_updated", None)
            if callable(notify):
                from app.services.integrity_check import latest_report
                report = latest_report(request.app.state.repo.store.data_dir)
                notify(report["asset_types"] if report else [], data_changed=repair)

    try:
        threading.Thread(target=execute, daemon=True, name=f"integrity-{job_id}").start()
    except RuntimeError:
        job_store.fail(job_id, "无法启动数据自检任务")
        raise
    return {"job_id": job_id}


@router.get("/integrity")
def get_integrity_report(request: Request) -> dict:
    from app.services import daily_health
    from app.services.integrity_check import latest_report

    root = request.app.state.repo.store.data_dir
    entries = daily_health.snapshot(root)["entries"]
    # 前端横幅要说清「哪一天、哪一类、什么毛病」。只给 asset 列表的话，
    # 读者只知道「有东西没修完」，不知道去修哪一天——于是只能把错误 toast
    # 当成唯一的线索，而那正是刷屏的来源。
    blocking = [
        {
            "asset_type": e["asset_type"],
            "day": e["day"],
            "kind": e["kind"],
            "reason": e["reason"] or e["kind"],
            "needs_raw": e["needs_raw"],
        }
        for e in entries
    ]
    return {"report": latest_report(root), "blocking_assets": sorted({
        e["asset_type"] for e in entries
    }), "blocking_entries": blocking[:20], "blocking_total": len(blocking),
        "data_revisions": {
            asset: daily_health.revision(root, asset) for asset in daily_health.ASSET_LABELS
        }}


@router.post("/integrity/scan")
def scan_integrity(body: IntegrityScanIn, request: Request) -> dict:
    from app.services.integrity_check import run_check

    root = request.app.state.repo.store.data_dir
    # end_date 交给 run_check 决定：盘中只到昨天，收盘定版后纳入今天。
    # 这样「当天登记的异常当天可修」，而不是提示你去修补、报告里却没有这一天。
    return _start_integrity_job(request, lambda progress: run_check(
        root, lookback_days=body.lookback_days, asset_types=body.asset_types,
        on_progress=progress,
    ), repair=False)


@router.post("/integrity/repair")
def repair_integrity(body: IntegrityRepairIn, request: Request) -> dict:
    from app.data_providers import custom
    from app.services import preferences
    from app.services.integrity_check import latest_report, run_repair
    from app.tickflow.capabilities import Cap

    state = request.app.state
    report = latest_report(state.repo.store.data_dir)
    if report is None or report.get("id") != body.report_id:
        raise HTTPException(status_code=409, detail="自检报告已变化, 请重新自检后再修补")
    if not report.get("repairable_count"):
        raise HTTPException(status_code=400, detail="当前没有可自动修补的问题")
    provider = preferences.get_daily_data_provider()
    custom_daily = provider != "tickflow" and custom.provider_has_dataset(provider, "daily")
    if not custom_daily and not state.capabilities.has(Cap.KLINE_DAILY_BATCH):
        raise HTTPException(status_code=403, detail="当前日K数据源不可用, 请先检查数据源配置")
    return _start_integrity_job(request, lambda progress: run_repair(
        state.repo, state.capabilities, body.report_id, on_progress=progress,
    ), repair=True)

# ===== 缓存:storage(文件扫描) + 每张表 aggregate 各自缓存 =====
# 同步期间前端 2s 轮一次 status,每张表 aggregate 全表 count + min/max + distinct
# 太重,加 TTL + 事件失效。stage 写完只清对应那张表的缓存。

_TABLE_TTL = 300.0  # 兜底 TTL,即使没人调 invalidate 也会过期。
# 注意: 不能短于单次重算耗时 —— DuckDB 全表 count 在 9p 挂载上 ~40s, 若 TTL < 重算耗时,
# 缓存一写入就"已过期", 每个请求都重算 (2026-11-19 /status 40s 超时的根因之一)。
_TABLE_TTL_LARGE = 600.0  # 大表(分钟K等)单独 TTL，避免多分区聚合反复重算
_STORAGE_TTL = 1800.0  # storage 文件扫描独立 TTL,stage 写完不触发重算。
# 注意: 必须远大于单次扫描耗时 —— 全量遍历 2 万+ 文件逐个 stat 在 9p 挂载上
# 实测 ~71s, 若 TTL(60s) < 扫描耗时, 缓存刚写满就过期, 扫描永远在后台刷、
# 永远占满 9p 元数据带宽, 把 /status 自身的文件操作也拖到 25~40s
# (2026-11-19 /status 超时的真正根因, 与表聚合无关)。30min 足够覆盖日更节奏。

# 聚合慢的大表（分区数多、行数多），使用更长的 TTL
_LARGE_TABLES = {"minute"}

_storage_cache: dict[str, Any] | None = None
_storage_cache_ts: float = 0.0
_storage_cache_stale: dict[str, Any] | None = None
_storage_lock = threading.Lock()

_table_cache: dict[str, dict | None] = {
    "daily": None,
    "enriched": None,
    "index_daily": None,
    "index_enriched": None,
    "index_instruments": None,
    "etf_daily": None,
    "etf_enriched": None,
    "etf_instruments": None,
    "minute": None,
    "adj_factor": None,
    "instruments": None,
    "financials": None,
}
_table_cache_ts: dict[str, float] = {k: 0.0 for k in _table_cache}
# 最后一次非空统计 (失效事件不清空): 同步任务期间 /status 的兜底展示 —
# 宁可数字旧一点, 也不能让重聚合在 DuckDB 锁上排队到前端超时
_table_cache_stale: dict[str, dict | None] = {}
_table_cache_lock = threading.Lock()

# 单飞 (single-flight): TTL 过期后的重算只允许一个请求执行, 其余等待复用。
# key = 表名, value = 该表正在重算的完成信号。
_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()

_last_finished_cache: dict[str, str | None] | None = None
_last_finished_lock = threading.Lock()

# 冷启动种子: 上一次进程算出的 storage 值。容器重启后首屏即可显示占用,
# 不必等一次全目录遍历 (1500+ 文件 stat, 9p 挂载上 3.4s)。
_SNAPSHOT_FILENAME = Path(".cache") / "data_status_snapshot.json"
_SNAPSHOT_MAX_AGE_S = 7 * 24 * 3600.0
_snapshot_loaded_for: Path | None = None
_storage_refresh_lock = threading.Lock()
_storage_refreshing = False


def _snapshot_path(data_dir: Path) -> Path:
    return data_dir / _SNAPSHOT_FILENAME


def _load_snapshot(data_dir: Path) -> None:
    """把上次进程落盘的 storage 统计灌进 stale 兜底槽。只做一次, 失败静默。"""
    global _storage_cache_stale, _snapshot_loaded_for
    if _snapshot_loaded_for == data_dir:
        return
    _snapshot_loaded_for = data_dir
    path = _snapshot_path(data_dir)
    try:
        import json

        if not path.is_file():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(payload.get("ts", 0)) > _SNAPSHOT_MAX_AGE_S:
            return
    except Exception as e:  # noqa: BLE001
        logger.debug("status snapshot unreadable: %s", e)
        return

    storage = payload.get("storage")
    if storage is not None:
        with _storage_lock:
            if _storage_cache_stale is None:
                _storage_cache_stale = storage


def _save_snapshot(data_dir: Path) -> None:
    """落盘 storage 供下次进程冷启动使用。写失败不影响请求。"""
    import json

    try:
        with _storage_lock:
            storage = _storage_cache_stale
        if storage is None:
            return
        path = _snapshot_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ts": time.time(), "storage": storage}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        logger.debug("status snapshot write failed: %s", e)


def invalidate_data_cache(table: str | None = None) -> None:
    """数据写入/清除后调用。

    table=None 时清所有表 cache + storage(粗粒度,用于 pipeline 完成/clear);
    指定 table 时只清那张表,不影响 storage(细粒度,用于单 stage 写完)。
    """
    with _table_cache_lock:
        if table is None:
            global _storage_cache, _storage_cache_ts, _last_finished_cache
            _storage_cache = None
            _storage_cache_ts = 0.0
            _last_finished_cache = None
            for k in _table_cache:
                _table_cache[k] = None
                _table_cache_ts[k] = 0.0
        elif table in _table_cache:
            _table_cache[table] = None
            _table_cache_ts[table] = 0.0


def invalidate_storage_cache() -> None:
    """向后兼容入口 — 清全部缓存。新代码请用 invalidate_data_cache(table)。"""
    invalidate_data_cache(None)


def _get_table_stats(
    name: str,
    fetch: Callable[[], dict | None],
    *,
    busy: bool = False,
) -> dict | None:
    """走 TTL+事件 双重缓存。fetch 在锁外执行避免阻塞别的请求。

    busy (同步任务运行中 / 指标预热未完成) 时完全不重算, 直接回最后一次非空值
    (可能为 None): 这些聚合夹着 DuckDB 查询, 与同步管道共用 repository 的
    DuckDB 单连接全局锁 — 管道持锁跑长查询时重算会在锁上排队, 2s 的 /status
    轮询堆积后触发前端 30s 超时 (2026-10-28 用户反馈)。任务结束的失效事件
    会清 TTL 缓存, 结束后第一次调用即恢复正常重算。

    单飞 (single-flight): TTL 过期后, 多个并发轮询若各自触发一次重算, 会在
    DuckDB 全局锁上排队叠加, 把一次 40s 的重算放大成 N 次。这里只让一个请求
    真正重算, 其余等待复用结果。
    """
    ttl = _TABLE_TTL_LARGE if name in _LARGE_TABLES else _TABLE_TTL
    with _table_cache_lock:
        if busy:
            return _table_cache_stale.get(name)
        cached = _table_cache.get(name)
        cached_ts = _table_cache_ts.get(name, 0.0)
        if cached is not None and (time.time() - cached_ts) < ttl:
            return cached

    with _inflight_lock:
        ev = _inflight.get(name)
        if ev is None:
            ev = _inflight[name] = threading.Event()
            owner = True
        else:
            owner = False

    if not owner:
        ev.wait(timeout=ttl)
        with _table_cache_lock:
            cached = _table_cache.get(name)
            if cached is not None:
                return cached
        # 等待超时仍未算完 (极端情况), 兜底自己算一次
        return fetch()

    try:
        fresh = fetch()
        with _table_cache_lock:
            _table_cache[name] = fresh
            # 时间戳必须是「重算完成后的当下」, 不能用进入函数时的旧值 ——
            # 否则 40s 的重算一写入就是「已过期」(>TTL), 每个请求都重算
            # (2026-11-19 /status 每次 40s 的根因)。
            _table_cache_ts[name] = time.time()
            if fresh is not None:
                _table_cache_stale[name] = fresh
        return fresh
    finally:
        with _inflight_lock:
            _inflight.pop(name, None)
        ev.set()


def _safe_aggregate(repo, view: str) -> dict | None:
    """聚合视图基础统计;视图不存在或为空时返 None。"""
    try:
        row = repo.execute_one(
            f"""SELECT count(*) AS rows,
                       min(date) AS earliest,
                       max(date) AS latest,
                       count(DISTINCT symbol) AS symbols,
                       count(DISTINCT date) AS trading_days
                FROM {view}"""
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate %s failed: %s", view, e)
        return None
    if not row or not row[0]:
        return None
    return {
        "rows": int(row[0]),
        "earliest_date": str(row[1]) if row[1] else None,
        "latest_date": str(row[2]) if row[2] else None,
        "symbols_covered": int(row[3] or 0),
        "trading_days": int(row[4] or 0),
    }


def _safe_aggregate_daily(repo, view: str = "kline_daily") -> dict | None:
    """日K轻量统计 — 零数据扫描。

    日期范围与交易日数来自分区目录名 (月分区读一次 date 列), 不读行情数据。
    标的数从 instruments 小表获取（~5000行，毫秒级）。
    """
    daily_dir = repo.store.data_dir / "kline_daily"
    if not daily_dir.exists():
        return None
    dates = list_partition_dates(daily_dir)
    if not dates:
        return None
    ordered = sorted(dates)

    symbols = _count_instruments_symbols(repo)

    return {
        "rows": 0,
        "earliest_date": ordered[0].isoformat(),
        "latest_date": ordered[-1].isoformat(),
        "symbols_covered": symbols,
        "trading_days": len(ordered),
    }


def _partition_column_count(dirpath: Path, *, sample: int = 5) -> int | None:
    """字段数 —— 读最近若干个分区各一个 parquet 的 schema 取并集。

    为什么不 `DESCRIBE <view>`: DuckDB 的视图在**每次**绑定时都要把
    `<dir>/**/*.parquet` 展开一遍, 每个文件一次元数据往返。data/ 走 9p
    绑定挂载时单次约 6ms, 370 个分区就是 2.7s —— 与数据体积无关, 只与
    文件数成正比。实测 2718ms → 约 30ms。

    为什么不是只读最新一个: 视图用的是 `union_by_name=true`, 语义是**并集**,
    而各分区 schema 会漂移 —— 2026-11-13 实测 `kline_etf_enriched` 的
    date=2026-11-12 分区少了 `quote_ts` 列 (11 列), 其余分区 12 列。只读
    最新一个会把字段数报成 11, 即"最新一天缺列"被静默成"这张表只有 11 列"。
    取最近 sample 个分区的并集既便宜又更接近视图语义; 出现分歧时记 warning,
    因为那是写入侧 schema 不一致的信号, 不该只体现在一个数字上。
    """
    parts = iter_partition_dirs(dirpath)
    if not parts:
        return None
    parts.sort(key=lambda p: (parse_partition_range(p.name) or (date.min, date.min))[1])
    names: set[str] = set()
    per_part: list[int] = []
    for part in reversed(parts[-sample:]):
        files = sorted(part.glob("*.parquet"))
        if not files:
            continue
        try:
            import pyarrow.parquet as pq

            # 必须用 ParquetFile 而不是 pq.read_table(单文件): 后者按 dataset
            # 打开, 单文件会被当成目录展开, schema 合并失败, 异常被吞成空集合。
            cols = pq.ParquetFile(files[0]).schema_arrow.names
        except Exception as e:  # noqa: BLE001
            logger.debug("partition schema read failed: %s", e)
            continue
        per_part.append(len(cols))
        names.update(cols)
    if not names:
        return None
    if len(set(per_part)) > 1:
        logger.warning(
            "enriched schema drift under %s: last %d partitions report %s columns; using union=%d",
            dirpath.name, len(per_part), per_part, len(names),
        )
    return len(names)


def _safe_aggregate_enriched(repo) -> dict | None:
    """Enriched 轻量统计 — 零数据扫描。

    字段数从最新分区一个 parquet 的 schema 读（6ms, 不碰数据）;
    日期范围从分区目录名获取（月分区读一次 date 列）;
    标的数从 instruments 小表取。
    """
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return None

    # 字段数：读一个文件的 schema；失败回退视图 DESCRIBE
    fields = _partition_column_count(enriched_dir)
    if fields is None:
        try:
            fields = len(repo.execute_all("DESCRIBE kline_enriched"))
        except Exception:  # noqa: BLE001
            fields = 0

    dates = list_partition_dates(enriched_dir)
    if not dates:
        return None
    ordered = sorted(dates)

    symbols = _count_instruments_symbols(repo)

    return {
        "rows": 0,
        "fields": fields,
        "earliest_date": ordered[0].isoformat(),
        "latest_date": ordered[-1].isoformat(),
        "symbols_covered": symbols,
        "trading_days": len(ordered),
    }


def _count_instruments_symbols(repo) -> int:
    """从 instruments 小表取标的数（~5000行，毫秒级）。"""
    try:
        sym_row = repo.execute_one(
            "SELECT count(DISTINCT symbol) FROM instruments"
        )
        if sym_row and sym_row[0]:
            return int(sym_row[0])
    except Exception:  # noqa: BLE001
        pass
    return 0


def _safe_aggregate_instruments(repo) -> dict | None:
    """instruments 视图统计(无 date 列,用 as_of)。"""
    try:
        row = repo.execute_one(
            """SELECT count(*) AS rows,
                      count(DISTINCT symbol) AS symbols,
                      max(as_of) AS latest_as_of,
                      count_if(name IS NOT NULL AND name != '') AS named
               FROM instruments"""
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate instruments failed: %s", e)
        return None
    if not row or not row[0]:
        return None
    return {
        "rows": int(row[0]),
        "symbols_covered": int(row[1] or 0),
        "latest_as_of": str(row[2]) if row[2] else None,
        "named": int(row[3] or 0),
    }


def _safe_aggregate_index_instruments(repo) -> dict | None:
    """指数 instruments 视图统计。"""
    try:
        row = repo.execute_one(
            """SELECT count(*) AS rows,
                      count(DISTINCT symbol) AS symbols,
                      count_if(name IS NOT NULL AND name != '') AS named
               FROM instruments_index"""
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate instruments_index failed: %s", e)
        return None
    if not row or not row[0]:
        return None
    return {
        "rows": int(row[0]),
        "symbols_covered": int(row[1] or 0),
        "latest_as_of": None,
        "named": int(row[2] or 0),
    }


def _safe_aggregate_etf_instruments(repo) -> dict | None:
    """ETF instruments 统计 — 优先独立 instruments_etf，兼容旧 instruments_index。"""
    queries = [
        """SELECT count(*) AS rows,
                  count(DISTINCT symbol) AS symbols,
                  count_if(name IS NOT NULL AND name != '') AS named
           FROM instruments_etf""",
        """SELECT count(*) AS rows,
                  count(DISTINCT symbol) AS symbols,
                  count_if(name IS NOT NULL AND name != '') AS named
           FROM instruments_index
           WHERE asset_type = 'etf'""",
    ]
    for sql in queries:
        try:
            row = repo.execute_one(sql)
        except Exception as e:  # noqa: BLE001
            logger.debug("aggregate etf instruments fallback failed: %s", e)
            continue
        if row and row[0]:
            return {
                "rows": int(row[0]),
                "symbols_covered": int(row[1] or 0),
                "latest_as_of": None,
                "named": int(row[2] or 0),
            }
    return None


def _partition_table_stats(
    dirpath: Path, symbols: int, *, fields: int | None = None
) -> dict | None:
    """按分区目录名汇总一张表的日期覆盖与交易日数（零 parquet 扫描）。

    消费方只用 earliest/latest/symbols_covered/trading_days（前端 Data 页把
    rows 硬编码为 0）, 因此不需要 `count(*)`。这与 `_safe_aggregate_daily`
    同一口径 —— 全市场行数在 9p 挂载上要逐个打开数百个文件, 换来一个没人
    显示的字段不值得。

    口径提示: `symbols_covered` 取自维表 (instruments_index / instruments_etf),
    是"登记在册的标的数", 不是"有日线的标的数"。两者实测不同 —— 2026-11-13
    的 `kline_etf_daily` 数据里 1720 只, 维表 1769 只 (49 只没有日线)。
    选维表口径的理由: 前端 `Data.tsx` 的兜底链条本来就是
    `etf_daily.symbols_covered ?? etf_instruments.rows`, 即这张卡片在日K为空的
    那天显示的就是维表数; 统一到维表口径反而消除了"同一个数字随有没有日K变"。
    要改成"有数据的标的数"必须先解决"如何在不打开 249 个文件的前提下求得它"。
    """
    dates = list_partition_dates(dirpath)
    if not dates:
        return None
    ordered = sorted(dates)
    out = {
        "rows": 0,
        "earliest_date": ordered[0].isoformat(),
        "latest_date": ordered[-1].isoformat(),
        "symbols_covered": symbols,
        "trading_days": len(ordered),
    }
    if fields is not None:
        out["fields"] = fields
    return out


def _safe_aggregate_index_daily(repo) -> dict | None:
    """指数日K统计 — 分区目录名 + 维表，不扫描行情数据。"""
    dirpath = repo.store.data_dir / "kline_index_daily"
    symbols = (_safe_aggregate_index_instruments(repo) or {}).get("symbols_covered", 0)
    return _partition_table_stats(dirpath, symbols)


def _safe_aggregate_index_enriched(repo) -> dict | None:
    """指数 enriched 统计 — 字段数读一个文件 schema，其余走目录名。"""
    dirpath = repo.store.data_dir / "kline_index_enriched"
    symbols = (_safe_aggregate_index_instruments(repo) or {}).get("symbols_covered", 0)
    return _partition_table_stats(dirpath, symbols, fields=_partition_column_count(dirpath) or 0)


def _safe_aggregate_etf_enriched(repo) -> dict | None:
    """ETF enriched 统计 — 独立 kline_etf_enriched，口径同指数 enriched。"""
    dirpath = repo.store.data_dir / "kline_etf_enriched"
    symbols = (_safe_aggregate_etf_instruments(repo) or {}).get("symbols_covered", 0)
    return _partition_table_stats(dirpath, symbols, fields=_partition_column_count(dirpath) or 0)


def _safe_aggregate_etf_daily(repo) -> dict | None:
    """ETF 日K统计 — 优先独立 kline_etf_daily，兼容旧 index 存储。"""
    dirpath = repo.store.data_dir / "kline_etf_daily"
    if list_partition_dates(dirpath):
        symbols = (_safe_aggregate_etf_instruments(repo) or {}).get("symbols_covered", 0)
        return _partition_table_stats(dirpath, symbols)
    # 旧布局: ETF 行混在 kline_index_daily 里, 只能走 SQL 过滤
    try:
        row = repo.execute_one(
            """SELECT count(*) AS rows,
                      min(date) AS earliest,
                      max(date) AS latest,
                      count(DISTINCT symbol) AS symbols,
                      count(DISTINCT date) AS trading_days
               FROM kline_index_daily
               WHERE symbol IN (
                   SELECT DISTINCT symbol FROM instruments_index WHERE asset_type = 'etf'
               )"""
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate etf daily fallback failed: %s", e)
        return None
    if not row or not row[0]:
        return None
    return {
        "rows": int(row[0]),
        "earliest_date": str(row[1]) if row[1] else None,
        "latest_date": str(row[2]) if row[2] else None,
        "symbols_covered": int(row[3] or 0),
        "trading_days": int(row[4] or 0),
    }


def _safe_aggregate_adj_factor(repo) -> dict | None:
    """adj_factor 视图统计,日期范围对齐日 K 覆盖区间。

    日 K 区间走分区目录名 (月分区读一次 date 列), 不再 `SELECT min/max(date)
    FROM kline_daily` —— 那条查询要展开 363 个文件的元数据, 实测 2.9s, 而它
    只是想要两个日期。
    """
    daily_dir = repo.store.data_dir / "kline_daily"
    daily_dates = list_partition_dates(daily_dir) if daily_dir.exists() else set()
    if not daily_dates:
        return None
    d_min, d_max = min(daily_dates), max(daily_dates)
    try:
        row = repo.execute_one(
            """SELECT count(*) AS rows,
                      count(DISTINCT symbol) AS symbols,
                      count(DISTINCT trade_date) AS trading_days
               FROM adj_factor
               WHERE trade_date BETWEEN ? AND ?""",
            [str(d_min), str(d_max)],
        )
        if not row or not row[0]:
            return None
        return {
            "rows": int(row[0]),
            "symbols_covered": int(row[1]) if isinstance(row[1], (int, float)) else 0,
            "earliest_date": d_min.isoformat(),
            "latest_date": d_max.isoformat(),
            "trading_days": int(row[2] or 0),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("aggregate adj_factor failed: %s", e)
        return None


def _safe_aggregate_minute(repo) -> dict | None:
    """kline_minute 统计 — 从分区目录名获取交易日数，跳过全表扫描。

    分钟 K 按 date=YYYY-MM-DD 分区存储，直接数目录即可，
    无需 count(*) / count(DISTINCT ...) 等昂贵查询。
    """
    minute_dir = repo.store.data_dir / "kline_minute"
    if not minute_dir.exists():
        return None

    dates = list_partition_dates(minute_dir)
    if not dates:
        return None
    ordered = sorted(dates)

    return {
        "rows": 0,  # 不再查询行数
        "earliest_date": ordered[0].isoformat(),
        "latest_date": ordered[-1].isoformat(),
        "symbols_covered": 0,  # 不再查询标的数
        "trading_days": len(ordered),
    }


def _safe_aggregate_financials(repo) -> dict | None:
    """财务数据统计 — 检查各表文件是否存在及行数。"""
    data_dir = repo.store.data_dir
    tables_info: dict[str, dict] = {}
    total_rows = 0

    for table in ("metrics", "income", "balance_sheet", "cash_flow", "shares"):
        path = data_dir / "financials" / table / "part.parquet"
        if path.exists():
            try:
                import polars as pl
                df = pl.read_parquet(path, columns=["symbol"])
                rows = len(df)
                symbols = df["symbol"].n_unique() if not df.is_empty() else 0
                tables_info[table] = {"rows": rows, "symbols": symbols}
                total_rows += rows
            except Exception:
                tables_info[table] = {"rows": 0, "symbols": 0}
        else:
            tables_info[table] = {"rows": 0, "symbols": 0}

    if total_rows == 0:
        return None

    return {
        "rows": total_rows,
        "tables": tables_info,
    }


def _scan_dir_stats(dirpath: Path) -> tuple[int, float]:
    """单次遍历统计目录下文件数和总大小(MB)。比 rglob+stat 快很多。"""
    if not dirpath.exists():
        return 0, 0.0
    count = 0
    total = 0
    for entry in os.scandir(dirpath):
        if entry.is_dir(follow_symlinks=False):
            c, s = _scan_dir_recursive(entry)
            count += c
            total += s
        elif entry.is_file(follow_symlinks=False):
            try:
                total += entry.stat().st_size
            except OSError:
                pass
            count += 1
    return count, round(total / 1048576, 2)


def _scan_dir_recursive(entry: os.DirEntry) -> tuple[int, int]:
    """递归统计一个 DirEntry 下的文件数和总字节数。"""
    count = 0
    total = 0
    try:
        for sub in os.scandir(entry.path):
            if sub.is_dir(follow_symlinks=False):
                c, s = _scan_dir_recursive(sub)
                count += c
                total += s
            elif sub.is_file(follow_symlinks=False):
                try:
                    total += sub.stat().st_size
                except OSError:
                    pass
                count += 1
    except PermissionError:
        pass
    return count, total


def _compute_storage(data_dir: Path) -> dict:
    """单次遍历计算 storage 统计，避免多次 rglob。"""
    import os

    # 只统计关心的子目录
    subdirs = {
        "daily": data_dir / "kline_daily",
        "enriched": data_dir / "kline_daily_enriched",
        "index_daily": data_dir / "kline_index_daily",
        "index_enriched": data_dir / "kline_index_enriched",
        "index_instruments": data_dir / "instruments_index",
        "etf_daily": data_dir / "kline_etf_daily",
        "etf_enriched": data_dir / "kline_etf_enriched",
        "etf_instruments": data_dir / "instruments_etf",
        "etf_adj_factor": data_dir / "adj_factor_etf",
        "minute": data_dir / "kline_minute",
        # 多周期分钟归档 (2026-11 起)。不进上面 "minute" 一项: 体量差 3 倍以上
        # (1m 1.5G / 5m 2.4G / 15m 1G / 30m 0.55G / 60m 0.3G), 合在一起会让
        # 「分钟占了多少」这个问题失去分辨率。
        "minute_5m": data_dir / "kline_5min",
        "minute_15m": data_dir / "kline_15min",
        "minute_30m": data_dir / "kline_30min",
        "minute_60m": data_dir / "kline_60min",
        # 日K全历史前复权归档 (1990-12 起)。与 kline_daily 口径不同, 单独计。
        "daily_qfq_archive": data_dir / "archive" / "kline_daily_qfq",
        "adj_factor": data_dir / "adj_factor",
        "instruments": data_dir / "instruments",
        "ext_data": data_dir / "ext_data",
    }
    stats = {}
    total_size = 0
    for key, d in subdirs.items():
        fc, sz = _scan_dir_stats(d)
        total_size += sz
        stats[f"{key}_files"] = fc
        stats[f"{key}_size_mb"] = sz

    # total: 再加上其他零散目录 (financials 有下方专属明细统计, 不在此列)
    other_dirs = ["pools", "backtest_results", "screener_results", "ai_cache"]
    for name in other_dirs:
        d = data_dir / name
        if d.exists():
            _, s = _scan_dir_stats(d)
            total_size += s

    # financials 单独统计 (明细与 total 各计入一次, 不得与其他目录循环重复累加)
    fin_dir = data_dir / "financials"
    if fin_dir.exists():
        fc, sz = _scan_dir_stats(fin_dir)
        stats["financials_files"] = fc
        stats["financials_size_mb"] = sz
        total_size += sz
    # 根目录散文件
    for entry in os.scandir(data_dir):
        if entry.is_file(follow_symlinks=False):
            try:
                total_size += entry.stat().st_size / 1048576
            except OSError:
                pass
    stats["total_size_mb"] = round(total_size, 2)
    return stats


def _next_cron_run(scheduler, job_id: str) -> str | None:
    """读 APScheduler 下次执行时间。"""
    if not scheduler:
        return None
    try:
        job = scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        pass
    return None


def _get_storage(data_dir: Path, *, busy: bool = False, allow_block: bool = True) -> dict | None:
    """返回缓存的 storage 统计；走独立 TTL，stage 写完不触发重算。

    busy 时同样不重算 (目录 du 在同步写盘期间会显著变慢), 回最后一次值;
    首次调用 (尚无缓存) 返回 None — 契约测试断言 is None, 注解勿再写 dict。

    allow_block=False 时不在请求路径上遍历目录: 一次全量遍历要对 1500+ 个
    parquet 逐个 stat, 9p 挂载上实测 3.4s, 而它只喂「存储占用」卡片。此时
    立即返回上次的值 (冷启动为 None) 并起后台线程重算。"""
    global _storage_cache, _storage_cache_ts, _storage_cache_stale
    now = time.time()
    with _storage_lock:
        if busy:
            return _storage_cache_stale
        if _storage_cache is not None and (now - _storage_cache_ts) < _STORAGE_TTL:
            return _storage_cache

    if not allow_block:
        _load_snapshot(data_dir)
        _schedule_storage_refresh(data_dir)
        with _storage_lock:
            return _storage_cache if _storage_cache is not None else _storage_cache_stale

    fresh = _compute_storage(data_dir)
    with _storage_lock:
        _storage_cache = fresh
        _storage_cache_ts = now
        _storage_cache_stale = fresh
    return fresh


def _schedule_storage_refresh(data_dir: Path) -> None:
    """storage 的单飞后台刷新：并发请求最多触发一个遍历线程。

    纯文件系统遍历，不进 DuckDB，因此不会与交互查询争锁 —— 这一点是它能
    放到后台的前提。表统计那类聚合反而**不能**这么搬：实测把它们塞进后台
    后，交互路径上的 `instruments_etf` 在小锁上等了 2.8s（2026-11-13 实测）。
    """
    global _storage_refreshing
    with _storage_refresh_lock:
        if _storage_refreshing:
            return
        _storage_refreshing = True

    def _run() -> None:
        global _storage_cache, _storage_cache_ts, _storage_cache_stale, _storage_refreshing
        try:
            fresh = _compute_storage(data_dir)
            if fresh is not None:
                with _storage_lock:
                    _storage_cache = fresh
                    _storage_cache_ts = time.time()
                    _storage_cache_stale = fresh
                _save_snapshot(data_dir)
        except Exception as e:  # noqa: BLE001
            logger.debug("background storage scan failed: %s", e)
        finally:
            with _storage_refresh_lock:
                _storage_refreshing = False

    threading.Thread(target=_run, name="status-refresh-storage", daemon=True).start()


def _last_finished(job_label: str) -> str | None:
    """从 JobStore 读最近一次该类型任务的完成时间（缓存到 pipeline 终态失效）。"""
    global _last_finished_cache
    with _last_finished_lock:
        if _last_finished_cache is not None:
            return _last_finished_cache.get(job_label)

    from app.services.pipeline_jobs import job_store
    jobs = job_store.list_recent(limit=50)
    cache: dict[str, str | None] = {}
    for j in jobs:
        if j["status"] not in ("succeeded", "failed"):
            continue
        if "instruments_rows" in (j.get("result") or {}) and "instruments" not in cache:
            cache["instruments"] = j["finished_at"]
        if "daily_days" in (j.get("result") or {}) and "pipeline" not in cache:
            cache["pipeline"] = j["finished_at"]
    with _last_finished_lock:
        _last_finished_cache = cache
    return cache.get(job_label)


def _data_pipeline_busy(request: Request) -> bool:
    """同步任务运行中或指标预热未完成 — /status 的重聚合应退化为纯缓存读。"""
    if not getattr(request.app.state, "indicators_ready", True):
        return True
    # matrix 缓存预热同样占着 DuckDB 全局锁 (启动时一次 ~34s), 期间重聚合会
    # 在锁上排队, 把冷启动首请求拖到 ~48s。让它也退回纯缓存读。
    owner = getattr(request.app.state, "matrix_prewarm_owner", None)
    if owner is not None and getattr(owner, "is_running", False):
        return True
    active_id = job_store.active_id()
    if not active_id:
        return False
    active = job_store.get(active_id) or {}
    return active.get("status") in ("pending", "running")


@router.get("/status")
def status(request: Request) -> dict:
    repo = request.app.state.repo
    scheduler = getattr(request.app.state, "scheduler", None)
    data_dir = repo.store.data_dir
    busy = _data_pipeline_busy(request)
    _load_snapshot(data_dir)

    def table(name: str, fetch) -> dict | None:
        return _get_table_stats(name, fetch, busy=busy)

    return {
        "daily":       table("daily",       lambda: _safe_aggregate_daily(repo)),
        "enriched":    table("enriched",    lambda: _safe_aggregate_enriched(repo)),
    "index_daily":       table("index_daily",       lambda: _safe_aggregate_index_daily(repo)),
    "index_enriched":    table("index_enriched",    lambda: _safe_aggregate_index_enriched(repo)),
    "index_instruments": table("index_instruments", lambda: _safe_aggregate_index_instruments(repo)),
    "etf_daily":         table("etf_daily",         lambda: _safe_aggregate_etf_daily(repo)),
    "etf_enriched":      table("etf_enriched",      lambda: _safe_aggregate_etf_enriched(repo)),
    "etf_instruments":   table("etf_instruments",   lambda: _safe_aggregate_etf_instruments(repo)),
    "minute":      table("minute",      lambda: _safe_aggregate_minute(repo)),
        "adj_factor":  table("adj_factor",  lambda: _safe_aggregate_adj_factor(repo)),
        "instruments": table("instruments", lambda: _safe_aggregate_instruments(repo)),
        "financials":  table("financials",  lambda: _safe_aggregate_financials(repo)),

        # 文件层面信息(缓存; 请求路径不遍历目录)
        "storage": _get_storage(data_dir, busy=busy, allow_block=False),

        # 调度
        "next_instruments_run": _next_cron_run(scheduler, "pre_market_instruments"),
        "next_pipeline_run":    _next_cron_run(scheduler, "daily_pipeline"),
        "last_instruments_run": _last_finished("instruments"),
        "last_pipeline_run":    _last_finished("pipeline"),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        # 同步/修复期间同样可能返回 null 统计, 不可把它当成无本地数据。
        "stats_pending": busy,
        # 指标缓存就绪标志 (启动时 enriched 异步预热, 完成前为 false)
        "indicators_ready": getattr(request.app.state, "indicators_ready", True),
    }


#: 因清库而停止的全量分钟增量线程，按 job 记账。
#: 它独立写 kline_minute/adj_factor，不占 run slot，必须单独暂停；但只靠 finally
#: 恢复的话，清库线程卡死就永久停摆。terminate 钩子按 job 收回复原。
_MINUTE_REFRESH_STOPPED: dict[str, object] = {}
_MINUTE_REFRESH_LOCK = threading.Lock()


def restore_minute_refresh(job_id: str) -> bool:
    """terminate 钩子：清库任务被终止时把全量分钟增量线程恢复回去。"""
    with _MINUTE_REFRESH_LOCK:
        mrs = _MINUTE_REFRESH_STOPPED.pop(job_id, None)
    if mrs is None:
        return False
    try:
        mrs.start()
    except Exception:  # noqa: BLE001
        logger.exception("terminate: 恢复分钟增量失败 job=%s", job_id)
        return False
    logger.warning("terminate: 已恢复 job %s 停止的全量分钟增量", job_id)
    return True


@router.post("/clear", status_code=202)
async def clear_data(request: Request):
    """清除所有本地 Parquet 数据 —— 任务化（202 + job_id + 轮询）。

    为什么不再同步：要删万级文件（9p 挂载上几分钟），同步请求既看不到进度也无法
    取消；客户端断开后线程照跑，卡死还会把 run slot 与行情暂停一起永久占住。
    改走与盘后管道同一条任务通道：单飞 + 重任务槽 + 按 job 收回资源。

    互斥 fail-closed（破坏性操作绝不复用别人的任务）:
      - 任何活跃任务 → 409；
      - 重任务槽被非任务入口占用 → 409。
    复用会返回别人的 job_id，调用方会以为自己的清库跑了 —— 这是最坏的“假成功”。
    """
    from app.services.pipeline_jobs import job_store, run_slot_owner

    state = request.app.state
    if job_store.active_id() or run_slot_owner():
        raise HTTPException(
            status_code=409,
            detail="有数据任务在运行（或上一次任务卡死未结束），请先停止任务或稍后再试",
        )

    from app.api.pipeline import _submit_job  # 局部导入: api.pipeline 反向依赖本模块

    return await _submit_job(
        state.repo,
        getattr(state, "quote_service", None),
        lambda progress: (lambda: _run_clear(state, progress)),
        kind="clear",
        allow_reuse=False,
        long_running=True,   # 万级文件删除, 用长任务停滞阈值
        pause_quotes=False,  # 任务体自己按 job 暂停/恢复, 不叠第二层
    )


def _run_clear(state, progress) -> dict:
    """清库任务体：暂停分钟增量与实时行情 → 删除 → 恢复。

    两个外部线程都按 job 记账：行情暂停走 quote_service 的所有者记账，分钟增量走
    `_MINUTE_REFRESH_STOPPED` + terminate 钩子。不能只靠 finally —— 线程卡死时
    finally 永远不执行，实时行情与分钟增量会一起永久停摆。
    """
    from app.services.job_context import current_job_id

    qs = getattr(state, "quote_service", None)
    mrs = getattr(state, "minute_refresh", None)
    job_id = current_job_id()
    stopped = False
    if mrs is not None:
        with contextlib.suppress(Exception):
            running = bool(mrs.status().get("running"))
            # 无条件 stop: 从 status() 到 stop() 之间它可能刚好启动
            mrs.stop()
            if running:
                stopped = True
                if job_id:
                    with _MINUTE_REFRESH_LOCK:
                        _MINUTE_REFRESH_STOPPED[job_id] = mrs
    progress("clear", 1, "暂停实时行情与全量分钟增量…")
    try:
        if qs is not None:
            with qs.paused():
                return _clear_data_impl(state, progress)
        return _clear_data_impl(state, progress)
    finally:
        if stopped:
            if job_id:
                with _MINUTE_REFRESH_LOCK:
                    _MINUTE_REFRESH_STOPPED.pop(job_id, None)
            with contextlib.suppress(Exception):
                mrs.start()


def _clear_data_impl(state, progress=None):
    import shutil

    from app.services.job_context import current_job_id
    from app.services.pipeline_jobs import JobCancelledError, is_cancelled

    emit = progress or (lambda *a, **k: None)
    repo = state.repo
    data_dir = repo.store.data_dir
    job_id = current_job_id()

    def check_cancel() -> None:
        # 逐文件检查: 只在每 200 个文件上报进度，取消信号不能等到下一次心跳
        if job_id and is_cancelled(job_id):
            raise JobCancelledError(job_id)

    from app.services.daily_health import clear_assets
    deleted = 0
    cleared: list[str] = []
    skipped: list[str] = []
    # recover=True: 清空语义就是接管一切 — 外部进程(崩掉的脚本/中断的管道)
    # 残留的 publishing 标记(owner pid 已死)不应永久阻塞清空; 活进程的发布
    # 仍会被拦(another publication is active), 写盘竞态保护不受影响。
    publications = {
        "kline_daily_enriched": EnrichedPublication(data_dir, "stock", recover=True),
        "kline_etf_enriched": EnrichedPublication(data_dir, "etf", recover=True),
    }

    # ⚠️ 下面这份清单**故意不包含** kline_5min / kline_15min / kline_30min /
    # kline_60min 与 archive/kline_daily_qfq:
    #   - 它们是「上游只保留最近 N 根」的一次性归档, 删掉要按小时重拉;
    #   - 更关键的是**拿不回同一批日期**: 上游窗口每天向前滚, 重拉只会得到
    #     更新的 500 天, 清空时丢掉的那段永远消失。
    # 要连归档一起清, 按 `scripts/archive_intraday_periods.py` 的目录手动处理 ——
    # 那是不可逆操作, 不该混在「清空本地数据重来」这种日常动作里。
    directories = (
        "kline_daily", "kline_daily_enriched", "kline_index_daily", "kline_index_enriched",
        "kline_etf_daily", "kline_etf_enriched", "kline_etf_minute", "kline_minute",
        "adj_factor", "adj_factor_etf", "instruments", "instruments_index", "instruments_etf", "pools", "financials",
        "backtest_results", "screener_results", "ai_cache",
    )
    total_dirs = len(directories)
    try:
        for index, sub in enumerate(directories):
            d = data_dir / sub
            if not d.exists():
                skipped.append(sub)
                continue
            check_cancel()
            stage = f"clear:{sub}"
            base_pct = 5 + int(80 * index / total_dirs)
            emit(stage, base_pct, f"清除 {sub}…")
            publication = publications.get(sub)
            parquet_files = list(d.rglob("*.parquet"))
            if publication is not None and parquet_files:
                publication.begin()
            try:
                for n, f in enumerate(parquet_files, 1):
                    check_cancel()
                    f.unlink()
                    deleted += 1
                    if publication is not None:
                        publication.mark_changed()
                    if n % 200 == 0:
                        # 万级文件在 9p 上可能删十几分钟: 心跳只走 log, 进度条按目录推进
                        emit(stage, base_pct, f"清除 {sub} {n}/{len(parquet_files)}",
                             stage_pct=int(100 * n / len(parquet_files)))
                for child in list(d.iterdir()):
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                if publication is not None:
                    publication.commit()
            except BaseException:
                if publication is not None:
                    publication.abandon()
                raise
            cleared.append(sub)
            emit(stage, 5 + int(80 * (index + 1) / total_dirs), f"{sub} 已清除")

        emit("clear", 86, "清理缓存与运行产物…")
        clear_assets(data_dir, {"stock", "etf", "index"})
        (data_dir / "user_data" / "integrity_report.json").unlink(missing_ok=True)
        # 清除同步历史（内存 + 磁盘 job_store/ 文件夹）。
        # keep=当前任务: 否则清库跑到一半会把自己的记录、_active_id 与取消标志一起删掉，
        # 页面随即 404，而且从此没有任何入口能取消它。
        from app.services.pipeline_jobs import job_store
        job_store.clear(keep=job_id)

        # 清除财务数据
        fin_dir = data_dir / "financials"
        for sub in ("metrics", "income", "balance_sheet", "cash_flow"):
            fp = fin_dir / sub / "part.parquet"
            if fp.exists():
                fp.unlink()
                deleted += 1

        # 清除监控运行数据 (user_data 下仅清运行产物, 不动 monitor_rules/preferences/secrets 等用户配置)
        # - 触发记录 alerts.jsonl
        from app.services import alert_store
        alert_store.clear(data_dir)
        # - 待推送的实时通知队列 (进程内存)
        qs = getattr(state, "quote_service", None)
        if qs is not None:
            qs.clear_pending_alerts()

        # 清除 Polars 缓存
        # 先 clear_cache 无条件清空内存 (refresh_cache 在磁盘无数据时会提前 return,
        # 导致 _enriched_cache 等旧数据残留 —— 清数据后看板仍显示旧数据的根因),
        # 再 refresh_cache 尝试重载 (磁盘有数据则重建缓存)。
        repo.clear_cache()
        repo.refresh_cache()

        # 清除 Screener 进程级 _history_cache (TTL 缓存)
        from app.services.screener import ScreenerService
        ScreenerService.clear_history_cache()

        # 清除 Overview 总览聚合结果缓存 (5s TTL)
        from app.api.overview import invalidate_overview_cache
        invalidate_overview_cache()

        # 刷新 DuckDB 视图（空 parquet 目录也需要重新挂载）——
        # 委托给 repository 的唯一权威实现, 覆盖全部视图 (此前这里内联的副本漏了几张)。
        emit("clear", 94, "重建 DuckDB 视图…")
        repo.rebuild_views()
    except JobCancelledError:
        raise
    except BaseException as exc:
        # 部分删除无法回滚: 必须把「已经删了多少」写进失败原因，
        # 否则用户分不清是「一个都没删」还是「删了一半」。
        raise RuntimeError(f"清除中断（已删除 {deleted} 个文件）：{exc}") from exc

    logger.info("数据已清除: 删除 %d 个 parquet 文件", deleted)
    invalidate_data_cache(None)
    return {"deleted_files": deleted, "cleared": cleared, "skipped": skipped}


# 各表字段说明
_TABLE_FIELD_DESC: dict[str, dict[str, str]] = {
    "kline_daily": {
        "symbol": "股票代码",
        "date": "交易日期",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量",
        "amount": "成交额",
    },
    "kline_enriched": ENRICHED_COLUMNS,
    "kline_index_daily": {
        "symbol": "指数代码",
        "date": "交易日期",
        "open": "开盘点位",
        "high": "最高点位",
        "low": "最低点位",
        "close": "收盘点位",
        "volume": "成交量",
        "amount": "成交额",
    },
    "kline_index_enriched": ENRICHED_COLUMNS,
    "kline_etf_daily": {
        "symbol": "ETF代码",
        "date": "交易日期",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量",
        "amount": "成交额",
    },
    "kline_etf_enriched": ENRICHED_COLUMNS,
    "kline_minute": {
        "symbol": "股票代码",
        "datetime": "分钟时间戳",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量",
        "amount": "成交额",
    },
    "adj_factor": {
        "symbol": "股票代码",
        "trade_date": "除权除息日",
        "ex_factor": "单事件等比因子 = 除权前收盘价 / 除权参考价, 按日期连乘得累计复权因子",
        # 事件明细列: 表结构固定 8 列, 明细由提供事件的数据源(扶摇)回填, TickFlow 源为 null
        "dividend": "每股现金分红(税前, 元)。可空, 仅事件明细数据源回填",
        "bonus": "每股送转比例(10送3 = 0.3)。可空, 仅事件明细数据源回填",
        "allot": "每股配股比例。可空, 仅事件明细数据源回填",
        "allot_price": "配股价(元/股)。可空, 仅事件明细数据源回填",
        "prev_close": "除权前一日收盘价(元), 因子推导基准。可空, 仅事件明细数据源回填",
    },
    "instruments": {
        "symbol": "股票代码",
        "name": "股票名称",
        "code": "股票编码(纯数字)",
        "exchange": "交易所(SH/SZ/BJ)",
        "region": "地区",
        "type": "证券类型",
        "listing_date": "上市日期",
        "total_shares": "总股本",
        "float_shares": "流通股本",
        "tick_size": "最小价格变动单位",
        "limit_up": "涨停限制(%)",
        "limit_down": "跌停限制(%)",
        "as_of": "快照日期",
    },
    "instruments_index": {
        "symbol": "指数代码",
        "name": "指数名称",
        "code": "指数编码(纯数字)",
        "asset_type": "资产类型(index)",
    },
    "instruments_etf": {
        "symbol": "ETF代码",
        "name": "ETF名称",
        "code": "ETF编码(纯数字)",
        "asset_type": "资产类型(etf)",
        "source": "数据源",
    },
}

# view 名 → DuckDB 视图名
_SCHEMA_VIEWS: dict[str, str] = {
    "daily": "kline_daily",
    "enriched": "kline_enriched",
    "index_daily": "kline_index_daily",
    "index_enriched": "kline_index_enriched",
    "index_instruments": "instruments_index",
    "etf_daily": "kline_etf_daily",
    "etf_enriched": "kline_etf_enriched",
    "etf_instruments": "instruments_etf",
    "minute": "kline_minute",
    "adj_factor": "adj_factor",
    "instruments": "instruments",
}


@router.get("/schema/{table}")
def table_schema(request: Request, table: str) -> list[dict]:
    """返回指定表的字段名、类型和中文说明。

    优先从 DuckDB DESCRIBE 读取(有数据时含精确类型)；
    视图不存在(无数据)时回退到 _TABLE_FIELD_DESC 静态定义。
    """
    view = _SCHEMA_VIEWS.get(table)
    if not view:
        return []
    desc_map = _TABLE_FIELD_DESC.get(view, {})
    repo = request.app.state.repo
    fields: list[dict] = []
    try:
        cols = repo.execute_all(f"DESCRIBE {view}")
        for col in cols:
            name = col[0]
            dtype = col[1]
            fields.append({
                "name": name,
                "type": dtype,
                "desc": desc_map.get(name, ""),
            })
    except Exception:  # noqa: BLE001
        # 视图不存在(本地无数据)，用静态字段定义兜底
        if desc_map:
            for name, desc in desc_map.items():
                fields.append({"name": name, "type": "—", "desc": desc})
    return fields


@router.get("/version")
def get_version(request: Request) -> dict:
    """返回当前项目版本号。

    优先读 app.__version__ (与 /health 接口同源, 唯一权威版本),
    回退到项目根 VERSION 文件, 最后兜底 v0.0.0。
    """
    from app import __version__

    # 1. 优先用 app.__version__ (唯一权威版本, 运行时从 frontend/package.json 加载)
    if __version__:
        v = __version__.strip()
        return {"version": v if v.startswith("v") else f"v{v}"}

    # 2. 回退到项目根 VERSION 文件
    from app.config import settings
    project_root = Path(settings.data_dir).parent
    version_file = project_root / "VERSION"
    if version_file.exists():
        v = version_file.read_text(encoding="utf-8").strip()
        if v:
            return {"version": v}

    return {"version": "v0.0.0"}


@router.post("/refresh-cache")
def refresh_cache(request: Request) -> dict:
    """重建 Polars 内存缓存 (clear + refresh), 不清数据、不清 alerts。

    用于跨天后缓存残留、或维表更新后手动刷新场景。
    与设置页「清理并刷新」的区别: 那个清前端 react-query, 这个重建后端 enriched 缓存。
    """
    repo = request.app.state.repo
    repo.clear_cache()
    repo.refresh_cache()
    # 清除 Overview 总览聚合结果缓存 + Screener 历史 TTL 缓存
    from app.api.overview import invalidate_overview_cache
    invalidate_overview_cache()
    from app.services.screener import ScreenerService
    ScreenerService.clear_history_cache()
    logger.info("refresh-cache: Polars 缓存已重建")
    return {"ok": True}
