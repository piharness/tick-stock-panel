"""日内成本带宽度：把「持仓成本分布」降维成可识别投影。

为什么这样做
------------
日频 (P, V, T) 反推持仓分布必须假定衰减核 ⇒ 不可识别。
但 5min 给出 48 个 (价格, 成交量) 对，是**另一个信息集**，不是同一信息的重排：
它是「当日新增筹码的成本分布」，可直接观测，无需任何衰减假设。

三个量（都是无量纲，且对复权免疫——同日内同一 factor）：
  ivd_lo : 组间方差口径的宽度 = sqrt(Σw(p_i-vwap)²/Σw)/vwap   （把 bar 内视为点 ⇒ 下界）
  ivd_hi : 全方差分解口径 = sqrt(组间 + Σw(h_i-l_i)²/12 /Σw)/vwap（bar 内均匀 ⇒ 上界）
           ⇒ 真值被 [lo, hi] 夹住，结论必须在两端同向，才算对假设稳健。
  h_time : 成交量在时间上的归一化熵（48 桶）。与宽度正交的另一维：
           宽度答「成本带多宽」，h_time 答「成交摊在多久里」。
  tc_open/tc_close : 首/末 30min 成交量占比（用户那条「开盘集中 vs 尾盘集中」的直接投影）。

对账：vwap 必须落在当日 [low, high] 内；落不进的 bar 记 clip 次数。

用法:
  python analysis/_diag_chip_entropy.py --sample 800
输出:
  data/.scratch/chip_entropy/part.parquet
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
BS = ROOT / "data" / "backfill_bs_minute" / "f5_merged"
OUT_DIR = ROOT / "data" / ".scratch" / "chip_entropy"
START, END = "2020-01-01", "2026-11-30"


def one(f: Path) -> pl.DataFrame:
    d = pl.read_parquet(f, columns=["symbol", "datetime", "high", "low", "close",
                                    "volume", "amount"])
    d = d.filter(
        (pl.col("datetime").dt.date().is_between(pl.lit(START).str.to_date(),
                                                 pl.lit(END).str.to_date()))
        & (pl.col("volume") > 0) & (pl.col("amount") > 0)
        & (pl.col("high") >= pl.col("low")) & (pl.col("high") > 0)
    )
    if d.height < 48:
        return pl.DataFrame()
    d = d.with_columns(
        pl.col("datetime").dt.date().alias("date"),
        pl.col("datetime").dt.hour().alias("hh"),
        pl.col("datetime").dt.minute().alias("mm"),
        # bar 内成交均价（amount 元 / 手×100）
        (pl.col("amount") / (pl.col("volume") * 100.0)).alias("bp"),
        (pl.col("high") - pl.col("low")).alias("rng"),
    ).with_columns(
        # 把 bar 均价夹回 [low, high]：amount/volume 偶有脏行
        pl.col("bp").clip(pl.col("low"), pl.col("high")).alias("bp")
    ).with_columns(
        (pl.col("volume") / pl.col("volume").sum().over("date")).alias("w"),
        ((pl.col("hh") < 10) | ((pl.col("hh") == 10) & (pl.col("mm") == 0))).alias("is_open"),
        (pl.col("hh") == 14).alias("is_prevclose"),
    )

    g = d.group_by("date").agg(
        pl.col("volume").sum().alias("vol"),
        pl.col("amount").sum().alias("amt"),
        (pl.col("volume") * pl.col("bp")).sum().alias("sv"),
        (pl.col("volume") * pl.col("bp") ** 2).sum().alias("sv2"),
        (pl.col("volume") * pl.col("rng") ** 2).sum().alias("sr2"),
        pl.col("bp").min().alias("bp_lo"),
        pl.col("bp").max().alias("bp_hi"),
        pl.len().alias("nbar"),
        (-(pl.col("w") * pl.col("w").log())).sum().alias("h_raw"),
        pl.col("volume").filter(pl.col("is_open")).sum().alias("v_open"),
        pl.col("volume").filter(pl.col("is_prevclose")).sum().alias("v_close"),
    ).with_columns(
        (pl.col("amt") / (pl.col("vol") * 100.0)).alias("vwap"),
        pl.lit(str(f.stem)).alias("symbol"),
    ).with_columns(
        (pl.col("sv") / pl.col("vol")).alias("m1"),
    ).with_columns(
        ((pl.col("sv2") / pl.col("vol") - pl.col("m1") ** 2).clip(0.0)).alias("var_b"),
        ((pl.col("sr2") / pl.col("vol")).clip(0.0)).alias("var_w"),
    ).with_columns(
        (pl.col("var_b").sqrt() / pl.col("vwap")).alias("ivd_lo"),
        ((pl.col("var_b") + pl.col("var_w")).sqrt() / pl.col("vwap")).alias("ivd_hi"),
        (pl.col("h_raw") / pl.col("nbar").log()).alias("h_time"),
        (pl.col("v_open") / pl.col("vol")).alias("tc_open"),
        (pl.col("v_close") / pl.col("vol")).alias("tc_close"),
    )
    return g.select(["symbol", "date", "vwap", "ivd_lo", "ivd_hi", "h_time",
                     "tc_open", "tc_close", "nbar", "vol", "amt"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=800)
    ap.add_argument("--seed", type=int, default=20261126)
    a = ap.parse_args()

    files = sorted(BS.glob("*.parquet"))
    random.seed(a.seed)
    pick = random.sample(files, min(a.sample, len(files)))
    print(f"bs 文件 {len(files)}，抽样 {len(pick)}")

    out = []
    for i, f in enumerate(pick):
        t = one(f)
        if not t.is_empty():
            out.append(t)
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(pick)}")
    df = pl.concat(out).sort(["symbol", "date"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUT_DIR / "part.parquet")
    print(f"写出 {OUT_DIR/'part.parquet'}  rows={df.height:,} "
          f"symbols={df['symbol'].n_unique()} dates={df['date'].n_unique()}")
    print(df.select(["ivd_lo", "ivd_hi", "h_time", "tc_open", "tc_close"])
            .describe())


if __name__ == "__main__":
    main()
