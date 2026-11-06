"""实证：常数基线 p0 的真实精度到底有多高？

对 20 日 horizon，以**每个交易日**为起点，算全市场未来 20 日上涨占比 p_t，
得到一条时间序列。然后比较两种标准误：

  naive   = sqrt(p(1-p) / N_obs)        <- 把 33,090 个横截面观测当独立样本
  cluster = sd(p_t) / sqrt(n_eff)       <- 按日期聚类，n_eff 用自相关修正

如果两者差一个数量级，说明"p0 = 35.69%"的精度被系统性高估。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import polars as pl

DATA = Path("data")
TABLE = "kline_daily_enriched"
HORIZON = 20


def sessions() -> list[str]:
    base = DATA / TABLE
    out = []
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("date="):
            out.append(child.name.split("=", 1)[1])
    return sorted(out)


def day_close(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close"]
    ).collect()


def main() -> None:
    cal = sessions()
    print(f"分区交易日: {len(cal)}  ({cal[0]} .. {cal[-1]})")
    if len(cal) <= HORIZON + 1:
        print("交易日不足")
        return

    # 预取所有交易日的收盘价，避免重复 IO
    closes: dict[str, pl.DataFrame] = {}
    for d in cal:
        df = day_close(d)
        if not df.is_empty():
            closes[d] = df

    rows = []
    for i in range(len(cal) - HORIZON):
        a, b = cal[i], cal[i + HORIZON]
        if a not in closes or b not in closes:
            continue
        joined = closes[a].join(closes[b], on="symbol", suffix="_end")
        joined = joined.filter((pl.col("close") > 0) & (pl.col("close_end") > 0))
        if joined.height < 100:
            continue
        ret = joined["close_end"] / joined["close"] - 1.0
        rows.append({
            "start": a,
            "n": joined.height,
            "p": float((ret > 0).sum()) / joined.height,
        })

    if not rows:
        print("无可用样本")
        return

    ps = [r["p"] for r in rows]
    ns = [r["n"] for r in rows]
    n_dates = len(ps)
    p_bar = sum(ps) / n_dates
    sd = math.sqrt(sum((x - p_bar) ** 2 for x in ps) / (n_dates - 1))
    n_obs = sum(ns)

    # 一阶自相关（重叠窗口必然正自相关）
    num = sum((ps[i] - p_bar) * (ps[i - 1] - p_bar) for i in range(1, n_dates))
    den = sum((x - p_bar) ** 2 for x in ps)
    rho1 = num / den if den > 0 else 0.0

    # 重叠窗口的理论自相关: 对 AR(1) 近似, rho_k ≈ 1 - k/H
    # 用 Bartlett 核估计 n_eff = n / (1 + 2*sum_{k=1..K} (1-k/(K+1)) * rho_k)
    # 这里用更保守的解析式: 重叠 H 期均值, n_eff ≈ n / H * 3 (经验)
    # 同时给出"完全不重叠"的下界
    naive_se = math.sqrt(p_bar * (1 - p_bar) / n_obs)
    cluster_se = sd / math.sqrt(n_dates)
    n_eff_simple = n_dates / (1 + 2 * rho1) if rho1 > -0.5 else n_dates
    n_eff_se = sd / math.sqrt(max(n_eff_simple, 1.0))
    nonoverlap = math.ceil(n_dates / HORIZON)

    print(f"\n{'='*62}")
    print(f"20 日 horizon，起点数 = {n_dates}（每个交易日一个起点）")
    print(f"总观测 = {n_obs:,}，平均每日 {n_obs // n_dates:,} 只")
    print(f"{'='*62}")
    print(f"p0（均值）            = {p_bar:.4%}")
    print(f"日间标准差 sd(p_t)    = {sd:.4%}")
    print(f"一阶自相关 rho1       = {rho1:+.3f}  (重叠窗口，必然为正)")
    print(f"不重叠独立样本        = {nonoverlap}")
    print()
    print(f"naive   SE (按观测)   = {naive_se:.4%}   -> 95%CI ±{1.96*naive_se:.2%}")
    print(f"cluster SE (按日期)   = {cluster_se:.4%}   -> 95%CI ±{1.96*cluster_se:.2%}")
    print(f"n_eff 修正 SE         = {n_eff_se:.4%}   -> 95%CI ±{1.96*n_eff_se:.2%}")
    print(f"精度高估倍数          = {cluster_se/naive_se:.1f}x  (cluster/naive)")
    print()
    print(f"要拿到 ±2% 的 95%CI（cluster），需要约 "
          f"{math.ceil((1.96*sd/0.02)**2)} 个独立起点")
    print(f"即约 {math.ceil((1.96*sd/0.02)**2)*HORIZON} 个交易日 "
          f"= {math.ceil((1.96*sd/0.02)**2)*HORIZON/244:.1f} 年（不重叠）")

    print(f"\n{'-'*62}")
    print("最近 12 个起点的 p_t：")
    for r in rows[-12:]:
        print(f"  {r['start']}  n={r['n']:>5}  p={r['p']:.2%}")
    print(f"{'-'*62}")
    print(f"p_t 区间: [{min(ps):.2%}, {max(ps):.2%}]")
    q = sorted(ps)
    print(f"p_t 中位数: {q[len(q)//2]:.2%}")


if __name__ == "__main__":
    sys.exit(main())
