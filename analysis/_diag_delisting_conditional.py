#!/usr/bin/env python3
"""_diag_delisting_conditional.py — 退市「跌破面值」的条件化：价格路径能否 ex-ante 区分幸存/退市

上一版（_diag_delisting_forced_sell.py）证明：无条件「跌破 1 元买跌」是负 EV 价值陷阱
（生存偏差修正后 20 日 −49%、60 日 −55%，46~67% 退市）。

本版追问：**能不能用事件前（ex-ante）可观测的信号，把会退市的 ~50% 和会回补的 ~50% 分开？**
若分得开 → 条件化可翻正；分不开 → 结论是「价格里没有足够信息，需要财务/股权结构数据」。

条件化变量（全部用事件日前数据，无前视）：
  ret_pre20 / ret_pre60  — 跌破前 20/60 日累计跌幅（急跌 vs 阴跌）
  peak60                  — 跌破前 60 日最高价（从多高摔下来）
  drawdown_from_peak60    — 跌破时距 60 日高点的回撤幅度

诚实边界：财务条件化（净资产正负）不可行——本地财务数据只覆盖在市股（跌破 1 元的 268 只里
仅 65 只有年报，其中 48 只是幸存者、17 只缺口股），用它条件化会把生存偏差重新引入。

产出：analysis/_diag_delisting_conditional_result.json
运行：backend/.venv/Scripts/python.exe analysis/_diag_delisting_conditional.py
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

ENR = Path("data/kline_daily_enriched")
GAP = Path("data/backfill_20261117/stock_gap_daily.parquet")
OUT_JSON = Path(__file__).with_name("_diag_delisting_conditional_result.json")


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


def bucket_ret(events: pl.DataFrame, col: str, edges: list[float], labels: list[str]) -> list[dict]:
    rows = []
    for i in range(len(labels)):
        lo, hi = edges[i], edges[i + 1]
        g = events.filter((pl.col(col) >= lo) & (pl.col(col) < hi))
        n = g.height
        if n == 0:
            continue
        ended = g.filter((pl.col("src") == "gap") & pl.col("fwd20").is_null())
        valid = g.filter(pl.col("fwd20").is_not_null())
        n_ended = ended.height
        rets = valid["fwd20"].to_list() + [-1.0] * n_ended
        rows.append({
            "bucket": labels[i], "n": n,
            "delist_pct": round(n_ended / n * 100, 1),
            "fwd20_corrected_mean_bp": round(sum(rets) / len(rets) * 1e4, 1),
            "fwd20_median_bp": round(valid["fwd20"].median() * 1e4, 1) if valid.height else None,
        })
    return rows


def main() -> None:
    full = load_full()
    full = full.with_columns(
        pl.col("raw_close").shift(20).over("symbol").alias("px20ago"),
        pl.col("raw_close").shift(60).over("symbol").alias("px60ago"),
        pl.col("raw_close").rolling_max(window_size=61, min_samples=1).over("symbol").alias("peak60"),
        pl.col("raw_close").shift(-20).over("symbol").alias("px_fwd20"),
        pl.col("raw_close").shift(-60).over("symbol").alias("px_fwd60"),
    )
    full = full.with_columns(
        (pl.col("raw_close") / pl.col("px20ago") - 1).alias("ret_pre20"),
        (pl.col("raw_close") / pl.col("px60ago") - 1).alias("ret_pre60"),
        (pl.col("raw_close") / pl.col("peak60") - 1).alias("dd_from_peak60"),
        (pl.col("px_fwd20") / pl.col("raw_close") - 1).alias("fwd20"),
        (pl.col("px_fwd60") / pl.col("raw_close") - 1).alias("fwd60"),
    )

    ev = full.filter(pl.col("raw_close") < 1.0).group_by("symbol", maintain_order=True).first()
    print(f"跌破 1 元事件数: {ev.height}")

    result = {"n_events": ev.height, "conditionals": {}}

    print("\n=== 条件化 1：跌破前 60 日累计跌幅 (ret_pre60) ===")
    # 跌幅更负 = 摔得更惨
    b = bucket_ret(ev, "ret_pre60", [-1.0, -0.7, -0.5, -0.3, 0.0], ["-100~-70%", "-70~-50%", "-50~-30%", "-30~0%"])
    result["conditionals"]["ret_pre60"] = b
    for r in b:
        print(f"  {r['bucket']:<12} n={r['n']:>3}  退市%={r['delist_pct']:>5}  修正均值={r['fwd20_corrected_mean_bp']:>7}bp")

    print("\n=== 条件化 2：从多高摔下来 (peak60 绝对价，元) ===")
    b = bucket_ret(ev, "peak60", [1.0, 1.5, 2.0, 3.0, 1e9], ["1.0~1.5", "1.5~2.0", "2.0~3.0", ">3.0"])
    result["conditionals"]["peak60"] = b
    for r in b:
        print(f"  {r['bucket']:<12} n={r['n']:>3}  退市%={r['delist_pct']:>5}  修正均值={r['fwd20_corrected_mean_bp']:>7}bp")

    print("\n=== 条件化 3：跌破时距 60 日高点回撤 (dd_from_peak60) ===")
    b = bucket_ret(ev, "dd_from_peak60", [-1.0, -0.7, -0.5, -0.3, 0.0], ["-100~-70%", "-70~-50%", "-50~-30%", "-30~0%"])
    result["conditionals"]["dd_from_peak60"] = b
    for r in b:
        print(f"  {r['bucket']:<12} n={r['n']:>3}  退市%={r['delist_pct']:>5}  修正均值={r['fwd20_corrected_mean_bp']:>7}bp")

    # 财务覆盖缺口（硬事实）
    bs = pl.read_parquet("data/backfill_20261117/fin_balance.parquet")
    bs_ann = bs.filter(pl.col("period_end").cast(pl.Utf8).str.ends_with("-12-31"))
    has_fin = set(bs_ann["symbol"].to_list())
    n_fin = sum(1 for s in ev["symbol"].to_list() if s in has_fin)
    result["financial_coverage"] = {"n_events": ev.height, "n_with_annual_fin": n_fin,
                                    "note": "财务数据仅覆盖在市股，条件化会用幸存者偏差污染"}
    print(f"\n财务覆盖：{n_fin}/{ev.height} 只跌破 1 元事件股有年报财务（其中 48 只幸存者），条件化不可行")

    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON: {OUT_JSON}")


if __name__ == "__main__":
    main()
