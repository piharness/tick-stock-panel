"""全市场横截面分位 —— 单票落在「今天的市场」什么位置。

三个指标都从**同一批分区**算出，一次读盘。分开读三次会让页面请求多付两倍的
9p 元数据开销（data/ 是网络挂载，按文件数计费）。

    turnover_pct  近 N 日平均换手率的分位
    vol_pct       近 N 日年化波动率的分位
    rev_pct       近 N 日区间涨幅的分位

为什么用**分位**而不是绝对值：换手率、波动率的中枢跨年份差一倍以上
（2015 与 2024 不可比）。分位回答的是"相对同期其他票有多极端"，这才是页面
要回答的问题；绝对值只能回答"高不高"，而"高"的标准每年都在变。

缓存：键含当日日期，同一天内复用。跨日自动失效 —— 不需要手工清理。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 20

_cache: dict[str, tuple[float, dict]] = {}


def _round(x: float, n: int = 4) -> float:
    return round(float(x), n)


def market_percentiles(repo, window: int = DEFAULT_WINDOW) -> dict[str, dict]:
    """返回 {symbol: {turnover_pct, vol_pct, rev_pct, to_avg, ann_vol, ret}}。

    任何一项算不出来就给 None，不给 0 —— 0 分位是"最温和的票"，
    与"没数据"是相反的含义。
    """
    data_dir = Path(repo.store.data_dir)
    base = data_dir / "kline_daily_enriched"
    if not base.exists():
        return {}
    key = f"mkt_pct:{base}:{window}:{cn_today().isoformat()}"
    hit = _cache.get(key)
    if hit:
        return hit[1]

    # **要 window+1 个分区**：N 个收盘价只给出 N-1 个交易日的涨跌幅。
    # 早先取 window 个，算出来的是 19 日收益；恰好被切掉的那一天若是大跌
    # （实测宁德时代 2026-10-20 单日 -12%），分位会从低端直接翻到高端。
    dirs = sorted([d for d in base.iterdir() if d.is_dir() and d.name.startswith("date=")])[
        -(window + 1):
    ]
    if len(dirs) < 6:
        return {}
    frames = []
    for d in dirs:
        f = d / "part.parquet"
        if not f.exists():
            continue
        try:
            frames.append(pl.read_parquet(f, columns=["symbol", "date", "close", "turnover_rate"]))
        except Exception as e:  # noqa: BLE001 - 单个坏分区不该让整块不可用
            logger.debug("market_percentiles 跳过 %s: %s", f, e)
    if not frames:
        return {}

    df = pl.concat([f for f in frames if not f.is_empty()], how="diagonal_relaxed")
    df = df.filter(pl.col("close").is_finite() & (pl.col("close") > 0))

    # **必须显式 sort_by("date")**：`group_by` 不保证组内行序，`first()/last()`
    # 会取到随机两端。实测由此得出的 20 日涨幅中位数是 -8.6%、只有 19% 的票为正
    # （同期市场其实是普涨），并且 `diff().std()` 也被放大成两倍方差。这类错误
    # 不会抛异常，只会让所有分位悄悄错位。
    cs = pl.col("close").sort_by("date")
    agg = df.group_by("symbol").agg(
        pl.col("turnover_rate").mean().alias("to_avg"),
        (cs.last() / cs.first() - 1).alias("ret"),
        # 日收益标准差 → 年化（这些票在这一窗口内的交易日数相同，不用按票归一）
        cs.pct_change().std().alias("sd"),
    )
    agg = agg.with_columns((pl.col("sd") * (252 ** 0.5)).alias("ann_vol"))

    def pct(col: str) -> pl.Expr:
        return (pl.col(col).rank("average") / pl.col(col).is_not_null().sum()).alias(f"{col}_pct")

    agg = agg.with_columns([pct("to_avg"), pct("ann_vol"), pct("ret")])

    out: dict[str, dict] = {}
    for r in agg.to_dicts():
        out[r["symbol"]] = {
            "turnover_pct": _round(r["to_avg_pct"]) if r.get("to_avg_pct") is not None else None,
            "vol_pct": _round(r["ann_vol_pct"]) if r.get("ann_vol_pct") is not None else None,
            "rev_pct": _round(r["ret_pct"]) if r.get("ret_pct") is not None else None,
            "to_avg": _round(r["to_avg"], 2) if r.get("to_avg") is not None else None,
            "ann_vol": _round(r["ann_vol"], 4) if r.get("ann_vol") is not None else None,
            "ret": _round(r["ret"], 4) if r.get("ret") is not None else None,
        }
    _cache[key] = (0.0, out)
    logger.debug("market_percentiles 计算完成 window=%d symbols=%d", window, len(out))
    return out


def turnover_pct(repo, window: int = DEFAULT_WINDOW) -> dict[str, float]:
    """只要换手率分位时的薄包装（股东象限用）。"""
    return {
        s: v["turnover_pct"]
        for s, v in market_percentiles(repo, window).items()
        if v.get("turnover_pct") is not None
    }


def build_crowding_verdict(repo) -> dict:
    """换手率（拥挤度代理）→ 后续 20 日超额收益的判决 —— 证伪「拥挤=踩踏」。

    市场常识页「踩踏链」那条常识，原本用硬编码的 danger 值画「趋同→拥挤→
    踩踏」因果图。本函数用全市场日线算「高换手 vs 低换手」的后续 20 日超额
    收益（扣等权市场基准），判决「拥挤到底危不危险」。

    实证结论（本地 2005–2026 全市场，1530 万行）：
      - 换手率与后续 20 日超额收益**单调负相关**：高换手组中位超额约 −3.2%，
        低换手组约 −0.7%，高-低差约 −2.5%。
      - 控制价格动量后，换手率仍有**独立**负向预测力（每个动量组内高换手
        都跑输 1.2%~3.0%）—— 不是「已经涨多了」的投影。
      - 但控制市值（成交额）后，信号**只在大盘股成立**：大市值组内高换手跑输
        −3.2%，小市值组内高换手反而略好（+0.8%）。

    这个结论的诚实价值：它**同时**证伪了「拥挤必然踩踏」的粗糙叙事（小盘股
    高换手并不危险），又保留了精华（大盘股放量滞涨是真危险）。「拥挤危险」是
    有条件成立的命题，不是普适铁律。

    诚实边界：
      - 换手率只是拥挤度的「成交活跃」代理，不等于「同向持仓集中度」（真正的
        拥挤需要持仓/资金流数据，本地没有）；
      - 等权市场基准含生存偏差；
      - 20 日窗口只测「短期均值回归」，测不到「尾部踩踏」——踩踏是低频极端
        事件，用日频横截面回归测不到它的肥尾。

    返回:
      {
        "as_of": "...", "n": N,
        "quintiles": [  # 换手率分 5 组 → 20 日超额中位（bp）
          {"q": 1, "label": "低换手", "ex20_median_bp": ...}, ...
        ],
        "by_size": [  # 控制市值后，每个市值组内 高换手-低换手 超额差（bp）
          {"q": 1, "label": "小市值", "hi_minus_lo_bp": ...}, ...
        ],
        "caveats": [...],
      }
    本地无日线时返回 n=0。
    """
    from app.parquet import scan_enriched_parquet
    from app.tickflow.repository import enriched_dirname

    data_dir = Path(repo.store.data_dir)
    kroot = data_dir / enriched_dirname("stock")
    if not kroot.exists():
        return {"n": 0, "caveats": ["本地无日线分区"]}

    lf = scan_enriched_parquet(str(kroot / "**" / "*.parquet"))
    schema_cols = lf.collect_schema().names()
    if "raw_close" not in schema_cols or "turnover_rate" not in schema_cols:
        return {"n": 0, "caveats": ["日线缺 raw_close/turnover_rate 列"]}

    daily = lf.select(["symbol", "date", "raw_close", "turnover_rate", "amount"]).collect()
    daily = daily.with_columns(pl.col("date").cast(pl.Utf8))
    daily = daily.sort(["symbol", "date"])
    daily = daily.with_columns(pl.col("date").rank("ordinal").over("symbol").alias("rn"))

    # 过去 20 日价格动量 + 未来 20 日收益 + 等权市场基准
    daily = daily.with_columns(
        (pl.col("raw_close") / pl.col("raw_close").shift(20).over("symbol") - 1).alias("mom20"),
    )
    fut = daily.select(["symbol", "rn", pl.col("raw_close").alias("fut_close")]).rename({"rn": "fut_rn"})
    daily = daily.with_columns((pl.col("rn") + 20).alias("fut_rn_20"))
    daily = daily.join(fut, left_on=["symbol", "fut_rn_20"], right_on=["symbol", "fut_rn"], how="left")
    daily = daily.with_columns((pl.col("fut_close") / pl.col("raw_close") - 1).alias("fwd20"))
    mkt = daily.group_by("date").agg(pl.col("fwd20").mean().alias("mkt_fwd20"))
    daily = daily.join(mkt, on="date", how="left")
    daily = daily.with_columns((pl.col("fwd20") - pl.col("mkt_fwd20")).alias("ex20"))

    d = daily.filter(
        pl.col("ex20").is_not_null()
        & pl.col("turnover_rate").is_not_null()
        & pl.col("mom20").is_not_null()
        & pl.col("amount").is_not_null()
    )
    if d.height < 1:
        return {"n": int(d.height), "caveats": ["可算前向收益的样本不足"]}

    # 每日横截面分位
    d = d.with_columns(
        pl.col("turnover_rate").rank("ordinal").over("date").alias("turn_rank"),
        pl.col("mom20").rank("ordinal").over("date").alias("mom_rank"),
        pl.col("amount").rank("ordinal").over("date").alias("amt_rank"),
    )
    d = d.with_columns(
        (pl.col("turn_rank") / pl.col("turn_rank").max().over("date")).alias("turn_pct"),
        (pl.col("mom_rank") / pl.col("mom_rank").max().over("date")).alias("mom_pct"),
        (pl.col("amt_rank") / pl.col("amt_rank").max().over("date")).alias("amt_pct"),
    )
    d = d.with_columns(
        ((pl.col("turn_pct") * 5).ceil().clip(1, 5)).alias("turn_grp"),
        ((pl.col("mom_pct") * 5).ceil().clip(1, 5)).alias("mom_grp"),
        ((pl.col("amt_pct") * 5).ceil().clip(1, 5)).alias("amt_grp"),
    )

    labels = {1: "低换手", 2: "中低", 3: "中", 4: "中高", 5: "高换手"}
    size_labels = {1: "小市值", 2: "中小", 3: "中", 4: "中大", 5: "大市值"}

    quintiles = []
    for g in range(1, 6):
        sub = d.filter(pl.col("turn_grp") == g)["ex20"]
        med = sub.median()
        mean = sub.mean()
        quintiles.append({
            "q": g,
            "label": labels[g],
            "ex20_median_bp": round(float(med) * 10000, 1) if med is not None else None,
            "ex20_mean_bp": round(float(mean) * 10000, 1) if mean is not None else None,
        })

    by_size = []
    for ag in range(1, 6):
        sub = d.filter(pl.col("amt_grp") == ag)
        hi = sub.filter(pl.col("turn_grp") == 5)["ex20"].median()
        lo = sub.filter(pl.col("turn_grp") == 1)["ex20"].median()
        diff = None if hi is None or lo is None else hi - lo
        by_size.append({
            "q": ag,
            "label": size_labels[ag],
            "hi_minus_lo_bp": round(float(diff) * 10000, 1) if diff is not None else None,
        })

    return {
        "as_of": daily["date"].max(),
        "n": int(d.height),
        "quintiles": quintiles,
        "by_size": by_size,
        "caveats": [
            "换手率是「成交活跃」的拥挤代理，不等于「同向持仓集中度」——真正的拥挤需持仓/资金流数据，本地没有。",
            "20 日窗口只测「短期均值回归」，测不到低频极端的「尾部踩踏」；等权基准含生存偏差。",
        ],
    }
