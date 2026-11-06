"""实证三：把 outcome 从「绝对涨跌」换成「跑赢同日全市场中位数」会发生什么？

三个问题：
1. 常数基线还需要估计吗？（p_exc 恒为 50% => 零估计误差）
2. 同一天多个 pick 之间相关吗？（决定 n_eff，也就是"带多少样本"）
3. 一个真实的信号（20 日动量）在这套口径下，胜率是多少、稳不稳？

如果 1 成立、2 可接受，那么"p_win 没有真实来源"这个结论就不成立——
来源可以是策略历史超额胜率，且样本量够。
"""
from __future__ import annotations

import math
from pathlib import Path

import polars as pl

DATA = Path("data")
TABLE = "kline_daily_enriched"
HORIZON = 20
LOOKBACK = 20
TOP_N = 20


def sessions() -> list[str]:
    base = DATA / TABLE
    return sorted(
        c.name.split("=", 1)[1]
        for c in base.iterdir()
        if c.is_dir() and c.name.startswith("date=")
    )


def day_close(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(["symbol", "close"]).collect()


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def main() -> None:
    cal = sessions()
    closes = {d: day_close(d) for d in cal}
    closes = {d: df for d, df in closes.items() if not df.is_empty()}

    picks_all: list[dict] = []
    rand_all: list[dict] = []
    n_days = 0
    for i in range(LOOKBACK, len(cal) - HORIZON):
        t, a, b = cal[i], cal[i + 1], cal[i + HORIZON]
        t0 = cal[i - LOOKBACK]
        if not all(d in closes for d in (t0, t, a, b)):
            continue
        base = closes[t0].join(closes[t], on="symbol", suffix="_t")
        base = base.join(closes[b], on="symbol", suffix="_b")
        base = base.join(closes[a], on="symbol", suffix="_a")
        base = base.filter(
            (pl.col("close") > 0) & (pl.col("close_b") > 0) & (pl.col("close_a") > 0)
        )
        if base.height < 200:
            continue
        base = base.with_columns(
            (pl.col("close") / pl.col("close_t") - 1.0).alias("mom"),
            (pl.col("close_b") / pl.col("close_a") - 1.0).alias("fwd"),
        )
        med = float(base["fwd"].median())
        base = base.with_columns((pl.col("fwd") - med).alias("exc"))
        n_days += 1

        top = base.sort("mom", descending=True).head(TOP_N)
        rnd = base.sample(min(TOP_N, base.height), seed=i)
        picks_all.append({
            "date": t,
            "n": top.height,
            "wins": int((top["exc"] > 0).sum()),
            "exc_mean": float(top["exc"].mean()),
        })
        rand_all.append({
            "date": t,
            "n": rnd.height,
            "wins": int((rnd["exc"] > 0).sum()),
        })

    print("=" * 68)
    print(f"动量策略（{LOOKBACK} 日动量取前 {TOP_N}），{n_days} 个决策日，"
          f"{HORIZON} 日持有，超额对全市场中位数")
    print("=" * 68)

    def summarize(rows: list[dict], label: str) -> None:
        k = sum(r["wins"] for r in rows)
        n = sum(r["n"] for r in rows)
        lo, hi = wilson(k, n)
        rates = [r["wins"] / r["n"] for r in rows]
        m = sum(rates) / len(rates)
        sd = math.sqrt(sum((x - m) ** 2 for x in rates) / (len(rates) - 1))
        # 若 pick 相互独立，每日胜率的标准差应为 sqrt(.25/TOP_N)
        indep = math.sqrt(0.25 / TOP_N)
        # 组内相关 ICC = 1 - (观测方差 / 理论独立方差)
        icc = max(0.0, 1 - (sd ** 2) / (indep ** 2))
        n_eff = len(rows) * TOP_N / (1 + (TOP_N - 1) * icc) if icc < 1 else len(rows)
        print(f"\n【{label}】")
        print(f"  总胜率      = {k}/{n} = {k/n:.2%}   Wilson95% [{lo:.2%}, {hi:.2%}]")
        print(f"  日均胜率    = {m:.2%}  日间 sd = {sd:.2%}")
        print(f"  独立假设 sd = {indep:.2%}  => 组内相关 ICC ≈ {icc:.2f}")
        print(f"  有效样本    ≈ {n_eff:.0f}（名义 {n}）")

    summarize(picks_all, f"动量前 {TOP_N}")
    summarize(rand_all, f"随机 {TOP_N}（对照组，应为 50%）")

    # 稳定性：前后两半
    half = len(picks_all) // 2
    for label, seg in (("前半段", picks_all[:half]), ("后半段", picks_all[half:])):
        k = sum(r["wins"] for r in seg)
        n = sum(r["n"] for r in seg)
        print(f"  {label}: {k}/{n} = {k/n:.2%}  "
              f"({seg[0]['date']} .. {seg[-1]['date']})")

    # 逐年
    by_year: dict[str, list[int]] = {}
    for r in picks_all:
        y = r["date"][:4]
        acc = by_year.setdefault(y, [0, 0])
        acc[0] += r["wins"]
        acc[1] += r["n"]
    print("\n  按年：")
    for y in sorted(by_year):
        k, n = by_year[y]
        print(f"    {y}: {k}/{n} = {k/n:.2%}")


if __name__ == "__main__":
    main()
