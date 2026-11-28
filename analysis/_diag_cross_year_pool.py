"""跨年妖候选池：6 条件到底有没有前向收益？

页面把 8 条画像翻译成 4 门槛 + 1 排序 + 1 徽标，实时筛出候选，并写明「候选池不是买入名单」。
但这句话一直只有 20 年「首板后买入」的旁证（yaogu-census），没有对这个池子本身的前向检验。

本脚本按页面同一套条件，在 2016-01 ~ 2026-08 的每个月末重放：
  门槛：现价 3~15 元、流通市值 10~60 亿、近 20 日有涨停、非 ST/北交所、上市 ≥250 日
  排序：net_gain = ret20 − ret60 降序
  分档：ret60 < 30% 为第一梯队，否则第二梯队
  收益：t+1 开盘买，t+21 / t+61 收盘卖；超额 = 候选均值 − 同日全市场同口径中位数

这不是策略回测（没计成本、没处理一字板、没有 PIT 股本/ST），只回答一个问题：
**这个池子的前向收益，是否稳定高于同日市场？**

输出：data/.scratch/squeeze/cross_year_pool.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "squeeze"
OUT.mkdir(parents=True, exist_ok=True)
KLINE = ROOT / "data" / "kline_daily_enriched"
INST = ROOT / "data" / "instruments" / "instruments.parquet"


def nw_t(x: np.ndarray, lag: int = 6) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan")
    e = x - x.mean()
    s = float((e * e).mean())
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * float((e[l:] * e[:-l]).mean())
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else float("nan")


def main() -> None:
    inst = pl.read_parquet(INST, columns=["symbol", "name", "float_shares", "listing_date"])
    inst = inst.with_columns(pl.col("listing_date").cast(pl.Date, strict=False))

    lf = pl.scan_parquet(str(KLINE / "date=*" / "*.parquet"), hive_partitioning=True)
    lf = lf.select(["symbol", "date", "open", "close", "raw_close", "consecutive_limit_ups"])
    lf = lf.filter((pl.col("date") >= pl.lit("2015-06-01").str.to_date()) & (~pl.col("symbol").str.ends_with(".BJ")))
    df = (lf.sort(["symbol", "date"])
            .with_columns([
                pl.col("close").pct_change().over("symbol").alias("ret1"),
                (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("ret20"),
                (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("ret60"),
                (pl.col("close").shift(-21).over("symbol") / pl.col("open").shift(-1).over("symbol") - 1).alias("fwd20"),
                (pl.col("close").shift(-61).over("symbol") / pl.col("open").shift(-1).over("symbol") - 1).alias("fwd60"),
                (pl.col("consecutive_limit_ups") >= 1).cast(pl.Int8).alias("lu"),
            ])
            .with_columns(pl.col("lu").rolling_sum(20).over("symbol").alias("lu20"))
            .collect())

    df = df.join(inst, on="symbol", how="left")
    df = df.with_columns([
        (pl.col("raw_close") * pl.col("float_shares") / 1e8).alias("float_cap"),
        (pl.col("date") - pl.col("listing_date")).dt.total_days().alias("listing_days"),
        pl.col("name").fill_null("").str.contains("ST").alias("st"),
        (pl.col("ret20") - pl.col("ret60")).alias("net_gain"),
    ])

    # 月末快照
    months = (df.group_by(pl.col("date").dt.strftime("%Y-%m"))
                .agg(pl.col("date").max().alias("dt"))
                .sort("dt")
                .filter((pl.col("dt") >= pl.lit("2016-01-01").str.to_date()) &
                        (pl.col("dt") <= pl.lit("2026-08-31").str.to_date())))
    snaps = [d for d in months["dt"].to_list()]

    rows = []
    for d in snaps:
        g = df.filter(pl.col("date") == d)
        if g.height < 500:
            continue
        base = g.filter(pl.col("fwd20").is_not_null() & pl.col("fwd60").is_not_null())
        if base.height < 300:
            continue
        mkt20 = float(base["fwd20"].median())
        mkt60 = float(base["fwd60"].median())
        mkt20_mean = float(base["fwd20"].mean())
        mkt60_mean = float(base["fwd60"].mean())
        valid = base.filter(
            (pl.col("raw_close") >= 3) & (pl.col("raw_close") <= 15) &
            (pl.col("float_cap") >= 10) & (pl.col("float_cap") <= 60) &
            (pl.col("listing_days") >= 250) & (~pl.col("st")) & (pl.col("lu20") >= 1)
        )
        if valid.height == 0:
            continue
        # 同日同门槛但未启动：对照
        ctrl = base.filter(
            (pl.col("raw_close") >= 3) & (pl.col("raw_close") <= 15) &
            (pl.col("float_cap") >= 10) & (pl.col("float_cap") <= 60) &
            (pl.col("listing_days") >= 250) & (~pl.col("st")) & (pl.col("lu20") == 0)
        )
        valid = valid.with_columns(pl.col("net_gain").rank("ordinal", descending=True).alias("rk"))
        top = valid.filter(pl.col("rk") <= 15)
        t1 = valid.filter(pl.col("ret60") < 0.3)
        t2 = valid.filter(pl.col("ret60") >= 0.3)

        def cell(x: pl.DataFrame, label: str) -> dict:
            if x.height == 0:
                return {"label": label, "n": 0}
            return {
                "label": label, "n": int(x.height),
                "ex20_mean": float(x["fwd20"].mean() - mkt20_mean),
                "ex20_med": float(x["fwd20"].median() - mkt20),
                "ex60_mean": float(x["fwd60"].mean() - mkt60_mean),
                "ex60_med": float(x["fwd60"].median() - mkt60),
                "win20": float((x["fwd20"] > mkt20).mean()),
                "win60": float((x["fwd60"] > mkt60).mean()),
            }

        rows.append({
            "month": d.strftime("%Y-%m"),
            "all": cell(valid, "all"),
            "top15": cell(top, "top15"),
            "tier1": cell(t1, "tier1"),
            "tier2": cell(t2, "tier2"),
            "ctrl": cell(ctrl, "ctrl"),
            "mkt20": mkt20, "mkt60": mkt60, "mkt20_mean": mkt20_mean, "mkt60_mean": mkt60_mean,
        })

    out: dict = {"months": len(rows), "window": f"{snaps[0]} ~ {snaps[-1]}", "rows": rows}
    # 汇总
    for key in ["all", "top15", "tier1", "tier2", "ctrl"]:
        vals20 = np.array([r[key]["ex20_mean"] for r in rows if r[key]["n"] > 0])
        vals60 = np.array([r[key]["ex60_mean"] for r in rows if r[key]["n"] > 0])
        med20 = np.array([r[key]["ex20_med"] for r in rows if r[key]["n"] > 0])
        out[key] = {
            "months": int(len(vals20)),
            "ex20_mean_bp": round(float(vals20.mean() * 1e4), 1),
            "ex20_t": round(nw_t(vals20), 2),
            "ex20_hit": round(float((vals20 > 0).mean()), 3),
            "ex20_med_bp": round(float(med20.mean() * 1e4), 1),
            "ex60_mean_bp": round(float(vals60.mean() * 1e4), 1),
            "ex60_t": round(nw_t(vals60), 2),
            "ex60_hit": round(float((vals60 > 0).mean()), 3),
        }
    # 分时段
    for period, lo, hi in [("2016-2019", "2016-01", "2019-12"), ("2020-2026", "2020-01", "2026-08")]:
        sub = [r for r in rows if lo <= r["month"] <= hi]
        out[period] = {
            k: {
                "ex20_bp": round(float(np.mean([r[k]["ex20_mean"] for r in sub if r[k]["n"] > 0]) * 1e4), 1),
                "ex60_bp": round(float(np.mean([r[k]["ex60_mean"] for r in sub if r[k]["n"] > 0]) * 1e4), 1),
                "months": len([r for r in sub if r[k]["n"] > 0]),
            } for k in ["all", "top15", "tier1", "tier2", "ctrl"]
        }
    (OUT / "cross_year_pool.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ["months", "window", "all", "top15", "tier1", "tier2", "ctrl",
                                          "2016-2019", "2020-2026"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
