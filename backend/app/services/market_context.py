"""市场环境层：把「现在是不是一个适合承担 β 的市场」变成可读的一行。

为什么只描述、不阻断
--------------------
本模块最初的设计是「环境差 ⇒ 禁止新开多头腿」。回算否决了这个形式：

1. `analysis/_diag_regime_gate.py --days 2400` 显示，把「20 日广度 < 50%」当
   开关做择时，10 年累计从 −13.3% 抬到 +134.0%（空仓 70%）。但阈值扫描是**尖峰**
   而不是平台：46% / 48% / 50% / 52% 对应 +64 / +121 / +134 / +39 —— 相邻两个
   百分点让结果掉 95 个点，属于在 200 次切换时点上拟合出来的参数。
2. 前瞻检验更直接：广度萎缩状态下未来 20 日等权中位 **−0.11%**、正收益概率 49%
   （非重叠抽样 84 个点，中位 −0.05%）。**没有预测力**。
3. 单看次日：空仓组 T+1 均值 −4.92bp（t=−1.40），持仓组 +12.94bp（t=+2.43）。
   看似成立，但 20 日滚动信号在 2382 天里重叠 20 倍，有效独立样本约等于 205 次
   切换，那一层的 t 远不足 2。
4. 分年度看它是什么：牛年大幅跑输（2021 −16.5%、2025 −16.4%），熊年大幅跑赢
   （2018 +38.5%、2026 +28.6%）。它是防御型 β 择时，不是 alpha。

所以这里不做「禁止」，只做「说清楚」。真正的硬事实不是择时能力，而是
**等权全市场 10 年年化 −1.4%、近 120 日 −27.1%**：β 暴露本身是负期望。
这条不需要阈值，也不需要预测，它决定了「中长期多头腿在当前环境里没有顺风」。

纪律
----
- 不引入第二个「今天」：`as_of` 由调用方传入（与账本/守护同一个来源）。
- 只读最近 25 个分区，按 `as_of` 缓存；读不到就是 `unknown`，绝不抛错。
- 不重复解释口径：广度定义与 `_diag_regime_gate.load_daily` 严格一致
  （日历相邻算一日收益、剔除 |ret| >= 11.5% 的坏样本），改一处必须改两处。
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any

BREADTH_WINDOW = 20
PARTS_NEEDED = BREADTH_WINDOW + 5
RISK_OFF_BREADTH = 0.50
MAX_ABS_RET = 0.115

# 历史证据来自 `analysis/_diag_regime_gate.py --days 2400`（2017-01-24 ~ 2026-11-18,
# 2382 个交易日）。改脚本后这几个数必须重算, 否则这里的说明会变成旧世界的化石。
_EVIDENCE = {
    "fwd20_median_pct": -0.11,
    "fwd20_positive_pct": 49,
    "same_state_days": 1646,
    "benchmark_10y_total_pct": -13.3,
    "source": "analysis/_diag_regime_gate.py --days 2400",
}

_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()


def _load_daily(enriched: Path, limit: int) -> Any:
    import os

    import polars as pl

    # 不用 glob: `data/` 是 9p 挂载, 对 5000+ 分区做 `date=*/part.parquet`
    # 要 300ms, 而 scandir 取目录名只要 4ms (75 倍)。这是状态条能否挂在
    # 每个请求上的分界线。
    try:
        with os.scandir(enriched) as it:
            names = sorted(entry.name for entry in it if entry.name.startswith("date="))
    except FileNotFoundError as exc:
        raise ValueError(f"日线分区目录不存在: {enriched}") from exc
    parts = [enriched / name / "part.parquet" for name in names[-limit:]]
    if len(parts) < BREADTH_WINDOW + 2:
        raise ValueError(f"分区不足: {len(parts)} < {BREADTH_WINDOW + 2}")
    frames = [
        pl.read_parquet(p, columns=["symbol", "date", "close"])
        for p in parts
        if p.is_file()
    ]
    if not frames:
        raise ValueError("没有可读的分区文件")
    df = pl.concat(frames).sort(["symbol", "date"])
    calendar = sorted(df["date"].unique().to_list())
    index = {day: i for i, day in enumerate(calendar)}
    df = df.with_columns(
        pl.col("date").replace_strict(index, return_dtype=pl.Int32).alias("ti"),
        pl.col("close").shift(1).over("symbol").alias("prev_close"),
    ).with_columns(pl.col("ti").shift(1).over("symbol").alias("prev_ti"))
    # 交易日历相邻才算一日收益: 跨停牌的行 gap > 1, 丢弃
    df = df.filter(pl.col("ti") - pl.col("prev_ti") == 1)
    df = df.with_columns((pl.col("close") / pl.col("prev_close") - 1).alias("ret"))
    # 超出主板涨跌停容忍 = 前复权跳变/复牌, 不计入等权
    df = df.filter(pl.col("ret").abs() < MAX_ABS_RET)
    return (
        df.group_by("date")
        .agg(
            pl.col("ret").mean().alias("eq_ret"),
            (pl.col("ret") > 0).mean().alias("breadth"),
            pl.len().alias("n"),
        )
        .sort("date")
    )


def build_market_context(data_dir: Path | str, as_of: date | None = None) -> dict[str, Any]:
    """返回当前市场环境描述。`state` 只有 risk_on / risk_off / unknown 三种。"""
    data_path = Path(data_dir)
    # key 必须带数据目录: 只用日期会让不同数据根 (测试、备用数据) 互相污染。
    key = f"{data_path.resolve()}|{as_of.isoformat() if as_of else 'latest'}"
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit

    result: dict[str, Any] = {
        "state": "unknown",
        "as_of": as_of.isoformat() if as_of else None,
        "breadth_20d": None,
        "eq_ma20_bp": None,
        "window": BREADTH_WINDOW,
        "samples": None,
        "threshold": RISK_OFF_BREADTH,
        "note": "没有读到日线分区, 环境未知",
        "evidence": dict(_EVIDENCE),
    }
    try:
        daily = _load_daily(data_path / "kline_daily_enriched", PARTS_NEEDED)
        if daily.height < BREADTH_WINDOW:
            raise ValueError(f"可用交易日不足: {daily.height}")
        tail = daily.tail(BREADTH_WINDOW)
        breadth = float(tail["breadth"].mean())
        eq_bp = float(tail["eq_ret"].mean()) * 1e4
        last_date = str(tail["date"][-1])
        result.update(
            {
                "state": "risk_off" if breadth < RISK_OFF_BREADTH else "risk_on",
                "as_of": result["as_of"] or last_date,
                "breadth_20d": round(breadth, 4),
                "eq_ma20_bp": round(eq_bp, 2),
                "samples": int(tail["n"].median()),
            }
        )
        if result["state"] == "risk_off":
            result["note"] = (
                f"近 {BREADTH_WINDOW} 日仅 {breadth:.1%} 的个股上涨, "
                f"等权日均 {eq_bp:+.1f}bp: β 暴露本身是负期望"
            )
        else:
            result["note"] = (
                f"近 {BREADTH_WINDOW} 日 {breadth:.1%} 的个股上涨, "
                f"等权日均 {eq_bp:+.1f}bp: β 环境正常"
            )
    except (OSError, ValueError, ImportError) as exc:  # 数据缺失不该让状态接口 500
        result["error"] = f"{type(exc).__name__}: {exc}"[:120]

    with _cache_lock:
        _cache[key] = result
    return result


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# 拥挤度：当日换手率分位
# ---------------------------------------------------------------------------

#: 「拥挤区」定义：当日换手率在全市场的分位 ≥ 此值。
#: 它是**分位**不是绝对换手率 —— 绝对阈值会随市场整体活跃度漂移，
#: 而分位在同一时点内永远可比。
CROWDING_QUANTILE = 0.80


def crowding_quantiles(
    data_dir: Path | str, as_of: date | str | None = None
) -> dict[str, float]:
    """返回 `{symbol: 当日换手率分位}`（0~1，越大越拥挤）。

    为什么用它做准入前置：`analysis/_diag_regime_gate.py` 的分层回算显示，
    近 120 个交易日按成交额五分位，最高流动性组中位数 −36.5%、最低组 −25.8%
    —— **拥挤区多跌 10.7 个百分点**，而且正收益占比更高（12.0% vs 2.9%）。
    即「少数暴涨 + 多数暴跌」。这不是「不要买热门股」的口号，是可测的暴露：
    一个策略的持仓平均分位越高，它赌的就是那个更肥的左尾。

    读不到当日分区时返回空字典（调用方判 UNKNOWN），不抛错。
    """
    if as_of is None:
        return {}
    day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
    path = Path(data_dir) / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    if not path.is_file():
        return {}
    try:
        import polars as pl

        df = pl.read_parquet(path, columns=["symbol", "turnover_rate"])
    except (OSError, ValueError, ImportError):
        return {}
    df = df.filter(pl.col("turnover_rate").is_not_null())
    n = df.height
    if n < 100:
        return {}
    ranked = df.with_columns(
        (pl.col("turnover_rate").rank("average") / n).alias("pct")
    )
    return {
        str(sym): float(pct)
        for sym, pct in zip(ranked["symbol"].to_list(), ranked["pct"].to_list())
    }


def crowding_summary(levels: dict[str, float]) -> dict[str, Any]:
    """把分位表压成一行读数。空表 = 未知，不是 0。"""
    if not levels:
        return {
            "available": False,
            "quantile": CROWDING_QUANTILE,
            "crowded_share": None,
            "note": "当天没有换手率数据，拥挤度未知",
        }
    crowded = sum(1 for v in levels.values() if v >= CROWDING_QUANTILE)
    return {
        "available": True,
        "quantile": CROWDING_QUANTILE,
        #: 严格来说恒等于 1−quantile；保留它是为了让「表变了」可被看见
        "crowded_share": round(crowded / len(levels), 4),
        "note": (
            f"换手率前 {round((1 - CROWDING_QUANTILE) * 100)}% 的股票占 "
            f"{crowded / len(levels):.1%}；近 120 日该组中位 −36.5% vs 最低组 −25.8%"
        ),
    }
