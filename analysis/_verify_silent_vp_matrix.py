"""沉默公民池的量价变化组合扫描：哪组超额收益最显著。

背景：前几轮已证伪「沉默有 alpha」「爆量上涨近5年衰减」「首阴负期望」。
本脚本做系统性扫描——把 silent 票的「量变化 × 价变化」切成 3×4 网格，
穷举 12 个量价组合，找出哪个组合的后续超额收益最显著（相对 silent 全体）。

量变化（vol_ratio_5d = 当日量 / 前5日均量）：
  放量 >=2.0 / 平量 [0.7,2) / 缩量 <0.7
价变化（当日收益 ret1）：
  大涨 >5% / 小涨 (0,5%] / 小跌 (-5%,0] / 大跌 <=-5%

纪律：
  - 防前视：量价变化 T 日收盘可知，T+1 开盘买入。
  - 基准 = silent 全体（同池，拆出「量价组合」的边际，避免小盘溢价污染）。
  - 超额 = 组合净收益 − silent 全体净收益（按日配对）。
  - 扣印花税 0.1% + 佣金双向万2.5，剔除 T+1 停牌/低成交额。
  - 12 组多重比较：报 BH-FDR 校正后的显著性，标出唯一最显著组合。

数据：kline_daily_enriched（2005–2026）。
输出：data/.scratch/silent_volume_price_matrix.json
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

HORIZONS = (5, 20)
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


def bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR 校正。"""
    n = len(pvals)
    idx = np.argsort(pvals)
    sorted_p = np.array(pvals)[idx]
    adj = np.empty(n)
    adj[idx] = sorted_p * n / np.arange(1, n + 1)
    # 单调化
    for i in range(n - 2, -1, -1):
        adj[idx[i]] = min(adj[idx[i]], adj[idx[i + 1]])
    return [min(1.0, float(a)) for a in adj]


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
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1")
    )
    df = df.with_columns(
        pl.col("volume").shift(1).rolling_mean(window_size=5, min_samples=5)
        .over("symbol").alias("vol_mean_5d")
    )
    df = df.with_columns((pl.col("volume") / pl.col("vol_mean_5d")).alias("vol_ratio"))
    df = df.with_columns(
        pl.col("date").diff(-1).over("symbol").dt.total_days().alias("_next_gap")
    )
    cost = STAMP_TAX + 2 * COMMISSION
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col("close").shift(-(h + 1)).over("symbol")
             / pl.col("open").shift(-1).over("symbol") - 1.0 - cost).alias(f"net{h}")
        )
    return df


def vol_bucket(vr: pl.Expr) -> pl.Expr:
    return (
        pl.when(vr >= 2.0).then(pl.lit("放量"))
        .when(vr >= 0.7).then(pl.lit("平量"))
        .otherwise(pl.lit("缩量"))
    )


def price_bucket(r: pl.Expr) -> pl.Expr:
    return (
        pl.when(r > 0.05).then(pl.lit("大涨"))
        .when(r > 0.0).then(pl.lit("小涨"))
        .when(r > -0.05).then(pl.lit("小跌"))
        .otherwise(pl.lit("大跌"))
    )


def main() -> None:
    print("读全历史面板…", flush=True)
    df = load_history()
    df = df.filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0) & (pl.col("open") > 0)
        & (pl.col("volume") > 0)
    )
    print("逐日算等级…", flush=True)
    df = df.group_by("date", maintain_order=True).map_groups(assign_level)
    print("加量价/收益…", flush=True)
    df = enrich(df)

    tradable = (
        (pl.col("amount") >= MIN_AMOUNT)
        & ((pl.col("_next_gap").is_null()) | (pl.col("_next_gap") <= 7))
    )
    silent = df.filter((pl.col("level") == "silent") & tradable)

    # silent 全体按日的净收益（基准）
    silent = silent.with_columns(
        vol_bucket(pl.col("vol_ratio")).alias("vol_b"),
        price_bucket(pl.col("ret1")).alias("px_b"),
    )

    report = {"n_silent": int(silent.height), "horizons": list(HORIZONS)}

    for h in HORIZONS:
        print(f"\n========== H={h} ==========", flush=True)
        # silent 全体基准（按日均值）
        base = silent.filter(pl.col(f"net{h}").is_not_null())
        base_daily = base.group_by("date").agg(pl.col(f"net{h}").mean().alias("base_e"))
        base_mean = float(base_daily["base_e"].mean())

        combos = []
        for vb in ["放量", "平量", "缩量"]:
            for pb in ["大涨", "小涨", "小跌", "大跌"]:
                seg = silent.filter((pl.col("vol_b") == vb) & (pl.col("px_b") == pb)
                                    & pl.col(f"net{h}").is_not_null())
                if seg.height < 50:
                    continue
                # 超额 = 组合净收益 − silent 全体（按日配对）
                seg = seg.join(base_daily, on="date", how="left")
                exc = seg[f"net{h}"] - seg["base_e"]
                daily_exc = seg.with_columns(exc.alias("_exc")).group_by("date").agg(
                    pl.col("_exc").mean().alias("e"))
                mean_exc = float(daily_exc["e"].mean())
                se_exc = block_se(daily_exc["e"].to_numpy(), block=2 * h)
                t_exc = mean_exc / se_exc if se_exc > 0 else float("nan")
                # p 值（近似正态，用于 FDR）
                from math import erfc, sqrt
                p = float(erfc(abs(t_exc) / sqrt(2))) if se_exc > 0 else 1.0
                combos.append({
                    "vol": vb, "px": pb, "n": int(seg.height),
                    "net_bp": round(float(seg[f"net{h}"].mean()) * 1e4, 1),
                    "exc_bp": round(mean_exc * 1e4, 1),
                    "se_bp": round(se_exc * 1e4, 1),
                    "t": round(t_exc, 2), "p": p,
                })

        # BH-FDR 校正
        pvals = [c["p"] for c in combos]
        adj = bh_fdr(pvals)
        for c, a in zip(combos, adj):
            c["q"] = round(a, 4)

        combos.sort(key=lambda c: c["exc_bp"], reverse=True)
        print(f"  silent 全体净收益基准: {base_mean*1e4:+.1f}bp", flush=True)
        print(f"  {'量':<4}{'价':<4}{'n':>7}{'净收益':>9}{'超额':>9}{'se':>8}{'t':>7}{'q(FDR)':>9}", flush=True)
        for c in combos:
            print(f"  {c['vol']:<4}{c['px']:<4}{c['n']:>7,}{c['net_bp']:>+9.1f}"
                  f"{c['exc_bp']:>+9.1f}{c['se_bp']:>8.1f}{c['t']:>+7.2f}{c['q']:>9.4f}", flush=True)

        top = combos[0]
        print(f"\n  → H={h} 超额最显著组合：{top['vol']}+{top['px']}，"
              f"超额 {top['exc_bp']:+.1f}bp (t={top['t']:+.2f}, q={top['q']})", flush=True)
        report[f"H{h}"] = {"base_bp": round(base_mean * 1e4, 1), "combos": combos,
                           "top": top}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "silent_volume_price_matrix.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n写出 data/.scratch/silent_volume_price_matrix.json", flush=True)


if __name__ == "__main__":
    sys.exit(main())
