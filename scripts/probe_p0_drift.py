"""实证六：settle.prior() 的 p0 稳定吗？

prior() 取「最近 12 个、间隔 20 日的起点」。数据每新增一个交易日，
这个窗口就整体右移一格 —— 所以 p0 是一个每天都在变的量，而不是常数。
本脚本模拟：把数据截断到不同的"最后一天"，看 p0 会漂到哪。
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


def main() -> None:
    cal = sessions()
    need = {d for d in cal}
    closes = {d: day_close(d) for d in need}
    closes = {d: f for d, f in closes.items() if not f.is_empty()}
    cal = [d for d in cal if d in closes]

    def p_at(start_idx: int) -> tuple[float, int] | None:
        a, b = cal[start_idx], cal[start_idx + HORIZON]
        j = closes[a].join(closes[b], on="symbol", suffix="_end")
        j = j.filter((pl.col("close") > 0) & (pl.col("close_end") > 0))
        if j.height < 100:
            return None
        ret = j["close_end"] / j["close"] - 1.0
        return float((ret > 0).sum()) / j.height, j.height

    print("=" * 72)
    print("p0 随「数据最后一天」的漂移（模拟每日增量更新）")
    print("=" * 72)
    print(f"{'数据截至':<12}{'p0':>9}{'n_obs':>9}   起点的 p 值")
    print("-" * 72)

    series = []
    # 从第 260 个交易日开始，模拟"已有足够历史"之后的每一天
    for last in range(260, len(cal)):
        end = last - HORIZON
        idxs = [i for i in range(end, -1, -STEP)][:SAMPLE]
        rows = [r for r in (p_at(i) for i in idxs) if r]
        if len(rows) < SAMPLE:
            continue
        tot = sum(n for _, n in rows)
        p0 = sum(p * n for p, n in rows) / tot
        series.append((cal[last], p0, tot, [round(p, 3) for p, _ in rows]))
        if last % 20 == 0 or last == len(cal) - 1:
            ps = " ".join(f"{p:.2f}" for p, _ in rows)
            print(f"{cal[last]:<12}{p0:>8.2%}{tot:>9,}   {ps}")

    vals = [p for _, p, _, _ in series]
    print("-" * 72)
    print(f"漂移区间: [{min(vals):.2%}, {max(vals):.2%}]")
    print(f"均值 {sum(vals)/len(vals):.2%}   sd {math.sqrt(sum((v-(sum(vals)/len(vals)))**2 for v in vals)/(len(vals)-1)):.2%}")
    print(f"最近 60 个交易日内变化: {vals[-60]:.2%} -> {vals[-1]:.2%}")
    print(f"\n当前实现（数据截至 {cal[-1]}）: p0 = {vals[-1]:.2%}")
    print(f"记录值 35.69% 是否在漂移区间内: "
          f"{'是' if min(vals) <= 0.3569 <= max(vals) else '否'}")


if __name__ == "__main__":
    main()
