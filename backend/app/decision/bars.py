"""决策账本用到的行情读取 — 只读、双分区布局、不引入第二套数据口径。

`kline_daily` 自 2026-11 起按月分区；enriched/指数/分钟仍按日分区。本模块
统一走 `app.tickflow.partitions` 的定位助手，调用方不需要知道底层目录名。

口径约束（与 CONTRIBUTING §3.2 一致）：
- 撮合与限价比较用**不复权 raw 价**（真实交易价格）；
- 收益与超额用**复权价**（`kline_daily_enriched.close`），避免除权日假跌幅。

`kline_daily_enriched` 只落 `raw_close / raw_high / raw_low`，没有 `raw_open`。
同一交易日的复权因子对 OHLC 一致，故 `raw_open = open / (close / raw_close)`；
分母缺失（首日、坏行）时返回 None，由调用方判 UNKNOWN，而不是拿复权价冒充原始价。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.tickflow.partitions import (
    is_monthly_daily_table,
    list_partition_dates,
    locate_partition_dir,
)

logger = logging.getLogger(__name__)

STOCK_ENRICHED = "kline_daily_enriched"
INDEX_ENRICHED = "kline_index_enriched"


class DuplicateBarsError(RuntimeError):
    """同一交易日分区里同一个 symbol 出现多行 —— 下游按只-日求和会双计。

    这条判据必须落在**读取时刻**, 不能等下游全 panel 拒绝:
    `l1_cohort` 的重复判据只有在两份文件的 symbol 高度重叠时才响, 若多出来的
    文件是一整份新快照(symbol 不重叠), 面板会静默多出几百只而名单照常出表。
    """


def _fmt_files(files: list[Path], dup_symbols: list[str]) -> str:
    """失败路径专用: 逐文件列出**重复 symbol 的行数**, 用来指出哪份是闯入者。

    必须按全部重复 symbol 统计, 不能只数消息里展示的那 6 只 —— 否则两个各含
    5570 行的文件会显示成 "6 行", 读的人会以为文件本身只有 6 行。
    """
    parts: list[str] = []
    for f in files:
        try:
            stat = f.stat()
            n = (
                pl.read_parquet(str(f), columns=["symbol"])
                .filter(pl.col("symbol").is_in(dup_symbols))
                .height
                if dup_symbols
                else 0
            )
            stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M:%S")
            parts.append(f"{f.name}({stat.st_size / 1024 / 1024:.1f}MB, {stamp}) -> {n} 行重复")
        except Exception as e:  # noqa: BLE001 — 诊断失败不该替换成另一个异常
            parts.append(f"{f.name}(读取失败: {e})")
    return "; ".join(parts)


def _assert_unique_bars(
    df: pl.DataFrame, *, table: str, day: str, part: Path, files: list[Path], by_date: bool,
) -> None:
    """(date,symbol) 在当日帧内必须唯一。干净路径只付一次 n_unique。"""
    if df.height <= 1:
        return

    if by_date:
        # 已 filter 到单一日期, symbol 唯一等价于 (date,symbol) 唯一, 省一次 struct。
        dup_rows = df.height - df["symbol"].n_unique()
    elif {"date", "symbol"} <= set(df.columns):
        dup_rows = df.height - df.select(pl.struct(["date", "symbol"]).n_unique()).item()
    else:
        return
    if dup_rows <= 0:
        return

    keys = ["date", "symbol"] if "date" in df.columns else ["symbol"]
    dup_symbols = sorted(
        set(df.filter(pl.struct(keys).is_duplicated())["symbol"].to_list())
    )
    sample = dup_symbols[:6]
    detail = _fmt_files(files, dup_symbols)  # 只读一次盘: 失败路径也不重复付
    logger.error(
        "分区内 (date,symbol) 重复: table=%s day=%s dup=%d/%d symbols=%s files=[%s]",
        table, day, dup_rows, df.height, sample, detail,
    )
    raise DuplicateBarsError(
        f"{table} 的 {part.name} 里有 {dup_rows} 行重复 (共 {df.height} 行), "
        f"涉及 {len(dup_symbols)} 个 symbol: {', '.join(sample)}"
        f"{' …' if len(dup_symbols) > len(sample) else ''}。"
        f"同一天同一只票被读两次会让下游按只-日双计。"
        f"逐文件行数: {detail}。"
        f"注意 *.parquet 会把 part.bak-*.parquet / part-stream-*.parquet 一并 concat,"
        f"备份与 staging 文件不要落在 date= 目录里。"
    )

#: 除权事件表。全市场单文件，不是按日分区——它是**输入**（除权公告的落库结果），
#: 不是派生列，所以不受「按日增量重算滞后」影响。`ex_factor` 是单事件的
#: 除权前收盘/除权参考价，不是累积因子（语义见 `app/indicators/pipeline.py`）；
#: 窗口内连乘即该窗口的累计复权系数。
EX_TABLE = "adj_factor"

_EX_CACHE: dict[tuple, pl.DataFrame] = {}
_EX_LOCK = threading.Lock()

# 决策账本只碰这几列；表里其它列（换手率、连板数）与本层无关。
_BAR_COLS = ("symbol", "date", "open", "high", "low", "close", "raw_close", "raw_high", "raw_low")


def sessions(data_dir: Path, table: str = "kline_daily") -> list[str]:
    """已落盘的交易日（ISO 字符串，升序）。

    用分区目录名而不是"今天减一天"：节假日、停机、只补了一半的数据都在这里
    如实体现。**没有数据的那天不是交易日**——这是本层对"新鲜度"的唯一判据来源。
    """
    base = Path(data_dir) / table
    if not base.is_dir():
        return []
    if is_monthly_daily_table(table):
        # 月分区目录名只有月份，真实交易日必须从数据的 date 列读；混存旧日分区
        # 时助手会同时枚举两种布局。
        return sorted(d.isoformat() for d in list_partition_dates(base))
    out: list[str] = []
    for child in base.iterdir():
        if not child.is_dir() or not child.name.startswith("date="):
            continue
        raw = child.name.split("=", 1)[1]
        try:
            date.fromisoformat(raw)
        except ValueError:
            continue
        out.append(raw)
    return sorted(out)


def latest_session(data_dir: Path, table: str = "kline_daily") -> str | None:
    dates = sessions(data_dir, table)
    return dates[-1] if dates else None


def _day_frame(data_dir: Path, table: str, day: str, symbols: list[str] | None) -> pl.DataFrame:
    try:
        target = date.fromisoformat(day)
    except ValueError:
        return pl.DataFrame()
    base = Path(data_dir) / table
    part = locate_partition_dir(base, table, target)
    if not part.is_dir():
        return pl.DataFrame()
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    lf = pl.scan_parquet([str(f) for f in files], missing_columns="insert", extra_columns="ignore")
    have = set(lf.collect_schema().names())
    cols = [c for c in _BAR_COLS if c in have]
    lf = lf.select(cols)
    # 月分区里同 symbol 的其他交易日也在这个目录；日分区过滤等价且更稳。
    by_date = "date" in have
    if by_date:
        lf = lf.filter(pl.col("date") == target)
    if symbols is not None:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    df = lf.collect()
    # 唯一性判据放在这里而不是下游: 同一份分区被两个文件各写一遍时,
    # 只有读取时刻知道"这一个分区里到底有几个文件、各含哪些行"。
    _assert_unique_bars(
        df, table=table, day=day, part=part, files=files, by_date=by_date,
    )
    return df


def day_rows(data_dir: Path, day: str, symbols: list[str] | None = None,
             table: str = STOCK_ENRICHED) -> pl.DataFrame:
    """某交易日、若干标的的 bar；缺表/缺分区返回空表（不是异常）。"""
    return _day_frame(Path(data_dir), table, day, symbols)


_TS_CACHE: dict[tuple[str, str, float], str | None] = {}
_TS_LOCK = threading.Lock()


def day_last_quote_ts(data_dir: Path, day: str,
                      table: str = STOCK_ENRICHED) -> str | None:
    """该日分区里 `quote_ts` 的最大值（本地 `HH:MM:SS`）；读不到返回 None。

    它回答的是**这一批 bar 是什么时候的快照**，不是「哪一天」。日线分区在盘中
    也会被实时路径写入：此时 `date` 已经是今天、行数齐、字段全、`(date,symbol)`
    无重复，唯一能分开「盘中快照」与「收盘定稿」的就是这一列
    （2026-11-13 实测 `quote_ts=10:42:47`，5,571 行看起来完全正常）。

    **None 是三态里的第三态**，不是「盘中」也不是「已收盘」：分区缺、列缺、
    整列为 null 都返回它（`date=2026-11-09` 与 `date=2026-11-10` 两个分区整列
    为空）。调用方必须自己决定那一态怎么处理，不许在这里替它猜。

    缓存键含分区 mtime：盘后重算会更新父目录 mtime，读到的自然是新快照。
    """
    try:
        target = date.fromisoformat(day)
    except ValueError:
        return None
    base = Path(data_dir) / table
    part = locate_partition_dir(base, table, target)
    if not part.is_dir():
        return None
    files = sorted(part.glob("*.parquet"))
    if not files:
        return None
    try:
        stamp = part.stat().st_mtime
    except OSError:
        stamp = 0.0
    key = (str(part.resolve()), day, stamp)
    with _TS_LOCK:
        if key in _TS_CACHE:
            return _TS_CACHE[key]

    lf = pl.scan_parquet([str(f) for f in files],
                         missing_columns="insert", extra_columns="ignore")
    have = set(lf.collect_schema().names())
    val: str | None = None
    if "quote_ts" in have:
        raw = lf.select(pl.col("quote_ts").max()).collect().item()
        if raw is not None:
            val = datetime.fromtimestamp(int(raw) / 1000).strftime("%H:%M:%S")
    with _TS_LOCK:
        # 只留最新一批：日线每天变，历史快照没有复用价值。
        if len(_TS_CACHE) > 64:
            _TS_CACHE.clear()
        _TS_CACHE[key] = val
    return val


def bar_map(data_dir: Path, day: str, symbols: list[str] | None = None,
            table: str = STOCK_ENRICHED) -> dict[str, dict]:
    """`{symbol: row}`，供逐标的撮合使用。"""
    df = day_rows(data_dir, day, symbols, table)
    if df.is_empty():
        return {}
    out: dict[str, dict] = {}
    for row in df.iter_rows(named=True):
        out[str(row["symbol"])] = row
    return out


def raw_open(row: dict) -> float | None:
    """从不复权收盘/复权收盘反推不复权开盘；口径不成立时返回 None。"""
    open_ = row.get("open")
    close = row.get("close")
    raw_close = row.get("raw_close")
    try:
        open_f, close_f, raw_close_f = float(open_), float(close), float(raw_close)
    except (TypeError, ValueError):
        return None
    if close_f <= 0 or raw_close_f <= 0 or open_f <= 0:
        return None
    return open_f * raw_close_f / close_f


def adj_factor(row: dict) -> float | None:
    """复权因子 `f = close / raw_close`（当日一致，用于把成交价换算到复权口径）。"""
    try:
        close_f = float(row.get("close"))
        raw_f = float(row.get("raw_close"))
    except (TypeError, ValueError):
        return None
    if close_f <= 0 or raw_f <= 0:
        return None
    return close_f / raw_f


def raw_low(row: dict) -> float | None:
    try:
        return float(row.get("raw_low"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 除权事件表
# ---------------------------------------------------------------------------


def _ex_frame(data_dir: Path) -> pl.DataFrame | None:
    """事件表全量。缺表返回 None——**缺表不是空表**，见 `ex_factor_product`。"""
    path = Path(data_dir) / EX_TABLE / "all.parquet"
    if not path.is_file():
        return None
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with _EX_LOCK:
        hit = _EX_CACHE.get(key)
    if hit is not None:
        return hit
    df = (pl.read_parquet(path)
          .select(["symbol", "trade_date", "ex_factor"])
          .with_columns(pl.col("trade_date").cast(pl.Utf8),
                        pl.col("ex_factor").cast(pl.Float64, strict=False))
          .drop_nulls())
    with _EX_LOCK:
        # 只留最新一份：按 mtime 做键的缓存不清理会按交易日数无限增长。
        _EX_CACHE.clear()
        _EX_CACHE[key] = df
    return df


#: 窗口可判定性。四个取值的**后续动作不同**，所以不能共用一个 `None`：
#:
#: | 状态 | 含义 | 等下去会变吗 | 动作 |
#: |---|---|---|---|
#: | `ok` | 表的时间范围盖住窗口 | — | 用事件因子算 |
#: | `absent` | 缺表 / 空表 | 不会（除非补数据） | 降级并标注 |
#: | `before_start` | 窗口早于表首 | 不会：表不向后生长 | 降级并标注 |
#: | `after_end` | 窗口晚于表末 | **会**：同步追上即自愈 | 挂起，不写盘 |
#:
#: 前两者与 `after_end` 的分界不是「严重程度」，是「等待是否会改变结论」。
#: 把三者压成一个 `None`，调用方只能一律降级——`after_end` 的降级行会被
#: `done` 键永久固化，而它本来只要再等一个交易日就是 `EVENT`。
WINDOW_OK = "ok"
WINDOW_ABSENT = "absent"
WINDOW_BEFORE_START = "before_start"
WINDOW_AFTER_END = "after_end"


def _ex_range(df: pl.DataFrame) -> tuple[str, str] | None:
    """事件表的时间范围。空表、日期列类型不对都返回 None，由调用方读作「不可判定」。"""
    if df.is_empty():
        return None
    lo, hi = df["trade_date"].min(), df["trade_date"].max()
    if not isinstance(lo, str) or not isinstance(hi, str):
        return None
    return str(lo), str(hi)


def ex_range(data_dir: Path) -> tuple[str, str] | None:
    """`(表首, 表末)`；缺表返回 None。表末是**已公告除权日**的最大值，不是覆盖终点。"""
    df = _ex_frame(Path(data_dir))
    return None if df is None else _ex_range(df)


def ex_window_state(data_dir: Path, entry: str, exit_: str) -> str:
    """`(entry, exit]` 这个窗口在事件表上属于四种状态中的哪一种。

    这是「不可判定」的**唯一判据来源**——`ex_factor_product` 也走它，两处不各写
    一遍，否则改了一处就分叉。
    """
    rng = ex_range(Path(data_dir))
    if rng is None:
        return WINDOW_ABSENT
    lo, hi = rng
    if str(entry) < lo:
        return WINDOW_BEFORE_START
    if str(exit_) > hi:
        return WINDOW_AFTER_END
    return WINDOW_OK


def ex_factor_product(data_dir: Path, symbols: list[str],
                      entry: str, exit_: str) -> dict[str, float] | None:
    """`(entry, exit]` 上每个标的的事件因子连乘；**窗口不可判定时返回 None**。

    返回 `None` 而不是 1.0，是这条函数最重要的契约：「窗口内没有除权事件」与
    「不知道窗口内有没有」必须分开。前者该乘 1，后者乘 1 会静默把收益退化成
    不复权口径——那恰好是账本已经犯过一次的错（陈旧复权序列里两端因子相等，
    因子约掉，分红整段消失）。缺表、只有空表、表的时间范围盖不住窗口，都是 None。

    判据是**表的整体时间范围**而不是「有没有查到行」：查不到行既可能是这段
    时间真没有事件，也可能是这段还没同步进来，只有范围能区分。

    具体是三种不可判定里的哪一种，读 `ex_window_state`——那决定调用方该降级
    还是该挂起。
    """
    if ex_window_state(Path(data_dir), entry, exit_) != WINDOW_OK:
        return None
    df = _ex_frame(Path(data_dir))
    if df is None:
        return None                 # 与上面同一判据；缓存命中，不会第二次读盘
    sub = df.filter(
        pl.col("symbol").is_in(list(symbols))
        & (pl.col("trade_date") > str(entry))
        & (pl.col("trade_date") <= str(exit_)))
    out = {str(s): 1.0 for s in symbols}
    if not sub.is_empty():
        for sym, val in sub.group_by("symbol").agg(pl.col("ex_factor").product()).iter_rows():
            out[str(sym)] = float(val)
    return out


def ex_window_readings(data_dir: Path, symbol: str,
                       entry: str, exit_: str) -> dict[str, Any]:
    """一条结算该随行保存的事件表读数：表端点 + 本窗口内该标的的事件行数。

    这三个键的用途各不相同，缺一个都会让事后核对退化成猜：

    - `ex_table_first` / `ex_table_last`：**当时的表长什么样**。它们替代不了
      指针，但也不需要指针——记录的是一行结算读到的字节范围，不是「哪次跑的」。
      回执会被清理，账本不会，所以账本不能指向回执。
    - `window_ex_events`：这一行的因子由几行事件构成。`0` 与 `None` 必须分开：
      `0` 是「窗口判得出、确实没有除权」，`None` 是「窗口判不出」（缺表 / 早于
      表首）。把后者写成 `0`，就与「确实没有除权」无法区分了。

    窗口不可判定时行数为 `None`，端点仍然照记——端点本身是可读的，只是不足以
    判定窗口。
    """
    rng = ex_range(Path(data_dir))
    first, last = (None, None) if rng is None else rng
    n: int | None = None
    if ex_window_state(Path(data_dir), entry, exit_) == WINDOW_OK:
        df = _ex_frame(Path(data_dir))
        if df is not None:
            n = df.filter(
                (pl.col("symbol") == str(symbol))
                & (pl.col("trade_date") > str(entry))
                & (pl.col("trade_date") <= str(exit_))).height
    return {"ex_table_first": first, "ex_table_last": last, "window_ex_events": n}


def ex_coverage(data_dir: Path, calendar: list[str]) -> dict[str, Any] | None:
    """事件表对日历的覆盖读数。缺表、空表、空日历都返回 `None`（都是「判不了」）。

    **空洞只在表的声明范围内数**：范围取 `[max(日历首, 表首), min(日历末, 表末)]`。
    表外那一段由 `endpoints_span_calendar` 回答，两处不重叠——否则一张表首晚于
    日历首的表，会把表首之前的日子全记成「空洞」，同一个事实被数两遍，而两边的
    修法完全不同（补表 vs 补同步）。

    `gap_runs` 存的是**段**，不是段的计数。同一份 21 个空档，「日历日相差 ≤3 天」
    切成 9 段最长 6 天，「交易日相邻」切成 12 段最长 5 天——两种切法都合理，
    所以只存数字的话，读的人连这个数字是怎么切出来的都无法还原。段一旦写进
    只增不改的回执，少一维就永久缺一维；存在这里则可以随时重切。

    `future_dated_rows` 是表里晚于日历末日的行数（已公告、还没走到的除权日）。
    健康状态下它就大于 0，所以它只是读数不是告警——把它当健康判据会天天红。
    """
    if not calendar:
        return None
    df = _ex_frame(Path(data_dir))
    if df is None or df.is_empty():
        return None
    rng = _ex_range(df)
    if rng is None:
        return None
    first, last = rng
    dated = set(df["trade_date"].unique().to_list())

    lo, hi = max(calendar[0], first), min(calendar[-1], last)
    span = [d for d in calendar if lo <= d <= hi]
    runs: list[list[str]] = []
    cur: list[str] = []
    for day in span:
        if day in dated:
            if cur:
                runs.append(cur)
                cur = []
        else:
            cur.append(day)
    if cur:
        runs.append(cur)

    return {
        "ex_table_first": first,
        "ex_table_last": last,
        "ex_rows": df.height,
        "future_dated_rows": df.filter(pl.col("trade_date") > calendar[-1]).height,
        "gap_sessions": sum(len(r) for r in runs),
        "gap_runs": [[r[0], r[-1]] for r in runs],
        "gap_max_run": max((len(r) for r in runs), default=0),
    }
