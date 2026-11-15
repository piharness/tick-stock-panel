"""L1 生存偏差量化 —— 缺口率、归零门槛、顶部代理。

## 为什么要单独量这一条

20 年 +96.02bp 的分母是 `kline_daily_enriched` 里的 5,580 只票，而这张表
**只含活到今天的标的**：已退市的 312 只（`instruments` 里名字带「退市/ST」）
一行行情都没有，且各数据源对它们返回空（`sina` 直接丢弃、`fuyao` 无历史）。
所以补不回来。既然补不回来，就必须回答：**这个缺口能不能把结论推翻**。

## 三层读数（都在真实数据上跑，不是假设）

1. **缺口率**：按 IPO 年份累计的存活率。全样本 5,580/5,889 = 94.75%，
   2005 年在市池缺口 14.2%，2026 年 5.2%。
2. **归零门槛**：`(1-w)·92.5 + w·X = 0` ⇒ w=9% 时 X 必须低到 **-935bp**
   （-9.4%/日）。主板跌停下限 -10%、ST -5%，而 cohort 只买 mom20 前 20
   —— 连续跌停的票根本进不了这个名单。
3. **顶部代理**：退市股的典型路径是「炒作见顶 → 崩塌」。用表内可测的同类
   样本（当日收盘在近 60 日高点、之后 60 日跌 >30%，n=65,617）代替：
   这些票-日的 `open→close` 均值 **+438bp**，负收益只占 9.3%。

## 结论

生存偏差的方向**不是高估**：缺口票要么进不了 cohort（下跌段），要么进来时
正处在最强势的一天（上涨段）。96.02bp 因此是**保守**读数，不是上界。

局限也要写清：顶部代理测的是「存活股的顶部」，不是「退市股的顶部」。
退市股的炒作通常更极端，方向上会让代理值偏高——但那条偏差指向「真实更高」，
与「结论会被推翻」相反。真正无法排除的是「退市股在上涨段也跑输幸存股」，
而那需要它们的历史行情，本仓拿不到。

用法：

    python analysis/_diag_l1_survivorship.py            # 只打印
    python analysis/_diag_l1_survivorship.py --write    # 写 _result.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import l1_cohort as l1  # noqa: E402
from app.parquet import scan_enriched_parquet  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_l1_survivorship_result.json"

#: cohort 的锚（与 `backend/tests/test_l1_cohort_production.py` 同值）
ANCHOR_END = "2026-11-06"
BASE_BP = 92.5
#: 全期平均缺口率：2005 年 14.2% 线性降到 2026 年 5.2%，中位约 9%。
GAP_SHARE = 0.09


def _instruments() -> pl.DataFrame:
    files = sorted(glob.glob(str(DATA / "instruments" / "**" / "*.parquet"), recursive=True))
    if not files:
        raise SystemExit("缺 instruments，无法判定存活率")
    return pl.read_parquet(files[0])


def survival_by_ipo_year() -> dict:
    inst = _instruments()
    inst = inst.with_columns(pl.col("listing_date").cast(pl.Utf8).str.slice(0, 4).alias("iy"))
    have = set(
        scan_enriched_parquet(DATA / "kline_daily_enriched")
        .select("symbol").unique().collect()["symbol"].to_list()
    )
    inst = inst.with_columns(pl.col("symbol").is_in(list(have)).alias("has_bars"))
    rows = [
        (r["iy"], r["listed"], r["wb"])
        for r in inst.group_by("iy")
        .agg(pl.len().alias("listed"), pl.col("has_bars").sum().alias("wb"))
        .sort("iy").iter_rows(named=True)
        if r["iy"] and r["iy"].isdigit()
    ]
    cum_l = cum_w = 0
    cumulative = {}
    for year, listed, with_bars in rows:
        if int(year) < 1990:
            continue
        cum_l += listed
        cum_w += with_bars
        if int(year) in (2000, 2005, 2010, 2015, 2020, 2026):
            cumulative[year] = {
                "listed": cum_l,
                "with_bars": cum_w,
                "gap_pct": round(100 * (cum_l - cum_w) / cum_l, 2),
            }
    return {
        "listed_total": int(inst.height),
        "with_bars": int(inst["has_bars"].sum()),
        "survival_pct": round(100 * float(inst["has_bars"].sum()) / inst.height, 2),
        "missing": int(inst.height - int(inst["has_bars"].sum())),
        "cumulative_by_year_end": cumulative,
    }


def cell_distribution() -> dict:
    cells = l1.cohort_cells(DATA, end=ANCHOR_END)
    df, m = l1._panel(DATA)
    idx = {d: i for i, d in enumerate(m.dates)}
    sidx = {s: j for j, s in enumerate(m.symbols)}
    out = []
    for d, s in zip(cells["date"].to_list(), cells["symbol"].to_list()):
        i, j = idx.get(d), sidx.get(s)
        if i is None or j is None:
            continue
        o, c = m.open[i, j], m.close[i, j]
        if np.isfinite(o) and o > 0 and np.isfinite(c):
            out.append(c / o - 1.0)
    x = np.array(out)
    return {
        "n": int(len(x)),
        "mean_bp": round(float(x.mean()) * 1e4, 2),
        "sd_bp": round(float(x.std(ddof=1)) * 1e4, 1),
        "pct_bp": {f"p{q}": round(float(np.percentile(x, q)) * 1e4, 1)
                   for q in (1, 5, 10, 25, 50)},
    }


def top_proxy() -> dict:
    """「之后 60 日跌 >30%」的顶部样本，测当日 open→close。

    选这个代理是因为退市股的典型死法是「炒作见顶 → 崩塌」。策略只持有一天，
    所以要问的是**顶部那一天**的日内收益，不是之后 60 天。
    """
    df, _ = l1._panel(DATA)
    d = df.sort(["symbol", "date"]).with_columns([
        pl.col("close").rolling_max(60).over("symbol").alias("hi60"),
        (pl.col("close").shift(-60).over("symbol") / pl.col("close") - 1).alias("fwd60"),
    ])
    top = d.filter((pl.col("close") >= pl.col("hi60") * 0.995) & (pl.col("fwd60") < -0.30))
    with np.errstate(invalid="ignore", divide="ignore"):
        oc = (top["close"] / top["open"] - 1).drop_nulls().to_numpy()
    oc = oc[np.isfinite(oc)]
    all_oc = df.select(pl.col("close") / pl.col("open") - 1).to_series().drop_nulls().to_numpy()
    return {
        "n": int(len(oc)),
        "share_of_cells_pct": round(100 * len(oc) / df.height, 3),
        "mean_bp": round(float(oc.mean()) * 1e4, 1),
        "pct_bp": {f"p{q}": round(float(np.percentile(oc, q)) * 1e4, 1)
                   for q in (5, 25, 50, 75)},
        "negative_share_pct": round(100 * float((oc < 0).mean()), 1),
        "all_cells_mean_bp": round(float(all_oc.mean()) * 1e4, 1),
    }


def correction_table(cell: dict) -> dict:
    """缺失票落在不同分位时，全样本收益会变成多少。"""
    rows = {}
    for label, x in (
        ("same_as_survivors", BASE_BP),
        ("p50", cell["pct_bp"]["p50"]),
        ("p25", cell["pct_bp"]["p25"]),
        ("p10", cell["pct_bp"]["p10"]),
        ("p5", cell["pct_bp"]["p5"]),
        ("p1", cell["pct_bp"]["p1"]),
    ):
        rows[label] = round((1 - GAP_SHARE) * BASE_BP + GAP_SHARE * x, 1)
    breakeven = -BASE_BP * (1 - GAP_SHARE) / GAP_SHARE
    return {
        "gap_share": GAP_SHARE,
        "base_bp": BASE_BP,
        "corrected_bp": rows,
        "breakeven_x_bp": round(breakeven, 0),
        "breakeven_daily_pct": round(breakeven / 100, 2),
        "verdict": (
            "归零需要缺失票平均 -9.35%/日，超过主板跌停下限（-10%）且要求"
            "连续跌停——而 cohort 只买 mom20 前 20，下跌中的票进不来"
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    print("① 存活率 / 缺口率")
    surf = survival_by_ipo_year()
    print("   全样本 %d 只，有行情 %d 只（%.2f%%），缺 %d 只"
          % (surf["listed_total"], surf["with_bars"], surf["survival_pct"], surf["missing"]))
    for year, v in surf["cumulative_by_year_end"].items():
        print("   end-%s 在市池 %5d 只，缺口 %5.1f%%" % (year, v["listed"], v["gap_pct"]))

    print("② cohort cell 收益分布")
    cell = cell_distribution()
    print("   n=%d mean=%+.1fbp sd=%.1fbp  %s"
          % (cell["n"], cell["mean_bp"], cell["sd_bp"], cell["pct_bp"]))

    print("③ 顶部代理（之后 60 日跌 >30%）")
    top = top_proxy()
    print("   n=%d（%.2f%% 票-日）mean=%+.1fbp 负收益 %.1f%%  %s"
          % (top["n"], top["share_of_cells_pct"], top["mean_bp"],
             top["negative_share_pct"], top["pct_bp"]))

    print("④ 修正表（w=%.0f%%）" % (GAP_SHARE * 100))
    corr = correction_table(cell)
    for k, v in corr["corrected_bp"].items():
        print("   缺失票=%-18s -> %+7.1fbp" % (k, v))
    print("   归零门槛 X=%+.0fbp（%.2f%%/日）" % (corr["breakeven_x_bp"], corr["breakeven_daily_pct"]))
    print("   判定：%s" % corr["verdict"])

    result = {
        "generated_at": "2026-11-15",
        "anchor_end": ANCHOR_END,
        "survival": surf,
        "cells": cell,
        "top_proxy": top,
        "correction": corr,
    }
    if args.write:
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print("written:", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
