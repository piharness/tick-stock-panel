"""第五批（修正）：2026 年内路径、背离、回撤。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_diag_regime_2026"

d = (pl.read_parquet(OUT / "daily_metrics.parquet").sort("date")
     .filter(pl.col("date") >= dt.date(2026, 1, 1)))
px = d.with_columns((pl.col("ew_ret").fill_null(0) + 1).cum_prod().alias("ew_idx"))
px = px.with_columns((pl.col("ew_idx") - 1).alias("ew_cum"))

idx_cols = ["sse", "hs300", "csi500", "csi1000", "chinext"]
for c in idx_cols:
    b = px[c].drop_nulls()[0]
    px = px.with_columns(((pl.col(c) / b) - 1).alias(f"{c}_cum"))

mm = (px.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m"))
      .group_by("m").agg(
          [pl.col("ew_cum").last().alias("EW")] +
          [pl.col(f"{c}_cum").drop_nulls().last().alias(c) for c in idx_cols] +
          [pl.col("xs_std").mean().alias("std"), pl.col("cr10").mean().alias("cr10"),
           (pl.col("amt").mean() / 1e8).alias("amt")])
      .sort("m"))

print("=== 2026 年内累计收益（%）===")
hdr = ["EW"] + idx_cols
print("month      " + "  ".join(f"{h:>8}" for h in hdr) + "     std   cr10    amt亿")
for r in mm.to_dicts():
    cells = []
    for h in hdr:
        v = r[h]
        cells.append(f"{v*100:>8.2f}" if v is not None else "     n/a")
    print(f"{r['m']}  " + "  ".join(cells)
          + f"  {r['std']*100:>5.2f} {r['cr10']*100:>5.1f} {r['amt']:>8.0f}")

dv = px.select("date", (pl.col("hs300_cum") - pl.col("ew_cum")).alias("div"),
               (pl.col("chinext_cum") - pl.col("ew_cum")).alias("div_cyb")).drop_nulls()
print("\n=== 指数 − 等权 背离（pct）===")
print(f"HS300:  max {dv['div'].max()*100:>7.2f} @ {dv.filter(pl.col('div')==dv['div'].max())['date'][0]}"
      f"   last {dv['div'][-1]*100:>7.2f}")
print(f"CYB  :  max {dv['div_cyb'].max()*100:>7.2f} @ {dv.filter(pl.col('div_cyb')==dv['div_cyb'].max())['date'][0]}"
      f"   last {dv['div_cyb'][-1]*100:>7.2f}")

rows = px.select("date", "ew_idx").to_dicts()
peak, mdd, peak_d, trough_d = 0.0, 0.0, None, None
for r in rows:
    v = r["ew_idx"]
    if v > peak:
        peak, peak_d = v, r["date"]
    dd = v / peak - 1
    if dd < mdd:
        mdd, trough_d = dd, r["date"]
print(f"\n等权 2026 MDD {mdd*100:.2f}%  {peak_d} -> {trough_d}")
print("等权 2026 末值", round((rows[-1]['ew_idx']-1)*100, 2), "%")

y = pl.read_parquet(OUT / "ytd_2026.parquet")
print("\n=== 2026 YTD 个股分布 ===")
for q in [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]:
    print(f"  p{int(q*100):>2} {y['ytd'].quantile(q)*100:>8.2f}%")
print(f"  正收益 {(y['ytd']>0).mean()*100:.1f}%   跌超30% {(y['ytd']<=-0.3).mean()*100:.1f}%   "
      f"跌超50% {(y['ytd']<=-0.5).mean()*100:.1f}%   n={y.height}")
