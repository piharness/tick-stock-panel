"""向前扩展历史数据 — 完全独立于 daily_pipeline 的盘后管道。

用户从日 K 卡片手动触发,指定往前补的时长 (x 天/月/年)。
流程:
  1. 获取当前最早日期
  2. 向前拉日 K batch (start = 最早日期 - offset, end = 最早日期)
  3. 向前拉除权因子 (同范围)
  4. 全量重算 enriched
  5. 刷新视图 + 缓存

⚠️ 本模块不导入 daily_pipeline 的任何函数,只复用基础设施:
  - kline_sync.sync_and_persist_daily_batch / sync_adj_factor
  - indicators.pipeline.run_pipeline
  - pipeline_jobs.JobStore
  - tickflow.repository.KlineRepository
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

from app.services import kline_sync
from app.services.pipeline_jobs import job_store
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _invalidate(table: str | None = None) -> None:
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet, repo=None) -> list[str]:
    """解析标的池 — 与 daily_pipeline 同口径: instruments 维表 + 自选 (单一来源)。

    不再拉 CN_Equity_A 池 (含 SW1 聚合兜底); 维表由盘后管道同步维护。
    repo 传入时过滤自选里的指数 symbol (指数日K走独立存储)。
    """
    from pathlib import Path

    import polars as pl

    from app.config import settings
    from app.tickflow.pools import get_pool

    base: set[str] = set(get_pool("watchlist"))
    inst_path = Path(settings.data_dir) / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:  # noqa: BLE001
            logger.warning("instruments 维表读取失败, 标的池仅剩自选: %s", e)
    if repo is not None:
        base -= set(repo.get_index_symbol_set())
    return sorted(base)


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    repo.refresh_view(name, path)


def compute_offset(value: int, unit: str) -> timedelta:
    """将用户输入的 value + unit 转成 timedelta。"""
    if unit == "day":
        return timedelta(days=value)
    elif unit == "month":
        return timedelta(days=value * 30)
    elif unit == "year":
        return timedelta(days=value * 365)
    else:
        raise ValueError(f"不支持的单位: {unit}")


# 除权因子可用性实测缓存: (时间戳, ok, 原因)。探测要打一次网络，1 小时内复用。
_ADJ_PROBE_TTL_S = 3600
_adj_probe_cache: tuple[float, bool, str] | None = None


def check_adj_factor_source(capset: CapabilitySet, *, probe_symbol: str = "000001.SZ") -> tuple[bool, str]:
    """这次「向前扩展历史」能不能拿到除权因子？（实测，不看 capability 声明）

    为什么不能只看 `capset.has(Cap.ADJ_FACTOR)`：本机 `data/capabilities.json` 里
    `adj_factor` 这个 key 是**在**的（limits 全 null），而探测日志同一份文件里写着
    `✗ adj_factor(无权限)`，真正调用 `ex_factors` 直接 PermissionError。声明与事实
    不一致时，只有调用一次能定案。

    为什么必须先问：补历史写入的是 `adjust="none"` 的原始价。缺因子时 enriched 不做
    前复权，图上每次送转/分红都会变成一根**假跳空** —— 实测 2024-08~2025-05 窗口，
    10 只样本 8 只受影响（002594 67%、000651 18%、000001 12%…）。写进去的是错数据，
    而且看起来完全正常。

    返回 (ok, 原因)。自定义因子源直接放行（不走 tickflow 探测）。
    """
    from app.services import preferences as _prefs

    provider = _prefs.get_adj_factor_provider()

    global _adj_probe_cache
    now = time.time()
    if _adj_probe_cache and now - _adj_probe_cache[0] < _ADJ_PROBE_TTL_S:
        return _adj_probe_cache[1], _adj_probe_cache[2]

    # 非 tickflow：探的是「配好的那个源到底给不给数」。不能只看 provider 名字
    # —— 配了但插件没装/没 key 时，整条链会在十分钟后以"0 行因子"结束，
    # 而那时原始价已经写进库了。
    if provider != "tickflow":
        try:
            from app.data_providers import custom as custom_sources

            probe = custom_sources.get_provider(provider).get_adj_factors(
                [probe_symbol], start_time=None, end_time=None,
            )
            if probe is not None and not probe.is_empty():
                _adj_probe_cache = (now, True, f"provider:{provider}")
                return True, f"provider:{provider}"
            reason = f"{provider} 未返回任何除权事件"
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
        logger.warning("自定义除权因子源不可用（%s），补历史将被拒绝: %s", provider, reason)
        _adj_probe_cache = (now, False, reason)
        return False, reason

    if not capset.has(Cap.ADJ_FACTOR):
        _adj_probe_cache = (now, False, "capability-missing")
        return False, "capability-missing"
    try:
        from app.services.kline_sync import get_client

        get_client().klines.ex_factors([probe_symbol])
        _adj_probe_cache = (now, True, "probe-ok")
        return True, "probe-ok"
    except Exception as e:  # noqa: BLE001 - 权限/超时都按"拿不到"处理，宁可不写
        reason = f"{type(e).__name__}: {e}"
        logger.warning("除权因子探测失败，补历史将被拒绝: %s", reason)
        _adj_probe_cache = (now, False, reason)
        return False, reason


def run_extend_history(
    repo: KlineRepository,
    capset: CapabilitySet,
    value: int,
    unit: str,
    on_progress: Callable | None = None,
) -> dict:
    """向前扩展历史数据的主函数。

    完全独立于 daily_pipeline.run_now(),不调用其任何逻辑。
    返回结果 dict 供 job_store 记录。
    """
    emit = on_progress or _noop

    # 0. 计算时间偏移
    offset = compute_offset(value, unit)
    today = date.today()

    # 1. 获取当前最早日期
    emit("extend_history", 2, "检查当前数据范围…")
    earliest = repo.earliest_daily_date()

    if not earliest:
        return {"error": "本地无日K数据,请先执行一次完整同步"}

    new_start = earliest - offset
    # 不能超过今天
    if new_start >= earliest:
        return {"error": "扩展范围无效,请增大时间跨度"}

    # 2. 解析标的池
    emit("extend_history", 5, "解析标的池…")
    universe = _resolve_universe(capset, repo)
    if not universe:
        return {"error": "标的池为空"}
    emit("extend_history", 8, f"标的池: {len(universe)} 只")

    start_str = new_start.strftime("%Y-%m-%d")
    end_str = earliest.strftime("%Y-%m-%d")

    # 3. 拉日 K
    emit("extend_history", 10, f"获取日K [{start_str} ~ {end_str}]…")
    logger.info("extend_history: daily K [%s ~ %s], %d symbols", start_str, end_str, len(universe))

    def _daily_chunk(cur: int, tot: int) -> None:
        emit("extend_history", 10 + int(35 * cur / tot),
             f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    written_daily = kline_sync.sync_and_persist_daily_batch(
        universe, repo, capset,
        start_date=datetime.combine(new_start, datetime.min.time()),
        end_date=datetime.combine(earliest, datetime.min.time()),
        on_chunk_done=_daily_chunk,
    )
    emit("extend_history", 45, f"日K 完成,写入 {written_daily} 行")
    logger.info("extend_history: daily K done, %d rows", written_daily)
    _refresh_single_view(repo, "kline_daily")
    _invalidate("daily")

    # 4. 拉除权因子 (新范围)
    written_adj = 0
    adj_start = datetime.combine(new_start, datetime.min.time())
    adj_end = datetime.combine(today, datetime.min.time())
    adj_start_str = new_start.strftime("%Y-%m-%d")
    adj_end_str = today.strftime("%Y-%m-%d")

    from app.services import preferences as _prefs
    adj_provider = _prefs.get_adj_factor_provider()
    can_sync_adj = capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"
    if can_sync_adj:
        emit("extend_history", 48, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("extend_history: adj_factor [%s ~ %s]", adj_start_str, adj_end_str)

        def _adj_chunk(cur: int, tot: int) -> None:
            emit("extend_history", 48 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

        written_adj, _affected = kline_sync.sync_adj_factor(
            universe, repo, capset,
            start_time=adj_start, end_time=adj_end,
            on_chunk_done=_adj_chunk,
            scope="universe",
        )
        emit("extend_history", 60, f"除权因子完成,{written_adj} 行")
        logger.info("extend_history: adj_factor done, %d rows", written_adj)
        _refresh_single_view(repo, "adj_factor")
        _invalidate("adj_factor")
    else:
        emit("extend_history", 60, "除权因子跳过(无权限)")
        logger.info("extend_history: adj_factor skipped, no ADJ_FACTOR capability")

    # 5. 全量重算 enriched
    emit("extend_history", 65, "全量计算 enriched…")
    logger.info("extend_history: full enriched rebuild start")

    from app.indicators.pipeline import run_pipeline
    written_enriched = run_pipeline()

    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0
    emit("extend_history", 92, f"enriched 完成,覆盖 {enriched_days} 天")
    logger.info("extend_history: enriched done, %d days", enriched_days)
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # 6. 刷新视图
    emit("extend_history", 95, "刷新视图…")
    _refresh_single_view(repo, "kline_daily")
    _refresh_single_view(repo, "kline_enriched")
    _refresh_single_view(repo, "adj_factor")
    _invalidate(None)

    # 7. 统计结果
    from app.tickflow.partitions import list_partition_dates

    daily_dir = repo.store.data_dir / "kline_daily"
    # 月分区布局下目录数是月数; 报给用户的"覆盖天数"必须读真实日期数。
    daily_days = len(list_partition_dates(daily_dir)) if daily_dir.exists() else 0

    emit("extend_history", 100, f"完成,已扩展至 {new_start}")

    return {
        "earliest_before": earliest.isoformat(),
        "earliest_after": new_start.isoformat(),
        "daily_rows": written_daily,
        "daily_days": daily_days,
        "adj_factor_rows": written_adj,
        "enriched_days": enriched_days,
        "universe_size": len(universe),
    }
