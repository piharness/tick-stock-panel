"""L1 缺口池的独立通道：不经过 asymmetry_probe / asymmetry_control。

为什么需要它
------------
审计文档里「每交易日只数为日均、不是中位日」这一条，在两套现有产物里都取不到：
`asymmetry_probe` 那一套是「选股日 ∩ 池规模下限 ∩ 缺口池下限 ∩ 选股数下限」的子集，
与全样本不是同一批日子。缺一个只依赖原始分区、不做任何筛选的版本。
本脚本只做一件事：从 `data/kline_daily_enriched/date=*/part.parquet` 直接
重建 日历 × 标的 矩阵，按 `_diag_base_t0.leg` 的同一口径重算

    gap[d] = open[d+1] / close[d] - 1
    i1[d]  = close[d+1] / open[d+1] - 1

不 import 任何 analysis 模块，只依赖 polars/numpy。它是「这个量不是面板构造出来的」
的外部证据，也是新数进入文档前的默认入口。

口径
----
- 日集合：分区里有数据的全部交易日，不做池规模与选股数过滤。
- 池：`gap <= -1%`，且前收与次日开高低收同时有限且为正。
- 「逐日等权」= 先对每日池内 i1 求均值，再对**有池的日子等权**平均。
- 「全样本」= 所有池内 cell 等权（大池日权重大）。
- 「无条件」= 全部可算 i1 的 cell。

产物 `_diag_l1_indep_recount_result.json`；`by_year` 段用于看单制度期假设。
"""

from __future__ import annotations

import glob
import json
import time
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "kline_daily_enriched"
OUT = Path(__file__).resolve().parent / "_diag_l1_indep_recount_result.json"
GAP_MAX = -0.01


def build_matrices() -> tuple[list[str], list[str], np.ndarray, np.ndarray]:
    files = sorted(glob.glob(str(SRC / "date=*" / "part.parquet")))
    if not files:
        raise SystemExit(f"no partitions under {SRC}")
    parts = [
        pl.read_parquet(f, columns=["symbol", "date", "open", "close"]) for f in files
    ]
    df = pl.concat(parts, how="vertical_relaxed")
    wide = df.pivot(values=["open", "close"], index="date", on="symbol",
                    aggregate_function="first").sort("date")
    dates = [str(x) for x in wide["date"].to_list()]
    o_cols = [c for c in wide.columns if c.startswith("open_")]
    c_cols = [c for c in wide.columns if c.startswith("close_")]
    symbols = [c.split("_", 1)[1] for c in o_cols]
    order = np.argsort(np.array(symbols))
    o = wide.select([o_cols[i] for i in order]).to_numpy().astype(np.float64)
    c = wide.select([c_cols[i] for i in order]).to_numpy().astype(np.float64)
    return dates, [symbols[i] for i in order], o, c


def main() -> None:
    t0 = time.time()
    dates, symbols, o, c = build_matrices()
    T, S = o.shape

    valid = np.isfinite(o) & (o > 0) & np.isfinite(c) & (c > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        gap = np.full((T, S), np.nan)
        i1 = np.full((T, S), np.nan)
        gap[:-1] = o[1:] / c[:-1] - 1.0
        i1[:-1] = c[1:] / o[1:] - 1.0
    pool = valid[:-1] & valid[1:] & np.isfinite(gap[:-1]) & np.isfinite(i1[:-1]) & (gap[:-1] <= GAP_MAX)
    all_i1 = i1[:-1][np.isfinite(i1[:-1]) & valid[1:]]

    day_n = pool.sum(axis=1)
    day_mean = np.array([
        float(np.nanmean(i1[d][pool[d]])) if day_n[d] else np.nan for d in range(T - 1)
    ])
    live = np.isfinite(day_mean)
    n_days_with_pool = int(live.sum())
    cells = i1[:-1][pool]

    # 分年（信号日所属年份），供单制度期判断
    by_year: dict[str, dict] = {}
    for d in range(T - 1):
        if not day_n[d]:
            continue
        y = dates[d][:4]
        b = by_year.setdefault(y, {"days": 0, "cells": 0, "_m": []})
        b["days"] += 1
        b["cells"] += int(day_n[d])
        b["_m"].append(float(day_mean[d]))

    out = {
        "source": "data/kline_daily_enriched/date=*/part.parquet",
        "method": "gap[d]=o[d+1]/c[d]-1, i1[d]=c[d+1]/o[d+1]-1, gap<=-1%, 无选股/池规模过滤",
        "gap_max": GAP_MAX,
        "n_partitions": len(glob.glob(str(SRC / "date=*" / "part.parquet"))),
        "n_dates": T,
        "n_symbols": S,
        "tradable_cells": int(pool.size),
        "n_cells_gap_pool": int(cells.size),
        "n_days_with_pool": n_days_with_pool,
        "names_per_day_mean": round(float(day_n[day_n > 0].mean()), 1),
        "names_per_day_median": float(np.median(day_n[day_n > 0])),
        "names_per_day_p10": float(np.percentile(day_n[day_n > 0], 10)),
        "names_per_day_max": int(day_n.max()),
        "i1_bp": {
            "daily_equal_weight": round(float(np.mean(day_mean[live])) * 1e4, 1),
            "pooled_cell_weight": round(float(cells.mean()) * 1e4, 1),
            "unconditional": round(float(all_i1.mean()) * 1e4, 1),
            "daily_equal_weight_trim_best5pct": round(
                float(np.mean(np.sort(day_mean[live])[:-max(1, int(live.sum() * 0.05))])) * 1e4, 1
            ),
        },
        "by_year": {
            y: {
                "days": v["days"],
                "cells": v["cells"],
                "i1_daily_equal_bp": round(float(np.mean(v["_m"])) * 1e4, 1),
            }
            for y, v in sorted(by_year.items())
        },
        "elapsed_s": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    for k in ("n_dates", "n_symbols", "n_cells_gap_pool", "n_days_with_pool",
              "names_per_day_mean", "names_per_day_median", "names_per_day_p10"):
        print(f"{k:32s} {out[k]}")
    for k, v in out["i1_bp"].items():
        print(f"i1 {k:32s} {v}bp")
    print("by_year:")
    for y, v in out["by_year"].items():
        print(f"  {y}  days {v['days']:3d}  cells {v['cells']:6d}  {v['i1_daily_equal_bp']:7.1f}bp")
    print(f"elapsed {out['elapsed_s']}s -> {OUT.name}")


if __name__ == "__main__":
    main()
