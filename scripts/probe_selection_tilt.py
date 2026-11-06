"""选股落在哪一层：成交额分位与「同层中位数」基准。

`probe_net_diff.py` 的 +185bp / −405bp 都是对**全样本中位数**算的。全样本
中位数是一只「中等成交额」的股票；而被选中的 20 只如果在成交额的高分位，
那个差里就混着规模/流动性 beta，不能读成信号优劣。

本脚本做两件事，逐日重算、不依赖缓存：

  1. 选中 20 只在当日成交额分布中的平均分位（0.5 = 中位）；
  2. 把基准从「全样本中位数」换成「**同一成交额五分位内的中位数**」，
     再算同一批 picks 的每笔超额（bp）。两个数并排，差多少就是 beta。

用法：
    python scripts/probe_selection_tilt.py [STEP]
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl

DATA = Path("data")
TABLE = "kline_daily_enriched"
HORIZON, LOOKBACK, TOP_N = 20, 20, 20
STEP = int(sys.argv[1]) if len(sys.argv) > 1 else 1
NMAX = int(sys.argv[2]) if len(sys.argv) > 2 else None

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "probe_paired_diff", ROOT / "scripts" / "probe_paired_diff.py")
_ppd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ppd)
SIGNALS = {
    "momentum_20": ("mom", True),
    "reversal_20": ("mom", False),
    "low_turnover": ("turn", False),
    "high_turnover": ("turn", True),
}


def sessions() -> list[str]:
    base = DATA / TABLE
    return sorted(c.name.split("=", 1)[1] for c in base.iterdir()
                  if c.is_dir() and c.name.startswith("date="))


def day_frame(day: str) -> pl.DataFrame:
    part = DATA / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files]).select(
        ["symbol", "close", "turnover_rate", "amount"]).collect()


def main() -> None:
    cal = sessions()
    frames = {d: day_frame(d) for d in cal}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}
    acc = {k: {"pct": [], "all": [], "q": []} for k in SIGNALS}
    used = 0
    for i in range(LOOKBACK, len(cal) - HORIZON, STEP):
        t0, t, a, b = cal[i - LOOKBACK], cal[i], cal[i + 1], cal[i + HORIZON]
        if not all(d in frames for d in (t0, t, a, b)):
            continue
        base = (
            frames[t0].select(["symbol", pl.col("close").alias("c0")])
            .join(frames[t].select(["symbol", pl.col("close").alias("c1"),
                                    pl.col("turnover_rate").alias("turn"),
                                    pl.col("amount")]), on="symbol")
            .join(frames[a].select(["symbol", pl.col("close").alias("ca")]), on="symbol")
            .join(frames[b].select(["symbol", pl.col("close").alias("cb")]), on="symbol")
            .filter((pl.col("c0") > 0) & (pl.col("c1") > 0)
                    & (pl.col("ca") > 0) & (pl.col("cb") > 0)
                    & pl.col("amount").is_not_null())
        )
        if base.height < 200:
            continue
        base = base.with_columns(
            (pl.col("c1") / pl.col("c0") - 1.0).alias("mom"),
            (pl.col("cb") / pl.col("ca") - 1.0).alias("fwd"),
        )
        n = base.height
        med = float(base["fwd"].median())
        base = base.with_columns(
            (pl.col("fwd") - med).alias("exc_all"),
            (pl.col("amount").rank("average") / n).alias("apct"),
        )
        base = base.with_columns(
            ((pl.col("apct") * 5).clip(0, 4.999).floor()).cast(pl.Int8).alias("q"))
        qmed = base.group_by("q").agg(pl.col("fwd").median().alias("qmed"))
        base = base.join(qmed, on="q").with_columns(
            (pl.col("fwd") - pl.col("qmed")).alias("exc_q"))
        used += 1
        for name, (col, desc) in SIGNALS.items():
            sel = base.drop_nulls(col).sort(col, descending=desc).head(TOP_N)
            if sel.height != TOP_N:
                continue
            acc[name]["pct"].append(float(sel["apct"].mean()))
            acc[name]["all"].append(float(sel["exc_all"].mean()))
            acc[name]["q"].append(float(sel["exc_q"].mean()))
        if NMAX and used >= NMAX:
            break

    print(f"{used} 个决策日；选中 20 只，成交额五分位内中位数作同层基准")
    print(f"{'策略':<15}{'成交额分位':>10}{'vs全样本':>10}{'se':>7}"
          f"{'vs同层':>9}{'se':>7}{'t同层':>7}")
    for name, d in acc.items():
        if not d["pct"]:
            continue
        pct = sum(d["pct"]) / len(d["pct"])
        a = [x * 1e4 for x in d["all"]]
        q = [x * 1e4 for x in d["q"]]
        ga, gq = sum(a) / len(a), sum(q) / len(q)
        sa, sq = _ppd.se_block(a), _ppd.se_block(q)
        print(f"{name:<15}{pct:>10.3f}{ga:>10.1f}{sa:>7.1f}"
              f"{gq:>9.1f}{sq:>7.1f}{gq / sq:>7.2f}")
    print("\n读法：两列差 = 全样本中位数与同层中位数的基准差。差大说明原读数"
          "\n主要是规模/流动性 beta；差小说明方向在层内仍然成立。")
    print("⚠️ 同层基准只控制成交额一层，不控制行业/波动率；t 用 40 天块重抽。")


if __name__ == "__main__":
    main()
