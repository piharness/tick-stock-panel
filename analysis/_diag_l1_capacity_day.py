"""容量结论的单日代表性检验 —— 582.9 万是 p93 那天的读数。

## 问题

`docs/l1-gap-reversal.md` §5 的「可实施容量」全部来自 **2026-11-12 单日**：
9 只、最薄腿 4.603 亿、α=1 时 `T_max` = 582.9 万。这个算法本身没错（同一天内
按 q20 达标门选规模），错的是**拿一天去代表 20 年**。

## 实测（4,615 个 cohort 日）

2026-11-12 的流动性分位：

| 口径 | 11-12 | 全样本分位 |
|---|---|---|
| 日合计成交额 | 136.84 亿 | **p86** |
| 单票 q20 | 7.86 亿 | **p92** |
| 最薄那只票 | 4.603 亿 | **p93** |

三个口径一致：那是流动性最好的 7% 的日子之一。

按「容量 ∝ 最薄腿流动性」线性外推（11-12 的 582.9 万为锚）：

| 分位 | 最薄腿 | T_max |
|---|---|---|
| p10 | 0.124 亿 | **16 万** |
| p50 | 1.119 亿 | **142 万** |
| p90 | 4.129 亿 | 523 万 |

所以「可实施容量是百万级」只在**中位日勉强成立**，四分之一个交易日不足 40 万。

## 这个外推的局限（方向已定）

线性外推忽略了 cohort **成员数**与**权重**随流动性一起变：11-12 是 9 只，
p10 日可能只有 3–4 只，每只分到的更少 ⇒ 真实容量**低于**上表，所以上表是上界。

要精确的跨日容量分布，得把 `_diag_l1_weight_frontier.py` 的模型逐日跑
（4,615 天 × α 网格）。本脚本只回答「11-12 是不是一个普通日子」——
答案是不是。

用法：

    python analysis/_diag_l1_capacity_day.py            # 只打印
    python analysis/_diag_l1_capacity_day.py --write    # 写 _result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import l1_cohort as l1  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_l1_capacity_day_result.json"

#: 文档 §5 引用的容量锚（2026-11-12，α=1，q20 达标门）。
ANCHOR_DAY = "2026-11-12"
ANCHOR_TMAX_WAN = 582.9


def build() -> dict:
    cells = l1.cohort_cells(DATA, require_next=False)
    df, _ = l1._panel(DATA)
    if "amount" not in df.columns:
        raise SystemExit("面板缺 amount 列，无法判定流动性")
    a = df.select(["date", "symbol", "amount"]).join(
        cells, on=["date", "symbol"], how="inner"
    )
    day = (
        a.group_by("date")
        .agg([
            pl.col("amount").sum().alias("total"),
            pl.col("amount").quantile(0.2, interpolation="linear").alias("q20"),
            pl.col("amount").min().alias("thinnest"),
            pl.len().alias("n"),
        ])
        .sort("date")
    )
    if day.is_empty():
        raise SystemExit("没有可用的 cohort 日")

    def _pct(col: str, value: float) -> float:
        v = day[col].to_numpy()
        return round(100 * float((v < value).mean()), 0)

    anchor = day.filter(pl.col("date") == ANCHOR_DAY)
    if anchor.is_empty():
        raise SystemExit(f"{ANCHOR_DAY} 不在 cohort 里，锚无法定位")
    arow = anchor.row(0, named=True)

    rows = {}
    for col in ("total", "q20", "thinnest"):
        v = day[col].to_numpy()
        rows[col] = {
            "p10_yi": round(float(np.percentile(v, 10)) / 1e8, 3),
            "p50_yi": round(float(np.percentile(v, 50)) / 1e8, 3),
            "p90_yi": round(float(np.percentile(v, 90)) / 1e8, 3),
            "anchor_yi": round(float(arow[col]) / 1e8, 3),
            "anchor_pct": _pct(col, float(arow[col])),
        }

    thin = day["thinnest"].to_numpy()
    implied = {
        f"p{q}": round(ANCHOR_TMAX_WAN * float(np.percentile(thin, q)) / float(arow["thinnest"]), 0)
        for q in (10, 25, 50, 75, 90)
    }
    implied["anchor"] = ANCHOR_TMAX_WAN
    return {
        "generated_at": "2026-11-15",
        "anchor_day": ANCHOR_DAY,
        "anchor_tmax_wan": ANCHOR_TMAX_WAN,
        "n_cohort_days": int(day.height),
        "liquidity": rows,
        "implied_tmax_wan_by_pct": implied,
        "assumption": (
            "容量 ∝ 最薄腿流动性，线性外推。忽略 cohort 成员数与权重随流动性同变，"
            "所以各分位的推算是**上界**：p10 日的成员更少，每只分到的更少。"
        ),
        "conclusion": (
            "锚日（11-12）的最薄腿处于 p93、日合计 p86 —— 那是流动性最好的 7%。"
            "「可实施容量是百万级」只在中位日勉强成立（约 142 万），"
            "582.9 万不能当常态引用。"
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    data = build()
    print("cohort 日数 %d，锚日 %s" % (data["n_cohort_days"], data["anchor_day"]))
    for col, v in data["liquidity"].items():
        print("  %-9s p10=%8.3f p50=%8.3f p90=%8.3f 亿 | 锚日 %8.3f 亿 = p%.0f%%"
              % (col, v["p10_yi"], v["p50_yi"], v["p90_yi"], v["anchor_yi"], v["anchor_pct"]))
    print("按最薄腿线性外推 T_max（锚 %.1f 万）：" % data["anchor_tmax_wan"])
    for k, v in data["implied_tmax_wan_by_pct"].items():
        print("   %-6s -> %6.0f 万" % (k, v))
    print(data["conclusion"])
    if args.write:
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print("written:", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
