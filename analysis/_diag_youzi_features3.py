"""游资标的画像：全部候选特征的单变量区分度（AUC + 分档 lift）。

样本：冷票（20日无涨停 且 |ret20|<15%），2025-11-12 ~ 2026-11-19
标签：龙虎榜 hot_money_net_value 非空（游资席位有动作）
性质：同期画像（profiling），不是预测。AUC 只说明「该特征能不能区分已被游资碰过的票」。
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
    add_features, attach_ownership, load_dragon, load_panel,
)

pl.Config.set_tbl_rows(90)
pl.Config.set_tbl_width_chars(240)
pl.Config.set_tbl_cols(40)


def auc(score, label) -> float:
    s = np.asarray(score, dtype=float)
    y = np.asarray(label, dtype=bool)
    m = np.isfinite(s)
    s, y = s[m], y[m]
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    ss = s[order]
    i = 0
    while i < len(ss):
        j = i
        while j + 1 < len(ss) and ss[j + 1] == ss[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def cut(expr: pl.Expr, edges: list[float], names: list[str]) -> pl.Expr:
    e = pl.when(expr < edges[0]).then(pl.lit(names[0]))
    for k in range(1, len(edges)):
        e = e.when(expr < edges[k]).then(pl.lit(names[k]))
    return e.otherwise(pl.lit(names[-1]))


def main() -> None:
    df = add_features(load_panel())

    # 补充特征
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
            (pl.col("date") - pl.col("list_date").cast(pl.Date)).dt.total_days().alias("list_days")
        )
    else:
        df = df.with_columns(pl.lit(None).cast(pl.Int64).alias("list_days"))

    dr = load_dragon().with_columns(pl.col("hm_net").is_not_null().alias("y"))
    d = df.join(dr.select(["symbol", "date", "y"]), on=["symbol", "date"], how="left") \
          .with_columns(pl.col("y").fill_null(False))

    panel = d.filter(
        (pl.col("date") >= pl.date(2025, 11, 12))
        & (pl.col("date") <= pl.date(2026, 11, 19))
        & (pl.col("n_bars_so_far") > 250)
        & pl.col("amt20").is_not_null() & pl.col("ffcap").is_not_null()
        & (pl.col("n_lu_20d").fill_null(0) == 0)
        & (pl.col("ret20").abs() < 0.15)
    )
    print(f"冷票样本 {panel.height:,} 行 / {panel['symbol'].n_unique()} 只 / 命中 {panel['y'].sum()} 次 "
          f"/ 基准 {panel['y'].mean()*1e4:.2f}bp", flush=True)

    feats = {
        "log_amt20": (pl.col("amt20").log(), "20日均成交额(对数)"),
        "log_ffcap": (pl.col("ffcap").log(), "自由流通市值(对数)"),
        "log_fmcap": (pl.col("fmcap").log(), "流通市值(对数)"),
        "turn20": (pl.col("turn20"), "20日均换手率%"),
        "ret120": (pl.col("ret120"), "120日涨幅"),
        "ret250": (pl.col("ret250"), "250日涨幅"),
        "amp20": (pl.col("amp20"), "20日均振幅"),
        "amt_cv20": (pl.col("amt_cv20"), "成交额变异系数"),
        "n_lu_60d": (pl.col("n_lu_60d"), "60日涨停次数"),
        "top1_ratio": (pl.col("top1_ratio"), "第一大股东持股%"),
        "free_float_pct": (pl.col("free_float_pct"), "自由流通占比%"),
        "raw_close": (pl.col("raw_close"), "股价"),
        "list_days": (pl.col("list_days"), "上市天数"),
    }

    rows = []
    for k, (expr, label) in feats.items():
        s = panel.select([expr.alias("x"), pl.col("y")]).drop_nulls()
        if s.height < 1000:
            continue
        a = auc(s["x"].to_numpy(), s["y"].to_numpy())
        rows.append({"feat": k, "label": label, "auc": a, "n": s.height,
                     "abs_dev": abs(a - 0.5)})
    tab = pl.DataFrame(rows).sort("abs_dev", descending=True)
    print("\n=== 单变量 AUC（0.5 = 无区分度）===")
    print(tab.select(["feat", "label", "auc", "n"]))
    res = {"auc": tab.to_dicts(), "n": panel.height, "base_bp": float(panel["y"].mean() * 1e4)}

    # 分档 lift 表
    specs = {
        "amt20_wan": (pl.col("amt20") / 1e4, [1500, 3000, 5000, 10000, 20000, 50000],
                      ["<1500", "1500-3000", "3000-5000", "5000-1w", "1-2w", "2-5w", ">5w"]),
        "turn20": (pl.col("turn20"), [0.5, 1.0, 2.0, 3.0, 5.0],
                   ["<0.5", "0.5-1", "1-2", "2-3", "3-5", ">5"]),
        "ret120_pct": (pl.col("ret120") * 100, [-20, -10, 0, 10, 30],
                       ["<-20", "-20..-10", "-10..0", "0..10", "10..30", ">30"]),
        "amp20_pct": (pl.col("amp20") * 100, [2, 3, 4, 5, 7],
                      ["<2", "2-3", "3-4", "4-5", "5-7", ">7"]),
        "n_lu_60d": (pl.col("n_lu_60d"), [1, 2, 4],
                     ["0", "1", "2-3", ">=4"]),
        "top1_ratio": (pl.col("top1_ratio"), [15, 25, 35, 50],
                       ["<15", "15-25", "25-35", "35-50", ">50"]),
        "free_float_pct": (pl.col("free_float_pct"), [40, 60, 80, 95],
                           ["<40", "40-60", "60-80", "80-95", ">95"]),
        "price": (pl.col("raw_close"), [5, 10, 20, 50],
                  ["<5", "5-10", "10-20", "20-50", ">50"]),
    }
    for name, (expr, edges, names) in specs.items():
        t = panel.with_columns(cut(expr, edges, names).alias("b"))
        g = t.group_by("b").agg([
            pl.len().alias("n"),
            pl.col("y").sum().alias("hit"),
            (pl.col("y").mean() * 1e4).alias("bp"),
        ]).with_columns([
            (pl.col("bp") / (panel["y"].mean() * 1e4)).alias("lift"),
            pl.struct(["b"]).map_elements(lambda s: names.index(s["b"]), return_dtype=pl.Int32).alias("_o"),
        ]).sort("_o").drop("_o")
        print(f"\n--- {name} ---")
        print(g.select(["b", "n", "hit", "bp", "lift"]))
        res[name] = g.to_dicts()

    # 二维：成交额 × 换手率（冷票内），看是否各自独立
    print("\n=== 二维：amt20 × turn20 → 游资命中 bp ===")
    p2 = panel.with_columns([
        cut(pl.col("amt20") / 1e4, [3000, 10000, 30000], ["<3000", "3000-1w", "1-3w", ">3w"]).alias("A"),
        cut(pl.col("turn20"), [1.0, 2.0, 4.0], ["<1", "1-2", "2-4", ">4"]).alias("T"),
    ])
    heat = p2.group_by(["A", "T"]).agg([
        pl.len().alias("n"), (pl.col("y").mean() * 1e4).alias("bp")
    ]).filter(pl.col("n") >= 300).sort(["A", "T"])
    print(heat)
    res["heat_amt_turn"] = heat.to_dicts()

    with open(OUT / "youzi_features3.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n写出 {OUT / 'youzi_features3.json'}")


if __name__ == "__main__":
    main()
