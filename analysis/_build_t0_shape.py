"""构建日内图形（shape）特征面，供做T条件化分析。

## 为什么这些特征能绕过 kline_5min 的价格缩放缺陷

`data/kline_5min` 有 12% 的交易日存在「当日逐标的常数缩放」错误
（MEMORY：40 只逐只-日匹配率 0.976，最严重 2026-10-23 达 97.4%）。
本脚本所有特征都是**当日内部的比值/相对位置**，常数缩放因子在分子分母
同时出现，**完全约掉**。唯一跨日的是 `gap`，取自 `kline_daily_enriched`
（原始价，干净），不从 5min 算。

## 特征

方向：i1 全天、f30 早盘30min、mid、pm 下午、last30 尾盘、r1h/r2h 上下半场
形态：eff 路径效率、zigzag 锯齿度、t_hi/t_lo 极值时点、n_vwap 穿越次数、
      vwap_dev、sh_up/sh_dn 影线、upvol 涨时量占比、maxbar 单根集中度
空间：dip 开盘后最深回撤、rise 开盘后最高涨幅、dd_close 收盘位置
回归：ac1 一阶自相关（负=均值回归=做T友好）

## 输出
`data/_probe_t0/shape.parquet`
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "_probe_t0"
START = "2024-11-06"
MIN_BARS = 47
EPS = 1e-12


def _c(k: int):
    """组内第 k 根 bar 的收盘价（gather 在 agg 中会退化为 list，须用 filter+first）。"""
    return pl.col("close").filter(pl.col("i") == k).first()


def one_day(d: str) -> pl.DataFrame | None:
    p5 = DATA / "kline_5min" / f"date={d}" / "part.parquet"
    if not p5.exists():
        return None
    m = (pl.read_parquet(p5)
         .filter(pl.col("volume") > 0, pl.col("close") > 0)
         .sort(["symbol", "datetime"]))
    if m.height == 0:
        return None

    m = m.with_columns([
        pl.col("close").shift(1).over("symbol").alias("pc"),
        pl.int_range(pl.len()).over("symbol").alias("i"),
        pl.len().over("symbol").alias("n"),
        # A股 volume 单位为手、amount 为元 => 每股均价 = amount/(volume*100)
        (pl.col("amount").cum_sum().over("symbol")
         / (pl.col("volume").cum_sum().over("symbol") * 100.0)).alias("rvwap"),
    ]).filter(pl.col("n") >= MIN_BARS)

    if m.height == 0:
        return None

    m = m.with_columns([
        (pl.col("close") / pl.col("pc") - 1).alias("r"),
        (pl.col("close") - pl.col("rvwap")).alias("dev"),
    ])

    g = m.group_by("symbol").agg([
        pl.len().alias("nbar"),
        pl.col("open").first().alias("o"),
        pl.col("close").last().alias("c"),
        pl.col("high").max().alias("h"),
        pl.col("low").min().alias("l"),
        pl.col("amount").sum().alias("amt"),
        pl.col("volume").sum().alias("vol"),
        # --- 方向 ---
        (_c(5) / pl.col("close").first() - 1).alias("f30"),
        (_c(29) / _c(5) - 1).alias("mid"),
        (pl.col("close").last() / _c(29) - 1).alias("pm"),
        (pl.col("close").last() / _c(41) - 1).alias("last30"),
        (_c(23) / pl.col("close").first() - 1).alias("r1h"),
        (pl.col("close").last() / _c(23) - 1).alias("r2h"),
        # --- 形态 ---
        (pl.col("high").arg_max() / (pl.len() - 1)).alias("t_hi"),
        (pl.col("low").arg_min() / (pl.len() - 1)).alias("t_lo"),
        ((pl.col("dev") * pl.col("dev").shift(1)) < 0).sum().alias("n_vwap"),
        pl.col("dev").last().alias("dev_close"),
        (pl.col("r") * pl.col("r").shift(1)).sum().alias("cov1"),
        (pl.col("r") ** 2).sum().alias("ss"),
        (pl.col("r") - pl.col("r").mean()).skew().alias("skew"),
        (pl.col("r").abs().sum()).alias("absr"),
        ((pl.col("high") - pl.col("low")).max()).alias("maxbar_abs"),
        ((pl.col("r") > 0).cast(pl.Float64) * pl.col("volume")).sum().alias("upvol_abs"),
        pl.col("amount").filter(pl.col("i") >= 24).sum().alias("amt_pm"),
    ])

    g = g.with_columns([
        (pl.col("c") / pl.col("o") - 1).alias("i1"),
        (pl.col("h") - pl.col("l")).alias("rng"),
    ]).with_columns([
        ((pl.col("c") - pl.col("o")).abs()
         / pl.col("rng").clip(lower_bound=EPS)).alias("eff"),
        (pl.col("rng") / pl.col("o")).alias("ampl_o"),
        (pl.col("maxbar_abs") / pl.col("rng").clip(lower_bound=EPS)).alias("maxbar"),
        (pl.col("upvol_abs") / pl.col("vol").clip(lower_bound=EPS)).alias("upvol"),
        (pl.col("cov1") / pl.col("ss").clip(lower_bound=EPS)).alias("ac1"),
        (pl.col("amt_pm") / pl.col("amt").clip(lower_bound=EPS)).alias("amt_pm_share"),
        (pl.col("absr") / pl.col("i1").abs().clip(lower_bound=1e-4)).alias("zigzag"),
        (pl.col("n_vwap") / (pl.col("nbar") - 1).clip(lower_bound=1)).alias("n_vwap_r"),
        (pl.col("dev_close") / pl.col("c")).alias("vwap_dev"),
        ((pl.col("o").clip(upper_bound=pl.col("c")) - pl.col("l"))
         / pl.col("rng").clip(lower_bound=EPS)).alias("sh_dn"),
        ((pl.col("h") - pl.col("o").clip(lower_bound=pl.col("c")))
         / pl.col("rng").clip(lower_bound=EPS)).alias("sh_up"),
        ((pl.col("l") - pl.col("o")) / pl.col("o")).alias("dip"),
        ((pl.col("h") - pl.col("o")) / pl.col("o")).alias("rise"),
        ((pl.col("c") - pl.col("l")) / pl.col("rng").clip(lower_bound=EPS)).alias("dd_close"),
    ]).drop(["cov1", "ss", "absr", "maxbar_abs", "upvol_abs", "amt_pm",
             "dev_close", "rng"])

    return g.with_columns(pl.lit(d, dtype=pl.String).alias("date"))


def main() -> None:
    days = sorted(d.split("=", 1)[1] for d in os.listdir(DATA / "kline_5min")
                  if d.startswith("date=") and d.split("=", 1)[1] >= START)
    print(f"[shape] {len(days)} candidate days {days[0]} .. {days[-1]}", flush=True)
    frames, t0 = [], time.time()
    for i, d in enumerate(days):
        g = one_day(d)
        if g is not None and g.height:
            frames.append(g)
        if i % 60 == 0:
            print(f"  {i}/{len(days)} {d} frames={len(frames)} {time.time() - t0:.0f}s",
                  flush=True)
    sh = pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "date"])
    sh.write_parquet(OUT / "shape.parquet")
    print(f"[shape] wrote {OUT / 'shape.parquet'} {sh.shape} {time.time() - t0:.0f}s")
    print(sh.select(["symbol", "date", "nbar", "i1", "f30", "eff", "ac1", "n_vwap_r",
                     "dip", "rise", "upvol"]).head(5))


if __name__ == "__main__":
    main()
