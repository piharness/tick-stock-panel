#!/usr/bin/env python3
"""_diag_delisting_oos.py — 退市「浅跌破面值」信号的样本外验证（闸门 2）

验证「浅跌 → 低退市 → 正 EV」是否是稳定规律，三个子测试一起做：
  1. 扩样本：事件阈值 1.0 / 1.2 / 1.5 元（1.2/1.5 捕获更早的保壳阶段，浅跌事件更多）
  2. 时间样本外：2020 退市新规前（<2020-01-01）vs 后（>=2020-01-01）
  3. 更保守：60 日退市率（20 日会漏掉「先回补、之后才退市」的延后退市）

条件变量：dd_from_peak60（跌破时距 60 日高点的回撤，浅=接近 0，深=接近 -1）
          peak60（从多高摔下来，元）

产出：analysis/_diag_delisting_oos_result.json
运行：backend/.venv/Scripts/python.exe analysis/_diag_delisting_oos.py
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

ENR = Path("data/kline_daily_enriched")
GAP = Path("data/backfill_20261117/stock_gap_daily.parquet")
OUT_JSON = Path(__file__).with_name("_diag_delisting_oos_result.json")


def load_full() -> pl.DataFrame:
    enr = (
        pl.scan_parquet(str(ENR / "**" / "*.parquet"), extra_columns="ignore")
        .select(["symbol", "date", "raw_close"])
        .collect()
    ).with_columns(pl.lit("enr").alias("src"))
    gap = (
        pl.read_parquet(GAP).select(["symbol", "date", "close"]).rename({"close": "raw_close"})
    ).with_columns(pl.lit("gap").alias("src"))
    return pl.concat([enr, gap]).unique(subset=["symbol", "date"], keep="last").sort(["symbol", "date"])


def add_features(full: pl.DataFrame) -> pl.DataFrame:
    full = full.with_columns(
        pl.col("raw_close").rolling_max(window_size=61, min_samples=1).over("symbol").alias("peak60"),
        pl.col("raw_close").shift(-60).over("symbol").alias("px_fwd60"),
    )
    full = full.with_columns(
        (pl.col("raw_close") / pl.col("peak60") - 1).alias("dd_from_peak60"),
        (pl.col("px_fwd60") / pl.col("raw_close") - 1).alias("fwd60"),
    )
    return full


def bucketize(dd: float, edges: list[float], labels: list[str]) -> str:
    for i in range(len(labels)):
        if edges[i] <= dd < edges[i + 1]:
            return labels[i]
    return labels[-1]


def main() -> None:
    full = add_features(load_full())
    result: dict = {}

    for thr in [1.0, 1.2, 1.5]:
        ev = full.filter(pl.col("raw_close") < thr).group_by("symbol", maintain_order=True).first()
        ev = ev.with_columns(
            pl.col("date").dt.year().alias("yr"),
            pl.when(pl.col("date") < pl.date(2020, 1, 1)).then(pl.lit("pre2020")).otherwise(pl.lit("post2020")).alias("period"),
        )
        # 60 日退市 = 缺口股 + 无 60 日后续数据
        ev = ev.with_columns(
            ((pl.col("src") == "gap") & pl.col("fwd60").is_null()).alias("ended60"),
        )
        thr_result = {"n_events": ev.height, "by_period": {}}
        for period in ["pre2020", "post2020"]:
            sub = ev.filter(pl.col("period") == period)
            rows = []
            edges = [-1.0, -0.7, -0.5, -0.3, 0.0]
            labels = ["-100~-70%", "-70~-50%", "-50~-30%", "-30~0%"]
            for i in range(len(labels)):
                g = sub.filter((pl.col("dd_from_peak60") >= edges[i]) & (pl.col("dd_from_peak60") < edges[i + 1]))
                n = g.height
                if n == 0:
                    continue
                n_ended = int(g["ended60"].sum())
                valid = g.filter(pl.col("fwd60").is_not_null())
                rets = valid["fwd60"].to_list() + [-1.0] * n_ended
                rows.append({
                    "drawdown": labels[i], "n": n,
                    "delist60_pct": round(n_ended / n * 100, 1),
                    "fwd60_corrected_mean_bp": round(sum(rets) / len(rets) * 1e4, 1),
                    "fwd60_median_bp": round(valid["fwd60"].median() * 1e4, 1) if valid.height else None,
                })
            thr_result["by_period"][period] = {"n": sub.height, "rows": rows}
        result[str(thr)] = thr_result

    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for thr in ["1.0", "1.2", "1.5"]:
        print(f"\n{'='*90}\n阈值 {thr} 元 — 60 日退市率与修正 EV（bp），按回撤分桶 × 时间样本外\n{'='*90}")
        for period in ["pre2020", "post2020"]:
            p = result[thr]["by_period"][period]
            print(f"\n[{period}] n={p['n']}")
            print(f"  {'回撤':<12}{'n':>6}{'60日退市%':>10}{'修正EV(bp)':>12}{'中位(bp)':>10}")
            for r in p["rows"]:
                print(f"  {r['drawdown']:<12}{r['n']:>6}{r['delist60_pct']:>10}{r['fwd60_corrected_mean_bp']:>12}{str(r['fwd60_median_bp']):>10}")

    print(f"\nJSON: {OUT_JSON}")


if __name__ == "__main__":
    main()
