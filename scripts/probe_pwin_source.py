"""实证四：扩展窗口的 p_win 到底能不能跑赢常数基线？

这是对「p_win 没有真实来源」这一判断的直接检验。

设计（严格遵守预登记）：
  决策日 t 的 p_win 只能使用**在 t 时刻已经结算完**的样本，
  即决策日 <= t - HORIZON 的历史信号。t 当天的结果当然不可见。
  收缩：p = (wins + kappa*0.5) / (n + kappa)，kappa 固定不调优。

对照三种 p_win 来源：
  A. 常数 0.5            —— 无信息基线
  B. 历史超额胜率（扩展） —— 本文主张的来源
  C. 手填 0.65           —— 模拟乐观偏差
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
KAPPA = 20.0


def sessions() -> list[str]:
    base = DATA / TABLE
    return sorted(
        c.name.split("=", 1)[1]
        for c in base.iterdir()
        if c.is_dir() and c.name.startswith("date=")
    )


def day_frame(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close", "turnover_rate"]
    ).collect()


SIGNALS = {
    "momentum_20": ("mom", True),
    "reversal_20": ("mom", False),
    "low_turnover": ("turn", False),
    "high_turnover": ("turn", True),
}


def main() -> None:
    cal = sessions()
    pos = {d: i for i, d in enumerate(cal)}
    frames = {d: day_frame(d) for d in cal}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}

    # signal -> [(决策日, [outcome per pick])]
    per_day: dict[str, dict[str, list[int]]] = {k: {} for k in SIGNALS}

    for i in range(LOOKBACK, len(cal) - HORIZON):
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("turnover_rate").alias("turn")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0))
        )
        if base.height < 200:
            continue
        base = base.with_columns(
            (pl.col("c1") / pl.col("c0") - 1.0).alias("mom"),
            (pl.col("cb") / pl.col("ca") - 1.0).alias("fwd"),
        )
        med = float(base["fwd"].median())
        base = base.with_columns((pl.col("fwd") - med).alias("exc"))

        for name, (col, desc) in SIGNALS.items():
            ranked = base.drop_nulls(col).sort(col, descending=desc).head(TOP_N)
            if ranked.height < TOP_N:
                continue
            outs = [1 if x > 0 else 0 for x in ranked["exc"].to_list()]
            per_day[name][t] = outs

    print("=" * 76)
    print(f"扩展窗口 p_win 检验  |  {HORIZON}日持有 / 前{TOP_N}只 / kappa={KAPPA:.0f}")
    print("=" * 76)

    for name in SIGNALS:
        days = sorted(per_day[name])
        if len(days) < 60:
            print(f"\n{name}: 样本不足 ({len(days)})")
            continue

        recs = []
        for t in days:
            cutoff = pos[t] - HORIZON
            past = [d for d in days if pos[d] <= cutoff]
            wins = sum(sum(per_day[name][d]) for d in past)
            n = sum(len(per_day[name][d]) for d in past)
            p_hist = (wins + KAPPA * 0.5) / (n + KAPPA) if n else None
            if p_hist is None:
                continue
            recs.append({"date": t, "p": p_hist, "outs": per_day[name][t],
                         "hist_n": n, "hist_hit": (wins / n) if n else None})

        if not recs:
            continue

        flat = [(r["p"], y) for r in recs for y in r["outs"]]
        N = len(flat)
        b_a = sum((0.5 - y) ** 2 for _, y in flat) / N
        b_b = sum((p - y) ** 2 for p, y in flat) / N
        b_c = sum((0.65 - y) ** 2 for _, y in flat) / N
        hit = sum(y for _, y in flat) / N

        # 按日期聚类的 skill 标准误（保守：先算每日 brier 差，再除以 sqrt(天数)）
        daily = []
        for r in recs:
            ba = sum((0.5 - y) ** 2 for y in r["outs"]) / len(r["outs"])
            bb = sum((r["p"] - y) ** 2 for y in r["outs"]) / len(r["outs"])
            daily.append(ba - bb)
        m = sum(daily) / len(daily)
        sd = math.sqrt(sum((x - m) ** 2 for x in daily) / (len(daily) - 1)) if len(daily) > 1 else 0
        se = sd / math.sqrt(len(daily))

        print(f"\n【{name}】  决策日 {len(recs)}  样本 {N}")
        print(f"  实际超额胜率 = {hit:.2%}")
        print(f"  p_hist 收敛  = [{min(r['p'] for r in recs):.3f}, "
              f"{max(r['p'] for r in recs):.3f}]  末值 {recs[-1]['p']:.3f} "
              f"(历史 n={recs[-1]['hist_n']})")
        print(f"  Brier A(0.5) = {b_a:.5f}")
        print(f"        B(hist)= {b_b:.5f}   skill = {1-b_b/b_a:+.2%}")
        print(f"        C(0.65)= {b_c:.5f}   skill = {1-b_c/b_a:+.2%}")
        print(f"  逐日 Brier 改善 = {m:+.5f} ± {1.96*se:.5f} (95%, 按日聚类)")
        verdict = "SIGNIFICANT" if m - 1.96 * se > 0 else (
            "NEGATIVE" if m + 1.96 * se < 0 else "NOT_SIGNIFICANT")
        print(f"  判定: {verdict}")


if __name__ == "__main__":
    main()
