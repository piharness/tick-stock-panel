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


def conditional_by_strength() -> dict:
    """条件于「会不会被选中」的分层收益 —— 修正本脚本第一版的口径错误。

    第一版用 `cohort cell` 的**无条件**分位数做修正（p1 = -948.9bp），那是错的：
    p1 那些日子对应的是**崩盘段**，而崩盘段的 top20 入选率只有全体的 **0.16×**
    —— 它们根本进不了候选。缺失票的收益必须条件在「它会被选中」上。

    所以改按「当日收盘 / 近 60 日最高」分层，并在每层内套用策略的实际动作
    （低开 1% 买入、当日收盘卖出）：

    - 崩盘段（dd<-30%）即使入选，也只有 -7.8bp，不是 -949bp；
    - 强势段（dd>-5%）是 +273bp，且「低开」比「高开」好（+273 对 +96.5）。

    两侧一比，缺失票的收益区间是 [-7.8, +273]，而归零需要 -935。
    """
    df, _ = l1._panel(DATA)
    d = (
        df.sort(["symbol", "date"])
        .with_columns([
            pl.col("close").rolling_max(60).over("symbol").alias("hi60"),
            (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("mom20"),
        ])
        .with_columns([
            pl.col("mom20").rank("ordinal", descending=True).over("date").alias("rk"),
            (pl.col("close") / pl.col("hi60") - 1).alias("dd"),
            (pl.col("open") / pl.col("close").shift(1).over("symbol") - 1).alias("gap"),
            (pl.col("close") / pl.col("open") - 1).alias("oc"),
        ])
        .with_columns(pl.col("rk").le(20).fill_null(False).alias("in20"))
    )
    base_rate = float(d["in20"].mean())

    def _stat(sub: pl.DataFrame) -> dict:
        v = sub["oc"].drop_nulls().to_numpy()
        v = v[np.isfinite(v)]
        if len(v) < 100:
            return {"n": int(len(v)), "mean_bp": None}
        return {
            "n": int(len(v)),
            "mean_bp": round(float(v.mean()) * 1e4, 1),
            "negative_share_pct": round(100 * float((v < 0).mean()), 1),
        }

    layers = {}
    for lo, hi, label in (
        (-1.0, -0.30, "crash_dd_lt_-30"),
        (-0.30, -0.15, "down_-30_-15"),
        (-0.15, -0.05, "pullback_-15_-5"),
        (-0.05, 1e-4, "strong_gt_-5"),
    ):
        mask = (pl.col("dd") > lo) & (pl.col("dd") <= hi)
        sel = d.filter(mask)
        entry = _stat(sel.filter(pl.col("gap") <= -0.01))
        entry["selection_rate_pct"] = round(100 * float(sel["in20"].mean()), 4)
        entry["selection_lift"] = round(float(sel["in20"].mean()) / base_rate, 2) if base_rate else None
        layers[label] = entry

    layers["strong_high_open"] = _stat(d.filter((pl.col("dd") > -0.05) & (pl.col("gap") > 0)))
    layers["base_selection_rate_pct"] = round(100 * base_rate, 4)
    return layers


def correction_table(layers: dict) -> dict:
    """缺失票落在不同强度层时，全样本收益会变成多少。

    与第一版的关键区别：不再用 cell 的**无条件**分位（p1 = -948.9bp），而是用
    「条件于入选」的分层收益。无条件分布里混着「进不了候选的日子」（崩盘段
    入选率仅 0.16×），拿它做修正会系统性低估修正后的收益。
    """
    rows = {"same_as_survivors": round(BASE_BP, 1)}
    xs = []
    for label in ("crash_dd_lt_-30", "down_-30_-15", "pullback_-15_-5", "strong_gt_-5"):
        x = layers[label]["mean_bp"]
        if x is None:
            continue
        xs.append(x)
        rows[label] = round((1 - GAP_SHARE) * BASE_BP + GAP_SHARE * x, 1)
    breakeven = -BASE_BP * (1 - GAP_SHARE) / GAP_SHARE
    return {
        "gap_share": GAP_SHARE,
        "base_bp": BASE_BP,
        #: 量级参考，不是结论：它用全期一个 w，会把 2009 的损失和早期年份的
        #: 正贡献互相抵消。结论看 `year_by_year`（逐年 w）。保留它只为说明
        #: 「即使按最粗的口径算，也不会归零」。
        "illustrative_bp": rows,
        "x_range_bp": [round(min(xs), 1), round(max(xs), 1)] if xs else None,
        "breakeven_x_bp": round(breakeven, 0),
        "breakeven_daily_pct": round(breakeven / 100, 2),
        "breakeven_by_gap_share": {
            f"w={int(w * 100)}%": round(-BASE_BP * (1 - w) / w, 0)
            for w in (0.05, 0.09, 0.15, 0.25, 0.40)
        },
        "verdict": (
            "条件于入选后，缺失票的收益区间是 [-7.8, +273.1]bp（逐年最差 2009 为"
            " -161.4bp）；归零需要 -935bp。两个区间不相交，且崩盘段的入选率只有"
            "全体的 0.16x —— 最差的那一层被策略结构挡在候选之外"
        ),
    }


def year_by_year() -> dict:
    """逐年修正：**当年缺口率 × 当年最差层收益**，替代全期一个 w。

    为什么必须逐年：缺口率 2005 年 14.2%、2026 年 5.2%，差近 3 倍；而最差层
    （崩盘段低开买入）的收益也随年份反号 —— 2005-2008 是 +24~+57bp，
    2009 年 -161.4bp，2011 年后几乎年年为负。全期平均的 -7.8bp 把两种相反
    的年份混成一个数，既掩盖了 2009，也低估了早期的正贡献。

    这里取**年内崩盘段**（dd<-30% 且低开 1%）的收益作为 X_y —— 它是最差的
    可入选层，真实缺失票大部分落在更强的一层，所以这是保守下界。
    """
    inst = _instruments()
    inst = inst.with_columns(pl.col("listing_date").cast(pl.Utf8).str.slice(0, 4).alias("iy"))
    have = set(
        scan_enriched_parquet(DATA / "kline_daily_enriched")
        .select("symbol").unique().collect()["symbol"].to_list()
    )
    inst = inst.with_columns(pl.col("symbol").is_in(list(have)).alias("hb"))
    rows = [
        (r["iy"], r["n"], r["w"])
        for r in inst.group_by("iy")
        .agg(pl.len().alias("n"), pl.col("hb").sum().alias("w"))
        .sort("iy").iter_rows(named=True)
        if r["iy"] and r["iy"].isdigit()
    ]
    gap_by_year: dict[str, float] = {}
    cum_l = cum_w = 0
    for year, n, w in rows:
        if int(year) < 1990:
            continue
        cum_l += n
        cum_w += w
        gap_by_year[year] = 1.0 - cum_w / cum_l

    cells = l1.cohort_cells(DATA, end=ANCHOR_END)
    df, m = l1._panel(DATA)
    ix = {d: i for i, d in enumerate(m.dates)}
    sx = {s: j for j, s in enumerate(m.symbols)}
    rec = []
    for d, s in zip(cells["date"].to_list(), cells["symbol"].to_list()):
        i, j = ix.get(d), sx.get(s)
        if i is None or j is None:
            continue
        o, c = m.open[i, j], m.close[i, j]
        if np.isfinite(o) and o > 0 and np.isfinite(c):
            rec.append((d, c / o - 1))
    per = (
        pl.DataFrame(rec, schema=["date", "ret"], orient="row")
        .group_by("date").agg(pl.col("ret").mean().alias("r"))
        .with_columns(pl.col("date").str.slice(0, 4).alias("y"))
    )
    mean_by_year = {
        r["y"]: float(r["m"]) * 1e4
        for r in per.group_by("y").agg(pl.col("r").mean().alias("m")).iter_rows(named=True)
    }

    d = (
        df.sort(["symbol", "date"])
        .with_columns([
            pl.col("close").rolling_max(60).over("symbol").alias("hi60"),
            (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("mom20"),
        ])
        .with_columns([
            pl.col("mom20").rank("ordinal", descending=True).over("date").alias("rk"),
            (pl.col("close") / pl.col("hi60") - 1).alias("dd"),
            (pl.col("open") / pl.col("close").shift(1).over("symbol") - 1).alias("gap"),
            (pl.col("close") / pl.col("open") - 1).alias("oc"),
            pl.col("date").str.slice(0, 4).alias("y"),
        ])
        .with_columns(pl.col("rk").le(20).fill_null(False).alias("in20"))
    )
    crash = (pl.col("dd") < -0.30) & (pl.col("gap") <= -0.01)
    g = d.group_by("y").agg([pl.when(crash).then(pl.col("oc")).mean().alias("x")]).sort("y")

    out: dict[str, dict] = {}
    for row in g.iter_rows(named=True):
        y = row["y"]
        base = mean_by_year.get(y)
        gap = gap_by_year.get(y)
        x = row["x"]
        if base is None or gap is None or x is None:
            continue
        x_bp = float(x) * 1e4
        corrected = (1 - gap) * base + gap * x_bp
        out[y] = {
            "cohort_bp": round(base, 1),
            "gap_pct": round(100 * gap, 1),
            "worst_layer_bp": round(x_bp, 1),
            "corrected_bp": round(corrected, 1),
            "delta_bp": round(corrected - base, 1),
        }
    worst = min(out.items(), key=lambda kv: kv[1]["corrected_bp"]) if out else None
    mean_raw = sum(v["cohort_bp"] for v in out.values()) / len(out) if out else None
    mean_cor = sum(v["corrected_bp"] for v in out.values()) / len(out) if out else None
    return {
        "by_year": out,
        #: 与 mean_corrected_bp **同口径**的未修正均值（年等权）。对外说「修正了
        #: 百分之几」必须拿这两个数相除：`cells.mean_bp` 是日等权、`BASE_BP`
        #: 是 cell 等权，与年等权均值不是同一个东西，相除会凭空造出 1–2pp。
        "mean_raw_bp": round(mean_raw, 1) if mean_raw is not None else None,
        "delta_pct": round(100 * (mean_cor - mean_raw) / mean_raw, 1) if mean_raw else None,
        "worst_year": worst[0] if worst else None,
        "worst_corrected_bp": worst[1]["corrected_bp"] if worst else None,
        "n_years_negative": sum(1 for v in out.values() if v["corrected_bp"] <= 0),
        "mean_corrected_bp": round(sum(v["corrected_bp"] for v in out.values()) / len(out), 1) if out else None,
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

    print("④ 条件于入选的分层收益（策略动作：低开 1% 买、当日收盘卖）")
    layers = conditional_by_strength()
    for label in ("crash_dd_lt_-30", "down_-30_-15", "pullback_-15_-5", "strong_gt_-5"):
        v = layers[label]
        print("   %-18s n=%7d 入选率 %.4f%%（%.2fx） 收益 %+8.1fbp 负收益 %.1f%%"
              % (label, v["n"], v["selection_rate_pct"], v["selection_lift"] or 0,
                 v["mean_bp"] or 0, v["negative_share_pct"] or 0))
    hv = layers["strong_high_open"]
    print("   %-18s n=%7d 收益 %+8.1fbp（对照：强势期高开）"
          % ("strong_high_open", hv["n"], hv["mean_bp"] or 0))

    print("⑤ 修正表（w=%.0f%%）" % (GAP_SHARE * 100))
    corr = correction_table(layers)
    for k, v in corr["illustrative_bp"].items():
        print("   缺失票=%-22s -> %+7.1fbp" % (k, v))
    print("   缺失票收益区间 %s；归零门槛 X=%+.0fbp（%.2f%%/日）"
          % (corr["x_range_bp"], corr["breakeven_x_bp"], corr["breakeven_daily_pct"]))
    print("   w 敏感性：" + "  ".join("%s->%+.0f" % (k, v)
                                       for k, v in corr["breakeven_by_gap_share"].items()))
    print("   判定：%s" % corr["verdict"])

    print("⑥ 逐年修正（当年缺口率 x 当年最差层）")
    yby = year_by_year()
    for y, v in yby["by_year"].items():
        print("   %s  cohort %+7.1f  缺口 %4.1f%%  最差层 %+8.1f  ->  %+7.1fbp（%+.1f）"
              % (y, v["cohort_bp"], v["gap_pct"], v["worst_layer_bp"], v["corrected_bp"], v["delta_bp"]))
    print("   最差年份 %s -> %+.1fbp；翻负年份 %d 个"
          % (yby["worst_year"], yby["worst_corrected_bp"], yby["n_years_negative"]))
    print("   逐年均值 %+.1f -> %+.1fbp（%+.1f%%，同口径年等权）"
          % (yby["mean_raw_bp"], yby["mean_corrected_bp"], yby["delta_pct"]))

    result = {
        "generated_at": "2026-11-15",
        "anchor_end": ANCHOR_END,
        "survival": surf,
        "cells": cell,
        "top_proxy": top,
        "conditional_layers": layers,
        "year_by_year": yby,
        "correction": corr,
    }
    if args.write:
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print("written:", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
