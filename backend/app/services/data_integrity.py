"""历史交易日数据完整性检测 — 停机缺口 / 盘中快照判别。

场景: 用户盘中停机后, 次日再启动并开实时行情, 实时 flush 写出"今天"分区后,
盘后管道的「今天已有数据 → 只刷今天」分支会让停机日的盘中快照永久留存
(close=停机时刻价, volume=半日累计, 技术指标全错且污染后续 lookback 类指标)。

判据 (quote_ts 列, 毫秒 Unix 时间戳, 仅实时 flush 写入真实值):
- null              → batch 拉取 / 盘后计算写入的权威历史 → 完整
- d < 今天 且 时刻 < d 15:30 → 实时写入 (盘中快照或 close_final 定版) → 坏
- d < 今天 且 时刻 ≥ d 15:30 → 完整 (仅理论可能: 盘后管道 15:35 前的极晚写入)
- batch 权威行中仅夹杂少量零成交实时行 → 停牌残留 → 忽略
- d == 今天         → 实时更新中, 属正常, 不校验
- 分区缺失的候选日  → 缺口 (配置了 fuyao apikey → 按真实交易日检测;
  未配置 → 直接降级工作日近似; 节假日误报对修复只是空拉取, 但对
  realtime_gate 是 409 死锁 — 休市日永远无数据, 门禁循环放行不了)

自动检查优先读取 parquet 元数据; 显式 deep 自检额外按有界批次验证日K字段和数值。
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_today
from app.tickflow.partitions import (
    is_monthly_daily_table,
    list_partition_dates,
    locate_partition_dir,
    parse_partition_range,
    partition_dates,
    partition_dates_or_span,
)

logger = logging.getLogger(__name__)

# 尾盘定版线: 实时侧写入的分区一律不算定版 — 15:00 后的 close_final 定版也不
# 可信 (实测有源"时间戳翻新但价格停在竞价前旧价"), 且 15:30 前科创/创业板盘后
# 固定价交易仍在成交 (量未定版)。数据完整以盘后管道 (默认 15:35, 见
# daily_pipeline) 的官方 batch 行覆盖为准; 定版线取盘后固定价终止线 15:30,
# 与 quote_service._FINAL_DEADLINE 的 close_final 重试截止对齐 (15:30 后不再
# 有新实时写入, 判 15:30 与判 15:35 等价)。
CLOSE_CUTOFF = dt_time(15, 30)

# 扫描窗口: 最近 N 个自然日内、今天之前的交易日
SCAN_WINDOW_DAYS = 7

# 自动修复窗口: 最早坏日距今超过 N 个自然日 → 只报告不自动修 (更大缺口由用户手动 repair)
AUTO_REPAIR_MAX_LAG_DAYS = 5

# 参与检测的日K族表 (实时 flush 会写这三族的 daily/enriched)
_DAILY_TABLES = ("kline_daily", "kline_etf_daily", "kline_index_daily")

# 表 → 资产族 (用于管道/修复侧按族取起点)
TABLE_FAMILY = {
    "kline_daily": "stock",
    "kline_etf_daily": "etf",
    "kline_index_daily": "index",
}


@dataclass(frozen=True)
class IntegrityIssue:
    day: date
    table: str
    kind: str  # snapshot / missing / corrupt / invalid / unconfirmed
    # snapshot 且能定位时: 缺权威行(batch)的 symbol 清单 (修复只拉这些);
    # 空 = 干净或无法定位 (missing / 列缺失 / 读失败), 修复回退全市场。
    symbols: tuple[str, ...] = ()
    reason: str = ""
    files: tuple[str, ...] = ()


def active_daily_tables() -> tuple[str, ...]:
    """自动检查只覆盖当前实际启用的资产族, 显式自检可另选范围。"""
    from app.services import preferences

    enabled = (
        preferences.get_pipeline_pull_a_share() or preferences.get_realtime_pull_stock(),
        preferences.get_pipeline_pull_etf() or preferences.get_realtime_pull_etf(),
        preferences.get_pipeline_pull_index(),
    )
    return tuple(table for table, on in zip(_DAILY_TABLES, enabled, strict=True) if on)


DAILY_REQUIRED = ("symbol", "date", "open", "high", "low", "close", "volume")

#: 分区覆盖度下限 (相对同族维表行数)。低于此比例判 `sparse`。
#:
#: 为什么需要这条判据: 分区"存在、字段合法、行数 > 0"挡住的是文件级损坏,
#: 挡不住「只写进了几条」。实时行情轮询会先把 6~8 只基准指数落成当日分区,
#: 盘后 batch 若整批超时被静默丢弃, 这一天就以 6/599 的形态留在盘上 ——
#: 下游按日读到的是一份残缺横截面, 而所有既有判据全绿 (2026-11-05..11-12
#: 实证: 指数 6/6/378/378/6/8, 体检报 clean)。
#:
#: 0.75 的取法: 正常交易日停牌+新股未上市约影响 1~5%; 取 75% 既能容忍,
#: 也能在「少了一半以上」时必响。阈值偏高只会多报一次可修的稀疏分区,
#: 偏低则重演上面那六天 —— 两侧代价不对称, 故取宽。
MIN_COVERAGE_RATIO = 0.75

#: 日K表 → 同族维表 (相对 data 目录)。维表缺席时不做稀疏判定。
_INSTRUMENT_TABLE = {
    "kline_daily": ("instruments", "instruments.parquet"),
    "kline_etf_daily": ("instruments_etf", "instruments_etf.parquet"),
    "kline_index_daily": ("instruments_index", "instruments_index.parquet"),
}


def _symbol_floor(data_dir: Path, table: str) -> int | None:
    """同族维表量级 × `MIN_COVERAGE_RATIO`；维表缺失/不可读时返回 None (不判定)。"""
    rel = _INSTRUMENT_TABLE.get(table)
    if rel is None:
        return None
    path = Path(data_dir) / rel[0] / rel[1]
    if not path.is_file():
        return None
    try:
        total = pl.scan_parquet(str(path)).select(pl.len()).collect().item()
    except Exception:  # noqa: BLE001 — 维表坏掉是另一个问题, 不在这里误判缺口
        return None
    if not total:
        return None
    return max(1, int(total * MIN_COVERAGE_RATIO))


def _daily_validation(frame: pl.DataFrame, day: date | None) -> tuple[pl.DataFrame, dict[str, pl.Expr]]:
    """数值判据同时用于拦截与错误说明, 停牌零价沿用现有规则。"""
    if not set(DAILY_REQUIRED).issubset(frame.columns):
        raise ValueError("日K字段不完整")
    frame = frame.with_columns(
        pl.col("symbol").cast(pl.String, strict=False),
        pl.col("date").cast(pl.Date, strict=False),
        *(pl.col(c).cast(pl.Float64, strict=False) for c in DAILY_REQUIRED[2:]),
    )
    prices = ("open", "high", "low", "close")
    halted = ((pl.col("volume") == 0) & (pl.col("open") == 0)
              & (pl.col("high") == 0) & (pl.col("low") == 0) & (pl.col("close") >= 0))
    checks = {
        "股票代码缺失": pl.col("symbol").is_null() | (pl.col("symbol").str.strip_chars() == ""),
        "日期缺失或非法": pl.col("date").is_null(),
        "成交量缺失、非有限或为负": (
            pl.col("volume").is_null() | ~pl.col("volume").is_finite() | (pl.col("volume") < 0)
        ),
        **{f"{label}缺失或非有限": pl.col(column).is_null() | ~pl.col(column).is_finite()
           for column, label in zip(prices, ("开盘价", "最高价", "最低价", "收盘价"), strict=True)},
        "非停牌日开高低收存在零价或负价": (
            ~halted.fill_null(False) & pl.any_horizontal(pl.col(c) <= 0 for c in prices)
        ),
        "最高价小于开盘价、收盘价或最低价": (
            ~halted.fill_null(False) & (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        ),
        "最低价大于开盘价、收盘价或最高价": (
            ~halted.fill_null(False) & (pl.col("low") > pl.min_horizontal("open", "close", "high"))
        ),
    }
    if day is not None:
        checks["日期与文件分区不符"] = pl.col("date") != day
    if "amount" in frame.columns:
        amount = pl.col("amount").cast(pl.Float64, strict=False)
        checks["成交额非有限或为负"] = ((amount < 0) | ~amount.is_finite()).fill_null(False)
    return frame, checks


def invalid_daily_rows(frame: pl.DataFrame, day: date | None = None) -> pl.DataFrame:
    """自检与入库校验共享同一判据; 停牌零价与异常数值明确区分。"""
    frame, checks = _daily_validation(frame, day)
    invalid = pl.lit(False)
    for check in checks.values():
        invalid = invalid | check
    return frame.filter(invalid.fill_null(True))


def _invalid_daily_reason(frame: pl.DataFrame, day: date) -> str:
    frame, checks = _daily_validation(frame, day)
    flags = frame.select(check.fill_null(False).any().alias(label) for label, check in checks.items()).row(0, named=True)
    return "；".join(label for label, found in flags.items() if found)


def inspect_invalid_history(root: Path, table: str, files: list[Path], invalid: pl.DataFrame) -> list[IntegrityIssue]:
    """只在重算发现坏行后定位源分区; 行内日期损坏时仍按真实分区修补。

    日期一律从源文件的 date 列读 (月分区目录名只有月份, 不能当日期);
    坏行日期可解析时取交集缩小范围, 全部损坏时退回整个文件的日期集合
    (多出的干净日期由 inspect_daily_partition 复检后自然排除)。
    """
    from app.parquet import scan_daily_parquet

    paths = (
        scan_daily_parquet(files, include_file_paths="__integrity_file")
        .join(invalid.select("symbol", "date").unique().lazy(),
              on=["symbol", "date"], how="semi", nulls_equal=True)
        .select("__integrity_file").unique().collect(engine="streaming")["__integrity_file"]
    )
    row_days: set[date] = set()
    for value in invalid["date"].to_list():
        if isinstance(value, datetime):
            row_days.add(value.date())
        elif isinstance(value, date):
            row_days.add(value)
        else:
            with contextlib.suppress(ValueError):
                row_days.add(date.fromisoformat(str(value)[:10]))
    days: set[date] = set()
    for raw_path in paths:
        parent = Path(raw_path).parent
        rng = parse_partition_range(parent.name)
        if rng is None:
            continue
        if rng[0] == rng[1]:
            days.add(rng[0])
            continue
        try:
            file_days = {
                d for d in (
                    v.date() if isinstance(v, datetime) else v if isinstance(v, date) else None
                    for v in pl.scan_parquet(raw_path).select("date").unique().collect()["date"].to_list()
                ) if d is not None
            }
        except Exception:  # noqa: BLE001
            continue
        days |= (file_days & row_days) if row_days else file_days
    # 与显式自检一致: 当天仍在更新, 不能登记成尚无法修补的历史问题。
    today = cn_today()
    return [issue for day in sorted(days) if day < today and (issue := inspect_daily_partition(root, table, day)) is not None]


def _partition_problem(
    root: Path, part: Path, day: date, *, deep: bool,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]] | None:
    """有界逐文件检查, 读取错误不能与「权威历史没有 quote_ts」混为一谈。

    月分区: 校验范围限定到目标日 (batch 按 date 过滤); rows 计数来自
    分区元数据 (整月), 只用于「分区完全为空」判定, 不影响单日校验。
    """
    import pyarrow.parquet as pq

    rng = parse_partition_range(part.name)
    monthly = rng is not None and rng[0] != rng[1]
    paths = sorted(part.glob("*.parquet"))
    if not paths:
        return "corrupt", "日期目录内没有日K文件", (), ()
    bad: list[str] = []
    reasons: list[str] = []
    kind = "invalid"
    rows = 0
    symbols: set[str] = set()
    #: 分区内实际出现过的 symbol（不论数值是否合法）。只服务覆盖度判据,
    #: 与 `symbols`（缺陷标的）语义不同 —— 后者会喂给定向修复。
    seen: set[str] = set()
    unknown_symbols = False
    for path in paths:
        if not path.resolve().is_relative_to(root.resolve()):
            return "corrupt", "文件链接指向数据目录以外, 请手动检查目录", (), ()
        try:
            # 用自持的 OSFile 包住 parquet 读取: 损坏文件在 ParquetFile 构造
            # 阶段就会抛错, 若交给 pyarrow 按路径打开, 失败的句柄可能直到 GC
            # 才释放; Windows 上会阻塞后续 os.replace (WinError 5)。
            import pyarrow as pa

            with pa.OSFile(str(path), "rb") as source:
                parquet = pq.ParquetFile(source)
                rows += parquet.metadata.num_rows
                if not deep:
                    # 浅扫也要读 symbol 列: 覆盖度判据挡的是「分区写了几条就
                    # 再没补过」, 这类分区字段完整、行数非零, 只看元数据看不见。
                    # 单列读取不碰数值页, 代价远小于把残缺横截面判成干净。
                    if "symbol" in parquet.schema.names:
                        try:
                            # 必须走 ParquetFile.read 而非 pq.read_table(path, ...):
                            # 后者见到 columns 参数会按 dataset 打开, 把单文件路径
                            # 当成分区目录去合并 schema, 遇 date 列类型不一致直接
                            # 抛错 (dictionary<string> vs date32), 覆盖度恒为 0。
                            seen.update(
                                s for s in parquet.read(columns=["symbol"])
                                .column("symbol").to_pylist() if s
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("日K symbol 列读取失败 %s: %s", path, exc)
                    continue
                if not set(DAILY_REQUIRED).issubset(parquet.schema.names):
                    bad.append(path.name)
                    reasons.append("日K字段不完整")
                    unknown_symbols = True
                    continue
                columns = [*DAILY_REQUIRED, *(["amount"] if "amount" in parquet.schema.names else [])]
                for batch in parquet.iter_batches(batch_size=65536, columns=columns):
                    frame = pl.from_arrow(batch)
                    if monthly:
                        frame = frame.filter(pl.col("date") == day)
                    seen.update(s for s in frame["symbol"].to_list() if s)
                    invalid = invalid_daily_rows(frame, day)
                    if not invalid.is_empty():
                        bad.append(path.name)
                        reasons.append(_invalid_daily_reason(invalid, day))
                        values = invalid["symbol"].unique().to_list()
                        unknown_symbols |= any(s is None or not s.strip() for s in values)
                        symbols.update(s for s in values if s and s.strip())
        except Exception as exc:  # noqa: BLE001 — 单文件失败记录为检查问题, 继续检查其他文件
            logger.warning("日K文件检查失败 %s: %s", path, exc)
            bad.append(path.name)
            reasons.append("日K文件损坏或无法读取")
            kind = "corrupt"
            unknown_symbols = True
    if bad:
        return (kind, "；".join(dict.fromkeys(reasons)), tuple(dict.fromkeys(bad)),
                () if unknown_symbols else tuple(sorted(symbols)))
    if rows == 0:
        return "corrupt", "日期目录内的日K文件为空", tuple(p.name for p in paths), ()
    floor = _symbol_floor(root, part.parent.name)
    if floor is not None and len(seen) < floor:
        # 只报覆盖度, 不把 seen 当缺陷标的: 缺的是「没写进来的那些」,
        # 而定向修复需要的是「写坏了的那些」。调用方据此走整日全量重拉。
        return ("sparse", f"分区仅覆盖 {len(seen)} 只, 低于同族应有个数量级 {floor}",
                tuple(p.name for p in paths), ())
    return None


def _read_day_frame(part: Path, day: date) -> pl.DataFrame | None:
    """把分区里属于 day 的行读成一张表; 任何读取失败返回 None。

    失败不在这里报 —— 文件级损坏由 `_partition_problem` 负责, 这里只做
    「事实之间是否自洽」的判定。读不到就不下结论 (无证据 ≠ 干净)。
    """
    frames: list[pl.DataFrame] = []
    for path in sorted(part.glob("*.parquet")):
        try:
            frames.append(pl.read_parquet(path))
        except Exception as exc:  # noqa: BLE001
            logger.debug("事实判据跳过 %s: %s", path, exc)
            return None
    if not frames:
        return None
    try:
        frame = pl.concat(frames, how="diagonal_relaxed")
        if "date" in frame.columns:
            frame = frame.filter(pl.col("date") == day)
        return frame
    except Exception as exc:  # noqa: BLE001
        logger.debug("事实判据合并失败 %s: %s", part, exc)
        return None


def _instrument_symbols(root: Path) -> set[str]:
    """个股维表的代码集合; 维表缺失/读不了返回空集 (不判定, 由其它判据报)。"""
    path = Path(root) / "instruments" / "instruments.parquet"
    try:
        return set(pl.read_parquet(path, columns=["symbol"])["symbol"].to_list())
    except Exception:  # noqa: BLE001
        return set()


def audit_fact_violations(root: Path, table: str, day: date) -> IntegrityIssue | None:
    """落盘事实之间的三条恒等式 —— 独立审计通道, **不驱动自动修补**。

    1. **主键唯一**: 同一 (date, symbol) 至多「一行权威 + 一行实时」。
       两行同类 = 真重复, 会让后续每一次 join 放大行数 (2026-11-05 staging 残留)。
       「权威 + 残留」并存是 #514 认可的既有形态 (残留已被覆盖, 只影响修补范围),
       故不在此判坏。
    2. **指数不混入个股表**: `kline_daily` 只装个股。指数行的 close 是点位,
       一旦进入个股排序就被当作价格 (2026-11-05..11-12 实证 4 个分区混入)。
    3. **维表覆盖面板**: 面板里出现的每个代码都必须能在 `instruments` 里找到。
       零容差 —— 代码已上市才会出现在面板, 维表没有它只能是维表被写残
       (2026-11-13: BJ 端点 503 后用 SH+SZ 覆盖, 355 只北交所消失, 当天
       `panel - instruments` 从 0 直接变成 355)。

    均价类恒等式 (`amount/(volume*100) ∈ [low, high]`) **故意不在这里** ——
    `minute_audit` 的 S 轴已经用 ±1 手 / 2bp 的口径在做, 并已落盘进管道。
    同一判据写两遍、阈值还不同, 就是下一场「两个数都对不上」的来源。

    ## 为什么单独一条通道

    `inspect_daily_partition` 的返回值会进 `daily_health` → 触发自动重拉 → 未清除
    则退避重试。而这两条**重拉修不掉**: 指数行混入只有整日重写才消失, 定向修补
    会去把指数当个股拉; 重复行则取决于写入方, 拉回来还是会被再写一遍。
    塞进修补链 = 制造永不收敛的红灯。
    """
    part = locate_partition_dir(Path(root) / table, table, day)
    if not part.is_dir():
        return None
    frame = _read_day_frame(part, day)
    if frame is None or frame.is_empty():
        return None

    if {"symbol", "date"}.issubset(frame.columns):
        cols = ["symbol", "date"] + (["quote_ts"] if "quote_ts" in frame.columns else [])
        keyed = frame.select(cols)
        if "quote_ts" in keyed.columns:
            keyed = keyed.with_columns(pl.col("quote_ts").is_null().alias("__auth"))
        else:
            keyed = keyed.with_columns(pl.lit(True).alias("__auth"))
        dup = (
            keyed.group_by(["symbol", "date", "__auth"]).len()
            .filter(pl.col("len") > 1).sort("len", descending=True)
        )
        if not dup.is_empty():
            symbols = tuple(sorted(s for s in dup["symbol"].to_list() if s))
            most = int(dup["len"].max() or 0)
            return IntegrityIssue(
                day, table, "corrupt", symbols=symbols,
                reason=f"同一 (日期,股票) 同类重复最多 {most} 行, 共 {len(symbols)} 只",
                files=tuple(p.name for p in sorted(part.glob("*.parquet"))),
            )

    if table == "kline_daily" and "symbol" in frame.columns:
        index_path = Path(root) / "instruments_index" / "instruments_index.parquet"
        try:
            index_symbols = set(pl.read_parquet(index_path, columns=["symbol"])["symbol"].to_list())
        except Exception:  # noqa: BLE001 — 维表缺失是另一个问题, 不在这里误判
            index_symbols = set()
        mixed = sorted(set(frame["symbol"].drop_nulls().to_list()) & index_symbols)
        if mixed:
            return IntegrityIssue(
                day, table, "invalid",
                reason=f"指数代码混入个股日线: {', '.join(mixed[:5])}",
                files=tuple(p.name for p in sorted(part.glob("*.parquet"))),
            )

        # 维表必须覆盖面板的每一个代码。零容差, 不需要阈值: 面板里出现的代码
        # 一定已上市, 维表没有它只可能是维表被写残 (2026-11-13: BJ 端点 503 后
        # 用 SH+SZ 整表覆盖, 355 只北交所消失, 而任何 join instruments 都静默丢它们)。
        instruments = _instrument_symbols(root)
        if instruments:
            absent = sorted(set(frame["symbol"].drop_nulls().to_list()) - instruments)
            if absent:
                return IntegrityIssue(
                    day, table, "invalid",
                    reason=f"标的维表缺 {len(absent)} 只面板代码: {', '.join(absent[:5])}",
                )
    return None


FACT_REPORT_REL = ("user_data", "source_facts_report.json")


def audit_recent_facts(
    data_dir: Path, *, today: date | None = None, days: int = SCAN_WINDOW_DAYS,
) -> list[IntegrityIssue]:
    """近 N 个交易日的恒等式审计 + 落盘报告。

    报告必须落盘而不是只写日志: 日志里的 WARNING 没人读 (2026-11-05..11-12 的
    指数 6/599 就是"体检报 clean"期间一直存在)。文件供 API/前端读取,
    也供「今天与昨天是否一样」这类跨日对比使用。
    """
    today = today or cn_today()
    issues: list[IntegrityIssue] = []
    for table in active_daily_tables():
        for day in _candidate_days(today, days):
            part = locate_partition_dir(Path(data_dir) / table, table, day)
            if not part.is_dir():
                continue
            try:
                issue = audit_fact_violations(Path(data_dir), table, day)
            except Exception as exc:  # noqa: BLE001 — 审计失败不能影响管道
                logger.warning("fact audit failed %s %s: %s", table, day, exc)
                continue
            if issue is not None:
                issues.append(issue)
    path = Path(data_dir).joinpath(*FACT_REPORT_REL)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from app.services.fs_utils import atomic_write_text

        atomic_write_text(path, json.dumps({
            "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "today": today.isoformat(),
            "window_days": days,
            "issues": [{
                "day": i.day.isoformat(), "table": i.table, "kind": i.kind,
                "reason": i.reason, "symbols": list(i.symbols),
            } for i in issues],
        }, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fact report write failed: %s", exc)
    return issues


def _quote_ts_max_ms(part_dir: Path, day: date | None = None) -> int | None:
    """读单个日期分区的 max(quote_ts); 列不存在/全 null/无统计 → None。

    优先走 parquet 元数据 row-group statistics (不解压数据页),
    statistics 缺失时回退 polars 列扫描。
    月分区: 元数据统计覆盖整月, 快照判定需要「该日」的最大值,
    必须带 day 走列扫描 (date 过滤 + quote_ts 聚合)。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    rng = parse_partition_range(part_dir.name)
    monthly = rng is not None and rng[0] != rng[1]
    use_metadata = not (monthly and day is not None)
    candidates: list[int | None] = []
    files = sorted(part_dir.glob("*.parquet"))
    if not files:
        return None
    for path in files:
        try:
            if not use_metadata:
                candidates.append(
                    pl.scan_parquet(path)
                    .filter(pl.col("date") == day)
                    .select(pl.col("quote_ts").max())
                    .collect()
                    .item()
                )
                continue
            # 显式管理文件句柄: 损坏文件的 read_metadata 会抛错, 按路径打开
            # 时失败的句柄可能滞留到 GC; Windows 上会阻塞后续原子替换。
            with pa.OSFile(str(path), "rb") as source:
                meta = pq.read_metadata(source)
                names = [meta.schema.column(i).name for i in range(meta.num_columns)]
                if "quote_ts" not in names:
                    continue
                idx = names.index("quote_ts")
                file_max: int | None = None
                for rg in range(meta.num_row_groups):
                    stats = meta.row_group(rg).column(idx).statistics
                    if stats is not None and stats.max is not None:
                        value = stats.max
                        file_max = int(value) if file_max is None else max(file_max, int(value))
                rows = meta.num_rows
            if file_max is None and rows > 0:
                # statistics 未写入 → 回退列扫描
                file_max = (
                    pl.scan_parquet(path)
                    .select(pl.col("quote_ts").max())
                    .collect()
                    .item()
                )
            candidates.append(file_max)
        except Exception as e:  # noqa: BLE001
            logger.debug("quote_ts scan skipped %s: %s", path, e)
    values = [v for v in candidates if v is not None]
    return max(values) if values else None


def _is_snapshot(day: date, quote_ts_ms: int | None) -> bool:
    """非空 quote_ts 且对应北京时间时刻早于当日收盘线 → 盘中快照。"""
    if quote_ts_ms is None:
        return False
    try:
        ts = datetime.fromtimestamp(int(quote_ts_ms) / 1000, tz=CN_TZ)
    except (OverflowError, OSError, ValueError):
        return False
    return ts.date() == day and ts.time() < CLOSE_CUTOFF


def _partition_snapshot_defects(
    day: date, part_dir: Path, quote_ts_max_ms: int | None
) -> set[str] | None:
    """分区级快照判定 → 缺陷 symbol 集合 (issue #514: 判坏下沉到 symbol 级)。

    batch 行用 null quote_ts 标识权威历史。实时轮询曾把停牌股票的 09:15、
    零成交记录写入分区; 后续 batch 会过滤停牌日, merge-upsert 因而留下这些
    孤立行。旧逻辑「任一行有成交即判坏整分区」会让 4 行残留触发全市场重拉。

    ## 为什么不再以 max(quote_ts) 作入口 (2026-11-13)

    旧入口是 `_is_snapshot(day, max(quote_ts))`: 只要分区里有**一行**盘后时间戳,
    symbol 级残留扫描就整体跳过。真实数据正好撞在这上面 —— 2026-11-05/06/11/12
    四个分区的 quote_ts 全部是 17:47~21:10 的盘后快照 (非 null 且 ≥15:30),
    于是这四天从不进入逐 symbol 判定; 任何一只停在 11:30 的股票都会被判干净。
    判据的灵敏度不该由同一分区里另一行的时间戳决定。

    现在逐行分类, **当日盘前/盘中的时间戳**即「可疑」。跨日时间戳仍沿用既有
    契约不判 (见 `tests/test_data_integrity.py::test_issue_from_other_day_timestamp_not_flagged`):
    现有源里有跨天写入的脏行, 也有盘后补写带前一日时间戳的行, 在拿到源端
    时间戳语义的证据之前, 把跨日一律判坏会制造无法收敛的修补。

    返回语义:
    - None      : 无法判定但按坏处理 (volume/amount 列缺失 / 读失败 / 无 symbol
                  列却有成交残留) — 保守 fail-closed, 修复回退全市场;
    - 空集合    : 分区干净 (残留 symbol 均已有权威行, 或仅零成交残留且权威行占多数);
    - 非空集合  : 恰好这些 symbol 写了有成交实时行且无权威行 → 只修它们。

    `quote_ts_max_ms` 仅为兼容既有调用方保留; 判定不再依赖它。
    """
    del quote_ts_max_ms  # 保留参数语义, 判定只看逐行事实

    start_ms = int(datetime.combine(day, dt_time.min, tzinfo=CN_TZ).timestamp() * 1000)
    cutoff_ms = int(datetime.combine(day, CLOSE_CUTOFF, tzinfo=CN_TZ).timestamp() * 1000)
    rng = parse_partition_range(part_dir.name)
    monthly = rng is not None and rng[0] != rng[1]
    authoritative_rows = 0
    suspicious_rows = 0
    authoritative_symbols: set[str] = set()
    suspicious_symbols: set[str] = set()  # 窗口内实时行的 symbol (含零成交)
    active_symbols: set[str] = set()  # 窗口内且有成交的实时行 symbol

    for path in sorted(part_dir.glob("*.parquet")):
        try:
            schema = pl.read_parquet_schema(path)
            if "quote_ts" not in schema:
                continue
            columns = [
                name for name in ("symbol", "quote_ts", "volume", "amount", "date")
                if name in schema
            ]
            frame = pl.read_parquet(path, columns=columns).with_columns(
                pl.col("quote_ts").cast(pl.Int64, strict=False),
            )
            if monthly:
                # 月分区: 权威/可疑行计数与 symbol 集合都必须限定到目标日,
                # 否则整月的权威行会把「该日缺权威行」的比例稀释掉。
                frame = frame.filter(pl.col("date") == day)
            has_symbol = "symbol" in frame.columns
            authoritative = frame.filter(pl.col("quote_ts").is_null())
            authoritative_rows += authoritative.height
            if has_symbol:
                authoritative_symbols.update(authoritative["symbol"].drop_nulls().to_list())
            suspicious = frame.filter(
                pl.col("quote_ts").is_between(start_ms, cutoff_ms, closed="left")
            )
            if suspicious.is_empty():
                continue
            suspicious_rows += suspicious.height

            activity_columns = [
                name for name in ("volume", "amount") if name in suspicious.columns
            ]
            if not activity_columns:
                return None
            active = suspicious.filter(
                pl.any_horizontal(
                    pl.col(name).cast(pl.Float64, strict=False).fill_null(0) > 0
                    for name in activity_columns
                )
            )
            if has_symbol:
                suspicious_symbols.update(suspicious["symbol"].drop_nulls().to_list())
                active_symbols.update(active["symbol"].drop_nulls().to_list())
            elif active.height > 0:
                return None  # 无 symbol 列却存在成交残留, 无法定位 → 整分区保守判坏
        except Exception as e:
            logger.debug("snapshot residue scan skipped %s: %s", path, e)
            return None

    if active_symbols:
        # 有成交残留: 只把「无权威行」的 symbol 记为缺陷; 两行兼有 = 无害滞后残留。
        return active_symbols - authoritative_symbols
    # 零成交残留: 沿用旧比例兜底 (权威行不占多数 → 整分区仍判坏, 现有行为锁定)。
    if suspicious_rows > 0 and authoritative_rows <= suspicious_rows:
        defects = suspicious_symbols - authoritative_symbols
        return defects or None  # 定位不到 symbol (如无 symbol 列) → 按未知处理
    return set()


def _partition_is_snapshot(day: date, part_dir: Path, quote_ts_max_ms: int | None) -> bool:
    """整个分区是否仍是盘中快照 (旧布尔口径, 供既有调用方; 内部走 symbol 级判据)。"""
    defects = _partition_snapshot_defects(day, part_dir, quote_ts_max_ms)
    return defects is None or bool(defects)


def inspect_daily_partition(data_dir: Path, table: str, day: date, *, deep: bool = True) -> IntegrityIssue | None:
    if table not in TABLE_FAMILY:
        raise ValueError("不支持的日K资产类型")
    part = locate_partition_dir(Path(data_dir) / table, table, day)
    if not part.is_dir():
        return IntegrityIssue(day, table, "missing", reason="交易日日K缺失")
    problem = _partition_problem(Path(data_dir), part, day, deep=deep)
    if problem:
        kind, reason, files, symbols = problem
        return IntegrityIssue(day, table, kind, symbols=symbols, reason=reason, files=files)
    defects = _partition_snapshot_defects(day, part, _quote_ts_max_ms(part, day))
    if defects is None or defects:
        return IntegrityIssue(day, table, "snapshot", symbols=tuple(sorted(defects)) if defects else ())
    return None


def _candidate_days(today: date, lookback_days: int) -> list[date]:
    """最近 lookback_days 自然日内、严格早于今天的交易日。

    fuyao 交易日历可用时按真实日历过滤 (休市日不进候选, 消除节假日误报 —
    2026 中秋 09-25 休市日曾被当缺失日, realtime_gate 409 死锁);
    从未取到日历时回退工作日近似 (历史行为)。
    """
    calendar = _trading_calendar()
    days: list[date] = []
    for offset in range(1, lookback_days + 1):
        d = today - timedelta(days=offset)
        if calendar is not None:
            if d in calendar:
                days.append(d)
        elif d.weekday() < 5:
            days.append(d)
    return sorted(days)


# ---------------------------------------------------------------------------
# 交易日历 (fuyao) — 「工作日近似」在节假日误报 missing; 修复任务对休市日
# 永远拉不到数据, realtime_gate 因此循环 409 (2026-09-25 中秋实证)。
# 取数失败时沿用上一次成功结果 (stale-while-error): 节假日表近乎不变,
# 陈旧日历仍远好于退回工作日近似 (长假期内误报会让门禁反复死锁)。
# ---------------------------------------------------------------------------
_CAL_TTL_S = 1800.0  # 成功/失败结论统一 30 分钟复取 (日历变化极稀疏)
_CAL_LOCK = threading.Lock()
_CAL: tuple[float, set[date] | None] = (0.0, None)  # (取数时刻, 交易日集合)


def _trading_calendar() -> set[date] | None:
    """A 股交易日集合 (fuyao 近一年); 从未取到过 → None (调用方回退周几近似)。

    与 trading_day.is_trading_day 探测链第 1 环同源; 这里需要**任意历史日**
    的判定, 故直接消费整年集合。线程安全 (gate / boot / 修复管道共用)。
    """
    now = time.monotonic()
    global _CAL
    with _CAL_LOCK:
        fetched_at, cached = _CAL
        if fetched_at and now - fetched_at < _CAL_TTL_S:
            return cached
    fetched: set[date] | None = None
    try:
        from app.data_providers import custom as custom_sources

        if custom_sources.is_custom_provider("fuyao"):
            raw = custom_sources.get_provider("fuyao").trading_days()
            if raw:
                fetched = set(raw)
    except Exception:  # noqa: BLE001 — 日历不可用不上抛, 回退 stale/近似
        fetched = None
    with _CAL_LOCK:
        _CAL = (now, fetched if fetched is not None else cached)
        return _CAL[1]


def scan_recent_integrity(
    data_dir: Path,
    *,
    today: date | None = None,
    lookback_days: int = SCAN_WINDOW_DAYS,
    tables: Iterable[str] | None = None,
    deep: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[IntegrityIssue]:
    """扫描最近交易日的数据完整性, 返回坏分区列表 (按日期升序)。

    每族表独立判定; 自动检查跳过近期无活动的表, 显式 deep 自检检查所选资产。
    首次同步前的日期不推断为缺失, deep 模式下未知开市日期仅报告待核实。
    """
    data_dir = Path(data_dir)
    today = today or datetime.now(CN_TZ).date()
    window_start = today - timedelta(days=lookback_days)
    issues: list[IntegrityIssue] = []

    selected = tuple(tables) if tables is not None else _DAILY_TABLES
    if not set(selected).issubset(_DAILY_TABLES):
        raise ValueError("不支持的日K资产类型")
    partitions: dict[str, set[date]] = {}
    # 月分区损坏后目录名读不出真实日期; 同资产的 enriched 仍是日分区,
    # 可用来证明「这个月实际有哪些交易日」, 避免把月内数据开始前的日期
    # 全部报成 corrupt 并触发无意义的整月回补。没有 enriched 证据时仍按
    # 月份区间 fail-closed。
    corroborating = list_partition_dates(data_dir / "kline_daily_enriched")
    for table in _DAILY_TABLES:
        base = data_dir / table
        if is_monthly_daily_table(table):
            # 月分区目录名只有月份; 缺失日检测要用真实覆盖日期 (读 date 列)。
            # 损坏/空分区退回月份区间, 让候选日逐日实读并报 corrupt, 而不是
            # 因日期集合为空把整张表跳过。
            existing: set[date] = set()
            if base.exists():
                for part in base.glob("date=*"):
                    if not part.is_dir():
                        continue
                    dates = partition_dates(part)
                    if not dates:
                        span = partition_dates_or_span(part)
                        dates = (span & corroborating) or span
                    existing |= dates
            partitions[table] = existing
            continue
        existing: set[date] = set()
        if base.exists():
            for part in base.glob("date=*"):
                if not part.is_dir():
                    continue
                try:
                    existing.add(date.fromisoformat(part.name[5:]))
                except ValueError:
                    continue
        partitions[table] = existing

    candidates = set(_candidate_days(today, lookback_days))
    unconfirmed: set[date] = set()
    if deep:
        calendar = _trading_calendar()
        known_days = set().union(*partitions.values())
        # 没有权威日历时, 其他资产的本地分区可以证明当天确实开市;
        # 其余工作日仅列为待核实, 不拿节假日假缺口反复重拉。
        weekdays = {window_start + timedelta(days=i) for i in range(lookback_days)
                    if (window_start + timedelta(days=i)).weekday() < 5}
        if calendar:
            unconfirmed = {d for d in weekdays if d < min(calendar) or d > max(calendar)} - known_days
        else:
            unconfirmed = weekdays - known_days
            candidates = weekdays & known_days
        candidates |= {d for d in known_days if window_start <= d < today}
        candidates |= unconfirmed

    total = max(1, len(selected) * len(candidates))
    done = 0
    for table in selected:
        base = data_dir / table
        existing = partitions[table]
        latest = max(existing) if existing else None
        # 族内近期无活动 → 不判定 (首次启动 / 长期停用)
        if latest is None or (not deep and latest < window_start):
            done += len(candidates)
            if on_progress:
                on_progress(done, total)
            continue

        earliest = min(existing)
        for day in sorted(candidates):
            done += 1
            if on_progress:
                on_progress(done, total)
            if day not in existing:
                if day >= earliest:
                    issues.append(IntegrityIssue(
                        day=day, table=table,
                        kind="unconfirmed" if day in unconfirmed else "missing",
                        reason="交易日历不可用, 该日期是否开市尚未确认" if day in unconfirmed else "交易日日K缺失",
                    ))
                continue
            issue = inspect_daily_partition(data_dir, table, day, deep=deep)
            if issue is not None:
                issues.append(issue)

    issues.sort(key=lambda i: (i.day, i.table))
    return issues


def earliest_issue_day(
    issues: list[IntegrityIssue],
    tables: Iterable[str] | None = None,
) -> date | None:
    """坏分区中最早的一天; tables 限定参与的表族 (None=全部)。"""
    scoped = (
        [i for i in issues if i.table in tables] if tables is not None else issues
    )
    return min((i.day for i in scoped), default=None)


def within_auto_repair_window(day: date | None, *, today: date | None = None) -> bool:
    """最早坏日是否落在自动修复窗口内 (≤ AUTO_REPAIR_MAX_LAG_DAYS 自然日)。"""
    if day is None:
        return False
    today = today or datetime.now(CN_TZ).date()
    return (today - day).days <= AUTO_REPAIR_MAX_LAG_DAYS


# ── 自动修复退避 (issue #514) ────────────────────────────────────────
# 同一坏日的自动修复失败后 (多为数据源拉取失败), 重启/再开实时行情不应无条件
# 再发一次全量修复。按坏日在 data_dir 记 last_attempt, 冷却期内跳过自动修复;
# 数据页手动修正不受限制 (不走 launch_integrity_repair)。
INTEGRITY_REPAIR_BACKOFF_S = 6 * 3600.0


def _backoff_path(data_dir: Path) -> Path:
    return Path(data_dir) / ".integrity_repair_backoff.json"


def repair_backoff_remaining_s(data_dir: Path, start_date: date) -> float:
    """自动修复冷却剩余秒数 (0 = 不在退避期)。按修复起点日 (最早坏日) 记账。"""
    try:
        payload = json.loads(_backoff_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    last = (payload.get("last_attempt") or {}).get(start_date.isoformat())
    if not isinstance(last, (int, float)):
        return 0.0
    elapsed = time.time() - float(last)
    if elapsed >= INTEGRITY_REPAIR_BACKOFF_S:
        return 0.0
    return INTEGRITY_REPAIR_BACKOFF_S - elapsed


def _record_repair_attempt(data_dir: Path, start_date: date) -> None:
    payload = {"last_attempt": {start_date.isoformat(): time.time()}}
    path = _backoff_path(data_dir)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.debug("repair backoff marker write failed: %s", e)


def prune_enriched_partitions(
    data_dir: Path,
    start: date,
    table: str = "kline_daily_enriched",
) -> int:
    """删除 enriched 表 date ≥ start 的日期分区, 使修复重算把它们当"新日期"。

    股票 enriched 增量重算只算 enriched 里不存在的日期; 盘中快照日分区已存在
    (虽是错的), 不删则永远不会被重算。指数/ETF 的 enriched 走 merge-upsert
    全行覆盖, 无需删除。删除后 run_pipeline(new_dates_only=True) 用剩余分区
    最近 60 天做历史前缀重算 (修复窗口 ≤5 天, 回看充足)。
    """
    base = Path(data_dir) / table
    if not base.exists():
        return 0
    import shutil

    removed = 0
    for part in base.glob("date=*"):
        try:
            d = date.fromisoformat(part.name[5:])
        except ValueError:
            continue
        if d >= start:
            shutil.rmtree(part, ignore_errors=True)
            removed += 1
    return removed


def describe_issues(issues: list[IntegrityIssue]) -> str:
    """面向用户的一句话描述 (409 详情 / 日志用)。"""
    if not issues:
        return ""
    labels = {
        "kline_daily": "股票日K",
        "kline_etf_daily": "ETF日K",
        "kline_index_daily": "指数日K",
    }
    groups: dict[tuple[str, str], set[date]] = {}
    for issue in issues:
        groups.setdefault((issue.table, issue.kind), set()).add(issue.day)
    parts = []
    for (table, kind), days in groups.items():
        day_text = "、".join(d.isoformat() for d in sorted(days))
        reason = {
            "snapshot": "停机前的盘中快照", "missing": "缺失",
            "corrupt": "文件损坏或为空", "invalid": "日K数值异常", "unconfirmed": "缺失待核实",
        }.get(kind, "需要检查")
        parts.append(f"{labels.get(table, '日K')}：{day_text} 的数据为{reason}")
    return "；".join(parts)


def launch_integrity_repair(
    app_state,
    start_date: date,
    reason: str,
    symbols: list[str] | None = None,
) -> tuple[str | None, bool]:
    """自动创建数据修复任务 (复用 repair_daily 管道 + JobStore 任务体系)。

    返回 (job_id, is_new):
    - (None, False)  : 无法修复 (无 batch 能力 / 无 repo) 或自动修复退避中
    - (id, False)    : 已有 pending/running 任务复用 (singleflight)
    - (id, True)     : 新建并启动
    任务体与 /api/kline/repair_daily 完全一致: run slot + 实时 paused 互斥 +
    显式自检/精准修补服务。

    symbols: 坏分区能定位到的缺陷标的清单 (issue #514 定向修复); None/空 = 全市场。

    调度自适应: 调用方在事件循环内 (API 端点) → executor 后台执行;
    无事件循环 (boot Timer 线程) → 独立 daemon 线程执行。
    """
    import asyncio
    import threading

    repo = getattr(app_state, "repo", None)
    capset = getattr(app_state, "capabilities", None)
    if repo is None or capset is None:
        return None, False
    try:
        from app.tickflow.capabilities import Cap

        if not capset.has(Cap.KLINE_DAILY_BATCH):
            logger.info("integrity repair skipped: no KLINE_DAILY_BATCH capability")
            return None, False
    except Exception:  # noqa: BLE001
        return None, False

    try:
        remaining = repair_backoff_remaining_s(repo.store.data_dir, start_date)
    except Exception:  # noqa: BLE001 — repo 形态异常时不因退避查询阻断修复
        remaining = 0.0
    if remaining > 0:
        logger.warning(
            "integrity: 自动修复退避中 (start=%s, 剩余 %.0f 分钟, reason=%s), 跳过。"
            "上次修复未成功多为数据源拉取失败, 可稍后自动重试或在数据页手动修正",
            start_date, remaining / 60, reason,
        )
        return None, False

    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )

    job_id, is_new = job_store.create(kind="integrity")
    if not is_new:
        return job_id, False

    with contextlib.suppress(Exception):
        _record_repair_attempt(repo.store.data_dir, start_date)
    qs = getattr(app_state, "quote_service", None)

    def progress(stage: str, pct: int, msg: str,
                 stage_pct: int | None = None, skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    def _run() -> dict:
        from app.services.integrity_check import run_check, run_repair

        assets = [TABLE_FAMILY[t] for t in active_daily_tables()]
        with qs.paused() if qs else contextlib.nullcontext():
            report = run_check(repo.store.data_dir, asset_types=assets, on_progress=progress)
            if report["repairable_count"]:
                report = run_repair(repo, capset, report["id"], on_progress=progress)
            return {"integrity_report": report}

    def _execute() -> None:
        try:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            result = run_with_capacity(job_id, _run)
            if isinstance(result, dict) and "error" in result:
                job_store.fail(job_id, str(result["error"]))
            else:
                job_store.succeed(job_id, result)
        except JobCancelledError:
            pass  # 已由 terminate() 标记失败
        except Exception as e:  # noqa: BLE001
            logger.exception("integrity repair failed: job_id=%s", job_id)
            job_store.fail(job_id, str(e))
        finally:
            release_run_slot(job_id)
            with contextlib.suppress(Exception):
                from app.api.data import invalidate_storage_cache

                invalidate_storage_cache()

    try:
        loop = asyncio.get_running_loop()

        async def task() -> None:
            await loop.run_in_executor(None, _execute)

        asyncio.create_task(task())
    except RuntimeError:
        threading.Thread(
            target=_execute, daemon=True, name=f"integrity-repair-{job_id[:8]}"
        ).start()

    logger.warning("integrity: 自动修复任务启动 job=%s start=%s reason=%s", job_id, start_date, reason)
    return job_id, True


def boot_integrity_check(app_state) -> None:
    """启动自检 (后台线程调用): 发现窗口内的坏数据自动创建修复任务。

    分钟K缺口无需单独处理 — 修复管道 Step 2.5 在 minute_sync_enabled 时
    以 start=max(datetime) 增量补洞, 天然覆盖停机缺口。
    """
    repo = getattr(app_state, "repo", None)
    if repo is None:
        return
    try:
        issues = [i for i in scan_recent_integrity(repo.store.data_dir) if i.table in active_daily_tables()]
    except Exception as e:  # noqa: BLE001
        logger.warning("boot integrity scan failed: %s", e)
        return
    if not issues:
        logger.info("boot integrity check: 近 %d 个交易日数据完整", SCAN_WINDOW_DAYS)
        return
    earliest = earliest_issue_day(issues)
    logger.warning("boot integrity check: %s (共 %d 个坏分区)", describe_issues(issues), len(issues))
    if not within_auto_repair_window(earliest):
        logger.warning(
            "integrity: 最早坏日 %s 超出自动修复窗口(%d 天), 请在数据页手动执行数据修正",
            earliest, AUTO_REPAIR_MAX_LAG_DAYS,
        )
        return
    with contextlib.suppress(Exception):
        launch_integrity_repair(
            app_state, earliest, "boot_integrity_check",
            symbols=_stock_snapshot_symbols(issues),
        )


def _stock_snapshot_symbols(issues: list[IntegrityIssue]) -> list[str] | None:
    """issues 里股票日K快照坏分区可定位的缺陷标的并集 (定向修复用; 空则全市场)。"""
    if any(i.table == "kline_daily" and (i.kind != "snapshot" or not i.symbols) for i in issues):
        return None
    symbols = {
        s
        for i in issues
        if i.table == "kline_daily" and i.kind == "snapshot"
        for s in i.symbols
    }
    return sorted(symbols) if symbols else None
