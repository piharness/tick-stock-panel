"""诊断：短线雷达（原市场公民）页面的「博弈角色主场」分类是否有独立信息。

问题：用户要求把页面主轴从「公民等级」换成「博弈角色主场」。
若主场只是把 level 换个名字，这次改版就是装饰——页面更有说服力，bug 更隐蔽。

本脚本回答三件事：
  1. 候选切分（市值 × 换手 四象限）与现有 level 的重叠度有多高？
  2. 各主场格子的画像（市值/换手/成交额/振幅/板块）是否真的不同？
  3. 各主场格子的「左尾风险 + 中位收益」是否分层（外部验证，用真实前瞻收益）？

只读本地 data/，不写任何生产数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENRICHED = DATA / "kline_daily_enriched"


def load_inst() -> pl.DataFrame:
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet")
    return inst.select(["symbol", "name", "float_shares"])


def cross_section(day: str, inst: pl.DataFrame) -> pl.DataFrame:
    f = ENRICHED / f"date={day}" / "part.parquet"
    if not f.exists():
        return pl.DataFrame()
    d = pl.read_parquet(f)
    df = (
        d.join(inst, on="symbol", how="left")
        .filter(
            (pl.col("close") > 0)
            & (pl.col("amount") > 0)
            & (pl.col("float_shares") > 0)
            & (pl.col("turnover_rate") > 0)
        )
        .with_columns([
            (pl.col("close") * pl.col("float_shares")).alias("mcap"),
            (100.0 / pl.col("turnover_rate")).alias("h_days"),
            ((pl.col("high") - pl.col("low")) / pl.col("close") * 100).alias("amp"),
            ((pl.col("close") - pl.col("low"))
             / (pl.col("high") - pl.col("low")).clip(1e-9, None)).alias("clspos"),
        ])
    )
    # 当日全市场分位
    n = pl.col("mcap").count() - 1
    df = df.with_columns([
        ((pl.col(c).rank("ordinal") - 1) / n.clip(1, None)).alias(f"p_{c}")
        for c in ("mcap", "turnover_rate", "amount")
    ])
    df = df.with_columns(
        pl.when(pl.col("p_amount") >= 0.90).then(pl.lit("core"))
        .when((pl.col("p_amount") >= 0.50) | (pl.col("p_turnover_rate") >= 0.90))
        .then(pl.lit("active"))
        .when((pl.col("p_amount") < 0.10) & (pl.col("p_turnover_rate") < 0.10))
        .then(pl.lit("silent"))
        .otherwise(pl.lit("background"))
        .alias("level")
    )
    return df


def quadrant(df: pl.DataFrame, mcap_q: float, turn_q: float) -> pl.DataFrame:
    """市值 × 换手 四象限（分位阈值）。"""
    big = pl.col("p_mcap") >= mcap_q
    hot = pl.col("p_turnover_rate") >= turn_q
    return df.with_columns(
        pl.when(big & hot).then(pl.lit("D_big_hot"))
        .when(big & ~hot).then(pl.lit("A_big_cold"))
        .when(~big & hot).then(pl.lit("C_small_hot"))
        .otherwise(pl.lit("B_small_cold"))
        .alias("arena")
    )


def profile(df: pl.DataFrame, key: str) -> pl.DataFrame:
    return (
        df.group_by(key)
        .agg([
            pl.len().alias("n"),
            (pl.col("mcap").median() / 1e8).round(1).alias("mcap亿_med"),
            pl.col("turnover_rate").median().round(2).alias("turn_med"),
            pl.col("h_days").median().round(0).alias("h_med"),
            (pl.col("amount").median() / 1e8).round(2).alias("amt亿_med"),
            pl.col("amp").median().round(2).alias("amp_med"),
            pl.col("clspos").median().round(2).alias("clspos_med"),
        ])
        .sort(key)
    )


def board_of(sym: str) -> str:
    if sym.startswith("688"):
        return "科创"
    if sym.startswith("300") or sym.startswith("301"):
        return "创业"
    if sym.startswith(("8", "4", "9")):
        return "北交"
    if sym.startswith("6"):
        return "沪主板"
    return "深主板"


def main() -> int:
    inst = load_inst()
    days = sorted(p.name[5:] for p in ENRICHED.glob("date=*"))
    latest = days[-1]
    print(f"最新交易日 {latest}，共 {len(days)} 个分区\n")

    df = cross_section(latest, inst)
    print(f"横截面 N={df.height}\n")

    # ---- 1. 四象限默认阈值：市值 p50，换手 p75 ----
    # 语义：大市值 = 全市场中位以上（≈40 亿）；高换手 = 前 25%（≈3.5%）
    for mq, tq in [(0.50, 0.75), (0.75, 0.75), (0.60, 0.80)]:
        q = quadrant(df, mq, tq)
        print(f"=== 阈值 mcap p{int(mq*100)} / turn p{int(tq*100)} ===")
        print(profile(q, "arena"))
        ct = q.group_by(["arena", "level"]).len().pivot(
            on="level", index="arena", values="len", aggregate_function="sum"
        ).fill_null(0)
        print(ct.sort("arena"))
        # 一致率：每个 arena 内 level 的最大占比
        share = (
            q.group_by(["arena", "level"]).len()
            .with_columns((pl.col("len") / pl.col("len").sum().over("arena")).round(3).alias("share"))
        )
        print("arena 内 level 最大占比：",
              share.group_by("arena").agg(pl.col("share").max().alias("max_share")).sort("arena").to_dicts())
        print()

    # ---- 2. 板块分布（默认阈值）----
    q = quadrant(df, 0.50, 0.75).with_columns(
        pl.col("symbol").map_elements(board_of, return_dtype=pl.Utf8).alias("board")
    )
    print("=== 板块 × 主场 ===")
    print(
        q.group_by(["arena", "board"]).len()
        .with_columns((pl.col("len") / pl.col("len").sum().over("arena") * 100).round(1).alias("pct"))
        .sort(["arena", "len"], descending=[False, True])
    )

    # ---- 3. 左尾/右尾：用 20 交易日后收盘价做外部验证 ----
    # 取最近的横截面里能算出 fwd20 的一天
    horizon = 20
    use_idx = len(days) - 1 - horizon
    if use_idx > 0:
        base_day = days[use_idx]
        fwd_day = days[-1]
        print(f"\n=== 前瞻 {horizon} 日：{base_day} → {fwd_day} ===")
        base = cross_section(base_day, inst)
        fwd = pl.read_parquet(ENRICHED / f"date={fwd_day}" / "part.parquet").select(
            ["symbol", "close"]
        ).rename({"close": "close_fwd"})
        res = quadrant(base, 0.50, 0.75).join(fwd, on="symbol", how="inner").with_columns(
            (pl.col("close_fwd") / pl.col("close") - 1).alias("ret")
        )
        agg = (
            res.group_by("arena")
            .agg([
                pl.len().alias("n"),
                (pl.col("ret").median() * 100).round(2).alias("ret_med_%"),
                (pl.col("ret").mean() * 100).round(2).alias("ret_mean_%"),
                (pl.col("ret") < -0.2).mean().round(4).alias("p_dd20"),
                (pl.col("ret") > 0.2).mean().round(4).alias("p_up20"),
            ])
            .sort("arena")
        )
        print(agg)
        agg_lv = (
            res.group_by("level")
            .agg([
                pl.len().alias("n"),
                (pl.col("ret").median() * 100).round(2).alias("ret_med_%"),
                (pl.col("ret") < -0.2).mean().round(4).alias("p_dd20"),
            ])
            .sort("level")
        )
        print("\n同一日按 level 分组：")
        print(agg_lv)

    return 0


if __name__ == "__main__":
    sys.exit(main())
