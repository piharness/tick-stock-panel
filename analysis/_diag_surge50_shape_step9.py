"""step9: 阈值敏感性 —— 50% 是不是一个特殊的目标，还是随便定的？

用户的 50% 可能是拍的。这里把阈值从 10% 扫到 100%，对同样的两种启动形状
（S1 首板 / S5b 跳空强）算命中率、终点收益、以及「赚回失败端」所需命中率，
看盈亏平衡点落在哪个阈值上。

如果 50% 附近命中率-收益最差，说明用户挑了一个数学上最难的目标。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
K = str(DATA / "kline_daily_enriched" / "*" / "part.parquet").replace("\\", "/")
SLIM = DATA / "_diag_surge50_slim.parquet"
OUT = DATA / "_diag_surge50_shape_step9.json"

df = pd.read_parquet(
    SLIM, columns=["symbol", "date", "r1", "r5", "r10", "r10_prev", "gap20", "age",
                   "fwd_ret10", "dist_hi250", "to20_pct", "cv60_pct"]
)
df["date"] = pd.to_datetime(df["date"])
valid = (df["age"].values >= 60) & np.isfinite(df["r10"].values) & (df["r10_prev"].values > -0.95)
r1 = df["r1"].values
s1 = valid & (r1 > 0.095) & (np.nan_to_num(df["r5"].values, nan=1) - r1 < 0.05)
s5b = valid & (df["gap20"].values >= 3) & (r1 > 0.09)

con = duckdb.connect()
con.execute("PRAGMA threads=6")
con.execute(
    f"""
    CREATE TABLE cal AS
    SELECT date, row_number() OVER (ORDER BY date) - 1 AS seq
    FROM (SELECT DISTINCT date FROM read_parquet('{K}'))
    """
)
con.execute(
    f"""
    CREATE TABLE k AS
    SELECT a.symbol, a.date, c.seq, a.close,
           row_number() OVER (PARTITION BY a.symbol ORDER BY a.date) rn
    FROM read_parquet('{K}') a JOIN cal c ON c.date = a.date
    WHERE a.close > 0
    """
)
# 未来 10 日最高收盘 / 第 10 日收盘（严格日历相邻）
fwd = con.execute(
    """
    SELECT k.symbol, k.date, k.close,
           max(k.close) OVER w AS fmax,
           count(*) OVER w AS cnt,
           lead(k.close, 10) OVER w2 AS c10,
           lead(k.seq, 10) OVER w2 AS s10,
           k.seq
    FROM k
    WINDOW
      w  AS (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND 10 FOLLOWING),
      w2 AS (PARTITION BY symbol ORDER BY rn)
    """
).df()
fwd = fwd[(fwd["cnt"] == 10) & (fwd["s10"] - fwd["seq"] == 10)].copy()
fwd["date"] = pd.to_datetime(fwd["date"])
print(f"fwd rows {len(fwd):,}", flush=True)

df["s1"] = s1
df["s5b"] = s5b
m = fwd.merge(df[["symbol", "date", "s1", "s5b", "fwd_ret10"]], on=["symbol", "date"], how="inner")
print(f"merged {len(m):,}", flush=True)

# 基准：当日等权 fwd10（用于超额）
bmk = df[valid].groupby("date")["fwd_ret10"].mean()
m["exc"] = m["fwd_ret10"] - m["date"].map(bmk)
m["max_gain"] = m["fmax"] / m["close"] - 1
m["end_gain"] = m["c10"] / m["close"] - 1

rows = []
for thr in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.80, 1.00):
    y = (m["max_gain"] >= thr).astype(float)
    base = y.mean()
    for name, mask in (("ALL", np.ones(len(m), bool)), ("S1", m["s1"].values), ("S5b", m["s5b"].values)):
        mm = mask & np.isfinite(m["fwd_ret10"].values)
        if mm.sum() < 500:
            continue
        p = float(y[mm].mean())
        fm = m["fwd_ret10"].values[mm]
        hit_ret = float(np.nanmean(fm[y[mm].values == 1])) if (y[mm].values == 1).sum() > 5 else np.nan
        miss_ret = float(np.nanmean(fm[y[mm].values == 0])) if (y[mm].values == 0).sum() > 5 else np.nan
        need = abs(miss_ret) / (abs(hit_ret) + abs(miss_ret)) if np.isfinite(hit_ret) and np.isfinite(miss_ret) else np.nan
        rows.append(dict(
            thr=int(thr * 100), who=name, n=int(mm.sum()),
            hit_pct=round(p * 100, 3), base_pct=round(float(base) * 100, 3),
            lift=round(p / float(base), 2) if base > 0 else None,
            hit_ret=round(hit_ret * 100, 2) if np.isfinite(hit_ret) else None,
            miss_ret=round(miss_ret * 100, 2) if np.isfinite(miss_ret) else None,
            need_pct=round(need * 100, 2) if np.isfinite(need) else None,
            exc=round(float(np.nanmean(m["exc"].values[mm])) * 100, 3),
        ))

out = pd.DataFrame(rows)
Path(OUT).write_text(out.to_json(orient="records", force_ascii=False), encoding="utf-8")
pd.set_option("display.width", 220)
for who in ("ALL", "S1", "S5b"):
    print(f"\n=== {who} ===")
    print(out[out.who == who].drop(columns=["who"]).to_string(index=False))
print("saved", OUT)
