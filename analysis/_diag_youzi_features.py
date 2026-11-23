"""游资参与标的的「结构特征」反推 —— 用真标签（龙虎榜游资净额）而不是印象。

两个标签，互为稳健性检验：
  A（真标签，短窗口）: 2025-11-12 ~ 2026-11-19 龙虎榜 hot_money_net_value 非空
  B（代理，长窗口）: consecutive_limit_ups >= 2（连板）

核心问题：容量（amt20 / 自由流通市值）到底能不能区分「游资会来」和「游资不来」。
口径：分母 = 该档全部 (symbol,date) 股票日；分子 = 该档被游资参与的股票日。
      ⇒ 命中率是无条件概率，可直接与基准率比 lift。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"
OUT.mkdir(parents=True, exist_ok=True)

pl.Config.set_tbl_rows(60)
pl.Config.set_tbl_width_chars(220)
pl.Config.set_tbl_cols(30)


def load_panel() -> pl.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2025-01"]
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select([
            "symbol", "date", "open", "high", "low", "close", "raw_close",
            "amount", "turnover_rate", "consecutive_limit_ups",
        ])
        .collect()
        .sort(["symbol", "date"])
    )
    return df


def add_features(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        pl.col("amount").rolling_mean(20).over("symbol").alias("amt20"),
        pl.col("amount").rolling_mean(5).over("symbol").alias("amt5"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turn20"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1.0).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(120).over("symbol") - 1.0).alias("ret120"),
        pl.col("consecutive_limit_ups").shift(1).over("symbol").alias("lu_prev"),
        # 20 日内涨停次数（不含当日）
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int32)
            .shift(1).over("symbol").rolling_sum(20).alias("n_lu_20d"),
        pl.len().over("symbol").alias("n_bars_so_far"),
    ]).with_columns([
        # 流通市值（元）：amount = 成交额, turnover_rate 为百分数
        pl.when(pl.col("turnover_rate") > 0)
          .then(pl.col("amount") / (pl.col("turnover_rate") / 100.0))
          .otherwise(None).alias("fmcap"),
    ])


def attach_ownership(df: pl.DataFrame) -> pl.DataFrame:
    tf = pl.read_parquet(glob.glob(str(ROOT / "data/shareholder/top_float/*.parquet"))[0])
    latest = tf.group_by("code").agg(pl.col("period_end").max().alias("period_end"))
    tf = tf.join(latest, on=["code", "period_end"], how="inner")
    own = tf.group_by("code").agg([
        pl.col("ratio").max().alias("top1_ratio"),
        pl.when(pl.col("ratio") >= 5.0).then(pl.col("ratio")).otherwise(0.0).sum().alias("block_ratio"),
    ]).with_columns(
        pl.col("code").str.replace(r"\..*$", "").alias("bare"),
        (100.0 - pl.col("block_ratio")).alias("free_float_pct"),
    )
    inst = pl.read_parquet(ROOT / "data/instruments/instruments.parquet", columns=None)
    inst = inst.select([c for c in ["symbol", "name", "total_shares", "list_date"] if c in inst.columns])
    return (
        df.with_columns(pl.col("symbol").str.replace(r"\..*$", "").alias("bare"))
          .join(own.select(["bare", "top1_ratio", "free_float_pct"]), on="bare", how="left")
          .join(inst, on="symbol", how="left")
          .with_columns(
              (pl.col("fmcap") * pl.col("free_float_pct") / 100.0).alias("ffcap"),
              (pl.col("raw_close") * pl.col("total_shares")).alias("mktcap"),
          )
          .drop("bare")
    )


def load_dragon() -> pl.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(ROOT / "data/dragon_tiger/date=*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        items = (d.get("all") or {}).get("stock_items") or []
        for it in items:
            rows.append({
                "symbol": it.get("thscode"),
                "date": d.get("trade_date"),
                "hm_net": it.get("hot_money_net_value"),
                "net_value": it.get("net_value"),
                "buy_value": it.get("buy_value"),
                "sell_value": it.get("sell_value"),
                "range_days": it.get("range_days"),
                "hot_rank": it.get("hot_rank"),
                "limit_reason": it.get("limit_reason"),
                "name": it.get("name"),
            })
    d = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())
    # 同一 symbol/date 多行（不同 range_days）：取 range_days 最小的那一行（当日榜）
    d = d.sort(["symbol", "date", "range_days"]).group_by(["symbol", "date"], maintain_order=True).first()
    return d


def bucket_amt(x: pl.Expr) -> pl.Expr:
    return (
        pl.when(x < 1.5e7).then(pl.lit("<1500"))
        .when(x < 3.0e7).then(pl.lit("1500-3000"))
        .when(x < 5.0e7).then(pl.lit("3000-5000"))
        .when(x < 1.0e8).then(pl.lit("5000-1w"))
        .when(x < 2.0e8).then(pl.lit("1-2w"))
        .when(x < 5.0e8).then(pl.lit("2-5w"))
        .otherwise(pl.lit(">5w"))
    )


def bucket_ff(x: pl.Expr) -> pl.Expr:
    return (
        pl.when(x < 1.0e9).then(pl.lit("<10"))
        .when(x < 2.0e9).then(pl.lit("10-20"))
        .when(x < 4.0e9).then(pl.lit("20-40"))
        .when(x < 8.0e9).then(pl.lit("40-80"))
        .when(x < 2.0e10).then(pl.lit("80-200"))
        .otherwise(pl.lit(">200"))
    )


def hit_table(df: pl.DataFrame, bucket_col: str, label: str, order: list[str]) -> pl.DataFrame:
    t = (
        df.group_by(bucket_col)
        .agg([
            pl.len().alias("n"),
            pl.col(label).sum().alias("n_hit"),
            (pl.col(label).mean() * 1e4).alias("hit_bp"),
        ])
        .with_columns((pl.col("n_hit") / pl.col("n") * 1e4).alias("hit_bp2"))
    )
    base = df[label].mean() * 1e4
    t = t.with_columns([
        (pl.col("hit_bp2") / base).alias("lift"),
        pl.lit(base).alias("base_bp"),
    ])
    # 按给定顺序排
    t = t.with_columns(
        pl.col(bucket_col).replace_strict({k: i for i, k in enumerate(order)}, default=99).alias("_o")
    ).sort("_o").drop("_o")
    return t


def main() -> None:
    print("装载面板…", flush=True)
    df = add_features(load_panel())
    df = attach_ownership(df)
    print(f"  行数 {df.height:,}  {df['date'].min()} ~ {df['date'].max()}", flush=True)

    print("装载龙虎榜…", flush=True)
    dr = load_dragon()
    print(f"  龙虎榜行 {dr.height:,}  {dr['date'].min()} ~ {dr['date'].max()}", flush=True)

    # 标签
    dr = dr.with_columns([
        pl.col("hm_net").is_not_null().alias("y_hm_any"),
        (pl.col("hm_net").fill_null(0) > 0).alias("y_hm_buy"),
        (pl.col("hm_net").fill_null(0) < 0).alias("y_hm_sell"),
    ])
    d = df.join(dr.select(["symbol", "date", "hm_net", "y_hm_any", "y_hm_buy", "y_hm_sell",
                           "net_value", "buy_value", "sell_value", "range_days", "limit_reason"]),
                on=["symbol", "date"], how="left")
    d = d.with_columns([
        pl.col("y_hm_any").fill_null(False),
        pl.col("y_hm_buy").fill_null(False),
        pl.col("y_hm_sell").fill_null(False),
        (pl.col("consecutive_limit_ups") >= 2).alias("y_2board"),
        (pl.col("consecutive_limit_ups") >= 1).alias("y_limitup"),
    ])

    # 样本期：龙虎榜覆盖期，且特征有效
    panel = d.filter(
        (pl.col("date") >= pl.date(2025, 11, 12))
        & (pl.col("date") <= pl.date(2026, 11, 19))
        & pl.col("amt20").is_not_null()
        & pl.col("ffcap").is_not_null()
        & (pl.col("n_bars_so_far") > 120)
    ).with_columns([
        bucket_amt(pl.col("amt20")).alias("b_amt"),
        bucket_ff(pl.col("ffcap")).alias("b_ff"),
    ])
    print(f"\n样本期股票日 {panel.height:,}  基准率: 游资参与 {panel['y_hm_any'].mean()*1e4:.0f}bp "
          f"/ 净买 {panel['y_hm_buy'].mean()*1e4:.0f}bp / 涨停 {panel['y_limitup'].mean()*1e4:.0f}bp "
          f"/ 2连板 {panel['y_2board'].mean()*1e4:.0f}bp", flush=True)

    res: dict = {}
    res["panel"] = {
        "rows": panel.height,
        "base_hm_any_bp": float(panel["y_hm_any"].mean() * 1e4),
        "base_hm_buy_bp": float(panel["y_hm_buy"].mean() * 1e4),
        "base_limitup_bp": float(panel["y_limitup"].mean() * 1e4),
        "base_2board_bp": float(panel["y_2board"].mean() * 1e4),
        "n_symbols": int(panel["symbol"].n_unique()),
    }

    ORD_AMT = ["<1500", "1500-3000", "3000-5000", "5000-1w", "1-2w", "2-5w", ">5w"]
    ORD_FF = ["<10", "10-20", "20-40", "40-80", "80-200", ">200"]

    print("\n=== A. 20日均成交额（万元档）→ 游资参与命中率 ===", flush=True)
    t1 = hit_table(panel, "b_amt", "y_hm_any", ORD_AMT)
    print(t1.select(["b_amt", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["by_amt_hm_any"] = t1.to_dicts()

    print("\n=== A2. 同档 → 涨停 / 2连板 命中率 ===", flush=True)
    t1b = hit_table(panel, "b_amt", "y_limitup", ORD_AMT)
    print(t1b.select(["b_amt", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["by_amt_limitup"] = t1b.to_dicts()
    t1c = hit_table(panel, "b_amt", "y_2board", ORD_AMT)
    print(t1c.select(["b_amt", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["by_amt_2board"] = t1c.to_dicts()

    print("\n=== B. 自由流通市值（亿元档）→ 游资参与命中率 ===", flush=True)
    t2 = hit_table(panel, "b_ff", "y_hm_any", ORD_FF)
    print(t2.select(["b_ff", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["by_ff_hm_any"] = t2.to_dicts()
    t2b = hit_table(panel, "b_ff", "y_2board", ORD_FF)
    print(t2b.select(["b_ff", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["by_ff_2board"] = t2b.to_dicts()

    print("\n=== C. 二维：自由流通 × 成交额 → 游资命中率(bp) ===", flush=True)
    heat = (
        panel.group_by(["b_ff", "b_amt"])
        .agg([pl.len().alias("n"), (pl.col("y_hm_any").mean() * 1e4).alias("hit_bp")])
        .pivot(on="b_amt", index="b_ff", values="hit_bp")
    )
    print(heat)
    res["heat_ff_amt_hm"] = heat.to_dicts()
    heatn = (
        panel.group_by(["b_ff", "b_amt"]).agg(pl.len().alias("n"))
        .pivot(on="b_amt", index="b_ff", values="n")
    )
    print("\n--- 同格样本数 ---")
    print(heatn)
    res["heat_ff_amt_n"] = heatn.to_dicts()

    print("\n=== D. 游资真实参与率：|hm_net| / 当日成交额 ===", flush=True)
    p2 = panel.filter(pl.col("y_hm_any") & (pl.col("amount") > 0)).with_columns(
        (pl.col("hm_net").abs() / pl.col("amount")).alias("part_rate"),
        (pl.col("hm_net") / pl.col("amount")).alias("part_rate_signed"),
        (pl.col("amount") / pl.col("amt20")).alias("vol_ratio"),
    )
    qs = [0.1, 0.25, 0.5, 0.75, 0.9, 0.99]
    pr = {f"p{int(q*100)}": float(p2["part_rate"].quantile(q)) for q in qs}
    pr["n"] = p2.height
    print("  参与率分位:", json.dumps(pr, ensure_ascii=False))
    vr = {f"p{int(q*100)}": float(p2["vol_ratio"].quantile(q)) for q in qs}
    vr["mean"] = float(p2["vol_ratio"].mean())
    print("  当日量/20日均量 分位:", json.dumps(vr, ensure_ascii=False))
    res["part_rate"] = pr
    res["vol_ratio"] = vr

    print("\n=== E. 上榜连续性：同一票连续上榜天数 ===", flush=True)
    dr2 = dr.sort(["symbol", "date"]).with_columns(
        pl.col("date").diff().over("symbol").dt.total_days().alias("gap")
    ).with_columns((pl.col("gap") != 1).fill_null(True).cum_sum().over("symbol").alias("run"))
    runs = dr2.group_by(["symbol", "run"]).agg([
        pl.len().alias("len"), pl.col("date").min().alias("start"), pl.col("hm_net").sum().alias("net_sum")
    ])
    rl = runs.group_by("len").agg(pl.len().alias("n_runs")).sort("len")
    print(rl.head(15))
    res["run_len"] = rl.to_dicts()

    print("\n=== F. limit_reason 词频（题材） ===", flush=True)
    reasons = dr.filter(pl.col("limit_reason").is_not_null())["limit_reason"].to_list()
    import re, collections
    words = collections.Counter()
    for r in reasons:
        for w in re.split(r"[+＋、,，/／\s]+", r):
            w = w.strip()
            if 2 <= len(w) <= 12:
                words[w] += 1
    print("  样本", len(reasons), " 唯一词", len(words))
    for w, c in words.most_common(40):
        print(f"   {w:14s} {c}")
    res["reason_words"] = words.most_common(60)

    print("\n=== G. 用户口径专项：amt20∈[1500,3000]万 且 ffcap∈[10,20]亿 ===", flush=True)
    tgt = panel.filter(
        (pl.col("amt20") >= 1.5e7) & (pl.col("amt20") < 3.0e7)
        & (pl.col("ffcap") >= 1.0e9) & (pl.col("ffcap") < 2.0e9)
    )
    g = {
        "n_stock_days": tgt.height,
        "n_symbols": int(tgt["symbol"].n_unique()),
        "n_hm_any": int(tgt["y_hm_any"].sum()),
        "hit_bp": float(tgt["y_hm_any"].mean() * 1e4),
        "n_limitup": int(tgt["y_limitup"].sum()),
        "limitup_bp": float(tgt["y_limitup"].mean() * 1e4),
        "n_2board": int(tgt["y_2board"].sum()),
        "board2_bp": float(tgt["y_2board"].mean() * 1e4),
        "base_hm_bp": float(panel["y_hm_any"].mean() * 1e4),
        "base_limitup_bp": float(panel["y_limitup"].mean() * 1e4),
    }
    print(json.dumps(g, ensure_ascii=False, indent=1))
    res["user_cell"] = g

    # 该格子里真被游资碰过的票长什么样
    touched = tgt.filter(pl.col("y_hm_any")).select(
        ["symbol", "date", "amt20", "ffcap", "turn20", "ret20", "ret120", "n_lu_20d", "hm_net", "limit_reason"]
    ).sort("date", descending=True)
    print("\n  --- 该格子内被游资碰过的样本（最近 30 条）---")
    print(touched.head(30))
    res["user_cell_touched"] = touched.head(30).to_dicts()

    with open(OUT / "youzi_features.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n写出 {OUT / 'youzi_features.json'}")


if __name__ == "__main__":
    main()
