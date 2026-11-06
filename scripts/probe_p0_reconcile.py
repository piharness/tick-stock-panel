"""实证二：p0 到底是多少？为什么会有 35.69% 与 48.79% 两个答案？

1. 复现 settle.prior() 的口径（最近 12 个、间隔 20 日的起点）
2. 对比「绝对收益胜率」与「超额收益胜率」的时序稳定性
3. 估算两者的有效样本数

核心问题：同一天的所有股票同涨同跌 => 绝对收益的 n_eff 约等于 1/天。
若改用横截面超额，同一天的不同股票近似独立 => n_eff 约等于 N/天。
"""
from __future__ import annotations

import math
from pathlib import Path

import polars as pl

DATA = Path("data")
TABLE = "kline_daily_enriched"
HORIZON = 20
SAMPLE = 12
STEP = 20


def sessions() -> list[str]:
    base = DATA / TABLE
    out = [
        c.name.split("=", 1)[1]
        for c in base.iterdir()
        if c.is_dir() and c.name.startswith("date=")
    ]
    return sorted(out)


def day_close(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(["symbol", "close"]).collect()


def main() -> None:
    cal = sessions()
    closes = {d: day_close(d) for d in cal}
    closes = {d: df for d, df in closes.items() if not df.is_empty()}

    def stats_at(i: int) -> dict | None:
        a, b = cal[i], cal[i + HORIZON]
        if a not in closes or b not in closes:
            return None
        j = closes[a].join(closes[b], on="symbol", suffix="_end")
        j = j.filter((pl.col("close") > 0) & (pl.col("close_end") > 0))
        if j.height < 100:
            return None
        ret = j["close_end"] / j["close"] - 1.0
        med = float(ret.median())
        return {
            "start": a,
            "n": j.height,
            "p_abs": float((ret > 0).sum()) / j.height,
            "p_exc": float((ret > med).sum()) / j.height,   # 跑赢全市场中位数
            "median_ret": med,
        }

    # --- 1. 复现 settle.prior() 的采样口径 ---
    last = len(cal) - 1 - HORIZON
    starts_idx = [i for i in range(last, -1, -STEP)][:SAMPLE]
    prior_rows = [r for r in (stats_at(i) for i in starts_idx) if r]
    print("=" * 66)
    print(f"settle.prior() 口径：最近 {SAMPLE} 个起点、间隔 {STEP} 日")
    print("=" * 66)
    for r in prior_rows:
        print(f"  {r['start']}  n={r['n']:>5}  p_abs={r['p_abs']:.2%}  "
              f"p_exc={r['p_exc']:.2%}  med={r['median_ret']:+.2%}")
    n_obs = sum(r["n"] for r in prior_rows)
    p_abs = sum(r["p_abs"] * r["n"] for r in prior_rows) / n_obs
    p_exc = sum(r["p_exc"] * r["n"] for r in prior_rows) / n_obs
    print(f"\n  加权 p_abs = {p_abs:.4%}   (记录值 35.69%)")
    print(f"  加权 p_exc = {p_exc:.4%}")
    print(f"  n_obs = {n_obs:,}  独立起点 = {len(prior_rows)}")

    # --- 2. 全序列时序稳定性 ---
    all_rows = [r for r in (stats_at(i) for i in range(len(cal) - HORIZON)) if r]
    for key, label in (("p_abs", "绝对收益胜率"), ("p_exc", "超额收益胜率")):
        xs = [r[key] for r in all_rows]
        n = len(xs)
        mean = sum(xs) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
        print(f"\n{'-'*66}")
        print(f"{label}（{n} 个起点，全样本）")
        print(f"  均值 {mean:.4%}   sd {sd:.4%}   区间 [{min(xs):.2%}, {max(xs):.2%}]")
        print(f"  用 {len(prior_rows)} 个点估均值的 SE = {sd/math.sqrt(len(prior_rows)):.4%}"
              f"  -> 95%CI ±{1.96*sd/math.sqrt(len(prior_rows)):.2%}")

    # --- 3. 同一日内横截面离散度 ---
    print(f"\n{'='*66}")
    print("同一天内，绝对收益 vs 超额收益的横截面离散度")
    print("（若超额显著更窄，说明横截面样本比时序样本更接近独立）")
    print("=" * 66)
    for i in range(max(0, len(cal) - HORIZON - 5), len(cal) - HORIZON):
        a, b = cal[i], cal[i + HORIZON]
        if a not in closes or b not in closes:
            continue
        j = closes[a].join(closes[b], on="symbol", suffix="_end")
        j = j.filter((pl.col("close") > 0) & (pl.col("close_end") > 0))
        ret = j["close_end"] / j["close"] - 1.0
        med = float(ret.median())
        exc = ret - med
        print(f"  {a}  abs_sd={float(ret.std()):.3f}  "
              f"exc_sd={float(exc.std()):.3f}  "
              f"p_abs={float((ret>0).sum())/j.height:.1%}  "
              f"p_exc={float((ret>med).sum())/j.height:.1%}")


if __name__ == "__main__":
    main()
