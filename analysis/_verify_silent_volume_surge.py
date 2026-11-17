"""沉默小盘股的「爆量」信号：事前可交易性检验。

背景：前几轮已证伪「沉默本身有 alpha」和「动量仍强的沉默有 alpha」。
本脚本挖掘一个新方向——沉默小盘股某日**突然爆量**（成交量相对自身近期均值异常放大），
是否是可交易的前瞻信号？

逻辑假设：silent = 低关注度票，某日爆量 = 有资金突然进场 = 关注度突变。
若为「启动」信号，爆量 + 上涨应预示后续正收益；若为「出货」信号，爆量 + 下跌应预示负收益。

关键纪律（区别于前几轮，直接做 silent 内部对照，避免小盘溢价污染）：
1. 防前视：量比/等级/方向都算 T 日收盘，T+1 开盘买入。
2. 量比：vol_ratio_5d = volume[T] / mean(volume[T-5..T-1])，对齐本地 signal_volume_surge(≥2) 口径。
3. 对照基准 = silent 内部（同是沉默小盘，天然可比），不用全市场中位数。
4. 扣成本 + 剔除停牌/低成交额。

数据：kline_daily_enriched（2005–2026）。open/close 都已是前复权价，直接相除。
输出：data/.scratch/silent_volume_surge.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

TABLE = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch"

CORE_MIN_AMOUNT = 0.90
ACTIVE_MIN_AMOUNT = 0.50
ACTIVE_MIN_TURNOVER = 0.90
SILENT_MAX_AMOUNT = 0.10
SILENT_MAX_TURNOVER = 0.10

HORIZONS = (5, 20, 60)
VOL_LOOKBACK = 5       # 量比回看窗口（日均量）
VOL_SURGE = 2.0        # 爆量阈值（量比 ≥ 2）

STAMP_TAX = 0.0010
COMMISSION = 0.00025
MIN_AMOUNT = 20_000_000


def block_se(x: np.ndarray, block: int, reps: int = 1000, seed: int = 0) -> float:
    n = len(x)
    if n < block:
        block = max(1, n)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]) % n
        means[i] = x[idx.ravel()[:n]].mean()
    return float(means.std(ddof=1))


def load_history() -> pl.DataFrame:
    files = sorted(str(p) for p in TABLE.glob("date=*/part.parquet"))
    cols = ["symbol", "date", "open", "close", "volume", "amount", "turnover_rate"]
    return (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(cols)
        .collect()
        .sort(["symbol", "date"])
    )


def assign_level(d: pl.DataFrame) -> pl.DataFrame:
    n = d.height
    if n < 2:
        return d
    a = (pl.col("amount").rank("ordinal") - 1) / (n - 1)
    t = (pl.col("turnover_rate").rank("ordinal") - 1) / (n - 1)
    level = (
        pl.when(a >= CORE_MIN_AMOUNT).then(pl.lit("core"))
        .when((a >= ACTIVE_MIN_AMOUNT) | (t >= ACTIVE_MIN_TURNOVER)).then(pl.lit("active"))
        .when((a < SILENT_MAX_AMOUNT) & (t < SILENT_MAX_TURNOVER)).then(pl.lit("silent"))
        .otherwise(pl.lit("background"))
    )
    return d.with_columns(level.alias("level"))


def enrich(df: pl.DataFrame) -> pl.DataFrame:
    """量比 + 日收益 + 停牌标记。全部只用当日及以前信息。"""
    # 过去 5 日均量（不含当日）
    df = df.with_columns(
        pl.col("volume").shift(1).rolling_mean(window_size=VOL_LOOKBACK, min_samples=VOL_LOOKBACK)
        .over("symbol").alias("vol_mean_5d")
    )
    df = df.with_columns((pl.col("volume") / pl.col("vol_mean_5d")).alias("vol_ratio_5d"))
    # 当日收益（用前复权 close）
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1")
    )
    # 停牌标记：下一交易日自然日间隔 > 7 天视为 T+1 停牌
    df = df.with_columns(
        pl.col("date").diff(-1).over("symbol").dt.total_days().alias("_next_gap")
    )
    return df


def compute_fwd_returns(df: pl.DataFrame) -> pl.DataFrame:
    """T+1 开盘买入，持有 N 日 T+1+N 收盘卖出，扣成本。"""
    cost = STAMP_TAX + 2 * COMMISSION
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col("close").shift(-(h + 1)).over("symbol")
             / pl.col("open").shift(-1).over("symbol") - 1.0).alias(f"gross{h}")
        )
        df = df.with_columns((pl.col(f"gross{h}") - cost).alias(f"net{h}"))
    return df


def tradable_mask(df: pl.DataFrame) -> pl.Expr:
    """可交易约束：成交额 ≥ 阈值 且 T+1 不停牌。"""
    return (
        (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )


def _report_group(df: pl.DataFrame, h: int) -> dict:
    """按日聚合某组的净收益统计（同日多票共享 beta，不独立）。"""
    seg = df.filter(pl.col(f"net{h}").is_not_null())
    if seg.height == 0:
        return {"horizon": h, "n_trades": 0, "net_bp": None, "n_days": 0, "t": None, "win_rate": None}
    daily = seg.group_by("date").agg(pl.col(f"net{h}").mean().alias("e"))
    mean = float(daily["e"].mean())
    se = block_se(daily["e"].to_numpy(), block=2 * h)
    t = mean / se if se > 0 else float("nan")
    win = float((seg[f"net{h}"] > 0).mean())
    return {
        "horizon": h, "n_trades": int(seg.height), "n_days": int(daily.height),
        "net_bp": round(mean * 1e4, 1), "se_bp": round(se * 1e4, 1),
        "t": round(t, 2), "win_rate": round(win, 4),
    }


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    print(f"  {df.height:,} 行  {df['date'].min()} → {df['date'].max()}", flush=True)
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("turnover_rate") > 0)
        & (pl.col("open") > 0) & (pl.col("volume") > 0)
    )
    print("逐日算等级…", flush=True)
    df = df.group_by("date", maintain_order=True).map_groups(assign_level)
    print("加量比/收益/停牌标记…", flush=True)
    df = enrich(df)
    print("算未来持有收益…", flush=True)
    df = compute_fwd_returns(df)

    # 沉默池（可交易）
    silent = df.filter((pl.col("level") == "silent") & tradable_mask(df))

    # 爆量信号：量比 >= 2（在 silent 内部）
    surge = silent.filter(pl.col("vol_ratio_5d") >= VOL_SURGE)
    no_surge = silent.filter(pl.col("vol_ratio_5d") < VOL_SURGE)

    # 方向区分：爆量当日涨 vs 跌
    surge_up = surge.filter(pl.col("ret1") > 0)
    surge_dn = surge.filter(pl.col("ret1") <= 0)

    print(f"\n沉默池(可交易): {silent.height:,}  爆量: {surge.height:,} "
          f"({surge.height/silent.height*100:.1f}%)  未爆量: {no_surge.height:,}", flush=True)
    print(f"爆量中: 上涨 {surge_up.height:,} / 下跌 {surge_dn.height:,}", flush=True)

    report = {"n_silent": int(silent.height), "n_surge": int(surge.height),
              "vol_threshold": VOL_SURGE, "vol_lookback": VOL_LOOKBACK}

    # 1. 爆量 vs 未爆量（silent 内部，拆出「爆量」的增量）
    print("\n=== 沉默池内部：爆量 vs 未爆量（净收益）===", flush=True)
    report["surge_vs_nosurge"] = {}
    for h in HORIZONS:
        a = _report_group(surge, h)
        b = _report_group(no_surge, h)
        diff_bp = (a["net_bp"] - b["net_bp"]) if (a["net_bp"] is not None and b["net_bp"] is not None) else None
        report["surge_vs_nosurge"][str(h)] = {"surge": a, "no_surge": b, "diff_bp": diff_bp}
        print(f"  H={h:>2}  爆量净收益 {a['net_bp']}bp(t={a['t']})  vs  未爆量 {b['net_bp']}bp(t={b['t']})  "
              f"差 {diff_bp}bp", flush=True)

    # 2. 爆量上涨 vs 爆量下跌（方向区分）
    print("\n=== 爆量内部：上涨 vs 下跌（方向区分）===", flush=True)
    report["surge_direction"] = {}
    for h in HORIZONS:
        a = _report_group(surge_up, h)
        b = _report_group(surge_dn, h)
        diff_bp = (a["net_bp"] - b["net_bp"]) if (a["net_bp"] is not None and b["net_bp"] is not None) else None
        report["surge_direction"][str(h)] = {"up": a, "down": b, "diff_bp": diff_bp}
        print(f"  H={h:>2}  爆量上涨 {a['net_bp']}bp(t={a['t']})  vs  爆量下跌 {b['net_bp']}bp(t={b['t']})  "
              f"差 {diff_bp}bp", flush=True)

    # 3. 量比敏感度：>=1.5 / >=2 / >=3
    print("\n=== 量比敏感度（H=20 净收益）===", flush=True)
    report["sensitivity"] = {}
    for thr in (1.5, 2.0, 3.0):
        seg = silent.filter(pl.col("vol_ratio_5d") >= thr)
        r = _report_group(seg, 20)
        report["sensitivity"][str(thr)] = r
        print(f"  量比>={thr}  净收益 {r['net_bp']}bp(t={r['t']})  n={r['n_trades']:,}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "silent_volume_surge.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/silent_volume_surge.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
