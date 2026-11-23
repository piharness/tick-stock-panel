"""step8: 收尾数字 —— 最优子集（低位首板+低换手+高波动）的逐年表现与容量。

step7 的 54 个子集里，唯一不亏钱的是它（exc -0.006%，t=1.59）。
这里补齐报告需要的三件事：
1. 逐年超额，看是不是靠某一年；
2. 容量：每日可选几只、成交额分位、换手分位；
3. 命中样本里 T+1 涨停开盘（买不到）的比例。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SLIM = DATA / "_diag_surge50_slim.parquet"
OUT = DATA / "_diag_surge50_shape_step8.json"

df = pd.read_parquet(
    SLIM,
    columns=["symbol", "date", "y10", "fwd_ret10", "r1", "r5", "r10", "r20", "r10_prev",
             "dist_hi250", "cv60_pct", "to20_pct", "amt_pct", "age", "next_gap", "ma20_d"],
)
df["date"] = pd.to_datetime(df["date"])
date_code, _ = pd.factorize(df["date"], sort=True)
D = int(date_code.max()) + 1
lp = np.where(df["symbol"].str.startswith(("688", "689", "300", "301", "302")), 0.20,
              np.where(df["symbol"].str.startswith(("8", "4", "920")), 0.30, 0.10))
df["untradable"] = df["next_gap"].values >= (lp - 0.005)
valid = (df["age"].values >= 60) & np.isfinite(df["r10"].values) & (df["r10_prev"].values > -0.95)
y = df["y10"].values.astype(np.float64)
bmk = df.loc[valid].groupby("date")["fwd_ret10"].mean()
df["exc"] = df["fwd_ret10"] - df["date"].map(bmk)
exc = df["exc"].values

r1 = df["r1"].values
r5n = np.nan_to_num(df["r5"].values, nan=1)
first_board = (r1 > 0.095) & (r5n - r1 < 0.05)
best = first_board & (df["dist_hi250"].values < -0.20) & (df["to20_pct"].values < 0.5) & (df["cv60_pct"].values >= 0.5)

yr = df["date"].dt.year.values
print("== best subset yearly ==")
rows = []
for yy in range(2012, 2027):
    m = best & valid & (yr == yy)
    if m.sum() < 100:
        continue
    rows.append(dict(
        year=int(yy), n=int(m.sum()),
        hit=round(float(y[m].mean()) * 100, 3),
        exc=round(float(np.nanmean(exc[m])) * 100, 3),
        fwd=round(float(np.nanmean(df["fwd_ret10"].values[m])) * 100, 3),
        untradable=round(float(df["untradable"].values[m].mean()) * 100, 2),
    ))
print(pd.DataFrame(rows).to_string(index=False))

per_day = pd.Series(best).groupby(date_code).sum()
cap = dict(
    days=int((per_day > 0).sum()),
    median_per_day=float(per_day[per_day > 0].median()),
    p90_per_day=float(per_day[per_day > 0].quantile(0.9)),
    amt_pct_median=round(float(np.nanmedian(df["amt_pct"].values[best])), 3),
    to_pct_median=round(float(np.nanmedian(df["to20_pct"].values[best])), 3),
    untradable_share=round(float(df["untradable"].values[best].mean()) * 100, 2),
)
print("capacity", cap)

# 全样本 vs 最优子集的 hit / exc 汇总
summ = {}
for name, m in (("S1_first_board", first_board), ("BEST_lowpos_lowturn_highvol", best)):
    mm = m & valid
    summ[name] = dict(
        n=int(mm.sum()), hit_pct=round(float(y[mm].mean()) * 100, 3),
        exc_pct=round(float(np.nanmean(exc[mm])) * 100, 3),
        untradable_pct=round(float(df["untradable"].values[mm].mean()) * 100, 2),
    )

Path(OUT).write_text(
    json.dumps(dict(yearly=rows, capacity=cap, summary=summ), ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print("saved", OUT)
