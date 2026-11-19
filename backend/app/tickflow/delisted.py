"""缺口标的（退市/ST/吸收合并）日K 源 —— 唯一入口。

## 为什么需要这个模块

`kline_daily_enriched` 是一份**按今天在册名单回溯填充**的行情库：5,573 只标的
全部活到最新交易日，没有任何一只在 2025 年及以前停止交易。名录里有、行情里没有
的 312 只（154 含「退」+ 100 ST + 58 吸收合并/改名）就是生存偏差的全部来源。

补全包 `data/backfill_20261117/stock_gap_daily.parquet` 存了其中 306 只的日K
（1,193,364 行，1990-12-19 起），但它是**独立文件**，不能塞进
`kline_daily_enriched/date=*/`：分区里多一个 parquet 会让同一天被读两遍，
且读取器不报错（本仓已知事故）。因此只能显式 union。

## 口径（混用会静默产生假数据）

- 本模块输出的 OHLC **全部是原始价**（未复权）。
- `kline_daily_enriched` 的 `open/high/low/close` 是**前复权**，`raw_*` 是原始价。
- 退市股没有分红/送转记录：东方财富 `RPT_SHAREBONUS_DET` 对 306 只全部返回空，
  `adj_factor` 覆盖 0/306。**无法为它们计算前复权**。
- 所以全样本只能在原始价口径上合并。已知代价：退市股在除权日会显示成假跌，
  方向固定为**压低**退市股收益 —— 若复检结论仍为负，缺分红只会让它更负；
  若发现正效应，那是被低估的（稳健）。见 `data/backfill_bs_minute/NEXT.md`。

## 收益计算的正确用法

    close / lag(close) - 1

且 `date` 与 `lag(date)` 必须在交易日历上 rn 差 1（退市整理期长期停牌后复牌，
`lag()` 数行不数天会把累计跌幅记成单日 -90%）。见
`analysis/_verify_survivorship_bias.py` 的 `make_px`。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import polars as pl

from app.parquet import scan_parquet_compat

logger = logging.getLogger(__name__)

#: 补全包目录名（相对 data/）。改名必须同步 MANIFEST.md 与 backfill 脚本。
GAP_DIRNAME = "backfill_20261117"
GAP_FILENAME = "stock_gap_daily.parquet"

#: 全样本视图/扫描结果里的来源标记列取值。
SRC_MAIN = "main"
SRC_GAP = "gap"

#: `price_basis` 取值：qfq = 前复权，raw = 原始价。
#: 缺口股**恒为 raw**（无除权记录，算不出 qfq），所以任何跨日序列只要含
#: `price_basis == 'raw'` 的行，就必须接受除权日假跌。
BASIS_QFQ = "qfq"
BASIS_RAW = "raw"

_lock = threading.Lock()
# 按 (路径, mtime) 缓存。补全包重跑会整文件覆盖（mtime 变），自动失效；
# 只保留最新一份，避免反复重跑后字典无限增长。
_symbols_cache: dict[tuple[str, float], frozenset[str]] = {}


def gap_parquet(data_dir: Path) -> Path | None:
    """补全包日K 文件；不存在（未补全/被清理）返回 None。"""
    path = Path(data_dir) / GAP_DIRNAME / GAP_FILENAME
    return path if path.is_file() else None


def gap_symbols(data_dir: Path) -> frozenset[str]:
    """缺口标的名单。读不出来时返回空集（降级为只有主表，不抛错）。"""
    path = gap_parquet(data_dir)
    if path is None:
        return frozenset()
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return frozenset()
    with _lock:
        hit = _symbols_cache.get(key)
    if hit is not None:
        return hit
    try:
        # 只读 symbol 列：1.19M 行 → 306 个唯一值，约 20ms。
        syms = frozenset(
            pl.scan_parquet(path).select("symbol").unique().collect()["symbol"].to_list()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("gap symbols unreadable (%s): %s", path, e)
        return frozenset()
    with _lock:
        _symbols_cache.clear()
        _symbols_cache[key] = syms
    return syms


def scan_gap_raw(data_dir: Path) -> pl.LazyFrame | None:
    """缺口标的日K，列名与 enriched 对齐，OHLC 一律原始价。

    缺的 7 列（raw_close/raw_high/raw_low/turnover_rate/consecutive_*/quote_ts）
    不伪造：`raw_*` 直接由 OHLC 复制（因为这里本来就是原始价），
    换手率与连板数保持 NULL —— 宁可缺失，也不要用"按 5% 猜涨跌停"这类
    指纹去填（实测 precision .448 / recall .423）。
    """
    path = gap_parquet(data_dir)
    if path is None:
        return None
    return scan_parquet_compat(path).select(
        pl.col("symbol").cast(pl.Utf8),
        pl.col("date").cast(pl.Date),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("amount").cast(pl.Float64),
        pl.lit(None, dtype=pl.Int64).alias("quote_ts"),
    )


def raw_open_expr() -> pl.Expr:
    """从 enriched 的复权 open 还原原始 open。

    同一天的开盘与收盘共享同一个复权比例（比例只随除权事件变化，日内不变），
    因此 `open * raw_close / close` 是**精确可逆**的，不是近似。
    实测 601127.SH 2024-11-07：128.761818 × 135.87 / 132.959934 = 131.58。
    """
    return (
        pl.when(pl.col("close") > 0)
        .then(pl.col("open") * pl.col("raw_close") / pl.col("close"))
        .otherwise(None)
        .alias("open")
    )


def to_raw_basis(df: pl.DataFrame, *, src: str = SRC_MAIN) -> pl.DataFrame:
    """把 enriched 行投影成原始价口径（8 个行情列 + src + price_basis）。"""
    return df.select(
        pl.col("symbol"),
        pl.col("date"),
        raw_open_expr(),
        pl.col("raw_high").alias("high"),
        pl.col("raw_low").alias("low"),
        pl.col("raw_close").alias("close"),
        pl.col("volume"),
        pl.col("amount"),
        pl.lit(src).alias("src"),
        pl.lit(BASIS_RAW).alias("price_basis"),
    )


def to_qfq_basis(df: pl.DataFrame, *, src: str = SRC_MAIN) -> pl.DataFrame:
    """保留 enriched 的前复权 OHLC，只补来源标记。

    这是「主表复权 + 缺口股原始价」的混合口径，也就是
    `analysis/_verify_l1_survivorship.py` 的口径：日内比值（close/open）
    在任何基准下都相同，跨日序列则 main 正确、gap 带除权噪声。
    """
    return df.select(
        pl.col("symbol"),
        pl.col("date"),
        pl.col("open"),
        pl.col("high"),
        pl.col("low"),
        pl.col("close"),
        pl.col("volume"),
        pl.col("amount"),
        pl.lit(src).alias("src"),
        pl.lit(BASIS_QFQ).alias("price_basis"),
    )


def gap_basis(df: pl.DataFrame) -> pl.DataFrame:
    """缺口股行：恒为原始价。"""
    return df.select(
        pl.col("symbol"),
        pl.col("date"),
        pl.col("open"),
        pl.col("high"),
        pl.col("low"),
        pl.col("close"),
        pl.col("volume"),
        pl.col("amount"),
        pl.lit(SRC_GAP).alias("src"),
        pl.lit(BASIS_RAW).alias("price_basis"),
    )


#: 全样本结果列（两种 basis 共用同一套列，避免 concat 时列数不等）。
ALL_COLUMNS: list[str] = [
    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
    "src", "price_basis",
]
