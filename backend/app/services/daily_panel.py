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

from datetime import date as date_cls
from pathlib import Path

import polars as pl

# 面板列：两个消费方共用的最小集合。缺列一律当无数据，不猜。
PANEL_COLUMNS = (
    "symbol", "date", "open", "high", "low", "close",
    "volume", "amount", "turnover_rate",
    "consecutive_limit_ups", "consecutive_limit_downs",
)


def enriched_files(data_dir: Path, start: date_cls, end: date_cls) -> list[Path]:
    """按区间取日线分区文件。

    月分区（`date=2026-11`）按目录名区间比较即可，因为分区名是 ISO 前缀，
    字符串序 == 时间序；但右端必须展开成 `-31`，否则 `'2026-11' < '2026-11-13'`
    会把整个月判成区间外。
    """
    base = Path(data_dir) / "kline_daily_enriched"
    if not base.is_dir():
        return []
    out: list[Path] = []
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith("date="):
            continue
        tag = d.name[5:]
        if len(tag) == 7:
            lo, hi = f"{tag}-01", f"{tag}-31"
        else:
            lo = hi = tag
        if hi < start.isoformat() or lo > end.isoformat():
            continue
        for f in d.glob("*.parquet"):
            out.append(f)
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
