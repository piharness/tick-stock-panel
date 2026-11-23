"""日线面板装载的唯一实现 —— 区间取分区、列选择、基础前向收益。

为什么单独成模块
----------------
原来只有 `table_game` 需要「按区间取日线分区」，遍历逻辑写在它的私有函数里。
当第二个消费方（操盘档案）也需要同一件事时，若再抄一份，月分区
（`date=2026-11`）的字符串比较陷阱就会分别出现两次——本项目已经在
`trading_days` 上付过一次学费（日分区正则看不见月分区，把 14 天滞后
讲成了「信息到达的硬约束」）。

这里只负责「哪些文件」和「读成什么」。派生列（收益率、振幅、前向收益）
留给各自的领域模块，因为口径不同：
- `table_game` 关心「次日开盘能不能买到」，需要 `d1_gap` / `d1_oc`；
- `case_lab` 关心「命中之后漂移多少」，需要 `fwd1/5/20` 与匹配桶。

两条口径都不该由装载层替对方决定。
"""

from __future__ import annotations

import logging
import os
from datetime import date as date_cls
from pathlib import Path

import polars as pl

from app.tickflow.partitions import partition_dates

logger = logging.getLogger(__name__)

# 面板列：两个消费方共用的最小集合。缺列一律当无数据，不猜。
PANEL_COLUMNS = (
    "symbol", "date", "open", "high", "low", "close",
    "volume", "amount", "turnover_rate",
    "consecutive_limit_ups", "consecutive_limit_downs",
)


def enriched_partition_dates(
    data_dir: Path, table: str = "kline_daily_enriched"
) -> list[date_cls]:
    """enriched 表的日分区日期（升序）。**只读目录名，不打开任何 parquet。**

    为什么不用 `kline_trading_days`（读 `kline_daily`）：那是月分区表，
    `list_partition_dates()` 必须对每个月分区 `scan_parquet` 读 `date` 列才能
    知道月内哪些日子真有行。容器内 bind mount 上 246 个月分区实测 **19.5s**，
    而整条查询的其余部分（读 340 个日分区 + 算 250 日滚动分位）只要 1.1s。
    `decision/bars.py`（18.36s → 页面 65s）与 `citizenship.py` 都已各踩过一次，
    并各自写了绕开版本；这是第三处，所以放在装载层由两个消费方共用。

    而且窗口的日期本来就该由**实际要读的那张表**给出：拿 `kline_daily` 的
    日历去截 `enriched` 的窗口，两张表的覆盖不一致时会出现「窗口里有一天没有
    文件」或「文件里有窗口外的天」，两者都不报错。这里两者同源，不会再分叉。

    月分区（`date=2026-11`）不是日期，正常情况下按目录名长度排除；真有月分区
    时回退到读它们的 `date` 列，并打一条 warning —— 慢 19s 比静默变成
    「这只票没数据」便宜。跨月混存由 `enriched_files` 单独兼容。
    """
    base = Path(data_dir) / table
    if not base.is_dir():
        return []
    out: list[date_cls] = []
    monthly: list[str] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith("date="):
                    continue
                tag = name[5:]
                if len(tag) == 10:
                    try:
                        out.append(date_cls.fromisoformat(tag))
                    except ValueError:
                        continue
                elif len(tag) == 7:
                    monthly.append(tag)
                # 其余长度（date=2026-11-08.bak 之类）两侧一律不认：
                # 日历认了而文件不认（或反之）就是「同一天读两遍还不报错」。
    except OSError:
        return []
    if monthly:
        # 布局迁移的 fail-safe：enriched 目前是纯日分区，这里零成本。
        # 若将来也按月分区，绝不能因为「日历空了」就静默返回无数据 ——
        # 那种失败看起来像「这只票没数据」，比慢 19s 更贵。
        logger.warning(
            "kline_daily_enriched 出现 %d 个月分区，交易日历回退到读 date 列", len(monthly)
        )
        for tag in monthly:
            out.extend(partition_dates(base / f"date={tag}"))
    out.sort()
    return out


def enriched_files(
    data_dir: Path, start: date_cls, end: date_cls, table: str = "kline_daily_enriched"
) -> list[Path]:
    """按区间取日线分区文件。

    月分区（`date=2026-11`）按目录名区间比较即可，因为分区名是 ISO 前缀，
    字符串序 == 时间序；但右端必须展开成 `-31`，否则 `'2026-11' < '2026-11-13'`
    会把整个月判成区间外。

    `os.scandir` 而不是 `iterdir()` + `Path.is_dir()`：后者每判断一个目录都要
    重新 `os.stat()`，5313 个分区在 bind mount 上实测 5.87s；而 `DirEntry.is_dir()`
    读的是 `readdir` 带回的 `d_type`，零系统调用。区间外的目录在比较阶段就被
    跳过，连 `is_dir()` 都不调。

    与 `KlineRepository._partition_files_in_range` 的分工：那个方法面对的是
    「任意表 + 可能未知的目录命名」，认不出的目录一律**纳入**（多读只是慢，
    少读是静默丢行）；本函数只服务已知的 enriched 日分区布局，认不出的一律
    **跳过**（`date=2026-11-08.bak` 被纳入就是同一天读两遍）。两者的差异来自
    对目录命名空间的假设不同，不是两套口径 —— 需要裁剪 repo 内路径时用那个。
    """
    base = Path(data_dir) / table
    if not base.is_dir():
        return []
    lo, hi = start.isoformat(), end.isoformat()
    out: list[Path] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith("date="):
                    continue
                tag = name[5:]
                if len(tag) == 7:
                    span_lo, span_hi = f"{tag}-01", f"{tag}-31"
                elif len(tag) == 10:
                    span_lo = span_hi = tag
                else:
                    # 与 `enriched_partition_dates` 同一判据。`date=2026-11-08.bak`
                    # 这类目录若被某一侧认了、另一侧没认，就会出现「日历里有、
                    # 文件里没有」或「同一天读两遍」—— 后者在跨区间查询时才会
                    # 暴露（单日区间被字符串比较挡住），把一天的成交额算成两天。
                    continue
                if span_hi < lo or span_lo > hi:
                    continue
                if not entry.is_dir():
                    continue
                for f in Path(entry.path).glob("*.parquet"):
                    out.append(f)
    except OSError:
        return []
    return sorted(out)


def load_panel(
    data_dir: Path,
    start: date_cls,
    end: date_cls,
    columns: tuple[str, ...] = PANEL_COLUMNS,
) -> pl.DataFrame:
    """读取 [start, end] 的日线面板，按 (symbol, date) 升序。

    空区间返回空 DataFrame（调用方判空），不抛异常——数据缺失是常态，
    不是错误。
    """
    files = enriched_files(data_dir, start, end)
    if not files:
        return pl.DataFrame()
    df = (
        pl.scan_parquet([str(f) for f in files], extra_columns="ignore")
        .select([c for c in columns])
        .collect()
    )
    if df.is_empty():
        return df
    return df.sort(["symbol", "date"])


def attach_returns(df: pl.DataFrame) -> pl.DataFrame:
    """基础派生列：当日涨跌幅、振幅、成交额均线、动量、前向收益。

    前向收益用复权 close 计算（enriched 的 close 已是前复权），因此跨除权日
    也成立；但 `fwd20` 这类长窗口在同一只票上会逐日重叠，统计时必须按日聚合，
    否则 t 值会被重叠样本放大。
    """
    if df.is_empty():
        return df
    eps = 1e-12
    return df.with_columns([
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret"),
        (
            (pl.col("high") - pl.col("low"))
            / (pl.col("close").shift(1).over("symbol") + eps)
        ).alias("amp"),
        (
            (pl.col("close") - pl.col("low"))
            / (pl.col("high") - pl.col("low") + eps)
        ).alias("close_pos"),
        (
            (pl.col("high") - pl.max_horizontal("open", "close"))
            / (pl.col("close").shift(1).over("symbol") + eps)
        ).alias("upper_shadow"),
        pl.col("amount").rolling_mean(20, min_samples=10).over("symbol").alias("amount_ma20"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("mom20"),
        (pl.col("close").shift(-1).over("symbol") / (pl.col("close") + eps) - 1).alias("fwd1"),
        (pl.col("close").shift(-5).over("symbol") / (pl.col("close") + eps) - 1).alias("fwd5"),
        (pl.col("close").shift(-20).over("symbol") / (pl.col("close") + eps) - 1).alias("fwd20"),
    ])
