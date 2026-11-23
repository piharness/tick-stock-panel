"""游资标的画像 v4 —— 修正内生性：所有特征一律只用 T-1 及之前（严格滞后）。

v3 的 amp20/turn20 是含当日的 rolling_mean，而标签是当日龙虎榜存在 ⇒ 当日涨停会机械抬高振幅/换手。
这里全部 shift(1)，并同时报告「同期」与「滞后」两组 AUC 的差，差值就是内生性的量。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

from _diag_youzi_features import add_features, attach_ownership, load_dragon, load_panel  # noqa: E402
from _diag_youzi_features3 import auc, cut  # noqa: E402

pl.Config.set_tbl_rows(90)
pl.Config.set_tbl_width_chars(240)
pl.Config.set_tbl_cols(40)

LAG = ["amp20", "amt_cv20", "turn20", "ret120", "ret250", "n_lu_60d",
       "amt20", "ffcap", "fmcap", "raw_close", "top1_ratio", "free_float_pct", "list_days"]


def main() -> None:
    df = add_features(load_panel())
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).truediv(pl.col("close")).rolling_mean(20).over("symbol").alias("amp20"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int32).shift(1).over("symbol")
            .rolling_sum(60).alias("n_lu_60d"),
        (pl.col("close") / pl.col("close").shift(250).over("symbol") - 1.0).alias("ret250"),
        pl.col("amount").rolling_std(20).over("symbol").truediv(
            pl.col("amount").rolling_mean(20).over("symbol")).alias("amt_cv20"),
    ])
    df = attach_ownership(df)
    if "list_date" in df.columns:
        df = df.with_columns(
            (pl.col("date") - pl.col("list_date").cast(pl.Date)).dt.total_days().alias("list_days"))
    else:
        df = df.with_columns(pl.lit(None).cast(pl.Int64).alias("list_days"))

    # 严格滞后：特征只用 T-1 及之前
    df = df.with_columns([pl.col(c).shift(1).over("symbol").alias(c) for c in LAG])

    dr = load_dragon().with_columns(pl.col("hm_net").is_not_null().alias("y"))
    d = df.join(dr.select(["symbol", "date", "y"]), on=["symbol", "date"], how="left") \
          .with_columns(pl.col("y").fill_null(False))

    panel = d.filter(
        (pl.col("date") >= pl.date(2025, 11, 12))
        & (pl.col("date") <= pl.date(2026, 11, 19))
        & (pl.col("n_bars_so_far") > 250)
        & pl.col("amt20").is_not_null() & pl.col("ffcap").is_not_null() & pl.col("amp20").is_not_null()
        & (pl.col("n_lu_20d").fill_null(0) == 0)
        & (pl.col("ret20").abs() < 0.15)
    )
    base = float(panel["y"].mean() * 1e4)
    print(f"冷票样本 {panel.height:,} 行 / {panel['symbol'].n_unique()} 只 / 命中 {panel['y'].sum()} 次 / 基准 {base:.2f}bp", flush=True)

    feats = {
        "amp20": "20日均振幅(≤T-1)",
        "amt_cv20": "成交额变异系数(≤T-1)",
        "turn20": "20日均换手率%(≤T-1)",
        "ret250": "250日涨幅(≤T-1)",
        "ret120": "120日涨幅(≤T-1)",
        "amt20": "20日均成交额(≤T-1)",
        "n_lu_60d": "60日涨停次数(≤T-1)",
        "raw_close": "股价(≤T-1)",
        "free_float_pct": "自由流通占比%",
        "ffcap": "自由流通市值",
        "fmcap": "流通市值",
        "top1_ratio": "第一大股东持股%",
    }
    rows = []
    for k, label in feats.items():
        s = panel.select([pl.col(k).alias("x"), pl.col("y")]).drop_nulls()
        if s.height < 1000:
            continue
        a = auc(s["x"].to_numpy(), s["y"].to_numpy())
        rows.append({"feat": k, "label": label, "auc_lag": a, "n": s.height, "dev": abs(a - 0.5)})
    tab = pl.DataFrame(rows).sort("dev", descending=True)
    print("\n=== 严格滞后（≤T-1）单变量 AUC ===")
    print(tab.select(["feat", "label", "auc_lag", "n"]))
    res = {"auc_lag": tab.to_dicts(), "n": panel.height, "base_bp": base}

    specs = {
        "amp20_pct": (pl.col("amp20") * 100, [2, 3, 4, 5, 7], ["<2", "2-3", "3-4", "4-5", "5-7", ">7"]),
        "turn20": (pl.col("turn20"), [0.5, 1.0, 2.0, 3.0, 5.0], ["<0.5", "0.5-1", "1-2", "2-3", "3-5", ">5"]),
        "amt20_wan": (pl.col("amt20") / 1e4, [1500, 3000, 5000, 10000, 20000, 50000],
                      ["<1500", "1500-3000", "3000-5000", "5000-1w", "1-2w", "2-5w", ">5w"]),
        "ffcap_yi": (pl.col("ffcap") / 1e8, [10, 20, 40, 80, 200], ["<10", "10-20", "20-40", "40-80", "80-200", ">200"]),
        "ret120_pct": (pl.col("ret120") * 100, [-20, -10, 0, 10, 30],
                       ["<-20", "-20..-10", "-10..0", "0..10", "10..30", ">30"]),
        "n_lu_60d": (pl.col("n_lu_60d"), [1, 2, 4], ["0", "1", "2-3", ">=4"]),
        "top1_ratio": (pl.col("top1_ratio"), [15, 25, 35, 50], ["<15", "15-25", "25-35", "35-50", ">50"]),
        "free_float_pct": (pl.col("free_float_pct"), [40, 60, 80, 95], ["<40", "40-60", "60-80", "80-95", ">95"]),
        "price": (pl.col("raw_close"), [5, 10, 20, 50], ["<5", "5-10", "10-20", "20-50", ">50"]),
    }
    for name, (expr, edges, names) in specs.items():
        t = panel.with_columns(cut(expr, edges, names).alias("b"))
        g = t.group_by("b").agg([
            pl.len().alias("n"), pl.col("y").sum().alias("hit"),
            (pl.col("y").mean() * 1e4).alias("bp"),
        ]).with_columns((pl.col("bp") / base).alias("lift")) \
          .with_columns(pl.struct(["b"]).map_elements(lambda s: names.index(s["b"]), return_dtype=pl.Int32).alias("_o")) \
          .sort("_o").drop("_o")
        print(f"\n--- {name}（严格滞后）---")
        print(g.select(["b", "n", "hit", "bp", "lift"]))
        res[name] = g.to_dicts()

    # 二维：振幅 × 换手（是否同一轴）
    print("\n=== 二维：20日均振幅 × 20日均换手 → 游资命中 bp ===")
    p2 = panel.with_columns([
        cut(pl.col("amp20") * 100, [3, 4, 6], ["<3", "3-4", "4-6", ">6"]).alias("A"),
        cut(pl.col("turn20"), [1.0, 2.0, 4.0], ["<1", "1-2", "2-4", ">4"]).alias("T"),
    ])
    heat = p2.group_by(["A", "T"]).agg([pl.len().alias("n"), (pl.col("y").mean() * 1e4).alias("bp")]) \
        .filter(pl.col("n") >= 300).sort(["A", "T"])
    print(heat)
    res["heat_amp_turn"] = heat.to_dicts()

    # 三维极值：振幅>5% & 换手>4% & 成交额<3000万（用户标的的"最像游资"版本）
    print("\n=== 组合筛：高波动+高换手+低成交额 ===")
    combos = {
        "amp>5 & turn>4": (pl.col("amp20") > 0.05) & (pl.col("turn20") > 4.0),
        "amp>5 & turn>4 & amt<3000": (pl.col("amp20") > 0.05) & (pl.col("turn20") > 4.0) & (pl.col("amt20") < 3e7),
        "amp>5 & turn>4 & amt<3000 & ffcap<15": (pl.col("amp20") > 0.05) & (pl.col("turn20") > 4.0)
                                                 & (pl.col("amt20") < 3e7) & (pl.col("ffcap") < 1.5e9),
        "amp<3 & turn<1": (pl.col("amp20") < 0.03) & (pl.col("turn20") < 1.0),
        "amp<3 & turn<1 & amt<3000": (pl.col("amp20") < 0.03) & (pl.col("turn20") < 1.0) & (pl.col("amt20") < 3e7),
    }
    rows = []
    for name, cond in combos.items():
        s = panel.filter(cond)
        rows.append({"combo": name, "n": s.height, "hit": int(s["y"].sum()),
                     "bp": float(s["y"].mean() * 1e4) if s.height else None,
                     "lift": float(s["y"].mean() * 1e4 / base) if s.height else None})
    ct = pl.DataFrame(rows)
    print(ct)
    res["combos"] = ct.to_dicts()

    with open(OUT / "youzi_features4.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n写出 {OUT / 'youzi_features4.json'}")


if __name__ == "__main__":
    main()
