"""龙虎榜营业部明细质检 + 前视检测。

前视检测是关键：东财的 RISE_PROBABILITY_3DAY 若是「当前时刻快照」，
那 2025-11-12 的行里已经含了 2025-11-12 之后的信息 —— 不能直接用。
判据：同一营业部在不同日期的 rise_prob_3d 是否变化。
  变化 → 截至当日算的 → 可用；不变 → 当前快照 → 前视 → 必须自算。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "dragon_tiger_seats"

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(240)
pl.Config.set_tbl_cols(40)


def main() -> None:
    files = sorted(OUT.glob("date=*.parquet"))
    print(f"落盘 {len(files)} 天", flush=True)
    df = pl.concat([pl.read_parquet(f) for f in files], how="vertical_relaxed")
    print(f"总记录 {df.height:,} 行，营业部 {df['dept_name'].n_unique():,} 个，票 {df['symbol'].n_unique():,} 只", flush=True)

    # 按方向统计
    print("\n=== 方向分布 ===")
    print(df.group_by("side").agg([pl.len().alias("n"),
                                   pl.col("dept_name").n_unique().alias("depts")]))

    # 前视检测：找出现最多的营业部，看其 rise_prob_3d 是否随时间变化
    top = df.group_by("dept_name").len().sort("len", descending=True).head(5)
    print("\n=== Top5 营业部 ===")
    print(top)
    for dept in top["dept_name"].to_list():
        sub = df.filter(pl.col("dept_name") == dept).sort("date")
        rp = sub.select(["date", "rise_prob_3d"]).unique(subset=["date"]).head(6)
        print(f"\n{dept[:20]} 的 rise_prob_3d 跨日期：")
        print(rp)
        vals = sub["rise_prob_3d"].drop_nulls().unique().to_list()
        print(f"  唯一值 {len(vals)} 个 → {'随时间变化(可用)' if len(vals) > 3 else '疑似当前快照(前视!)'}")

    # 席位声誉快照版（先看分布，不急着定论）
    print("\n=== rise_prob_3d 分布 ===")
    print(df.select([pl.col("rise_prob_3d").mean().alias("mean"),
                     pl.col("rise_prob_3d").median().alias("med"),
                     pl.col("rise_prob_3d").quantile(0.9).alias("p90")]))
    print("=== sales_times_3d 分布 ===")
    print(df.select([pl.col("sales_times_3d").mean().alias("mean"),
                     pl.col("sales_times_3d").max().alias("max")]))

    df.write_parquet(ROOT / "data/.scratch/youzi_seats_all.parquet")
    print(f"\n合并写出 data/.scratch/youzi_seats_all.parquet")


if __name__ == "__main__":
    main()
