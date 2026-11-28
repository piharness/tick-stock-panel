"""盘后管道 + 盘前维表同步。

调度:
  09:10 盘前 — 同步个股维表 instruments (全量覆盖)
  15:35 盘后 — 日K同步 + 增量除权因子 + enriched 计算 + 刷新视图
  (默认 15:35: 盘后固定价 15:30 终止 + 供应商日线定稿缓冲, 见 preferences)

盘后同步策略:
  日 K: QuoteService 交易时段已实时落盘 → 有数据时跳过 batch,首次拉 1 年区间
  除权因子: 从已有数据最新日期的下一天开始增量获取,避免重复拉取和计算
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import polars as pl
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.indicators.pipeline import filter_halt_days, run_pipeline
from app.market_time import cn_today
from app.services import index_sync, instrument_sync, kline_sync
from app.services import preferences as _prefs
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.partitions import list_partition_dates, locate_partition_dir
from app.tickflow.pools import get_pool
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

ProgressCb = Callable[..., None]

#: enriched 覆盖校验 / 收盘价比对只看最近多少个日期分区。
#:
#: 2026-11-19 起每日维护固定在 sync_adj 之后被 watchdog 杀掉, 原因就是这件事:
#: 覆盖校验逐个遍历全部 5,315 个历史分区, 每个分区要开两个 parquet (daily 月分区
#: + enriched 日分区)。宿主机 9p 挂载冷读实测 ~0.34s/分区 ⇒ 30 分钟静默 I/O,
#: 超过 1200s 停滞阈值, 任务在 compute_enriched 之前就判死, 后面的指数/ETF 同步
#: 与手册链全都没跑 —— 页面表现是「每日维护一直报错 + 手册数据停在昨天」。
#:
#: 窗口 30 个交易日 (≈6 周) 足够, 因为异常分区只诞生在前沿: 实时 flush 只写「今天」
#: 那一个分区, 历史分区由当日管道按官方日线重算。更早的历史缺口属于「数据修正」
#: (override_start_date 全量重算) 的职责, 不该让每日链路重复付这个成本。
_ENRICHED_VERIFY_WINDOW = 30


def _recent_enriched_partitions(enriched_dir: Path, max_dates: int) -> list[Path]:
    """取最近 max_dates 个 enriched 日期分区, 升序。

    分区名是 ISO 日期 (date=YYYY-MM-DD), 字符串序即时间序, 不需要读 parquet。
    """
    parts: list[Path] = []
    for part in enriched_dir.glob("date=*"):
        try:
            date.fromisoformat(part.stem.split("=", 1)[1])
        except ValueError:
            continue
        parts.append(part)
    parts.sort(key=lambda p: p.name)
    return parts[-max_dates:] if max_dates > 0 else parts


def _prune_partial_enriched_partitions(
    daily_dir: Path,
    enriched_dir: Path,
    *,
    max_dates: int = _ENRICHED_VERIFY_WINDOW,
    on_check: Callable[[str], None] | None = None,
) -> list[str]:
    """删除 symbol 覆盖不完整的 enriched 日期分区, 返回被删的日期 (#223)。

    自选实时路径会在全市场 enriched 生成前提前创建当日分区 (只有几只自选),
    仅按日期目录计数比较会把它误判为完整分区而跳过计算, 造成日K缺失与
    均线错误。按与加工相同的停牌过滤口径检查 symbol 覆盖, 不能直接比较
    行数: 正常剔除停牌记录会让 enriched 少行, 导致每次管道都删除重算。
    删除后 run_pipeline(new_dates_only=True) 会把它们当"新日期"全市场补齐。
    daily 同日分区不存在 (今日日K尚未同步) 时不处理, 留给当日正常流程。

    max_dates / on_check: 只校验最近 max_dates 个分区 (见 _ENRICHED_VERIFY_WINDOW),
    并逐个回调 on_check(日期) —— 调用方用它发进度心跳, 否则慢盘上的长时间 I/O
    会被 1200s 停滞阈值误杀。
    """
    import shutil

    pruned: list[str] = []
    for part in _recent_enriched_partitions(enriched_dir, max_dates):
        day_iso = part.stem.split("=", 1)[1]
        if on_check is not None:
            on_check(day_iso)
        try:
            day = date.fromisoformat(day_iso)
        except ValueError:
            continue
        # kline_daily 月分区: 定位到日所在的月分区 (缺席回退日分区), 数据再按 date 过滤到当日。
        daily_part = locate_partition_dir(daily_dir, "kline_daily", day)
        if not daily_part.exists():
            continue
        try:
            expected: set[str] = set()
            # 每次只读单文件的停牌判定列, 不加载全历史或指标宽表。
            for path in daily_part.glob("*.parquet"):
                schema = pl.read_parquet_schema(path)
                if not {"symbol", "open", "high"}.issubset(schema):
                    raise ValueError("daily 缺少 symbol/open/high, 无法判断有效标的覆盖")
                columns = [c for c in ("symbol", "date", "open", "high", "volume", "amount") if c in schema]
                daily = pl.read_parquet(path, columns=columns)
                if "date" in daily.columns:
                    daily = daily.filter(pl.col("date") == day)
                expected.update(filter_halt_days(daily)["symbol"].drop_nulls().to_list())
            actual: set[str] = set()
            for path in part.glob("*.parquet"):
                actual.update(pl.read_parquet(path, columns=["symbol"])["symbol"].drop_nulls().to_list())
        except Exception as e:
            logger.warning("enriched 覆盖检查跳过 %s, 保留分区: %s", part.name, e)
            continue
        if expected - actual:
            shutil.rmtree(part, ignore_errors=True)
            pruned.append(part.stem.split("=")[1])
    return pruned


def _prune_stale_price_partitions(
    daily_dir: Path,
    enriched_dir: Path,
    max_dates: int = 5,
    on_check: Callable[[str], None] | None = None,
) -> list[str]:
    """删除收盘价与官方日线不一致的 enriched 日期分区。

    实时 flush 写入的当日分区行数与 daily 相同, 但收盘价可能停留在收盘集合
    竞价前的快照 (实测: TickFlow 实时端点收盘后仍长期返回旧价, 3392/5554 只
    股票当日收盘价与官方日线不符), #223 的行数校验识别不到。对最近若干交易日
    做值级比对: enriched.raw_close 与 daily.close 任一标的差超过半个最小报价
    单位即删分区, 由后续增量重算按官方日线全市场重建。

    候选集合直接从最近 max_dates 个 enriched 分区取, 不再遍历全历史分区逐个
    stat —— 这里本来只比对最新几天, 却要为 5,315 个历史分区做 locate_partition_dir。
    """
    import shutil

    candidates: list[str] = []
    for part in reversed(_recent_enriched_partitions(enriched_dir, max_dates)):
        try:
            day = date.fromisoformat(part.stem.split("=", 1)[1])
        except ValueError:
            continue
        if locate_partition_dir(daily_dir, "kline_daily", day).exists():
            candidates.append(day.isoformat())
    common = candidates[:max_dates]
    pruned: list[str] = []
    for ds in common:
        # 每个候选要读整个日K月分区(几千只 × 当月所有行) + 整个 enriched 日分区,
        # 9p 上单个可达数十秒。不发心跳就会被 1200s 停滞判定当成卡死。
        if on_check is not None:
            on_check(ds)
        try:
            day = date.fromisoformat(ds)
            part_dir = locate_partition_dir(daily_dir, "kline_daily", day)
            schema = pl.read_parquet_schema(next(part_dir.glob("*.parquet")))
            columns = [c for c in ("symbol", "date", "close") if c in schema]
            daily = pl.read_parquet(part_dir / "*.parquet", columns=columns)
            if "date" in daily.columns:
                daily = daily.filter(pl.col("date") == day)
            enr = pl.read_parquet(
                enriched_dir / f"date={ds}" / "*.parquet", columns=["symbol", "raw_close"]
            )
        except Exception:
            continue  # 列缺失/不可读 → 交给既有完整性检查兜底
        joined = enr.join(daily, on="symbol", how="inner").drop_nulls()
        if joined.is_empty():
            continue
        bad = joined.filter((pl.col("raw_close") - pl.col("close")).abs() > 0.005)
        if not bad.is_empty():
            shutil.rmtree(enriched_dir / f"date={ds}", ignore_errors=True)
            pruned.append(ds)
    return pruned


class PipelineStageError(RuntimeError):
    """管道有阶段软失败(数据可能陈旧)时抛出, 让上层 job_store 把任务标记为 failed。

    这些阶段单独 try/except 吞掉异常以不中断整条管道, 但一旦失败即代表对应数据陈旧。
    抛出前进度协议已走完(done/100), 故前端进度条正常收尾, 仅终态如实反映为 failed ——
    不再"部分失败却报成功"。
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("盘后管道部分阶段失败: " + "; ".join(errors))


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _invalidate(table: str | None = None) -> None:
    """stage 写完调用,让 /api/data/status 只重算被影响的那张表。"""
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet, repo=None) -> list[str]:
    """解析标的池 — 本地 instruments 维表 + 自选 (单一来源, 无池兜底)。

    不再拉 CN_Equity_A 池: 其 universe 匹配曾错配到 Expired 变体、触发
    SW1 行业聚合兜底, 产出 4224 只的残缺池。维表由盘后管道 Step 0 先同步,
    这里只读 parquet; 维表缺失时仅剩自选, 宁缺毋假 (不塞演示标的),
    由上游按空池结果如实呈现。

    repo 传入时过滤自选里的指数 symbol (指数日K走独立 kline_index_* 存储,
    进股票池会污染 kline_daily/kline_minute)。ETF 刻意保留 (既有行为)。
    """
    base: set[str] = set(get_pool("watchlist"))
    inst_path = Path(settings.data_dir) / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:  # noqa: BLE001
            logger.warning("instruments 维表读取失败, 标的池仅剩自选: %s", e)
    else:
        logger.warning("instruments 维表缺失, 标的池仅剩自选 %d 只", len(base))
    if repo is not None:
        base -= set(repo.get_index_symbol_set())
    return sorted(base)


def run_instruments_sync(repo: KlineRepository) -> dict:
    """盘前同步个股维表。

    维表含当日涨跌停价 (limit_up/down), 同步完成后刷新 enriched 内存缓存,
    确保跨天后连板梯队/策略等读到的是基于最新维表的数据 (而非前一交易日残留)。
    """
    rows = instrument_sync.sync_instruments(repo.store.data_dir)
    _refresh_instruments_view(repo)
    _invalidate("instruments")
    # 维表更新后重建 enriched 缓存 (clear + refresh, 与设置页「清理并刷新」同等效果)
    if rows > 0:
        repo.clear_cache()
        repo.refresh_cache()
    return {"instruments_rows": rows}


def run_now(
    repo: KlineRepository,
    capset: CapabilitySet,
    on_progress: ProgressCb | None = None,
    override_start_date: _date | None = None,
    override_symbols: list[str] | None = None,
) -> dict:
    """立即执行一次盘后管道,支持进度回调。

    跳过的 stage **不 emit**,避免前端把"无 capability"的卡片错误标记为 active/done。
    result 里带 skipped_stages 列表供前端展示。

    override_start_date: 传入时强制走 batch 拉取分支,用该日期作为日K/除权/指数的
        拉取起点(到今天),用于「数据修正/补数据」场景。None 时走原有自动判定逻辑。
    override_symbols: 数据修正时只重拉这些股票的日K (issue #514: 修复只补坏标的,
        不再全市场重拉)。None = 全标的池。仅作用于日K (kline_daily) 阶段;
        ETF/指数族沿用各自的全量口径。
    """
    emit = on_progress or _noop
    skipped: list[str] = []
    # 阶段软失败累积: 下列阶段 try/except 吞异常以不中断管道, 但失败即代表数据可能陈旧。
    # 管道末尾若非空则抛 PipelineStageError, 让任务终态如实标记为 failed(而非误报成功)。
    stage_errors: list[str] = []

    # Step 0: 先同步个股维表, 再解析标的池 — 确保标的池基于最新 instruments
    emit("sync_instruments", 2, "同步个股维表…")
    inst_rows = instrument_sync.sync_instruments(
        repo.store.data_dir,
        # 只换文案、不涨百分比: 阶段总进度仍是 2%→8%, 但三家交易所逐个可见,
        # 不会在 2% 上静默两三分钟。
        on_progress=lambda msg: emit("sync_instruments", 2, msg, skip_log=True),
    )
    if inst_rows > 0:
        _refresh_instruments_view(repo)
    emit("sync_instruments", 8, f"个股维表同步完成,{inst_rows} 只标的")
    _invalidate("instruments")

    emit("resolve_universe", 9, "解析标的池…")
    universe = _resolve_universe(capset, repo)
    emit("resolve_universe", 10, f"标的池规模:{len(universe)} 只")

    # 已知异常不能先进入指标计算, 再在末尾才报失败。修补与自检页面共用链路。
    from app.services.integrity_check import repair_known_data
    repair_known_data(repo, capset, on_progress=emit)

    # Step 1: 日 K 同步
    #   override_start_date 传入 → 强制 batch 拉取 [override_start_date ~ today] (数据修正)
    #   付费档 + 今天有数据 → 实时行情接口拉一次覆写（1请求全市场）
    #   有历史数据 → batch K-line API 补齐缺口
    #   无任何数据 → batch K-line API 拉首次 1 年
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    latest_daily = repo.latest_daily_date()
    # 管道「今天」必须是北京日期: 美西主机 15:35 北京时间仍是本地昨天,
    # date.today() 会把昨日日K当成已齐, 当日官方收盘价永远拉不进来。
    today = cn_today()
    today_exists = latest_daily and latest_daily >= today
    new_daily_days = 0

    # 完整性自愈: 检测最近交易日的盘中快照/缺口 (盘中停机后次日开实时会留下
    # 中午快照, 而下方"今天已有数据→只刷今天"分支会让它永久留存)。
    # 命中 → 本次管道放弃实时覆写分支, 降级为从最早坏日起的范围拉取。
    integrity_issues: list = []
    stale_day: _date | None = None
    etf_stale_day: _date | None = None
    index_stale_day: _date | None = None
    if override_start_date is None:
        try:
            from app.services import data_integrity
            integrity_issues = data_integrity.scan_recent_integrity(
                repo.store.data_dir, today=today,
            )
            integrity_issues = [i for i in integrity_issues if i.table in data_integrity.active_daily_tables()]
            if integrity_issues:
                stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_daily",))
                etf_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_etf_daily",))
                index_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_index_daily",))
                logger.warning(
                    "integrity: 检测到 %d 个不完整分区(%s), 本次管道改走范围拉取修复",
                    len(integrity_issues), data_integrity.describe_issues(integrity_issues),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("integrity scan failed (soft, 按无坏数据处理): %s", e)
            integrity_issues = []
        # 恒等式审计: 与修补链分离。这里只报告 + 落盘, 不改范围拉取起点 ——
        # 因为其中「北交所 amount 口径」「指数行混入」重拉修不好, 接进修补链
        # 会变成永不收敛的红灯。
        try:
            from app.services import data_integrity as _di

            fact_issues = _di.audit_recent_facts(repo.store.data_dir, today=today)
            if fact_issues:
                logger.warning(
                    "facts: %d 个分区违反落盘恒等式(不自动修补, 见 %s): %s",
                    len(fact_issues), "/".join(_di.FACT_REPORT_REL),
                    "; ".join(f"{i.day} {i.table} {i.reason}" for i in fact_issues[:3]),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("fact audit failed (soft): %s", e)
    # 日K范围拉取的起点(分支3补缺口/分支4首次/数据修正); 实时增量/跳过时为 None。
    # 供 Step 1.5 除权因子回溯范围对齐: 范围拉取→用日K范围, 非范围→最近N天兜底。
    daily_range_start: _date | None = None

    # A 股日K拉取开关(默认开);关闭时跳过日K同步,保留已有数据。
    # 数据修正(override_start_date)时即使关闭开关也强制拉取 — 修正就是来补数据的。
    pull_a_share = _prefs.get_pipeline_pull_a_share()
    if not pull_a_share and not override_start_date:
        emit("sync_daily", 45, "已跳过 A 股日K同步(拉取内容未勾选)")
        logger.info("sync_daily: skipped (pipeline_pull_a_share=False)")
    elif override_start_date:
        # 数据修正: 强制用传入日期作起点 batch 拉取, 忽略实时行情覆写分支。
        start_date = override_start_date
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] repair/override", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        # 完整性修复带了坏 symbol 清单时只拉这些标的 (issue #514);
        # 单票失败也要上浮 (failed_out), 不能只留 WARNING。
        symbols_for_repair = list(override_symbols) if override_symbols else universe
        repair_failed_symbols: list[str] = []
        written_daily = kline_sync.sync_and_persist_daily_batch(
            symbols_for_repair, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
            failed_out=repair_failed_symbols,
        )
        if override_symbols:
            emit("sync_daily", 45,
                 f"日K 完成(定向修复 {len(symbols_for_repair)} 只, 失败 {len(repair_failed_symbols)} 只)")
        else:
            gap_days = (today - start_date).days
            new_daily_days = gap_days
            emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info(
            "sync_daily: [%s ~ %s] done, symbols=%d(%s) failed=%d",
            start_date, today, len(symbols_for_repair),
            "targeted" if override_symbols else "universe",
            len(repair_failed_symbols),
        )
        if repair_failed_symbols:
            preview = ", ".join(repair_failed_symbols[:10])
            more = "" if len(repair_failed_symbols) <= 10 else f" 等 {len(repair_failed_symbols)} 只"
            stage_errors.append(f"日K修复有 {len(repair_failed_symbols)} 只标的拉取失败: {preview}{more}")
    elif (
        today_exists
        and stale_day is None
        and capset.has(Cap.QUOTE_POOL)
        and _prefs.get_daily_data_provider() == "tickflow"
    ):
        # 付费档:今天有数据(QuoteService 已落盘)→ 实时行情覆写,确保最新。
        # stale_day 非空时禁用本分支: "只刷今天"会让停机日的盘中快照永久留存,
        # 降级到下方 batch 路径从坏日起重拉。
        # free/none 档无 quote.pool 能力,即便今天已有数据(如从 expert 降级),
        # 也降级到下方 batch 路径刷新,避免调用无权限的实时行情接口。
        emit("sync_daily", 12, f"获取日K [{today} ~ {today}] 实时行情…")
        written_daily = kline_sync.sync_daily_by_quotes(repo)
        new_daily_days = 1
        emit("sync_daily", 45, f"日K 完成,{written_daily} 只标的")
        logger.info("sync_daily: [%s ~ %s] live quotes, %d symbols", today, today, written_daily)
    elif latest_daily or stale_day:
        # 有历史 → batch 补齐缺口。
        # 也覆盖"今天已有数据但无实时行情权限(free/none)"的降级场景:
        #   此时 start_date = latest_daily = today,batch 刷新当天日K。
        # 完整性修复场景: start_date = min(本地最新日, 最早坏日) —
        #   today_exists 时 latest_daily=今天, 不取 min 会漏掉坏日。
        start_date = min(d for d in (latest_daily, stale_day) if d is not None)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] %s", start_date, today,
                    "refresh today" if today_exists else "gap fill")

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        gap_days = (today - start_date).days
        new_daily_days = gap_days
        emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info("sync_daily: [%s ~ %s] done, %d days", start_date, today, gap_days)
    else:
        # 首次：无任何数据 → batch 拉 1 年
        start_date = today - _td(days=365)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] initial fetch", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        new_daily_days = 365
        emit("sync_daily", 45, "日K 完成")
        logger.info("sync_daily: [%s ~ %s] done", start_date, today)
    _invalidate("daily")

    # 完整性修复时删除股票 enriched 的坏分区: 增量重算只算 enriched 里不存在
    # 的日期, 盘中快照日分区已存在(虽是错的), 不删永远不会被重算。删除后
    # Step 2 把这些日期当"新日期"重算 (剩余分区最近 60 天做历史前缀, 窗口 ≤5 天回看充足)。
    repair_start = override_start_date if override_start_date is not None else stale_day
    if repair_start is not None:
        try:
            from app.services.data_integrity import prune_enriched_partitions
            pruned = prune_enriched_partitions(
                repo.store.data_dir, repair_start, "kline_daily_enriched",
            )
            if pruned:
                logger.info("integrity: 已删除 %d 个待重算的 enriched 分区 (≥ %s)", pruned, repair_start)
        except Exception as e:  # noqa: BLE001
            logger.warning("enriched prune failed (soft): %s", e)


    # 单标的新鲜度: 全局 max(date) 会被任一有今日数据的标的"拉高", 掩盖停牌/复牌/
    # 一直拉失败而掉队的个股缺口(全局判据只刷"今天", 永不回补掉队标的的历史缺口)。
    # 这里检测并**可见化**(WARNING + 计入结果), 让掉队标的不再隐形。
    # (自动回补暂不做 —— 需带退市判定, 否则对已退市标的每轮空拉浪费 API 额度。)
    lagging_symbols: list[str] = []
    if pull_a_share and latest_daily:
        try:
            lagging_symbols = repo.symbols_lagging(today, min_gap_days=3)
            if lagging_symbols:
                logger.warning("日K新鲜度: %d 只标的落后 >3 日 (停牌/退市/拉取失败; 样例: %s)",
                               len(lagging_symbols), lagging_symbols[:10])
        except Exception as e:  # noqa: BLE001
            logger.warning("laggard detection failed: %s", e)
            stage_errors.append(f"laggard detection: {e}")

    # Step 1.5: 同步除权因子 — 范围与日K拉取方式对齐
    #   日K范围拉取(补缺口/首次) → 除权用日K范围 [daily_range_start, now]
    #     首次会覆盖整个日K区间内的历史除权事件; 补缺口天然只增量(起点=latest_daily≈昨天)
    #   日K实时增量/跳过(分支2/分支1) → 除权兜底拉最近 30 天, 补可能遗漏的新除权
    #     (这两类分支不拉历史日K, 除权不能用日K范围, 只能兜底最近几日)
    written_adj = 0
    affected_symbols: list[str] = []
    adj_provider = _prefs.get_adj_factor_provider()
    can_sync_adj = capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"
    if can_sync_adj:
        from datetime import datetime, timedelta
        adj_end = datetime.now()
        if daily_range_start is not None:
            adj_start = datetime.combine(daily_range_start, datetime.min.time())
        else:
            # 日K实时增量/跳过时, 除权兜底拉最近 N 天, 覆盖周末/长假/停机期间的新除权事件。
            # 15 天: 覆盖春节/国庆最长约10天长假 + 故障恢复缓冲; sync_adj_factor 内部 merge+unique 幂等, 多拉无副作用。
            adj_start = adj_end - timedelta(days=15)
        adj_start_str = adj_start.strftime("%Y-%m-%d")
        adj_end_str = adj_end.strftime("%Y-%m-%d")
        emit("sync_adj", 50, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("sync_adj: [%s ~ %s] start", adj_start_str, adj_end_str)

        def _adj_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_adj", 50 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_adj, affected_symbols = kline_sync.sync_adj_factor(
            universe, repo, capset,
            start_time=adj_start, end_time=adj_end,
            on_chunk_done=_adj_chunk_progress,
            # 这里是全市场清单: 声明 universe 才允许回执为「未失败的标的」提供覆盖。
            scope="universe",
        )
        if affected_symbols:
            # 视图刷新 = 重建 DuckDB view + 元数据扫描, 9p 上可达数十秒;
            # 放在 emit 之后会让「除权完成」与下一步之间留出静默段。
            emit("sync_adj", 60, "刷新除权视图…")
            _refresh_single_view(repo, "adj_factor")
            emit("sync_adj", 60, f"除权因子完成,新增 {len(affected_symbols)} 只个股")
            logger.info("sync_adj: [%s ~ %s] done, %d symbols", adj_start_str, adj_end_str, len(affected_symbols))
        else:
            emit("sync_adj", 60, "除权因子完成,无新增")
            logger.info("sync_adj: [%s ~ %s] no new factors", adj_start_str, adj_end_str)
        _invalidate("adj_factor")
    else:
        skipped.append("sync_adj")
        logger.info("sync_adj skipped: no ADJ_FACTOR capability")

    # Step 2: 计算 enriched
    #   判断策略:
    #     - 首次 (enriched 目录不存在) → 全量
    #     - 往前扩展历史 (新日期 < enriched 已有最早日期) → 全量
    #       前面的除权因子会改变累积因子链,影响后面所有日期的复权价格
    #     - 往后新增日期或已有历史区间内的缺口
    #       → 增量补新区块(所有标的) + 受除权影响个股全日期重算
    #     - 无新日期 + 有新除权因子 → 增量: 只重算受影响个股的全部日期
    #     - 无新日期 + 无变化 → 跳过
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
    daily_dir = repo.store.data_dir / "kline_daily"
    # 月分区后目录数是月数而非天数; 这里与 enriched 目录数比较判方向,
    # 必须用真实日期数 (月分区读 date 列), 否则 121 < 367 会把增量检测关死。
    # 读一次复用两处: 每个分区一次读盘, 重复调用在 9p 上是 18s 级开销。
    # 心跳: 这段是纯读盘(逐月分区扫 date 列), 盘慢时是下一个静默段。
    emit("compute_enriched", 61, "统计日K覆盖…")
    daily_date_list = list_partition_dates(daily_dir) if daily_dir.exists() else []
    daily_days = len(daily_date_list)
    emit("compute_enriched", 61, f"日K覆盖 {daily_days} 天, 统计 enriched 分区…")
    prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 部分分区修复 (#223) + 收盘价过期分区修复: 删除被实时合并提前创建、覆盖不全
    # 或收盘价停留在竞价前快照的 enriched 分区, 让下方计数比较与增量计算把它们
    # 重新当新日期处理 (值级比对以官方日线为准, 实时源不纠错也能自愈)
    if enriched_exists:
        # 进度心跳: 校验与比对要读盘, 慢盘上数分钟无输出会被 reap_stale 判成卡死。
        emit("compute_enriched", 62,
             f"校验最近 {_ENRICHED_VERIFY_WINDOW} 个 enriched 分区…")

        def _verify_progress(day_iso: str) -> None:
            emit("compute_enriched", 63, f"校验 enriched 分区 {day_iso}", skip_log=True)

        partial_pruned = _prune_partial_enriched_partitions(
            daily_dir, enriched_dir, on_check=_verify_progress,
        )
        stale_pruned = _prune_stale_price_partitions(
            daily_dir, enriched_dir, on_check=_verify_progress,
        )
        pruned_dates = sorted(set(partial_pruned) | set(stale_pruned))
        if pruned_dates:
            logger.warning(
                "compute_enriched: 发现 %d 个异常 enriched 分区 (覆盖不全 %d / 收盘价过期 %d), "
                "已删除待重算: %s",
                len(pruned_dates), len(partial_pruned), len(stale_pruned),
                ", ".join(pruned_dates[:10]),
            )
            enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
            prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 判断新日期方向: 找 daily 和 enriched 的日期集合做比较
    forward_incremental = False
    backward_extension = False

    if daily_days > prev_enriched_days and enriched_exists:
        # kline_daily 月分区的真实日期从数据 date 列读; enriched 保持日分区,
        # 目录名即日期。
        daily_dates = sorted(daily_date_list)
        enriched_dates = sorted(
            date.fromisoformat(d.stem.split("=")[1]) for d in enriched_dir.glob("date=*")
        )
        earliest_enriched = enriched_dates[0]
        new_dates = set(daily_dates) - set(enriched_dates)
        if new_dates:
            # 有新日期早于 enriched 最早日期 → 往前扩展
            if any(d < earliest_enriched for d in new_dates):
                backward_extension = True
            # 包含中间被删的异常分区; 没有新增末日也必须补算。
            # 往前扩展仍由下方优先走全量分支。
            forward_incremental = True

    def _enriched_batch_progress(cur: int, tot: int) -> None:
        emit("compute_enriched", 65 + int(23 * cur / tot),
             f"计算指标 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    if not enriched_exists or backward_extension:
        # 首次 或 往前扩展 → 全量
        emit("compute_enriched", 65, "全量计算 enriched…")
        logger.info("compute_enriched: full rebuild (first=%s, backward=%s, daily=%d, enriched=%d)",
                    not enriched_exists, backward_extension, daily_days, prev_enriched_days)
        written_enriched = run_pipeline(on_batch_done=_enriched_batch_progress)
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: full rebuild done, %d days", new_enriched_days)
    elif forward_incremental:
        # 往后新增日期: 增量补新区块 + 受影响个股全日期重算
        symbols_to_recompute = list(set(affected_symbols)) if affected_symbols else []
        emit("compute_enriched", 65,
             f"增量计算 enriched (新日期 + {len(symbols_to_recompute)} 只个股重算)…"
             if symbols_to_recompute else "增量计算 enriched (新日期)…")
        logger.info("compute_enriched: forward incremental, %d symbols to recompute",
                    len(symbols_to_recompute))
        written_enriched = run_pipeline(
            new_dates_only=True,
            symbols=symbols_to_recompute or None,
            on_batch_done=_enriched_batch_progress,
        )
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: forward incremental done, %d days", new_enriched_days)
    elif affected_symbols:
        # 无新日期,仅除权因子变更 → 只重算受影响个股的全部日期
        emit("compute_enriched", 65, f"增量计算 enriched ({len(affected_symbols)} 只个股)…")
        logger.info("compute_enriched: adj_factor incremental, %d symbols", len(affected_symbols))
        written_enriched = run_pipeline(symbols=affected_symbols, on_batch_done=_enriched_batch_progress)
        emit("compute_enriched", 88, f"enriched 完成,{len(affected_symbols)} 只个股")
    else:
        written_enriched = 0
        logger.info("compute_enriched: skip (no new daily, no adj_factor changes)")
    emit("compute_enriched", 88, "刷新指标视图…")
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # Step 2.1: 数据充足性可见化 (#303) — 空库首跑/仅当日实时覆写 1 天的库,
    # 均线/动量/量比等指标暖机不足, 管道各 stage 都"成功"但策略会静默全 0。
    enriched_total_days = warn_if_enriched_too_thin(repo.store.data_dir)

    # Step 2.3: 指数 / ETF 同步 — 物理分开存储；ETF 可复权，指数不复权。
    written_index_daily = 0
    written_etf_daily = 0
    index_count = 0
    etf_count = 0
    etf_adj_symbols = 0
    pull_index = _prefs.get_pipeline_pull_index()
    pull_etf = _prefs.get_pipeline_pull_etf()

    from app.data_providers import custom as custom_sources
    daily_provider = _prefs.get_daily_data_provider()
    daily_allowed = capset.has(Cap.KLINE_DAILY_BATCH) or (
        daily_provider != "tickflow" and custom_sources.provider_has_dataset(daily_provider, "daily")
    )
    if daily_allowed and (pull_index or pull_etf):
        _types = []
        if pull_index:
            _types.append("指数")
        if pull_etf:
            _types.append("ETF")
        emit("sync_index", 88, f"同步{'+'.join(_types)}日K…")
        # 子阶段进度分配: 88.0(开始) → 89.0(完成), 指数占前半, ETF 占后半
        try:
            if pull_index:
                emit("sync_index", 88, "同步指数维表…")
                index_count = index_sync.sync_index_instruments(
                    repo, pull_index=True, pull_etf=False,
                    # 免费接口超时是这一段「不动」的主因: 逐交易所回报, 至少能看见
                    # 它是在重试而不是死了。
                    on_progress=lambda msg: emit("sync_index", 88, msg, skip_log=True),
                )
                emit("sync_index", 88, f"指数维表完成,{index_count} 只")
                index_dir = repo.store.data_dir / "kline_index_enriched"
                index_dates = sorted(
                    d.name[5:] for d in index_dir.glob("date=*")
                    if d.is_dir() and d.name.startswith("date=")
                ) if index_dir.exists() else []
                # 数据修正模式下用传入起点; 否则用本地指数最新日期补到今天;
                # 完整性修复时起点再提前到最早坏日 (实时写过今天的指数分区时,
                # "最新日期=今天"会让停机日的快照/缺口永久留存)
                if override_start_date:
                    index_start = override_start_date
                else:
                    index_start = _date.fromisoformat(index_dates[-1]) if index_dates else today - _td(days=365)
                if index_stale_day is not None and index_start > index_stale_day:
                    index_start = index_stale_day

                def _index_chunk(cur: int, tot: int) -> None:
                    emit("sync_index", 88, f"指数日K批次 {cur}/{tot}",
                         stage_pct=int(100 * cur / tot) if tot else 100, skip_log=cur < tot)

                written_index_daily = index_sync.sync_and_persist_index_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(index_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_index_chunk,
                )
                emit("sync_index", 88, f"指数日K完成,{written_index_daily} 行")
                _invalidate("index_instruments")
                _invalidate("index_daily")
                _invalidate("index_enriched")

            if pull_etf:
                emit("sync_index", 88, "同步 ETF 维表…")
                etf_count = index_sync.sync_etf_instruments(repo)
                emit("sync_index", 88, f"ETF 维表完成,{etf_count} 只")
                etf_symbols: list[str] = []
                etf_inst = repo.get_etf_instruments()
                if not etf_inst.is_empty() and "symbol" in etf_inst.columns:
                    etf_symbols = sorted(set(etf_inst["symbol"].to_list()))
                if etf_symbols and capset.has(Cap.ADJ_FACTOR):
                    try:
                        emit("sync_index", 88, "同步 ETF 除权因子…")
                        from datetime import datetime, timedelta
                        adj_end = datetime.now()
                        adj_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
                        # 首次无文件时与 ETF 日K 默认一年窗口对齐, 不要只拉 30 天。
                        history_start = adj_end - timedelta(days=365)
                        adj_start = index_sync.etf_adj_factor_window_start(adj_path, history_start)
                        _, affected_etfs = index_sync.sync_etf_adj_factor(
                            etf_symbols,
                            repo,
                            capset,
                            start_time=adj_start,
                            end_time=adj_end,
                            # 清单来自 ETF 工具表, 是全量: 透传一层不能替调用方改口。
                            scope="universe",
                        )
                        etf_adj_symbols = len(affected_etfs)
                        emit("sync_index", 88, f"ETF 除权因子完成,{etf_adj_symbols} 只")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("ETF adj_factor skipped: %s", e)
                        stage_errors.append(f"ETF adj_factor: {e}")
                etf_dir = repo.store.data_dir / "kline_etf_enriched"
                etf_dates = sorted(
                    d.name[5:] for d in etf_dir.glob("date=*")
                    if d.is_dir() and d.name.startswith("date=")
                ) if etf_dir.exists() else []
                if override_start_date is not None:
                    etf_start = override_start_date
                else:
                    etf_start = _date.fromisoformat(etf_dates[-1]) if etf_dates else today - _td(days=365)
                # 同指数: 完整性修复时把 ETF 起点提前到最早坏日
                if etf_stale_day is not None and etf_start > etf_stale_day:
                    etf_start = etf_stale_day

                def _etf_chunk(cur: int, tot: int) -> None:
                    emit("sync_index", 88, f"ETF 日K批次 {cur}/{tot}",
                         stage_pct=int(100 * cur / tot) if tot else 100, skip_log=cur < tot)

                written_etf_daily = index_sync.sync_and_persist_etf_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(etf_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_etf_chunk,
                )
                emit("sync_index", 88, f"ETF 日K完成,{written_etf_daily} 行")
                _invalidate("etf_instruments")
                _invalidate("etf_daily")

            repo.refresh_index_views()
            emit(
                "sync_index",
                89,
                f"同步完成,指数 {index_count} 只/{written_index_daily} 行, ETF {etf_count} 只/{written_etf_daily} 行"
                + (f", ETF复权 {etf_adj_symbols} 只" if etf_adj_symbols else ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sync_index/etf failed: %s", e)
            emit("sync_index", 89, f"指数/ETF同步失败:{e}")
            stage_errors.append(f"index/etf sync: {e}")
    else:
        skipped.append("sync_index")

    # Step 2.5: 分钟 K 同步(可选) — 未启用或无 capability 时静默跳过(不 emit)
    from app.services import preferences
    minute_on = preferences.get_minute_sync_enabled()
    minute_days = preferences.get_minute_sync_days()
    written_minute = 0
    # 门控口径与 api/kline._minute_allowed 一致: TickFlow 需 KLINE_MINUTE_BATCH
    # 能力; 自定义源声明 minute 数据集即放行 (resolver 异常按降级 TickFlow 处理)。
    _, minute_fallback, _minute_resolve_err = kline_sync._resolve_minute_provider(
        preferences.get_minute_data_provider()
    )
    minute_allowed = (not minute_fallback) or capset.has(Cap.KLINE_MINUTE_BATCH)
    if minute_on and minute_allowed:
        minute_start = today - _td(days=minute_days)
        emit("sync_minute", 90, f"获取分钟K [{minute_start} ~ {today}]…")
        logger.info("sync_minute: [%s ~ %s] start", minute_start, today)
        minute_symbols = _resolve_minute_symbols(capset, repo)
        def _minute_chunk_progress(cur: int, tot: int, seg_label: str = "") -> None:
            emit("sync_minute", 90 + int(3 * cur / tot),
                 f"分钟K 批次 {cur}/{tot}" + (f" [{seg_label}]" if seg_label else ""),
                 stage_pct=int(100 * cur / tot), skip_log=True)
        try:
            written_minute = kline_sync.sync_and_persist_minute(
                minute_symbols, repo, capset, days=minute_days,
                on_chunk_done=_minute_chunk_progress,
            )
        except Exception as e:  # noqa: BLE001
            # 越界副本（13:00 幽灵）已改为**逐行隔离**（kline_sync._quarantine_off_grid_duplicates），
            # 不再抛错：一条幽灵行不该让 20 交易日 × 100 只整帧不落盘。这里的兜底留给
            # 真正的失败（通道异常、隔离记录写不成）。一天失败不该连带丢掉
            # regime/主线/结算/台账/复盘 —— 但**终态仍须 failed**: 那一天的分钟数据确实
            # 没更新，静默继续就是「绿得比昨天更快」。
            logger.error("sync_minute failed: %s", e)
            emit("sync_minute", 93, f"分钟K同步失败:{e}")
            stage_errors.append(f"sync_minute: {e}")
        minute_dir = repo.store.data_dir / "kline_minute"
        minute_cover_days = len(list(minute_dir.glob("date=*"))) if minute_dir.exists() else 0
        emit("sync_minute", 93, f"分钟K完成,覆盖 {minute_cover_days} 天")
        logger.info("sync_minute: [%s ~ %s] done, %d days", minute_start, today, minute_cover_days)
        _invalidate("minute")
    else:
        skipped.append("sync_minute")
        if minute_on:
            logger.info("sync_minute skipped: minute provider has no capability/dataset")
        else:
            logger.info("sync_minute skipped: user disabled")

    # Step 2.5b: 分钟K盘后验收(只读, 软失败) — 落盘闸门只拦「会双计」的那一支,
    # L/B/S/M 四条轴它一条都不管(见 services/minute_audit 的分工说明)。判据原先只在
    # analysis/ 里, 而 analysis/ 不随后端镜像发布 ⇒ 容器内既无验收也无修法; 现在与
    # CLI 共用同一份实现, 每天把结论落在 data/_audit/minute/ 并进 job result。
    #
    # 只审计**当天**: 每天各审计一次, `date=*.json` 自然累积成时间序列。改成回看整个
    # 同步窗口(默认 5 天)会让同一条历史异常连报 5 天, 报警一吵就没人看了; 回看用 CLI。
    # 进度停在 93: 紧接其后的 regime/mainline 从 90 起步, 报 94 会让进度条倒退。
    minute_audit_summary: dict | None = None
    try:
        from app.services import minute_audit as _minute_audit

        audit_day_str = today.isoformat()
        if (repo.store.data_dir / "kline_minute" / f"date={audit_day_str}").is_dir():
            _rep = _minute_audit.audit_days(repo.store.data_dir, [audit_day_str])
            _tot = _minute_audit.summarize(_rep)
            _rc = _minute_audit.verdict(_tot)
            _lines = _minute_audit.format_findings(_rep)
            minute_audit_summary = {"day": audit_day_str, "totals": _tot, "verdict": _rc}
            if _rc:
                logger.warning("minute_audit %s: %s", audit_day_str, "; ".join(_lines))
                if _tot.get("phantom"):
                    # 只报计数等于把可修的东西变成一句「有 N 处」——把命令写进同一条日志,
                    # 值班的人不用再去翻 analysis/ (那目录根本不在镜像里)。
                    logger.warning(
                        "minute_audit: %d 行 13:00 幽灵(11:30 的副本), 修法: "
                        "python -m app.services.minute_audit --repair-phantom --apply",
                        _tot["phantom"],
                    )
            else:
                logger.info("minute_audit %s: %s", audit_day_str, "; ".join(_lines))
            if _tot.get("quarantined"):
                # 隔离之后盘上的数据是对的 ⇒ 退出码不必红；但「源还在产脏」必须每天
                # 有一条声音，否则隔离就成了静默丢弃（绿但无感知比红更贵）。
                logger.warning(
                    "minute_audit: %s 有 %d 行越界副本被隔离(未落盘), 说明源仍在产脏; "
                    "证据在 data/_quarantine/kline_minute/date=%s/, 修法见 "
                    "docs/features.md → 全量分钟",
                    audit_day_str, _tot["quarantined"], audit_day_str,
                )
            try:
                from app.services.fs_utils import atomic_write_text

                _out = (repo.store.data_dir / "_audit" / "minute"
                        / f"date={audit_day_str}.json")
                _out.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(_out, json.dumps(
                    {"days": _rep, "totals": _tot, "verdict": _rc},
                    ensure_ascii=False, indent=2, default=str))
            except Exception as e:  # noqa: BLE001 — 产物写不成不影响判定
                logger.warning("minute_audit artifact failed (soft): %s", e)
            emit("minute_audit", 93, f"分钟K验收:{'通过' if not _rc else '发现异常'}")
        else:
            skipped.append("minute_audit")
            logger.info("minute_audit skipped: %s 无分钟分区", audit_day_str)
    except Exception as e:  # noqa: BLE001 — 观测不许把观测变成故障源
        logger.warning("minute_audit failed (soft): %s", e)
        minute_audit_summary = {"error": f"{type(e).__name__}: {e}"}

    # Step 2.6: 市场环境(regime) 增量计算 — enriched 已就绪后聚合环境指标。
    # 双检测(缺口+stale), 自动补算遗漏/被覆写的日。软失败: 不阻断主管道。
    # 默认关闭: regime 是本地聚合计算(非拉取), 首次/regime 表为空时需全量回填
    # 多日, 内存与耗时较高。用户可在数据页「市场环境」卡片设置里开启自动计算,
    # 或直接在该页面点「重算」手动触发(不受此开关影响)。
    regime_days = 0
    from app.services import preferences as _prefs_regime
    if not _prefs_regime.get_pipeline_regime_enabled():
        skipped.append("regime")
        logger.info("compute_regime skipped: user disabled (pipeline_regime_enabled=False)")
    else:
        try:
            emit("compute_regime", 90, "计算市场环境…")
            from app.api.regime import invalidate_regime_cache
            from app.services import regime_builder
            new_regime = regime_builder.compute_regime_incremental(repo, repo.store.data_dir)
            regime_days = new_regime.height if not new_regime.is_empty() else 0
            if regime_days:
                invalidate_regime_cache()
                logger.info("compute_regime: %d days", regime_days)
            emit("compute_regime", 92, f"市场环境 {regime_days} 天")
            # 阶段切换推送监控通知 (软失败, 不影响管道): 末两日阶段不同 = 今日发生切换。
            # 切入退潮/冰点为风险信号, 用 warn 级别; 其余 info。
            if regime_days:
                try:
                    _push_phase_change_alert(repo.store.data_dir)
                except Exception as e:
                    logger.warning("phase change alert failed (soft): %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("compute_regime failed (soft): %s", e)
            stage_errors.append(f"compute_regime: {e}")
            skipped.append("regime")

    # Step 2.7: 市场主线(概念/行业涨停梯队聚合) 增量计算 — regime 同开关。
    # 只窄扫连板 >=1 的行, 增量通常 1 天, 开销可忽略。软失败: 不阻断主管道。
    mainline_rows = 0
    if not _prefs_regime.get_pipeline_regime_enabled():
        skipped.append("mainline")
    else:
        try:
            emit("compute_mainline", 93, "计算市场主线…")
            from app.services import market_mainline
            for _kind in ("concept", "industry"):
                rows = market_mainline.compute_mainline_incremental(
                    repo, repo.store.data_dir, kind=_kind
                )
                mainline_rows += rows.height if not rows.is_empty() else 0
            if mainline_rows:
                logger.info("compute_mainline: %d rows", mainline_rows)
            emit("compute_mainline", 94, f"市场主线 {mainline_rows} 行")
        except Exception as e:
            logger.warning("compute_mainline failed (soft): %s", e)
            stage_errors.append(f"compute_mainline: {e}")
            skipped.append("mainline")

    # Step 2.8: 模拟盘结算: 顺延单按当日开盘/收盘撮合 → 除权调整 → 定版净值。
    # 幂等: 重跑同日不会重复成交/二次除权 (订单状态与 corp_action 台账守卫)。
    # 必须在日K/除权同步之后, 才能读到当日 raw OHLC 与因子。核心账务不受
    # 「市场环境」等可选开关控制, 恒运行 (开销可忽略); 软失败不阻断主管道。
    paper_summary: dict = {}
    try:
        emit("paper_settle", 94, "模拟盘结算…")
        from app.strategy import paper as paper_trading
        # 逐账户结算 (账户间订单/台账隔离), 汇总合并供日志与结果展示
        totals = {"filled": 0, "expired": 0, "corp_actions": 0, "nav": None, "accounts": []}
        for acc_id in paper_trading.list_account_ids(repo.store.data_dir):
            s = paper_trading.settle_day(repo.store.data_dir, today.isoformat(), account_id=acc_id)
            totals["filled"] += s.get("filled", 0)
            totals["expired"] += s.get("expired", 0)
            totals["corp_actions"] += s.get("corp_actions", 0)
            if s.get("nav") is not None:
                totals["nav"] = s["nav"]
            totals["accounts"].append({"account": acc_id, **{k: s.get(k) for k in ("filled", "expired", "corp_actions")}})
        paper_summary = totals
        if paper_summary.get("filled") or paper_summary.get("corp_actions"):
            logger.info("paper_settle: %s", paper_summary)
        # 结算成交留痕 (V3): next_open/close 单的成交发生在盘后管道内, 盘中钩子
        # 覆盖不到; 管道无 SSE 广播器, 这里补 alert_store 留痕进监控中心即可见。
        if totals["filled"]:
            try:
                from app.services import alert_store
                settle_events = []
                for acc_id in paper_trading.list_account_ids(repo.store.data_dir):
                    settle_events.extend(paper_trading.day_fill_events(
                        repo.store.data_dir, today.isoformat(), account_id=acc_id))
                if settle_events:
                    alert_store.append_many(repo.store.data_dir, settle_events)
            except Exception as e:
                logger.warning("模拟盘结算成交留痕失败: %s", e)
        emit("paper_settle", 94, "模拟盘结算完成")
    except Exception as e:
        logger.warning("paper_settle failed (soft): %s", e)
        stage_errors.append(f"paper_settle: {e}")

    # Step 2.9: 决策账本 — 结算到期卡 + 按配置自动发卡 (docs/decision-ledger.md §8)。
    # 默认关闭 (decision_auto_enabled=False); 开启后这是「策略 → 卡」生产者,
    # 让账本每天积累前向校准证据。软失败不阻断主管道; 残缺账本 (fail-closed)
    # 会在这里上抛并计入 stage_errors, 与其他软失败同口径。
    decision_summary: dict = {}
    try:
        emit("decision_ledger", 94, "决策账本结算/发卡…")
        from app.decision.producer import run_decision_stage
        app_state = _get_app_state()
        decision_summary = run_decision_stage(
            repo,
            getattr(app_state, "strategy_engine", None) if app_state else None,
            today=today,
        )
        if decision_summary.get("status") != "disabled":
            emit("decision_ledger", 94, f"决策账本完成 ({decision_summary.get('status')})")
            logger.info("decision_ledger: %s", decision_summary)
    except Exception as e:
        logger.warning("decision_ledger failed (soft): %s", e)
        stage_errors.append(f"decision_ledger: {e}")

    # Step 3: 刷新视图
    emit("refresh_views", 95, "刷新 DuckDB 视图…")
    _refresh_views(repo)

    emit("done", 100, "完成")
    _invalidate(None)  # 兜底:全清

    result = {
        "universe_size": len(universe),
        "daily_days": new_daily_days,
        "adj_factor_symbols": len(affected_symbols),
        "enriched_days": written_enriched,
        "index_count": index_count,
        "index_daily_rows": written_index_daily,
        "etf_count": etf_count,
        "etf_daily_rows": written_etf_daily,
        "etf_adj_factor_symbols": etf_adj_symbols,
        "minute_rows": written_minute,
        "minute_audit": minute_audit_summary,
        "regime_days": regime_days,
        "mainline_rows": mainline_rows,
        "paper_settle": paper_summary,
        "decision_ledger": decision_summary,
        "lagging_symbols": len(lagging_symbols),
        "enriched_total_days": enriched_total_days,
        "integrity_repair_from": repair_start.isoformat() if repair_start else None,
        "integrity_issues": len(integrity_issues),
        "skipped_stages": skipped,
        "stage_errors": stage_errors,
    }

    # 修复路径后置校验 (issue #514): 修完必须重扫一遍, 仍有坏分区时如实报失败 —
    # 不再出现「修复自报成功 (integrity_issues 恒 0) 但坏行仍在」的假闭环。
    # 夜间常规管道 (无 override) 不走此逻辑 (它自己开头扫过)。
    if override_start_date is not None:
        try:
            from app.services import data_integrity as _di

            remaining_issues = _di.scan_recent_integrity(repo.store.data_dir, today=today)
            checked_tables = set(_di.active_daily_tables())
            remaining_issues = [i for i in remaining_issues if i.table in checked_tables]
            if remaining_issues:
                stage_errors.append(
                    f"数据修复后完整性仍不通过: {_di.describe_issues(remaining_issues)}"
                    f" (剩余 {len(remaining_issues)} 个不完整分区;"
                    " 请查看同步日志, 或到 数据→修正数据 手动执行)"
                )
                logger.warning(
                    "integrity: post-repair scan found %d remaining issues: %s",
                    len(remaining_issues), _di.describe_issues(remaining_issues),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("integrity: post-repair scan failed (soft): %s", e)

    # 有阶段软失败: 进度协议已走完(done/100, 前端进度条正常收尾), 但数据可能陈旧,
    # 抛出让上层 job_store 把终态标记为 failed —— 不再"部分失败却报成功"。
    if stage_errors:
        raise PipelineStageError(stage_errors)

    return result


def run_adj_factor_sync(
    repo: KlineRepository,
    capset: CapabilitySet,
    on_progress: ProgressCb | None = None,
) -> dict:
    """独立同步除权因子(数据页单独按钮)。

    与盘后管道 Step 1.5 的差异: 管道只拉日K范围或 15 天兜底窗口;
    这里 start_time=None 拉**全历史** —— 用于切换数据源后的因子修复、
    以及事件明细列扩表后的存量回填 (明细回填不改变 ex_factor,
    sync_adj_factor 的变化判定不会因此触发 enriched 重算)。

    下游联动与管道同语义: adj_factor 视图刷新 → 受影响个股 enriched
    局部重算(全部日期) → kline_enriched 视图刷新 → 两表缓存失效。
    enriched 尚未构建时跳过重算 (全量计算是盘后管道的职责)。
    """
    from datetime import datetime

    emit = on_progress or _noop
    adj_provider = _prefs.get_adj_factor_provider()
    if not (capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"):
        return {
            "adj_factor_symbols": 0,
            "adj_written": 0,
            "enriched_days": 0,
            "skipped_stages": ["sync_adj"],
            "reason": "无可用除权因子数据源",
        }

    emit("resolve_universe", 5, "解析标的池…")
    universe = _resolve_universe(capset, repo)
    emit("resolve_universe", 10, f"标的池规模:{len(universe)} 只")

    def _adj_chunk_progress(cur: int, tot: int) -> None:
        emit("sync_adj", 10 + int(55 * cur / tot),
             f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    # 按需窗口: 钳到本地日K起点。前复权里早于任一根已存K线的事件在
    # cum/total 两侧同时出现、恰好抵消, 对所有已存价格零影响 (见
    # indicators.pipeline._apply_adj_factor) — 拉它们只会为远古事件
    # 逐只回补历史K线, 全市场一次曾多花 ~15 分钟。
    store_min = repo.earliest_daily_date()
    start = datetime(store_min.year, store_min.month, store_min.day) if store_min else None
    window_label = f"自 {store_min.isoformat()}" if store_min else "全历史"
    logger.info(
        "standalone adj sync: %d symbols, event window %s",
        len(universe), window_label,
    )
    emit("sync_adj", 10, f"获取除权因子 [{window_label}, {len(universe)} 只]…")
    written_adj, affected = kline_sync.sync_adj_factor(
        universe, repo, capset,
        start_time=start, end_time=datetime.now(),
        on_chunk_done=_adj_chunk_progress,
        scope="universe",
    )
    affected = sorted(set(affected))
    _refresh_single_view(repo, "adj_factor")
    emit("sync_adj", 65,
         f"除权因子完成,新增 {written_adj} 行,因子变更 {len(affected)} 只")
    _invalidate("adj_factor")

    # enriched 局部重算: 与管道「无新日期 + 有新除权因子」分支同语义 (只重算受影响个股)
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
    written_enriched = 0
    if affected and enriched_exists:
        def _enriched_batch_progress(cur: int, tot: int) -> None:
            emit("compute_enriched", 65 + int(30 * cur / tot),
                 f"计算指标 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

        emit("compute_enriched", 65, f"增量计算 enriched ({len(affected)} 只个股)…")
        logger.info("compute_enriched: adj_factor incremental (standalone), %d symbols", len(affected))
        written_enriched = run_pipeline(symbols=affected, on_batch_done=_enriched_batch_progress)
        _refresh_single_view(repo, "kline_enriched")
        emit("compute_enriched", 95, f"enriched 完成,{len(affected)} 只个股")
        _invalidate("enriched")
    elif affected:
        logger.info("standalone adj sync: enriched 未构建, 跳过局部重算 (交由盘后管道全量计算)")

    emit("done", 100, "完成")
    return {
        "universe_size": len(universe),
        "adj_written": written_adj,
        "adj_factor_symbols": len(affected),
        "enriched_days": written_enriched,
        "skipped_stages": [],
    }


def warn_if_enriched_too_thin(data_dir: Path) -> int:
    """enriched 总覆盖天数; 低于常见指标暖机窗口时 WARN 引导全量回填 (#303)。

    返回天数供管道 result 上报。目录列举 O(天数), 不在热路径。
    """
    from app.services.screener import MIN_INDICATOR_WARMUP_DAYS, enriched_history_days

    days = enriched_history_days(data_dir)
    if days < MIN_INDICATOR_WARMUP_DAYS:
        logger.warning(
            "enriched 仅覆盖 %d 个交易日 (<%d): 指标暖机不足, 策略可能全部 0 命中且无提示 — "
            "建议全量回填 (同步标的维表 → 日K批量同步(带后缀符号) → 重算 enriched)",
            days, MIN_INDICATOR_WARMUP_DAYS,
        )
    return days


def _refresh_views(repo: KlineRepository) -> None:
    """刷新所有 DuckDB 视图 —— 委托给 repository 的唯一权威实现 rebuild_views()。"""
    repo.rebuild_views()


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_index_daily": f"{d}/kline_index_daily/**/*.parquet",
        "kline_index_enriched": f"{d}/kline_index_enriched/**/*.parquet",
        "kline_etf_daily": f"{d}/kline_etf_daily/**/*.parquet",
        "kline_etf_enriched": f"{d}/kline_etf_enriched/**/*.parquet",
        "kline_etf_minute": f"{d}/kline_etf_minute/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
        "instruments_index": f"{d}/instruments_index/**/*.parquet",
        "instruments_etf": f"{d}/instruments_etf/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    repo.refresh_view(name, path)


def _resolve_minute_symbols(capset: CapabilitySet, repo=None) -> list[str]:
    """分钟 K 同步标的 — 与日K共用同一标的池。"""
    return _resolve_universe(capset, repo)


def _refresh_instruments_view(repo: KlineRepository) -> None:
    """单独刷新 instruments 视图。"""
    d = repo.store.data_dir.as_posix()
    repo.refresh_view("instruments", f"{d}/instruments/**/*.parquet")


def _push_phase_change_alert(data_dir) -> None:
    """情绪周期阶段切换 → 推送监控通知(SSE toast + 监控中心)。

    阶段切换(如 退潮→冰点)是重要的市场信号, 原先只有打开市场环境页才能看到。
    复用 quote_service.push_alerts 广播通道; 未发生切换静默返回。
    """
    from app.services.market_phase import PHASE_LABELS
    from app.services.regime_builder import latest_phase_transition

    tr = latest_phase_transition(data_dir)
    if not tr:
        return
    prev, cur, d = tr
    msg = f"情绪周期阶段切换: {PHASE_LABELS.get(prev, prev)} → {PHASE_LABELS.get(cur, cur)} ({d})"
    severity = "warn" if cur in ("ebb", "ice") else "info"
    app_state = _get_app_state()
    qs = getattr(app_state, "quote_service", None) if app_state else None
    if qs:
        qs.push_alerts([{
            "source": "market",
            "type": "phase_change",
            "message": msg,
            "severity": severity,
        }])
    logger.info("phase change alert: %s (severity=%s)", msg, severity)


def _run_tracked(fn, job_label: str, *, kind: str = "pipeline") -> bool:
    """调度触发时包装 JobStore 跟踪，确保同步历史有记录。

    单飞: 若已有活跃(pending∨running)任务(手动同步中), 本次调度直接跳过, 不并发。
    重任务执行槽: 再挡一层僵尸并发(reap 后线程仍活时不得并行写 parquet)。
    返回 True 仅表示任务已成功并且执行槽已释放。
    """
    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )

    job_id, is_new = job_store.create(kind=kind)
    if not is_new:
        logger.info("scheduled %s 跳过: 已有活跃任务在运行 (job_id=%s)", job_label, job_id)
        return False
    if not try_acquire_run_slot(job_id):
        logger.warning("scheduled %s 跳过: 重任务执行槽被占用(疑似上次任务卡死)", job_label)
        job_store.fail(job_id, f"scheduled {job_label} skipped: 已有数据任务在运行")
        return False

    def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                 skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    succeeded = False
    try:
        result = run_with_capacity(job_id, lambda: fn(on_progress=progress))
        job_store.succeed(job_id, result)
        succeeded = True
        logger.info("scheduled %s completed: job_id=%s", job_label, job_id)
    except JobCancelledError:
        # 已由 terminate() 标记失败(卡死/手动取消), 拉取线程在分块回调处自行退出
        logger.warning("scheduled %s cancelled: job_id=%s", job_label, job_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduled %s failed: job_id=%s", job_label, job_id)
        # 带上异常文本：run_all 的「未补齐（ETF 2026-11-23）」这类结论就写在里面，
        # 只写 "scheduled X failed" 会让页面重新变成「只有失败、没有原因」。
        job_store.fail(job_id, f"scheduled {job_label} failed: {e}")
    finally:
        release_run_slot(job_id)
    return succeeded


def _scheduled_pipeline_task(pipeline_fn) -> None:
    """Run the scheduled lab cycle (weekly mining + daily guardian) after pipeline success."""
    if not _run_tracked(pipeline_fn, "daily_pipeline"):
        return
    try:
        from app.services.lab_schedule import run_scheduled_lab

        result = run_scheduled_lab(_get_app_state())
        logger.info("scheduled lab result: %s", result)
    except Exception:
        logger.exception(
            "scheduled lab enqueue failed; daily pipeline remains succeeded"
        )


# ================================================================
# 定时复盘 (AI 大盘复盘报告)
# ================================================================

REVIEW_JOB_ID = "scheduled_review"


async def _run_scheduled_review(repo) -> None:
    """定时复盘 job: 流式生成复盘 → 实时推 SSE(开着页面可见) → 落盘归档 → 推飞书。

    与手动「生成复盘」体验一致: 流式事件经 quote_service.push_review_event →
    /api/intraday/stream 的 review_progress 事件 → 前端 reviewStore, 用户开着复盘页
    即可看到报告边生成边显示, 切走再回来也能看到生成中/已生成。
    LLM 偶发断流(peer closed connection)时自动重试最多 2 次。
    任何异常都吞掉只记日志, 绝不影响调度器主循环。
    """
    import json

    try:
        from app import secrets_store as ss
        from app.services import market_recap_reports

        # AI Key 未配置时跳过(避免每日报错刷日志)
        if not ss.get_ai_key():
            logger.info("scheduled review skipped: AI key not configured")
            return

        app_state = _get_app_state()
        quote_service = getattr(app_state, "quote_service", None) if app_state else None
        depth_service = getattr(app_state, "depth_service", None) if app_state else None

        content, meta = await _stream_review_with_retry(repo, quote_service, depth_service)
        if not content:
            logger.warning("scheduled review produced no content (meta=%s)", meta)
            # 通知前端进入 error 态(若有页面在听)
            if quote_service:
                quote_service.push_review_event(json.dumps(
                    {"type": "error", "message": "复盘生成失败,请稍后手动重试"},
                    ensure_ascii=False))
            return

        # 落盘: 与手动生成完全相同的归档格式
        market_recap_reports.save_report({
            "as_of": meta.get("as_of"),
            "focus": "",
            "content": content,
            "summary": meta.get("summary", ""),
            "emotion_score": meta.get("emotion_score"),
            "emotion_label": meta.get("emotion_label", ""),
        })
        logger.info("scheduled review saved: as_of=%s", meta.get("as_of"))

        # 通知前端: 生成完成且已归档(archived=true 让前端只刷新列表, 不重复归档)
        if quote_service:
            quote_service.push_review_event(json.dumps(
                {"type": "done", "archived": True}, ensure_ascii=False))

        # 推送门控: review_push_mode=manual 时定时复盘只归档不推送,
        # 由用户对当日报告显式确认后才推; auto 时保持既有自动推送行为。
        # 失败静默降级, 不影响已归档的报告。
        if _prefs.get_review_push_mode() == "auto":
            _maybe_push_review(content, meta)
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduled review failed: %s", e)
        # 兜底: 异常时通知前端停止「生成中」状态, 避免页面卡在 streaming
        try:
            app_state = _get_app_state()
            qs = getattr(app_state, "quote_service", None) if app_state else None
            if qs:
                import json as _json
                qs.push_review_event(_json.dumps(
                    {"type": "error", "message": "复盘生成异常,请稍后手动重试"},
                    ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass


async def _stream_review_with_retry(repo, quote_service, depth_service) -> tuple[str, dict]:
    """流式生成复盘, 每个事件推 SSE + 累积内容。LLM 断流时最多重试 2 次。

    返回 (content, meta)。重试时推一个 retry 事件让前端清空已累积内容重新开始。
    成功(收到 done/无 error)或耗尽重试后返回。
    """
    import asyncio
    import json

    from app.services.market_recap import recap_market_stream

    max_attempts = 3  # 初次 + 2 次重试
    last_meta: dict = {}
    content_parts: list[str] = []

    for attempt in range(1, max_attempts + 1):
        content_parts = []  # 每次重试重新累积
        failed = False
        try:
            async for evt_json in recap_market_stream(repo, quote_service, depth_service):
                evt = json.loads(evt_json)
                t = evt.get("type")

                # 推给前端(让开着页面的用户实时看到, 与手动一致)
                if quote_service:
                    quote_service.push_review_event(evt_json)

                if t == "meta":
                    last_meta = evt
                elif t == "delta" and evt.get("content"):
                    content_parts.append(evt["content"])
                elif t == "error":
                    failed = True
                    logger.warning("scheduled review stream error (attempt %d/%d): %s",
                                   attempt, max_attempts, evt.get("message"))
                    break  # 触发重试
                elif t == "done":
                    # 正常完成
                    return "".join(content_parts), last_meta
            # 流自然结束(无 done 事件)且有内容, 视为成功
            if content_parts and not failed:
                return "".join(content_parts), last_meta
        except Exception as e:  # noqa: BLE001
            # LLM 断流等异常(httpx.RemoteProtocolError)落到这里
            failed = True
            logger.warning("scheduled review stream exception (attempt %d/%d): %s",
                           attempt, max_attempts, e)

        # 失败: 决定是否重试
        if attempt < max_attempts:
            logger.info("scheduled review retrying in 3s (attempt %d → %d)", attempt, attempt + 1)
            # 通知前端: 即将重试, 清空已累积内容重新开始
            if quote_service:
                quote_service.push_review_event(json.dumps(
                    {"type": "retry", "attempt": attempt + 1}, ensure_ascii=False))
            await asyncio.sleep(3)

    # 耗尽重试, 返回已累积内容(可能为空)和最后 meta
    return "".join(content_parts), last_meta


def _maybe_push_review(content: str, meta: dict) -> None:
    """复盘报告归档后, 按 review_push_channels 选定的外部工具逐个推送完整报告。

    定时生成与手动生成共用本函数 (手动归档端点 POST /api/market-recap/reports 也会调用)。
    channels 为空则不推送; 复用监控中心的全局外部渠道配置。
    推送失败静默降级 (Webhook 是辅助通道), 不影响已归档的报告。
    """
    try:
        from app import secrets_store
        from app.services import email_adapter, preferences, webhook_adapter

        channels = preferences.get_review_push_channels()
        if not channels:
            return

        emotion = f"{meta.get('emotion_label') or ''}".strip()
        as_of = meta.get("as_of") or ""
        subtitle = as_of + (f" · 情绪 {emotion}" if emotion else "")

        for ch in channels:
            if ch == "feishu":
                url = preferences.get_feishu_webhook_url()
                if not url:
                    logger.info("review push(feishu) skipped: webhook not configured")
                    continue
                secret = preferences.get_feishu_webhook_secret()
                ok = webhook_adapter.send_feishu_card(
                    url, "每日复盘", subtitle, content, secret
                )
                logger.info("review push(feishu) %s", "sent" if ok else "failed")
            elif ch == "wecom":
                url = preferences.get_wecom_webhook_url()
                if not url:
                    logger.info("review push(wecom) skipped: webhook not configured")
                    continue
                # 企业微信 markdown 标题已含一级标题, subtitle 拼到正文首行
                full_body = (f"**{subtitle}**\n\n{content}" if subtitle else content)
                ok = webhook_adapter.send_wecom_markdown(
                    url, "每日复盘", full_body
                )
                logger.info("review push(wecom) %s", "sent" if ok else "failed")
            elif ch == "custom":
                url = preferences.get_custom_webhook_url()
                if not url:
                    logger.info("review push(custom) skipped: webhook not configured")
                    continue
                ok = webhook_adapter.send_custom(
                    url,
                    "每日复盘",
                    content,
                    "market_review",
                    meta,
                    secrets_store.get_custom_webhook_secret(),
                )
                logger.info("review push(custom) %s", "sent" if ok else "failed")
            elif ch == "email":
                config = preferences.get_email_smtp_config()
                if not email_adapter.is_configured(config):
                    logger.info("review push(email) skipped: SMTP not configured")
                    continue
                email_body = (f"{subtitle}\n\n{content}" if subtitle else content)
                ok = email_adapter.send_email(
                    config,
                    secrets_store.get_email_smtp_password(),
                    "每日复盘",
                    email_body,
                )
                logger.info("review push(email) %s", "sent" if ok else "failed")
            # 未来更多渠道在此追加分支
    except Exception as e:  # noqa: BLE001
        logger.warning("review push error: %s", e)


# ================================================================
# 盘后龙虎榜增量 (data/dragon_tiger/date=*.json)
# ================================================================

DRAGON_TIGER_JOB_ID = "dragon_tiger_daily"
DRAGON_TIGER_RETRY_JOB_ID = "dragon_tiger_daily_retry"
# 披露通常在 18:00 之后; 19:10 主跑, 20:40 幂等补一次兜披露延迟
DRAGON_TIGER_SLOTS = ((DRAGON_TIGER_JOB_ID, 19, 10), (DRAGON_TIGER_RETRY_JOB_ID, 20, 40))


# ================================================================
# 短线雷达排序体检 (data/shortline/sort_ledger.jsonl + sort_audit.json)
# ================================================================

SHORTLINE_AUDIT_JOB_ID = "shortline_audit_daily"
SHORTLINE_AUDIT_RETRY_JOB_ID = "shortline_audit_daily_retry"
# enriched 管道默认 15:35，体检只读已落盘的 enriched（不自己拉数）；
# 17:10 主跑 + 20:10 幂等补跑（record_day 同日期跳过，rebuild 每次重算）。
SHORTLINE_AUDIT_SLOTS = ((SHORTLINE_AUDIT_JOB_ID, 17, 10), (SHORTLINE_AUDIT_RETRY_JOB_ID, 20, 10))


def _run_shortline_audit(repo) -> None:
    """登记当日雷达池 + 重算排序体检产出物。失败只记日志，不阻断调度主循环。

    幂等性靠 record_day 的「同日期跳过」；rebuild 每次全量重算（台账只有几百行，
    评估只读 ~3 个分区/登记日，秒级）。 enriched 尚未更新到今天时，record_day
    会按最新可得交易日登记 —— 若该日已在台账则跳过，等补跑时点拿到新数据。
    """
    try:
        from app.services import shortline_audit as svc

        data_dir = repo.store.data_dir
        row = svc.record_day(data_dir)
        art = svc.rebuild(data_dir)
        if row:
            logger.info("shortline_audit %s 登记: 池 %s 只 / 现行 %s / 候选 %s",
                        row["date"], row["budget"].get("cold"),
                        len(row["picks"]), len(row["shadow"]))
        logger.info("shortline_audit 产出物已更新: 登记日 %s / 可评估月 %s",
                    art.get("days_registered"), art.get("months"))

        # 预热雷达载荷：一个交易日内这份字节不变，但冷构建要读 188 个 parquet 分区
        # （容器内约 2.4s + 序列化 0.2s）。数据刚落库就把磁盘缓存写好，第一个打开
        # 页面的人就不必替所有人付这笔钱。必须排在 rebuild 之后：sort_audit.json
        # 是输入指纹的一部分，先预热会让指纹立刻过期。
        try:
            from app.services import citizenship

            started = time.time()
            citizenship.clear_citizenship_cache()
            _, packed = citizenship.get_citizenship_serialized(data_dir)
            logger.info("短线雷达载荷已预热: %.1fMB / %.1fs", len(packed) / 1e6, time.time() - started)
        except Exception:  # noqa: BLE001 — 预热失败只是慢一点，不影响产出物
            logger.warning("短线雷达载荷预热失败（下次请求会自行构建）", exc_info=True)
    except Exception:  # noqa: BLE001 — 定时任务异常不得影响调度器主循环
        logger.exception("shortline_audit scheduled run 异常")


def _register_shortline_audit_jobs(scheduler, repo) -> None:
    for job_id, hour, minute in SHORTLINE_AUDIT_SLOTS:
        scheduler.add_job(
            lambda: _run_shortline_audit(repo),
            trigger=CronTrigger(day_of_week="mon-fri",
                                hour=hour, minute=minute,
                                timezone="Asia/Shanghai"),
            id=job_id,
            misfire_grace_time=3600,
            replace_existing=True,
        )


# ================================================================
# 投资手册自动维护（扫描 → 账户/现金腿 → 回放）
# ================================================================

HANDBOOK_REFRESH_SLOTS = (
    ("handbook_refresh", 16, 20),
    ("handbook_refresh_retry", 18, 20),
    ("handbook_refresh_late", 20, 20),
)


def _handbook_refresh_job(repo, on_progress=None) -> dict:
    from app.services import maintenance

    state = _get_app_state()
    capset = getattr(state, "capabilities", None) if state else None
    return maintenance.run_all(repo, capset, repo.store.data_dir,
                               on_progress or (lambda *a, **k: None))


def _run_handbook_refresh(repo) -> bool:
    """已覆盖最近交易日就什么都不做；否则走 _run_tracked（单飞 + 重任务槽）。

    为什么不等页面：手册链只在有人打开数据页时才补，等于把「自动维护」绑在浏览器上。
    16:20 给盘后管道留 45 分钟；18:20 兜「管道仍在跑」与供应商迟交；20:20 再兜一次
    —— 三次都幂等（`run_all` 先算缺口，全最新就只写一条日志），代价远小于当天永久
    缺一天。这条阶梯就是「维护入口自愈」：任一次失败，下一次自己补，不需要人记住点哪。
    """
    from app.services import maintenance

    # 阶梯要在「没人开页面」时也生效：卡死的上一次任务不会自己消失，不先回收就会
    # 永远被单飞挡在门外（16:20/18:20/20:20 三次全跳过，当天彻底缺一天）。
    # reap 本身幂等：任务在正常跑且进度在推进时不会被动。
    try:
        from app.services.pipeline_jobs import job_store

        job_store.reap_stale()
    except Exception:  # noqa: BLE001 —— 回收失败不该阻断本轮判断
        logger.exception("handbook refresh: 回收卡死任务失败")

    try:
        st = maintenance.status(repo.store.data_dir)
    except Exception:  # noqa: BLE001 — 状态读不到就按需要更新处理，不能在调度器里抛
        logger.exception("handbook refresh: 状态读取失败，按需要更新处理")
        st = {"stale": True, "domains": []}
    if not st.get("stale"):
        logger.info("handbook refresh skipped: 已覆盖最近交易日 (%s)", st.get("expected"))
        return False
    if st.get("domains"):
        logger.info(
            "handbook refresh plan: %s",
            " → ".join(
                f"{d['id']}({'落后' if d['stale'] else 'ok'}: {d['detail']})"
                for d in st["domains"]
            ),
        )
    return _run_tracked(
        lambda on_progress=None: _handbook_refresh_job(repo, on_progress),
        "handbook_refresh",
        kind="handbook",
    )


def _register_handbook_refresh_jobs(scheduler, repo) -> None:
    for job_id, hour, minute in HANDBOOK_REFRESH_SLOTS:
        scheduler.add_job(
            lambda job_id=job_id: _run_handbook_refresh(repo),
            trigger=CronTrigger(day_of_week="mon-fri",
                                hour=hour, minute=minute,
                                timezone="Asia/Shanghai"),
            id=job_id,
            misfire_grace_time=3600,
            replace_existing=True,
        )
    logger.info("handbook refresh scheduled @%s mon-fri",
                "/".join(f"{h:02d}:{m:02d}" for _, h, m in HANDBOOK_REFRESH_SLOTS))


# ================================================================
# 每日名单二次成型 (data/user_data/daily_slate/slate-<as_of>.json)
# ================================================================

SLATE_FINALIZE_JOB_ID = "decision_slate_finalize"
SLATE_FINALIZE_RETRY_JOB_ID = "decision_slate_finalize_retry"
# 管道默认 15:35 跑，此时日线可能还是竞价快照 ⇒ 名单写成 `skipped_not_closed`。
# 那一刻起就没有第二轮了：单份文件一旦「未成立」，当天永久没有名单。
# 16:40 在收盘定稿后补一次，18:10 再兜一次供应商迟交。两个时点都调用同一个
# 幂等阶段（名单 → 结算 → 发卡），15:35 已经成功过就只是空转。
SLATE_FINALIZE_SLOTS = (
    (SLATE_FINALIZE_JOB_ID, 16, 40),
    (SLATE_FINALIZE_RETRY_JOB_ID, 18, 10),
)


def _run_slate_finalize(repo) -> dict:
    """补齐今天的名单（以及随之而来的卡）。

    `today=cn_today()` 而不是 `None`：`None` 会让 `build_slate` 取「最新分区」，
    在节假日/同步失败时补出**上一个交易日**的名单并试图为那天发卡——
    补算历史只能走 `python -m app.decision.slate --backfill`，那是有意的动作，
    不该藏在定时任务里。

    返回 dict 而不是 None：`_run_tracked` 把返回值写进 JobStore 的完成记录，
    任务列表要能看见这次到底产出了什么（哪一天、几只）。
    """
    from app.decision.producer import run_decision_stage

    try:
        state = _get_app_state()
        engine = getattr(state, "strategy_engine", None) if state else None
        out = run_decision_stage(repo, engine, today=cn_today())
        slate_summary = out.get("slate") or {}
        logger.info("slate finalize: stage=%s slate=%s as_of=%s rows=%s",
                    out.get("status"), slate_summary.get("status"),
                    slate_summary.get("as_of"), slate_summary.get("n_rows"))
        return out
    except Exception as exc:  # noqa: BLE001 — 定时任务异常不得影响调度器主循环
        logger.exception("slate finalize 异常")
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def _register_slate_finalize_jobs(scheduler, repo) -> None:
    """注册两个时点 (定稿后补跑 + 迟交兜底), 工作日 mon-fri Asia/Shanghai。

    走 `_run_tracked`：与盘后管道共用重任务槽位。16:40 时管道若仍在跑
    （分钟K同步可能拖到一小时后），这里会被跳过并在任务列表里留一条 failed——
    比并发写账本安全：`settle` 的幂等靠读已结算键，两个进程同时读会双写。
    """
    for job_id, hour, minute in SLATE_FINALIZE_SLOTS:
        scheduler.add_job(
            # `job_id=job_id` 是默认参数绑定，不是多余：闭包晚绑定会让两个任务
            # 都报成最后一个 id，任务列表里从此分不清是谁跑失败了。
            # `on_progress` 必须接住：`_run_tracked` 用关键字传它。
            lambda job_id=job_id, on_progress=None: _run_tracked(
                lambda on_progress=None: _run_slate_finalize(repo), job_id, kind="slate"),
            trigger=CronTrigger(day_of_week="mon-fri",
                                hour=hour, minute=minute,
                                timezone="Asia/Shanghai"),
            id=job_id,
            misfire_grace_time=3600,
            replace_existing=True,
        )


def _is_trading_day() -> bool | None:
    """今日是否交易日 (True/False/None=未知)。单独包一层便于测试注入。"""
    from app.services.trading_day import is_trading_day

    return is_trading_day()


def _run_dragon_tiger_pull(repo) -> None:
    """把当日龙虎榜三榜落进本地缓存。失败只记日志, 不阻断任何主流程。

    必须单独排 job 的两个理由:
    - 在线路径 (get_dragon_tiger) 只在 trade_date < today 时写缓存, 拉「今天」
      永远不落盘 ⇒ 复用在线路径做增量等于每天都不写;
    - fuyao 只允许回溯一年, 漏掉的这一天过了窗口就永久缺失, 补不回来。

    完成判据是 is_expected_day 而非 state —— 服务端未发布目标日时会安静返回
    上一交易日, state 依旧是 ok, 只看 state 会得到「每天绿、每天没写今天」。
    """
    try:
        from app.market_time import cn_today
        from app.services import dragon_tiger as svc

        data_dir = repo.store.data_dir
        today = cn_today()
        if svc.has_cache(data_dir, today):
            logger.info("dragon_tiger %s 已在缓存, 跳过", today)
            return
        if _is_trading_day() is False:
            logger.info("dragon_tiger %s 休市, 跳过", today)
            return
        res = svc.pull_latest(data_dir, today)
        state = res.get("state")
        if state == "ok" and res.get("is_expected_day"):
            logger.info("dragon_tiger %s 入库: %s 只 / %s 席位",
                        today, res.get("stocks"), res.get("seats"))
        elif state == "ok":
            logger.warning(
                "dragon_tiger %s 未发布, 服务端仍返回 %s; 若 20:40 那次也没等到, "
                "手工跑 backfill_dragon_tiger.py --today --force",
                today, res.get("trade_date"))
        elif state == "source_unavailable":
            logger.info("dragon_tiger 跳过: fuyao 未配置")
        else:
            logger.warning("dragon_tiger %s 失败(%s): %s",
                           today, state, res.get("message"))
    except Exception:  # noqa: BLE001 — 定时任务异常不得影响调度器主循环
        logger.exception("dragon_tiger scheduled pull 异常")


def _register_dragon_tiger_jobs(scheduler, repo) -> None:
    """注册两个时点 (主跑 + 幂等补跑), 工作日 mon-fri Asia/Shanghai。"""
    for job_id, hour, minute in DRAGON_TIGER_SLOTS:
        scheduler.add_job(
            lambda: _run_dragon_tiger_pull(repo),
            trigger=CronTrigger(day_of_week="mon-fri",
                                hour=hour, minute=minute,
                                timezone="Asia/Shanghai"),
            id=job_id,
            misfire_grace_time=3600,
            replace_existing=True,
        )


def _register_review_job(scheduler, repo, hour: int, minute: int) -> None:
    """注册/更新定时复盘 job(工作日 mon-fri, Asia/Shanghai)。

    供 start_scheduler(启动时) 和 settings API(改时间时) 共用。
    用 replace_existing=True, 重复注册只更新 trigger。

    注意: _run_scheduled_review 是协程函数, 必须把函数对象本身(配合 args)传给
    add_job, 而非用 lambda 包裹 —— 否则 APScheduler 会把 lambda 当同步函数在线程池
    执行, 仅得到一个未 await 的协程对象, 复盘实际不会运行。
    """
    scheduler.add_job(
        _run_scheduled_review,
        args=[repo],
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=hour, minute=minute,
                            timezone="Asia/Shanghai"),
        id=REVIEW_JOB_ID,
        misfire_grace_time=7200,  # 复盘非关键, 允许 2 小时内补跑
        replace_existing=True,
    )


def start_scheduler(repo: KlineRepository, capset: CapabilitySet) -> AsyncIOScheduler:
    """启动调度器。

    工作日 09:10 — 同步个股维表
    工作日 HH:MM — 盘后管道（时间由用户偏好决定，默认 15:35）
    """
    from app.services import preferences
    sched = preferences.get_pipeline_schedule()
    inst_sched = preferences.get_instruments_schedule()

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # 盘前: 同步 instruments（时间由偏好决定）
    def _instruments_task(on_progress=None):
        emit = on_progress or _noop
        emit("sync_instruments", 0, "同步个股维表…")
        result = run_instruments_sync(repo)
        emit("done", 100, f"个股维表同步完成,{result.get('instruments_rows', 0)} 只标的")
        return result

    scheduler.add_job(
        lambda: _run_tracked(_instruments_task, "instruments_sync", kind="instruments"),
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=inst_sched["hour"], minute=inst_sched["minute"],
                            timezone="Asia/Shanghai"),
        id="pre_market_instruments",
        misfire_grace_time=1800,
        replace_existing=True,
    )

    # 盘后: 日 K + enriched（时间由偏好决定）
    def _pipeline_then_refresh(on_progress=None):
        # 与手动触发 (/api/pipeline/run) 对齐: 管道落盘后重建 Polars 内存缓存,
        # 否则 live_agg 的昨日连板数等基准列会停留在旧交易日, 次日开盘连板梯队
        # 整体少算一档 (仅手动触发或重启才会刷缓存, cron 调度路径此前漏了这步)。
        # 用 app.state 上的**实时** capset(周期重探会热更新它), 而非启动时捕获的
        # 旧 capset —— 否则 Key 中途过期/续费后, 调度管道仍按旧档位打端点。
        app_state = _get_app_state()
        capset_live = getattr(app_state, "capabilities", None) or capset
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = getattr(app_state, "quote_service", None)
        try:
            if qs:
                with qs.paused():
                    result = run_now(repo, capset_live, on_progress=on_progress)
            else:
                result = run_now(repo, capset_live, on_progress=on_progress)
        finally:
            # 即便有阶段软失败(run_now 末尾抛 PipelineStageError), 已落盘的日K/enriched
            # 仍需刷进内存缓存, 否则 live_agg 基准列停留在旧交易日。放 finally 保证部分
            # 成功也生效; 随后异常继续上抛, 由 _run_tracked 标记任务 failed。
            repo.refresh_cache()
        return result

    scheduler.add_job(
        lambda: _scheduled_pipeline_task(_pipeline_then_refresh),
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=sched["hour"], minute=sched["minute"],
                            timezone="Asia/Shanghai"),
        id="daily_pipeline",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 盘后: 五档盘口 sealed 定版(时间由偏好决定, 默认15:02, 范围15:01~18:00)
    depth_sched = preferences.get_depth_finalize_time()

    def _depth_finalize():
        depth_svc = getattr(_get_app_state(), "depth_service", None) if _get_app_state() else None
        if depth_svc:
            depth_svc.finalize()

    scheduler.add_job(
        _depth_finalize,
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=depth_sched["hour"], minute=depth_sched["minute"],
                            timezone="Asia/Shanghai"),
        id="depth_finalize",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 盘后: 龙虎榜当日榜单入库 (19:10 主跑 + 20:40 幂等补跑)
    # 与日K管道解耦: 榜单披露晚于 15:35 管道, 且一年窗口使漏采不可逆。
    _register_dragon_tiger_jobs(scheduler, repo)

    # 盘后: 每日名单二次成型 (16:40 定稿后补跑 + 18:10 兜供应商迟交)
    # 解耦的理由: 名单要的是「收盘定稿」，不是「管道跑完」。15:35 那次若
    # 拿到竞价快照，名单会写成 skipped_not_closed，而单份文件不会自己变新。
    _register_slate_finalize_jobs(scheduler, repo)

    # 盘后: 短线雷达排序体检 (17:10 主跑 + 20:10 幂等补跑)
    # 预注册见 docs/structural-edge/prereg-heat-sort.md：只登记与重算产出物，
    # 不做任何裁决 —— 裁决在登记满 120 个交易日后按文档判据执行。
    _register_shortline_audit_jobs(scheduler, repo)

    # 盘后: 投资手册链 (16:20 主跑 + 18:20 兜底)。已覆盖最近交易日则跳过；
    # 落后时按「行情 → 扫描 → 账户 → 回放」补齐，与数据页按钮同一条执行体。
    _register_handbook_refresh_jobs(scheduler, repo)

    # 周期性能力重探: 付费 Key 中途过期/续费无需重启即可被发现。
    # 只热更新 app.state.capabilities(API 端点、盘后管道 _pipeline_then_refresh 均读它);
    # 档位变化记 WARNING, 让「Key 失效」在日志/前端可见, 不再静默按旧档位打 403 端点。
    def _reprobe_capabilities():
        from app.tickflow.policy import detect_capabilities, tier_label
        app_state = _get_app_state()
        if app_state is None:
            return
        try:
            old = getattr(app_state, "capabilities", None)
            old_n = len(old.all()) if old else -1
            new_capset = detect_capabilities(force=True)
            app_state.capabilities = new_capset
            new_n = len(new_capset.all())
            if old_n != new_n:
                logger.warning(
                    "能力集变化: %d → %d capabilities (档位=%s)。Key 过期/续费或端点波动, "
                    "已热更新 app.state.capabilities。", old_n, new_n, tier_label(),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("周期能力重探失败(保留现有能力集): %s", e)

    scheduler.add_job(
        _reprobe_capabilities,
        trigger=IntervalTrigger(minutes=60),
        id="reprobe_capabilities",
        misfire_grace_time=600,
        replace_existing=True,
    )

    # 定时复盘 (AI 大盘复盘报告): 工作日到点自动生成并归档。
    # 默认关闭 —— 仅当用户在复盘页开启时才注册 job。
    # 复用 recap_market_once(非流式) + market_recap_reports.save_report(落盘)。
    # quote_service / depth_service 通过 _get_app_state() 延迟取用。
    review_sched = preferences.get_review_schedule()
    if review_sched["enabled"]:
        _register_review_job(scheduler, repo, review_sched["hour"], review_sched["minute"])
        logger.info("scheduled_review enabled @%02d:%02d mon-fri",
                    review_sched["hour"], review_sched["minute"])

    scheduler.start()
    # 这一行是「今天到底定了几个闹钟」的唯一摘要。漏掉一个时点，运维就只能靠
    # 翻上面十几条 Added job 才知道它存在——而「没人知道它在跑」和「它没在跑」
    # 在事故里长得一样。
    _slate_slots = "+".join(f"{h:02d}:{m:02d}" for _, h, m in SLATE_FINALIZE_SLOTS)
    logger.info("scheduler started; instruments@%02d:%02d, pipeline@%02d:%02d, "
                "depth@%02d:%02d, slate_finalize@%s mon-fri",
                inst_sched["hour"], inst_sched["minute"], sched["hour"], sched["minute"],
                depth_sched["hour"], depth_sched["minute"], _slate_slots)
    return scheduler


# app_state 延迟引用(start_scheduler 在 lifespan 早期调用, app.state 可能还没就绪)
_app_state_ref = None


def set_app_state(app_state) -> None:
    """lifespan 注册 app.state 引用, 供 scheduled job 访问 depth_service 等单例。"""
    global _app_state_ref
    _app_state_ref = app_state


def _get_app_state():
    return _app_state_ref
