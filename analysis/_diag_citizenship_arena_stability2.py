"""诊断三：主场分类该用「当日」还是「滚动中位」？

「主场」是结构性概念（这里平时是谁的地盘）。若用当日换手率分类，一只票
换手从 3.4% 跳到 3.6% 就越过阈值，日间来回跳 —— 那它就不是主场，是噪声。

本脚本测：同一只票连续两个交易日的 arena 一致率，
  当日版（τ_t）vs 20 日中位版（median τ over t-19..t）。
一致率高 = 分类稳定 = 「主场」成立。
"""
from __future__ import annotations

import sys

import polars as pl

from _diag_citizenship_arena import ENRICHED, load_inst

LOOKBACK = 20
RECENT = 60


def main() -> int:
    inst = load_inst()
    days = sorted(p.name[5:] for p in ENRICHED.glob("date=*"))
    window = days[-(RECENT + LOOKBACK):]
    print(f"窗口 {window[0]} .. {window[-1]}（{len(window)} 日）")

    inst2 = inst.select(["symbol", "float_shares"])
    frames = []
    for d in window:
        t = pl.read_parquet(ENRICHED / f"date={d}" / "part.parquet").select(
            ["symbol", "close", "amount", "turnover_rate", "date"]
        )
        frames.append(t)
    panel = pl.concat(frames, how="vertical_relaxed")
    print(f"面板 {panel.height} 行 / {panel['symbol'].n_unique()} 只\n")

    panel = panel.join(inst2, on="symbol", how="left").filter(
        (pl.col("close") > 0) & (pl.col("amount") > 0)
        & (pl.col("float_shares") > 0) & (pl.col("turnover_rate") > 0)
    ).with_columns(
        (pl.col("close") * pl.col("float_shares")).alias("mcap"),
        pl.col("date").cast(pl.Utf8).alias("dstr"),
    )

    # 滚动中位（按 symbol，窗口 LOOKBACK，只含当日及之前）
    panel = panel.sort(["symbol", "date"]).with_columns([
        pl.col("turnover_rate").rolling_median(window_size=LOOKBACK, min_samples=5)
        .over("symbol").alias("turn_med"),
        pl.col("mcap").rolling_median(window_size=LOOKBACK, min_samples=5)
        .over("symbol").alias("mcap_med"),
    ])

    # 当日横截面分位
    def add_arena(df: pl.DataFrame, mcol: str, tcol: str, tag: str) -> pl.DataFrame:
        n = pl.col(mcol).count() - 1
        df = df.with_columns([
            ((pl.col(mcol).rank("ordinal") - 1) / n.clip(1, None)).alias("_pm"),
            ((pl.col(tcol).rank("ordinal") - 1) / n.clip(1, None)).alias("_pt"),
        ])
        return df.with_columns(
            pl.when((pl.col("_pm") >= 0.50) & (pl.col("_pt") >= 0.75)).then(pl.lit("D"))
            .when((pl.col("_pm") >= 0.50) & (pl.col("_pt") < 0.75)).then(pl.lit("A"))
            .when((pl.col("_pm") < 0.50) & (pl.col("_pt") >= 0.75)).then(pl.lit("C"))
            .otherwise(pl.lit("B")).alias(tag)
        )

    # 「当日」版：分位需按当日横截面算 → 逐日 map
    today_parts = []
    for d in window:
        cur = panel.filter(pl.col("dstr") == d)
        today_parts.append(add_arena(cur, "mcap", "turnover_rate", "arena_today"))
    today = pl.concat(today_parts, how="vertical_relaxed")

    # 「中位」版：同样逐日横截面分位（用中位值）
    med_parts = []
    for d in window:
        cur = panel.filter(pl.col("dstr") == d).filter(pl.col("turn_med").is_not_null())
        med_parts.append(add_arena(cur, "mcap_med", "turn_med", "arena_med"))
    med = pl.concat(med_parts, how="vertical_relaxed")

    def switch_rate(df: pl.DataFrame, col: str) -> dict:
        s = df.sort(["symbol", "date"]).with_columns(
            pl.col(col).shift(1).over("symbol").alias("prev")
        ).drop_nulls(["prev"])
        same = (s[col] == s["prev"]).mean()
        return {"col": col, "n": s.height, "一致率": round(float(same), 4),
                "日间切换率": round(1 - float(same), 4)}

    print("=== 日间 arena 一致率（越接近 1 越像「主场」）===")
    for r in (switch_rate(today, "arena_today"), switch_rate(med, "arena_med")):
        print(r)

    # 中位版四格分布（最新一天）
    last = med.filter(pl.col("dstr") == window[-1])
    print("\n=== 中位版最新一天分布 ===")
    print(last.group_by("arena_med").len().sort("arena_med"))
    print("\n=== 当日版最新一天分布 ===")
    print(today.filter(pl.col("dstr") == window[-1]).group_by("arena_today").len().sort("arena_today"))

    # 中位版四格画像
    print("\n=== 中位版四格画像（最新一天）===")
    print(
        last.group_by("arena_med").agg([
            pl.len().alias("n"),
            (pl.col("mcap_med") / 1e8).median().round(1).alias("mcap亿_med"),
            pl.col("turn_med").median().round(2).alias("turn_med_中位"),
            (pl.col("amount").median() / 1e8).round(2).alias("amt亿_med"),
        ]).sort("arena_med")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
