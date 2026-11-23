"""游资特征的第二步：剥离反向因果，并算清「建仓-出货」的真实容量。

三件事：
  1. 冷票条件下重算 amt20 效应（n_lu_20d==0 且 |ret20|<15%）——排除「成交额大是因为已经在炒」
  2. 容量模拟：真实游资单日买入占当日成交额、T+1 出货占 T+1 成交额 ⇒ 「进得去出不来」的正确算法
  3. 用户标的同构：amt20∈[1500,3000]万、ffcap∈[10,15]亿、冷票 ⇒ 未来 20 日命运
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

from _diag_youzi_features import (  # noqa: E402
    add_features, attach_ownership, bucket_amt, bucket_ff, hit_table, load_dragon, load_panel,
)

pl.Config.set_tbl_rows(80)
pl.Config.set_tbl_width_chars(230)
pl.Config.set_tbl_cols(30)


def main() -> None:
    df = add_features(load_panel())
    df = attach_ownership(df)
    dr = load_dragon()
    dr = dr.with_columns([
        pl.col("hm_net").is_not_null().alias("y_hm_any"),
    ])
    d = df.join(
        dr.select(["symbol", "date", "hm_net", "y_hm_any", "range_days", "limit_reason"]),
        on=["symbol", "date"], how="left",
    ).with_columns([
        pl.col("y_hm_any").fill_null(False),
        (pl.col("consecutive_limit_ups") >= 1).alias("y_lu"),
        (pl.col("consecutive_limit_ups") >= 2).alias("y_2b"),
        # 未来 20 日：是否涨停 / 连板 / 最大涨幅
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(20).over("symbol") >= 1).alias("f_lu20"),
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(20).over("symbol") >= 2).alias("f_2b20"),
        (pl.col("high").shift(-1).rolling_max(20).over("symbol") / pl.col("close") - 1.0).alias("f_maxret20"),
        # 次日成交额（出货容量）
        pl.col("amount").shift(-1).over("symbol").alias("amt_next"),
        pl.col("amount").shift(-2).over("symbol").alias("amt_next2"),
    ])

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

    res: dict = {}
    ORD_AMT = ["<1500", "1500-3000", "3000-5000", "5000-1w", "1-2w", "2-5w", ">5w"]

    # ---------- 1. 冷票条件 ----------
    cold = panel.filter(
        (pl.col("n_lu_20d").fill_null(0) == 0)
        & (pl.col("ret20").abs() < 0.15)
        & pl.col("ret120").is_not_null()
    )
    print(f"=== 1. 冷票子样本（20日无涨停 且 |ret20|<15%）===", flush=True)
    print(f"  行 {cold.height:,}  基准游资率 {cold['y_hm_any'].mean()*1e4:.1f}bp "
          f"涨停率 {cold['y_lu'].mean()*1e4:.1f}bp 2连板 {cold['y_2b'].mean()*1e4:.1f}bp", flush=True)

    t = hit_table(cold, "b_amt", "y_hm_any", ORD_AMT)
    print("\n  冷票内 amt20 分档 → 游资命中率")
    print(t.select(["b_amt", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["cold_by_amt_hm"] = t.to_dicts()

    t2 = hit_table(cold, "b_amt", "y_lu", ORD_AMT)
    print("\n  冷票内 amt20 分档 → 当日涨停命中率")
    print(t2.select(["b_amt", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["cold_by_amt_lu"] = t2.to_dicts()

    t3 = hit_table(cold, "b_ff", "y_hm_any", ["<10", "10-20", "20-40", "40-80", "80-200", ">200"])
    print("\n  冷票内 ffcap 分档 → 游资命中率")
    print(t3.select(["b_ff", "n", "n_hit", "hit_bp2", "base_bp", "lift"]))
    res["cold_by_ff_hm"] = t3.to_dicts()

    # 冷票内 amt20 × ffcap 二维（只看游资）
    heat = (
        cold.group_by(["b_ff", "b_amt"])
        .agg([pl.len().alias("n"), (pl.col("y_hm_any").mean() * 1e4).alias("bp")])
        .filter(pl.col("n") >= 500)
        .sort(["b_ff", "b_amt"])
    )
    print("\n  冷票内 二维（仅 n>=500 格）")
    print(heat)
    res["cold_heat"] = heat.to_dicts()

    # ---------- 2. 容量模拟 ----------
    print("\n=== 2. 容量：真实买入占当日成交额 / 出货占次日成交额 ===", flush=True)
    ev = panel.filter(pl.col("y_hm_any") & pl.col("hm_net").is_not_null() & (pl.col("hm_net") > 0)) \
        .with_columns([
            (pl.col("hm_net") / pl.col("amount")).alias("buy_share"),
            (pl.col("hm_net") / pl.col("amt_next")).alias("sell_share_next"),
            (pl.col("amount") / pl.col("amt20")).alias("vol_x"),
            (pl.col("amt_next") / pl.col("amt20")).alias("next_vol_x"),
        ]).filter(pl.col("amt_next").is_not_null() & (pl.col("amt_next") > 0))
    qs = [0.1, 0.25, 0.5, 0.75, 0.9, 0.95]
    cap = {
        "n": ev.height,
        "hm_net_wan": {f"p{int(q*100)}": float(ev["hm_net"].quantile(q) / 1e4) for q in qs},
        "buy_share": {f"p{int(q*100)}": float(ev["buy_share"].quantile(q)) for q in qs},
        "sell_share_next": {f"p{int(q*100)}": float(ev["sell_share_next"].quantile(q)) for q in qs},
        "vol_x": {f"p{int(q*100)}": float(ev["vol_x"].quantile(q)) for q in qs},
        "next_vol_x": {f"p{int(q*100)}": float(ev["next_vol_x"].quantile(q)) for q in qs},
    }
    print("  游资净买入额(万):", json.dumps({k: round(v, 0) for k, v in cap["hm_net_wan"].items()}, ensure_ascii=False))
    print("  买入占当日成交:", json.dumps({k: round(v, 4) for k, v in cap["buy_share"].items()}, ensure_ascii=False))
    print("  卖出占次日成交:", json.dumps({k: round(v, 4) for k, v in cap["sell_share_next"].items()}, ensure_ascii=False))
    print("  当日量/20日均量:", json.dumps({k: round(v, 2) for k, v in cap["vol_x"].items()}, ensure_ascii=False))
    print("  次日量/20日均量:", json.dumps({k: round(v, 2) for k, v in cap["next_vol_x"].items()}, ensure_ascii=False))
    res["capacity"] = cap

    # 分档：游资净买入额 vs 次日成交额，看出货难度
    print("\n  按游资净买入额分档 → 次日「卖出占次日成交额」中位数")
    ev2 = ev.with_columns(
        pl.when(pl.col("hm_net") < 5e6).then(pl.lit("<500"))
        .when(pl.col("hm_net") < 1e7).then(pl.lit("500-1000"))
        .when(pl.col("hm_net") < 2e7).then(pl.lit("1000-2000"))
        .when(pl.col("hm_net") < 5e7).then(pl.lit("2000-5000"))
        .otherwise(pl.lit(">5000")).alias("b_size")
    ).group_by("b_size").agg([
        pl.len().alias("n"),
        pl.col("hm_net").median().alias("net_med"),
        pl.col("sell_share_next").median().alias("sell_share_med"),
        (pl.col("sell_share_next") > 0.3).mean().alias("p_gt30"),
        (pl.col("sell_share_next") > 0.5).mean().alias("p_gt50"),
    ])
    print(ev2)
    res["size_vs_exit"] = ev2.to_dicts()

    # ---------- 3. 用户标的同构 ----------
    print("\n=== 3. 用户口径同构：amt20∈[1500,3000]万, ffcap∈[10,15]亿, 冷票 ===", flush=True)
    tg = panel.filter(
        (pl.col("amt20") >= 1.5e7) & (pl.col("amt20") < 3.0e7)
        & (pl.col("ffcap") >= 1.0e9) & (pl.col("ffcap") < 1.5e9)
        & (pl.col("n_lu_20d").fill_null(0) == 0)
        & (pl.col("ret20").abs() < 0.15)
    )
    # 同构组 & 全市场对照
    allcold = cold
    g = {
        "n_stock_days": tg.height, "n_symbols": int(tg["symbol"].n_unique()),
        "hm_bp": float(tg["y_hm_any"].mean() * 1e4), "hm_n": int(tg["y_hm_any"].sum()),
        "lu_bp": float(tg["y_lu"].mean() * 1e4), "lu_n": int(tg["y_lu"].sum()),
        "lu2_bp": float(tg["y_2b"].mean() * 1e4),
        "f_lu20_bp": float(tg["f_lu20"].fill_null(False).mean() * 1e4),
        "f_2b20_bp": float(tg["f_2b20"].fill_null(False).mean() * 1e4),
        "f_maxret20_med": float(tg["f_maxret20"].median()),
        "ctrl_hm_bp": float(allcold["y_hm_any"].mean() * 1e4),
        "ctrl_lu_bp": float(allcold["y_lu"].mean() * 1e4),
        "ctrl_f_lu20_bp": float(allcold["f_lu20"].fill_null(False).mean() * 1e4),
        "ctrl_f_2b20_bp": float(allcold["f_2b20"].fill_null(False).mean() * 1e4),
        "ctrl_f_maxret20_med": float(allcold["f_maxret20"].median()),
    }
    print(json.dumps(g, ensure_ascii=False, indent=1))
    res["user_isomorph"] = g

    # 分年度稳定性
    yr = tg.with_columns(pl.col("date").dt.year().alias("y")).group_by("y").agg([
        pl.len().alias("n"), (pl.col("y_hm_any").mean() * 1e4).alias("hm_bp"),
        (pl.col("y_lu").mean() * 1e4).alias("lu_bp"),
        (pl.col("f_lu20").fill_null(False).mean() * 1e4).alias("f_lu20_bp"),
    ]).sort("y")
    print("\n  分年：")
    print(yr)
    res["user_isomorph_by_year"] = yr.to_dicts()

    # 该组里真的走出连板的样本
    won = tg.filter(pl.col("f_2b20").fill_null(False)).select(
        ["symbol", "date", "amt20", "ffcap", "turn20", "ret20", "n_lu_20d", "f_maxret20"]
    ).sort("date", descending=True)
    print(f"\n  该组未来 20 日出现 2 连板的样本 {won.height} 条，最近 15：")
    print(won.head(15))
    res["user_isomorph_winners"] = won.head(15).to_dicts()

    with open(OUT / "youzi_features2.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n写出 {OUT / 'youzi_features2.json'}")


if __name__ == "__main__":
    main()
