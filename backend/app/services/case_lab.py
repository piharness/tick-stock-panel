"""操盘档案 —— 用「痕迹」对账「叙事」，把操盘手法变成可证伪的判据。

为什么不做「案例故事库」
------------------------
处罚决定书通常滞后 1–3 年，爆料真假混合，二手分析不可核。把这些文本直接
排成时间轴，读者得到的是后见之明：故事永远自洽，因为它已经知道结局。
真正的学习必须反过来——先用**当时可知**的市场痕迹把区间切开，再让叙事来对账：

    叙事说「3 月建仓」 → 3 月有没有换手 / 席位 / 量能上的对应结构？
    有  → traced（痕迹支持）
    没有 → untraced（本地数据在该日看不到），不等于「假」

三类证据的可验证性必须显式分级，不允许混在一个列表里：
    official   官方文书（处罚 / 立案 / 问询）—— 事实权威，但时间滞后
    trace      市场痕迹（本地可算）—— 时点可知、可复现，本模块的核心
    narrative  爆料 / 媒体 / 研报 —— 可能为真，也可能本身就是出货工具

手法判据（fingerprint）的三条入库纪律
------------------------------------
1. 只用 T 日及之前的数据（无前视）。参数写进 `params`，展示文本由同一份参数
   格式化——阈值只有一处定义，不给「文档与代码不一致」留空间。
2. 命中后的收益先按**日期**聚合，再算 t。同日几十只票不是独立样本，且
   fwd20 逐日重叠；直接对行算 t 会得到虚高的显著性。
3. 每条判据必须写 `caveat`：它在什么情况下会给出错误印象。判据的价值只在于
   它被执行过——`python -m app.services.case_lab --write` 是重算入口。

本模块不产生任何「某股是庄股」的结论。`scan` 输出的是**结构异常候选**，
排序只看命中了多少种不同结构，不看历史收益——用全样本收益给当期打分，
等于把未来信息放回过去。
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import random
import re
import threading
from datetime import date as date_cls
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_now, cn_today
from app.services.daily_panel import attach_returns, load_panel
from app.services.trading_days import kline_trading_days

logger = logging.getLogger(__name__)

# ===== 参数 =====

# 统计窗口：6 年覆盖 2021 抱团、2022–2023 存量博弈、2024 微盘股流动性危机、
# 2025–2026 当前结构。再往前（2015 杠杆牛/股灾）交易制度差异过大，
# 混在一起算会让「手法」的统计变成制度变迁的统计。
DEFAULT_STATS_YEARS = 6
# 扫描回看窗口：60 个交易日 ≈ 一个完整的拉升-派发周期
DEFAULT_SCAN_LOOKBACK = 60
# 扫描面板需要的前置历史：250 日分位 + 60 日回看 + 余量
_SCAN_WARMUP_DAYS = 320
# 判据命中的最小样本：低于这个数不进统计表（n=12 的均值没有意义）
MIN_HITS = 30
# 可成交口径：T+1 开盘涨幅 ≥ 9.8% 视为一字板买不进（主板 10% 涨停留 20bp 容差）。
# 与 table_game 同一阈值，两处含义相同：剔除「看到了但买不到」的样本。
LIMIT_GAP = 0.098

_CACHE_LOCK = threading.Lock()
# 缓存**结果**而不是面板：scan 一份 30 行、dissect 一份几百根 bar。
# 面板是 300–500MB 级的中间态，挂在进程里会随标签页数量线性吃内存。
_SCAN_CACHE: dict[tuple, dict] = {}
_DISSECT_CACHE: dict[tuple, dict] = {}
_RESULT_CACHE_MAX = 12
_STATS_CACHE: dict[str, dict] = {}

STATS_FILENAME = "fingerprint_stats.json"


# ===== 判据库 =====
#
# 每条判据 = 语义 + 参数 + 条件 + 反例（caveat）。
# `build` 只允许读 params，不许在表达式里写死阈值——展示文本用同一份 params
# 格式化，两边一起漂移就不会出现「文档说 2%，代码是 3%」。


def _fp_blowoff_stall(p: dict) -> pl.Expr:
    return (
        (pl.col("amount") >= pl.col(f"amount_max{p['win']}_prev"))
        & (pl.col("ret") < p["max_ret"])
        & (pl.col("close_pos") < p["max_close_pos"])
    )


def _fp_low_turnover_new_high(p: dict) -> pl.Expr:
    return (
        (pl.col("close") >= pl.col(f"high_max{p['win']}_prev"))
        & (pl.col("turnover_rate") <= pl.col(f"turn_p{p['q']}_{p['win']}"))
    )


def _fp_wash_high_amp(p: dict) -> pl.Expr:
    return (
        (pl.col("amp") >= pl.col(f"amp_p{p['amp_q']}_{p['win']}"))
        & (pl.col("turnover_rate") >= pl.col(f"turn_p{p['turn_q']}_{p['win']}"))
        & (pl.col("ret").abs() < p["max_abs_ret"])
    )


def _fp_limit_up_gap_down(p: dict) -> pl.Expr:
    return (
        (pl.col("consecutive_limit_ups").shift(1).over("symbol") >= p["min_boards"])
        & (pl.col("ret") <= -p["min_gap_down"])
        & (pl.col("amount") >= pl.col("amount").shift(1).over("symbol") * p["vol_mult"])
    )


def _fp_upper_shadow(p: dict) -> pl.Expr:
    return (
        (pl.col("upper_shadow") >= p["shadow_mult"] * pl.col("body"))
        & (pl.col("close") >= pl.col(f"high_max{p['win']}_prev") * p["near_high"])
        & (pl.col("turnover_rate") >= pl.col(f"turn_p{p['turn_q']}_{p['win']}"))
    )


FINGERPRINTS: list[dict[str, Any]] = [
    {
        "id": "blowoff_stall",
        "label": "天量滞涨",
        "family": "distribution",
        "thesis": "成交额创阶段新高却推不动价格，收盘又落在当日下半区——放量的钱没换来涨幅。",
        "params": {"win": 250, "max_ret": 0.02, "max_close_pos": 0.5},
        "rule": "成交额 ≥ 前 {win} 日最高 ∧ 当日涨幅 < {max_ret:.0%} ∧ 收盘位于当日振幅下 {max_close_pos:.0%}",
        "needs": ["daily"],
        "caveat": "除权、复牌、指数调仓带来的被动换手也会触发；单看一天没有意义，要看它出现在区间高位还是低位。",
        "build": _fp_blowoff_stall,
    },
    {
        "id": "low_turnover_new_high",
        "label": "缩量新高",
        "family": "lockup",
        "thesis": "价格创阶段新高，换手却落在极低分位——浮筹被锁住，可交易筹码很少。",
        "params": {"win": 250, "q": 20},
        "rule": "收盘 ≥ 前 {win} 日最高 ∧ 换手率 ≤ 前 {win} 日 {q}% 分位",
        "needs": ["daily"],
        "caveat": "小盘股天然换手低，缩量新高在流通盘极小的票上会长期成立；必须和成交额绝对值一起看。",
        "build": _fp_low_turnover_new_high,
    },
    {
        "id": "wash_high_amp",
        "label": "高换手对倒（代理）",
        "family": "wash",
        "thesis": "振幅与换手同时达到极值，但价格几乎没动——成交被人为放大，方向却没定。",
        "params": {"win": 250, "amp_q": 95, "turn_q": 95, "max_abs_ret": 0.03},
        "rule": "振幅 ≥ 前 {win} 日 {amp_q} 分位 ∧ 换手 ≥ 前 {win} 日 {turn_q} 分位 ∧ |涨跌幅| < {max_abs_ret:.0%}",
        "needs": ["daily"],
        "caveat": "这是代理变量，不是对倒的证据：重大消息落地当天的多空分歧同样长这样。只能当「值得看一眼」的线索。",
        "build": _fp_wash_high_amp,
    },
    {
        "id": "limit_up_gap_down",
        "label": "涨停次日闷杀",
        "family": "pump_dump",
        "thesis": "前一天还在涨停，次日直接低开且放量——接力盘被一次性交卸。",
        "params": {"min_boards": 1, "min_gap_down": 0.04, "vol_mult": 1.5},
        "rule": "前一日连板数 ≥ {min_boards} ∧ 当日跌幅 ≥ {min_gap_down:.0%} ∧ 成交额 ≥ 前一日 {vol_mult} 倍",
        "needs": ["daily"],
        "caveat": "大盘系统性下跌日会批量出现，必须和同日全市场跌幅中位数比较，不能把 beta 当手法。",
        "build": _fp_limit_up_gap_down,
    },
    {
        "id": "upper_shadow_near_high",
        "label": "高位长上影",
        "family": "distribution",
        "thesis": "价格已在阶段高位，盘中冲高被砸回，留下长上影且换手放大——上方有人持续交货。",
        "params": {"win": 250, "turn_q": 90, "shadow_mult": 2.0, "near_high": 0.9},
        "rule": "上影 ≥ 实体 × {shadow_mult:g} ∧ 收盘 ≥ 前 {win} 日最高 × {near_high} ∧ 换手 ≥ 前 {win} 日 {turn_q}% 分位",
        "needs": ["daily"],
        "caveat": "「上影 ≥ 2 倍实体」在低开高走的日子里也会成立，所以叠加了「接近阶段高点」与换手条件；仍会漏掉盘中诱多后收平的形态。",
        "build": _fp_upper_shadow,
    },
]


def _fp_index(fp_id: str) -> dict[str, Any]:
    for fp in FINGERPRINTS:
        if fp["id"] == fp_id:
            return fp
    raise KeyError(fp_id)


def fingerprint_catalog() -> list[dict[str, Any]]:
    """前端展示用的判据清单（不含可执行表达式）。"""
    out = []
    for fp in FINGERPRINTS:
        out.append({
            "id": fp["id"],
            "label": fp["label"],
            "family": fp["family"],
            "thesis": fp["thesis"],
            "rule": fp["rule"].format(**fp["params"]),
            "params": fp["params"],
            "needs": fp["needs"],
            "caveat": fp["caveat"],
        })
    return out


# ===== 面板与派生列 =====


def _attach_fingerprint_columns(df: pl.DataFrame) -> pl.DataFrame:
    """把判据需要的滚动列一次算齐，避免每条判据各算一遍 250 日窗口。"""
    if df.is_empty():
        return df
    wins = {fp["params"].get("win", 250) for fp in FINGERPRINTS}
    exprs: list[pl.Expr] = [
        (
            (pl.col("close") - pl.col("open")).abs()
            / (pl.col("close").shift(1).over("symbol") + 1e-12)
        ).alias("body"),
    ]
    for w in sorted(wins):
        exprs += [
            pl.col("amount").shift(1).rolling_max(w, min_samples=60).over("symbol")
            .alias(f"amount_max{w}_prev"),
            pl.col("high").shift(1).rolling_max(w, min_samples=60).over("symbol")
            .alias(f"high_max{w}_prev"),
        ]
    for w in sorted(wins):
        for q in (20, 90, 95):
            exprs.append(
                pl.col("turnover_rate").rolling_quantile(q / 100, "nearest", w, min_samples=60)
                .over("symbol").alias(f"turn_p{q}_{w}")
            )
    for w in sorted(wins):
        exprs.append(
            pl.col("amp").rolling_quantile(0.95, "nearest", w, min_samples=60)
            .over("symbol").alias(f"amp_p95_{w}")
        )
    return df.with_columns(exprs)


def _attach_exec_returns(df: pl.DataFrame) -> pl.DataFrame:
    """可成交前向收益：T+1 开盘买，T+1 / T+5 / T+20 收盘卖。

    为什么不用 close→close：A 股里最有「操纵特征」的形态（一字板、缩量新高）
    恰恰是次日开盘就涨停的形态，close→close 会把这些**买不进**的样本算成收益。
    那是 paper alpha，不是可执行收益。`table_game` 也用了同一约束。
    """
    if df.is_empty():
        return df
    eps = 1e-12
    return df.with_columns([
        (pl.col("open").shift(-1).over("symbol") / (pl.col("close") + eps) - 1).alias("d1_gap"),
        (
            pl.col("close").shift(-1).over("symbol")
            / (pl.col("open").shift(-1).over("symbol") + eps) - 1
        ).alias("x1"),
        (
            pl.col("close").shift(-5).over("symbol")
            / (pl.col("open").shift(-1).over("symbol") + eps) - 1
        ).alias("x5"),
        (
            pl.col("close").shift(-20).over("symbol")
            / (pl.col("open").shift(-1).over("symbol") + eps) - 1
        ).alias("x20"),
    ]).with_columns(
        (pl.col("d1_gap") < LIMIT_GAP).fill_null(False).alias("buyable")
    )


def _panel(data_dir: Path, end: date_cls, days: int) -> pl.DataFrame:
    """取 end 之前 days 个交易日的面板（含判据滚动所需的前置历史）。

    **不缓存面板**。一份 400 天 × 5500 只 × 30 列的面板约 300–500MB；把它挂在
    进程里，一个人打开两次页面就能把容器的内存预算吃掉。这里缓存的是**结果**
    （`_SCAN_CACHE` / `_DISSECT_CACHE`，几十 KB 到几百 KB），不是中间态。
    重算一次的代价是 3 秒，换来的是不会随标签页数量增长的内存曲线。
    """
    all_days = [d for d in kline_trading_days(Path(data_dir)) if d <= end]
    if not all_days:
        return pl.DataFrame()
    window = all_days[-days:]
    df = load_panel(Path(data_dir), window[0], window[-1])
    if df.is_empty():
        return df
    return _attach_exec_returns(_attach_fingerprint_columns(attach_returns(df)))


# ===== 统计检验 =====


def _bucket_expr(col: str, n: int) -> pl.Expr:
    """按当日横截面排名切等数量桶。不用 qcut：涨停日大量重复值会把桶压塌。"""
    return (
        ((pl.col(col).rank("ordinal").over("date") - 1) / pl.len().over("date") * n)
        .floor().clip(0, n - 1).cast(pl.Int32)
    )


def _with_matched(df: pl.DataFrame) -> pl.DataFrame:
    """匹配桶：日期 × 20 日动量五分位 × 成交额规模五分位。

    规模用**当时的成交额**（`amount_ma20`）而不是最新流通股本：后者含增发、
    解禁带来的前视，用它分桶等于让 2021 年的样本知道 2026 年的股本。

    不控制换手率：多数判据本身就用换手定义，再控制它会把要测的效应一起
    抹掉（over-control）。

    基准只统计**可买入**样本的收益（`x*_b`，买不进的位置为 null，均值自动跳过）：
    对照组若是「随便买一只同桶的票」，那一只也必须买得进。
    """
    df = df.with_columns([
        pl.when(pl.col("buyable")).then(pl.col("x1")).alias("x1_b"),
        pl.when(pl.col("buyable")).then(pl.col("x5")).alias("x5_b"),
        pl.when(pl.col("buyable")).then(pl.col("x20")).alias("x20_b"),
    ]).with_columns([
        _bucket_expr("mom20", 5).alias("mom_b"),
        _bucket_expr("amount_ma20", 5).alias("size_b"),
    ]).with_columns([
        pl.col("x1_b").mean().over(["date", "mom_b", "size_b"]).alias("bench_x1"),
        pl.col("x5_b").mean().over(["date", "mom_b", "size_b"]).alias("bench_x5"),
        pl.col("x20_b").mean().over(["date", "mom_b", "size_b"]).alias("bench_x20"),
    ]).with_columns([
        (pl.col("x1_b") - pl.col("bench_x1")).alias("exc1"),
        (pl.col("x5_b") - pl.col("bench_x5")).alias("exc5"),
        (pl.col("x20_b") - pl.col("bench_x20")).alias("exc20"),
    ])
    return df


def _day_series_stats(df: pl.DataFrame, col: str) -> dict[str, Any]:
    """先按日聚合再算统计量。

    同一交易日命中的几十只票共享同一段市场 beta，不是独立样本；fwd20 还在
    时间上逐日重叠。两种相关性都会把 t 抬高。按日取均值后，横截面相关被
    压掉；重叠仍在，所以 t 只作参考——页面必须显示 `overlap_warning`，
    并且同时给出中位数与胜率（不依赖分布假设，也不会被少数极端值拉走）。
    """
    s = df.group_by("date").agg(pl.col(col).mean().alias("v")).sort("date")["v"].drop_nulls()
    n = s.len()
    if n < 2:
        return {"n_days": n, "mean_bp": None, "median_bp": None, "win_rate": None, "t": None}
    mean = float(s.mean() or 0.0)
    std = float(s.std() or 0.0)
    t = mean / (std / math.sqrt(n)) if std > 0 else None
    return {
        "n_days": n,
        "mean_bp": round(mean * 1e4, 1),
        "median_bp": round(float(s.median() or 0.0) * 1e4, 1),
        "win_rate": round(float((s > 0).mean() or 0.0), 3),
        "t": round(t, 2) if t is not None else None,
    }


def compute_stats(data_dir: Path, years: int = DEFAULT_STATS_YEARS) -> dict[str, Any]:
    """全量重算每条判据的历史统计。入口：`python -m app.services.case_lab --write`。"""
    end = cn_today()
    days = int(years * 245) + _SCAN_WARMUP_DAYS
    df = _panel(Path(data_dir), end, days)
    if df.is_empty():
        return {"state": "no_data", "as_of": end.isoformat(), "years": years, "items": []}
    df = _with_matched(df)
    rows_total = df.height
    bars = df.select(pl.col("date").n_unique()).item()

    items: list[dict[str, Any]] = []
    for fp in FINGERPRINTS:
        hit_all = df.filter(fp["build"](fp["params"]))
        n_raw = hit_all.height
        # 命中之后还要买得进：一字板/停牌不计入可执行收益
        hit = hit_all.filter(pl.col("buyable")).drop_nulls(["exc1"])
        n_rows = hit.height
        buyable_ratio = round(n_rows / n_raw, 4) if n_raw else None
        if n_rows < MIN_HITS:
            items.append({
                "id": fp["id"], "label": fp["label"], "family": fp["family"],
                "n_raw": n_raw, "n_rows": n_rows, "n_days": 0,
                "buyable_ratio": buyable_ratio,
                "hit_rate": round(n_raw / rows_total, 6) if rows_total else 0,
                "fwd": {}, "state": "insufficient",
            })
            continue
        fwd = {}
        for k in ("1", "5", "20"):
            fwd[k] = _day_series_stats(hit, f"exc{k}")
        items.append({
            "id": fp["id"],
            "label": fp["label"],
            "family": fp["family"],
            "n_raw": n_raw,
            "n_rows": n_rows,
            "n_days": fwd["1"]["n_days"],
            "buyable_ratio": buyable_ratio,
            "hit_rate": round(n_raw / rows_total, 6),
            "fwd": fwd,
            "state": "ok" if fwd["1"]["n_days"] >= 20 else "insufficient",
        })
    return {
        "state": "ok",
        # as_of = **数据截止日**（面板里最后一根 bar），不是「今天」。
        # 用今天会让 --check 在周末/停更时永远报 stale —— 一条永远红的判据
        # 等于没有判据。计算当天另记 computed_on。
        "as_of": str(df["date"].max()),
        "computed_on": cn_today().isoformat(),
        "years": years,
        "window_days": days,
        "bars": bars,
        "rows": rows_total,
        "min_hits": MIN_HITS,
        "generated_at": cn_now().isoformat(timespec="seconds"),
        "items": items,
    }


def stats_path(data_dir: Path) -> Path:
    return Path(data_dir) / "case_lab" / STATS_FILENAME


def read_stats(data_dir: Path) -> dict[str, Any]:
    p = stats_path(data_dir)
    if not p.exists():
        return {"state": "missing", "items": [], "as_of": None, "years": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("判据统计文件读取失败: %s", exc)
        return {"state": "corrupt", "items": [], "as_of": None, "years": None}


def write_stats(data_dir: Path, payload: dict[str, Any]) -> Path:
    p = stats_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    with _CACHE_LOCK:
        _STATS_CACHE.pop(str(data_dir), None)
    return p


def get_stats(data_dir: Path, allow_compute: bool = True) -> dict[str, Any]:
    """读盘上统计；不存在时同步算一次（首次几秒），之后只读。"""
    key = str(data_dir)
    with _CACHE_LOCK:
        hit = _STATS_CACHE.get(key)
    if hit is not None:
        return hit
    payload = read_stats(Path(data_dir))
    if payload.get("state") in ("missing", "corrupt") and allow_compute:
        payload = compute_stats(Path(data_dir))
        with contextlib.suppress(OSError):
            write_stats(Path(data_dir), payload)
    with _CACHE_LOCK:
        _STATS_CACHE[key] = payload
    return payload


# ===== 扫描：结构异常候选 =====


def scan(
    data_dir: Path,
    as_of: date_cls | None = None,
    lookback: int = DEFAULT_SCAN_LOOKBACK,
    limit: int = 30,
) -> dict[str, Any]:
    """最近 lookback 日内，哪些票同时出现了多种异常结构。

    打分只看**命中了几种不同判据**，不看历史收益——后者会用到未来样本。
    这是「值得打开看一眼」的列表，不是推荐，也不是指控。
    """
    end = as_of or cn_today()
    lookback = max(10, min(lookback, 120))
    key = (str(data_dir), end.isoformat(), lookback, limit)
    with _CACHE_LOCK:
        hit = _SCAN_CACHE.get(key)
    if hit is not None:
        return hit
    payload = _scan_impl(Path(data_dir), end, lookback, limit)
    with _CACHE_LOCK:
        if len(_SCAN_CACHE) >= _RESULT_CACHE_MAX:
            _SCAN_CACHE.clear()
        _SCAN_CACHE[key] = payload
    return payload


def _scan_impl(data_dir: Path, end: date_cls, lookback: int, limit: int) -> dict[str, Any]:
    df = _panel(data_dir, end, lookback + _SCAN_WARMUP_DAYS)
    if df.is_empty():
        return {"state": "no_data", "as_of": end.isoformat(), "names": [], "counts": {}}

    days = sorted(df["date"].unique().to_list())
    if len(days) <= lookback:
        return {"state": "no_data", "as_of": end.isoformat(), "names": [], "counts": {}}
    start = days[-lookback]
    recent = df.filter(pl.col("date") >= start)

    # 每条判据一列布尔，再按 symbol 聚合命中次数与最近命中日
    hit_cols = []
    for fp in FINGERPRINTS:
        hit_cols.append(fp["build"](fp["params"]).alias(f"h_{fp['id']}"))
    recent = recent.with_columns(hit_cols)

    aggs = [pl.col(f"h_{fp['id']}").sum().alias(f"n_{fp['id']}") for fp in FINGERPRINTS]
    grouped = (
        recent.group_by("symbol")
        .agg([
            pl.len().alias("bars"),
            pl.col("date").max().alias("last_date"),
            pl.col("ret").sum().alias("cum_ret"),
            pl.col("amount").sum().alias("amount_sum"),
            pl.col("turnover_rate").mean().alias("turn_avg"),
            *aggs,
        ])
        .filter(pl.col("bars") >= lookback // 2)
    )
    # 命中种类数：把每条判据的「至少命中一次」加起来
    kinds = pl.sum_horizontal(
        [(pl.col(f"n_{fp['id']}") > 0).cast(pl.Int32) for fp in FINGERPRINTS]
    )
    grouped = grouped.with_columns(kinds.alias("kinds"))
    grouped = grouped.filter(pl.col("kinds") >= 2)
    total = grouped.height
    grouped = grouped.sort(
        ["kinds", "cum_ret"], descending=[True, True]
    ).head(limit)

    names = _name_map(Path(data_dir))
    out = []
    for r in grouped.iter_rows(named=True):
        hit_detail = [
            {"id": fp["id"], "label": fp["label"], "count": int(r[f"n_{fp['id']}"] or 0)}
            for fp in FINGERPRINTS if (r[f"n_{fp['id']}"] or 0) > 0
        ]
        out.append({
            "symbol": r["symbol"],
            "name": names.get(r["symbol"]),
            "kinds": int(r["kinds"]),
            "hits": hit_detail,
            "bars": int(r["bars"]),
            "last_date": str(r["last_date"]),
            "cum_ret_pct": round(float(r["cum_ret"] or 0) * 100, 2),
            "turn_avg_pct": round(float(r["turn_avg"] or 0), 2),
            "amount_yi": round(float(r["amount_sum"] or 0) / 1e8, 2),
        })
    return {
        "state": "ok",
        "as_of": end.isoformat(),
        "start": str(start),
        "lookback": lookback,
        "counts": {"candidates": total, "shown": len(out)},
        "names": out,
        "disclaimer": (
            "本列表按「命中了多少种异常结构」排序，只用于历史学习与复盘。"
            "它是统计特征，不构成对任何主体操纵的指控，也不是交易建议。"
        ),
    }


def _name_map(data_dir: Path) -> dict[str, str]:
    p = Path(data_dir) / "instruments" / "instruments.parquet"
    if not p.exists():
        return {}
    try:
        df = pl.read_parquet(p, columns=["symbol", "name"])
    except (OSError, pl.exceptions.PolarsError):
        return {}
    return {s: n for s, n in zip(df["symbol"].to_list(), df["name"].to_list(), strict=False)}


# ===== 个股解剖 =====


def _events_of(data_dir: Path, symbol: str, start: date_cls, end: date_cls) -> list[dict]:
    """区间内该票的龙虎榜记录（只取 1 日榜，3 日榜是累计值、不是同一条时间轴）。"""
    base = Path(data_dir) / "dragon_tiger"
    if not base.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(base.glob("date=*.json")):
        tag = p.stem[5:]
        try:
            d = date_cls.fromisoformat(tag)
        except ValueError:
            continue
        if d < start or d > end:
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for it in (payload.get("all") or {}).get("stock_items") or []:
            if it.get("thscode") != symbol or it.get("range_days") != 1:
                continue
            seats = []
            for seat in (payload.get("hot_money") or {}).get("hot_money_items") or []:
                for r in seat.get("rows") or []:
                    if r.get("thscode") == symbol:
                        seats.append({
                            "seat": seat.get("name"),
                            "net_yi": round(float(r.get("hot_money_item_net_value") or 0) / 1e8, 2),
                        })
            out.append({
                "date": payload.get("trade_date") or tag,
                "net_yi": round(float(it.get("net_value") or 0) / 1e8, 2),
                "org_net_yi": round(float(it.get("org_net_value") or 0) / 1e8, 2),
                "hot_net_yi": round(float(it.get("hot_money_net_value") or 0) / 1e8, 2),
                "chg_pct": round(float(it.get("change") or 0) * 100, 2),
                "seats": seats,
            })
    return out


def dissect(
    data_dir: Path,
    symbol: str,
    start: date_cls,
    end: date_cls,
) -> dict[str, Any]:
    """个股在 [start, end] 的痕迹剖面：逐日判据命中 + 龙虎榜 + 候选拐点。

    刻意**不**输出「吸筹/拉升/派发」的自动分段。分段是判断，不是观测；
    机器只报痕迹与候选拐点，分段留给案例里的 `phases` 由人写。
    """
    symbol = symbol.strip().upper()
    if end < start:
        start, end = end, start
    key = (str(data_dir), symbol, start.isoformat(), end.isoformat())
    with _CACHE_LOCK:
        hit = _DISSECT_CACHE.get(key)
    if hit is not None:
        return hit
    payload = _dissect_impl(Path(data_dir), symbol, start, end)
    with _CACHE_LOCK:
        if len(_DISSECT_CACHE) >= _RESULT_CACHE_MAX:
            _DISSECT_CACHE.clear()
        _DISSECT_CACHE[key] = payload
    return payload


def _dissect_impl(
    data_dir: Path,
    symbol: str,
    start: date_cls,
    end: date_cls,
) -> dict[str, Any]:
    span = (end - start).days
    # 上限 600 个交易日：学习用的区间很少超过两年半；再长的窗口只会让
    # 单次请求多读 200 万行，而页面根本画不下那么多根蜡烛。
    days_needed = min(max(int(span * 0.75) + _SCAN_WARMUP_DAYS, 200), 600)
    df = _panel(data_dir, end, days_needed)
    if df.is_empty():
        return {"state": "no_data", "symbol": symbol, "bars": [], "hits": [], "events": []}
    one = df.filter(pl.col("symbol") == symbol).sort("date")
    if one.is_empty():
        return {"state": "not_in_universe", "symbol": symbol, "bars": [], "hits": [], "events": []}

    win = one.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    if win.is_empty():
        return {"state": "no_data", "symbol": symbol, "bars": [], "hits": [], "events": []}

    hit_exprs = [fp["build"](fp["params"]).alias(fp["id"]) for fp in FINGERPRINTS]
    win = win.with_columns(hit_exprs)

    bars = []
    for r in win.iter_rows(named=True):
        bars.append({
            "date": str(r["date"]),
            "open": round(float(r["open"]), 3),
            "high": round(float(r["high"]), 3),
            "low": round(float(r["low"]), 3),
            "close": round(float(r["close"]), 3),
            "ret_pct": round(float(r["ret"] or 0) * 100, 2),
            "turn_pct": round(float(r["turnover_rate"] or 0), 2),
            "amount_yi": round(float(r["amount"] or 0) / 1e8, 2),
            "amp_pct": round(float(r["amp"] or 0) * 100, 2),
            "boards": int(r["consecutive_limit_ups"] or 0),
        })

    hits = []
    for fp in FINGERPRINTS:
        sub = win.filter(pl.col(fp["id"])).select(["date", "ret", "turnover_rate"])
        if sub.is_empty():
            continue
        hits.append({
            "id": fp["id"],
            "label": fp["label"],
            "family": fp["family"],
            "dates": [str(d) for d in sub["date"].to_list()],
            "n": sub.height,
        })

    # 候选拐点：5 日成交额均值相对前 20 日的突变 + 区间极值日。
    # 明确标注为「候选」——它只是提示人从哪一天开始读，不是分段结论。
    pivot = _pivot_hints(win)
    names = _name_map(Path(data_dir))
    return {
        "state": "ok",
        "symbol": symbol,
        "name": names.get(symbol),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bars": bars,
        "hits": hits,
        "events": _events_of(Path(data_dir), symbol, start, end),
        "pivots": pivot,
        "summary": _range_summary(win),
    }


def _range_summary(win: pl.DataFrame) -> dict[str, Any]:
    first, last = win.head(1), win.tail(1)
    high = float(win["high"].max() or 0)
    low = float(win["low"].min() or 0)
    close = float(last["close"][0])
    return {
        "bars": win.height,
        "range_ret_pct": round((close / float(first["close"][0]) - 1) * 100, 2),
        "max_drawdown_pct": round((close / high - 1) * 100, 2) if high else None,
        "high": round(high, 3),
        "low": round(low, 3),
        "turn_median_pct": round(float(win["turnover_rate"].median() or 0), 2),
        "amount_yi_total": round(float(win["amount"].sum() or 0) / 1e8, 2),
    }


def _pivot_hints(win: pl.DataFrame) -> list[dict[str, Any]]:
    """成交额 5 日均值相对前 20 日的倍数 > 2 的日期，附当日涨跌幅。

    只作为「从哪天开始读」的提示。倍数阈值 2 是经验值，写在这里而不进判据库，
    因为它不参与任何统计检验。
    """
    if win.height < 30:
        return []
    s = win.with_columns([
        pl.col("amount").rolling_mean(5, min_samples=3).alias("a5"),
        pl.col("amount").shift(5).rolling_mean(20, min_samples=10).alias("a20_prev"),
    ]).with_columns([
        (pl.col("a5") / (pl.col("a20_prev") + 1e-12)).alias("ratio"),
    ]).filter(pl.col("ratio") >= 2.0)
    return [
        {
            "date": str(r["date"]),
            "amount_ratio": round(float(r["ratio"]), 2),
            "ret_pct": round(float(r["ret"] or 0) * 100, 2),
        }
        for r in s.iter_rows(named=True)
    ][:20]


# ===== 案例库 =====

ACTOR_TYPES = ("hot_money", "cap_mgmt", "dealer", "institution", "unknown")
CASE_STATUS = ("penalized", "investigated", "alleged", "trace_only")
EVIDENCE_KINDS = ("penalty", "inquiry", "filing", "leak", "media", "research", "other")
RELIABILITY = ("official", "media", "hearsay", "inferred")

_ID_SAFE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def cases_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "case_lab" / "cases"


def _case_path(data_dir: Path, case_id: str) -> Path:
    """案例 id → 文件路径。id 只允许安全字符，防止路径穿越。"""
    if not _ID_SAFE.match(case_id):
        raise ValueError(f"非法案例 id: {case_id!r}")
    return cases_dir(data_dir) / f"{case_id}.json"


def list_cases(data_dir: Path) -> list[dict[str, Any]]:
    d = cases_dir(data_dir)
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "id": c.get("id") or p.stem,
            "symbol": c.get("symbol"),
            "name": c.get("name"),
            "title": c.get("title"),
            "actor_type": c.get("actor_type"),
            "status": c.get("status"),
            "start": c.get("start"),
            "end": c.get("end"),
            "updated_at": c.get("updated_at"),
            "evidence_count": len(c.get("evidence") or []),
            "playbook": c.get("playbook") or [],
        })
    out.sort(key=lambda x: (x.get("updated_at") or ""), reverse=True)
    return out


def get_case(data_dir: Path, case_id: str) -> dict[str, Any] | None:
    p = _case_path(data_dir, case_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_case(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """新建或更新。id 缺失时生成；更新时保留 created_at。"""
    now = cn_now().isoformat(timespec="seconds")
    case_id = (payload.get("id") or "").strip()
    existing = get_case(data_dir, case_id) if case_id else None
    if not case_id:
        case_id = f"c-{cn_now().strftime('%Y%m%d')}-{random.randint(0x1000, 0xFFFF):04x}"
    if existing is None:
        existing = {}
    doc = {
        "id": case_id,
        "symbol": (payload.get("symbol") or "").strip().upper(),
        "name": payload.get("name"),
        "title": (payload.get("title") or "").strip(),
        "actor_type": payload.get("actor_type") or "unknown",
        "status": payload.get("status") or "trace_only",
        "start": payload.get("start"),
        "end": payload.get("end"),
        "thesis": payload.get("thesis") or "",
        "evidence": payload.get("evidence") or [],
        "phases": payload.get("phases") or [],
        "playbook": payload.get("playbook") or [],
        "lessons": payload.get("lessons") or "",
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    if not doc["symbol"]:
        raise ValueError("缺少 symbol")
    d = cases_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = _case_path(data_dir, case_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return doc


def delete_case(data_dir: Path, case_id: str) -> bool:
    p = _case_path(data_dir, case_id)
    if not p.exists():
        return False
    p.unlink()
    return True


# ===== 叙事与痕迹对账 =====


def reconcile(data_dir: Path, case_id: str, tol_days: int = 3) -> dict[str, Any]:
    """把案例里的每条证据，与同期本地痕迹对齐。

    `traced` 表示「该日附近能看到对应结构」，`untraced` 表示「看不到」。
    untraced **不等于虚假**：本地只有日线与 2025-11 起的龙虎榜，公告、大宗、
    股东名册都不在仓库里——看不到是数据边界，不是事实判断。
    """
    case = get_case(data_dir, case_id)
    if case is None:
        return {"state": "not_found"}
    symbol = case.get("symbol")
    start, end = case.get("start"), case.get("end")
    if not (symbol and start and end):
        return {"state": "incomplete", "case": case, "items": []}
    d0, d1 = date_cls.fromisoformat(start), date_cls.fromisoformat(end)
    dis = dissect(data_dir, symbol, d0, d1)
    if dis.get("state") != "ok":
        return {"state": dis.get("state"), "case": case, "items": []}

    hit_by_date: dict[str, list[str]] = {}
    for h in dis["hits"]:
        for dt in h["dates"]:
            hit_by_date.setdefault(dt, []).append(h["label"])
    bars = {b["date"]: b for b in dis["bars"]}
    events = {e["date"]: e for e in dis["events"]}
    bar_dates = sorted(bars)

    items = []
    for ev in case.get("evidence") or []:
        dt = ev.get("date")
        if not dt:
            items.append({"evidence": ev, "support": "undated", "within": None, "traces": []})
            continue
        # 容忍窗口：证据日期附近 tol_days 个交易日内的痕迹
        near = [d for d in bar_dates if abs((date_cls.fromisoformat(d) - date_cls.fromisoformat(dt)).days) <= tol_days]
        traces = []
        for d in near:
            labels = hit_by_date.get(d) or []
            if labels:
                traces.append({"date": d, "kind": "fingerprint", "labels": labels})
            if d in events:
                traces.append({"date": d, "kind": "lhb", "labels": [f"龙虎榜净额 {events[d]['net_yi']} 亿"]})
            b = bars[d]
            if abs(b["ret_pct"]) >= 7:
                traces.append({"date": d, "kind": "move", "labels": [f"当日 {b['ret_pct']:+.2f}%"]})
        items.append({
            "evidence": ev,
            "support": "traced" if traces else "untraced",
            "within": tol_days,
            "traces": traces[:6],
        })
    traced = sum(1 for i in items if i["support"] == "traced")
    return {
        "state": "ok",
        "case": case,
        "symbol": symbol,
        "start": start,
        "end": end,
        "items": items,
        "counts": {"total": len(items), "traced": traced, "untraced": len(items) - traced},
        "note": (
            "untraced 只说明本地数据在该日附近看不到对应结构。"
            "公告、大宗交易、股东名册不在本仓库，缺数据不等于叙事为假。"
        ),
    }


# ===== CLI =====


def _main() -> int:  # pragma: no cover - 手工入口
    import argparse

    from app.config import settings

    ap = argparse.ArgumentParser(description="操盘档案判据统计")
    ap.add_argument("--write", action="store_true", help="重算并落盘")
    ap.add_argument("--check", action="store_true", help="校验盘上统计与最新数据是否同步")
    ap.add_argument("--years", type=int, default=DEFAULT_STATS_YEARS)
    args = ap.parse_args()
    data_dir = Path(settings.data_dir)

    if args.check:
        payload = read_stats(data_dir)
        if payload.get("state") != "ok":
            print(f"统计不可用: {payload.get('state')}")
            return 1
        days = [d for d in kline_trading_days(data_dir)]
        latest = days[-1].isoformat() if days else None
        stale = payload.get("as_of") != latest
        print(f"stats.as_of={payload.get('as_of')} latest_bar={latest} stale={stale}")
        for it in payload.get("items") or []:
            f1 = (it.get("fwd") or {}).get("1") or {}
            print(f"  {it['id']:<24} n_days={f1.get('n_days')} exc1={f1.get('mean_bp')}bp t={f1.get('t')}")
        return 0 if not stale else 1

    payload = compute_stats(data_dir, args.years)
    if args.write:
        p = write_stats(data_dir, payload)
        print(f"written {p}")
    print(json.dumps({k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False))
    for it in payload.get("items") or []:
        f1 = (it.get("fwd") or {}).get("1") or {}
        print(f"  {it['id']:<24} n_rows={it['n_rows']} n_days={f1.get('n_days')} "
              f"exc1={f1.get('mean_bp')}bp t={f1.get('t')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
