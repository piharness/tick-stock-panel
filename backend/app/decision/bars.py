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

import threading
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.tickflow.partitions import (
    is_monthly_daily_table,
    list_partition_dates,
    locate_partition_dir,
)

STOCK_ENRICHED = "kline_daily_enriched"
INDEX_ENRICHED = "kline_index_enriched"

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
    if "date" in have:
        lf = lf.filter(pl.col("date") == target)
    if symbols is not None:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    return lf.collect()


def day_rows(data_dir: Path, day: str, symbols: list[str] | None = None,
             table: str = STOCK_ENRICHED) -> pl.DataFrame:
    """某交易日、若干标的的 bar；缺表/缺分区返回空表（不是异常）。"""
    return _day_frame(Path(data_dir), table, day, symbols)


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
