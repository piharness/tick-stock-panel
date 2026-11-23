"""做T（底仓 T+0 日内往返）在不同公民等级上的效果。

## 口径钉死（先写，后跑）

**做T 的定义**：账户已持有底仓，同一交易日内完成一次「卖高—买低」或
「买低—卖高」的等量往返，收盘时底仓股数不变，收益是兑现的现金差。
不是「持有一天」，不是「隔夜」。

**为什么只用日内价格**：日内往返的收益 = 卖出价/买入价 − 1，两个价格在
同一天、同一复权基准下，**天然免疫前复权/原始价口径混用**。本脚本因此
完全不用跨日收益。

**绝不用 high/low 判断成交**。5 分钟 bar 内路径未知，用 `low` 当作「能买到」
会系统性高估，且高估幅度随波动率上升——而波动率正是等级之间的差异维度，
一旦用 high/low，结论会**按等级精确地反向**。所有成交价只取 bar 的
`open` / `close`（可观测成交价）。

**成本**：A 股做T一次往返 = 佣金双边 + 卖出印花税 + 滑点。
2023-08-28 起印花税 0.05%（卖出单边 5bp）。主口径 `12bp`，悲观 `20bp`。

## 四层输出

L0 `space`   日内振幅 (high−low)/prev_close —— 理论上界，不是策略。
L1 `shape`   路径效率 E=|close−open|/(high−low)。E→1 单边趋势，E→0 纯震荡。
L2 `rules`   真实规则回测，bar close 成交。唯一算数的层。
L3 `attrib`  等级差异是波动率投影还是日内结构差异：振幅分层 + 标签置换负控。

## 预注册判据

C-1 某等级规则净收益 t < 2.0 ⇒ 该等级「做T不成立」。
C-2 等级间差异必须在振幅分层内仍存在，否则是「振幅的投影」。
C-3 方向反转负控：反转后同样赚钱 ⇒ 赚的是日内漂移不是做T。
C-4 标签置换 200 次，实际极差须超 p95。
C-5 单笔胜率与单笔均值符号须一致，否则是「靠右尾」。

ASCII output only（GBK console）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "_probe_t0"
OUT.mkdir(parents=True, exist_ok=True)

START = "2024-11-06"
MIN_BARS = 40
COST_BP = 12.0
COST_BP_PESS = 20.0


def build_panel() -> pl.DataFrame:
    f = OUT / "panel.parquet"
    if f.exists():
        print(f"[panel] cached {f}")
        return pl.read_parquet(f)

    days = sorted(
        d.split("=", 1)[1] for d in os.listdir(DATA / "kline_5min")
        if d.startswith("date=") and d.split("=", 1)[1] >= START
    )
    print(f"[panel] {len(days)} days {days[0]} .. {days[-1]}", flush=True)

    frames = []
    t0 = time.time()
    for i, d in enumerate(days):
        p5 = DATA / "kline_5min" / f"date={d}" / "part.parquet"
        pdly = DATA / "kline_daily_enriched" / f"date={d}" / "part.parquet"
        if not p5.exists() or not pdly.exists():
            continue
        m = pl.read_parquet(p5).sort(["symbol", "datetime"])
        m = m.with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("r"))
        g = m.group_by("symbol").agg([
            pl.col("open").first().alias("o"),
            pl.col("close").last().alias("c"),
            pl.col("high").max().alias("h"),
            pl.col("low").min().alias("l"),
            pl.col("close").head(6).last().alias("c30"),
            pl.col("amount").sum().alias("amt"),
            pl.len().alias("nbar"),
            ((pl.col("r") - pl.col("r").mean()) *
             (pl.col("r").shift(1) - pl.col("r").mean())).mean().alias("cov"),
            pl.col("r").std().alias("rsd"),
        ]).filter(pl.col("nbar") >= MIN_BARS)
        g = g.with_columns(
            (pl.col("cov") / (pl.col("rsd") ** 2)).alias("ac1")).drop(["cov", "rsd"])

        dd = (pl.read_parquet(pdly)
              .select(["symbol", "close", "amount", "turnover_rate"])
              .rename({"close": "dclose", "amount": "damt", "turnover_rate": "turn"}))
        g = g.join(dd, on="symbol", how="inner")
        g = g.with_columns(pl.lit(d, dtype=pl.String).alias("date"))
        frames.append(g)
        if i % 60 == 0:
            print(f"  {i}/{len(days)} {d} rows={g.height} {time.time()-t0:.0f}s", flush=True)

    panel = pl.concat(frames)
    panel = panel.sort(["symbol", "date"]).with_columns(
        pl.col("dclose").shift(1).over("symbol").alias("prev_close"))
    panel = panel.with_columns([
        (pl.col("o") / pl.col("prev_close") - 1).alias("gap"),
        (pl.col("h") - pl.col("l")).truediv(pl.col("prev_close")).alias("ampl"),
        (pl.col("c") - pl.col("o")).abs().truediv(
            (pl.col("h") - pl.col("l")).clip(lower_bound=1e-9)).alias("eff"),
        (pl.col("c") / pl.col("o") - 1).alias("i1"),
        ((pl.col("c30") / pl.col("o") - 1)).alias("i30"),
    ])
    panel = panel.with_columns([
        ((pl.col("damt").rank("ordinal").over("date") - 1) /
         (pl.col("damt").count().over("date") - 1).clip(lower_bound=1)).alias("p_amount"),
        ((pl.col("turn").rank("ordinal").over("date") - 1) /
         (pl.col("turn").count().over("date") - 1).clip(lower_bound=1)).alias("p_turn"),
    ]).with_columns(
        pl.when(pl.col("p_amount") >= 0.90).then(pl.lit("core"))
        .when((pl.col("p_amount") >= 0.50) | (pl.col("p_turn") >= 0.90)).then(pl.lit("active"))
        .when((pl.col("p_amount") < 0.10) & (pl.col("p_turn") < 0.10)).then(pl.lit("silent"))
        .otherwise(pl.lit("background")).alias("level")
    )
    panel.write_parquet(f)
    print(f"[panel] wrote {f} {panel.shape} {time.time()-t0:.0f}s")
    return panel


if __name__ == "__main__":
    p = build_panel()
    print(p.select(["symbol", "date", "o", "c", "ampl", "eff", "i1", "ac1", "level"]).head(4))
    print(p.group_by("level").agg(pl.len(), pl.col("ampl").median().alias("ampl_med"))
          .sort("level"))
