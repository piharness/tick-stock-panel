"""step7: 两个收尾问题。

1. 口径确认：用户说的「10 日累计偏离 50%」如果指 BIAS(10)>=50%（乖离率），
   事件有多少？与「10 日累计涨幅 >=50%」是不是同一批样本？

2. 在负 alpha 的启动形状里，有没有「失败端亏得少」的子集？
   盈亏平衡：p*50% + (1-p)*miss > 0  =>  p > |miss| / (50% + |miss|)
   S5b 的 miss 约 -4%，需要 p>7.4%，实际 3.6%。若能把 miss 压到 -1%，p>2% 即可。
   候选压缩变量：低位（距 250 日高为负）、低换手、低波动。
   注意这是 36 格网格搜索，必然有过拟合，报告时必须给多重检验门槛。
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
OUT = DATA / "_diag_surge50_shape_step7.json"

# ---- 1. BIAS 口径 ----
con = duckdb.connect()
con.execute("PRAGMA threads=6")
bias = con.execute(
    f"""
    WITH b AS (
      SELECT symbol, date, close,
             row_number() OVER (PARTITION BY symbol ORDER BY date) rn
      FROM read_parquet('{K}') WHERE close > 0
    )
    SELECT
      sum(CASE WHEN close / m10 - 1 >= 0.50 THEN 1 ELSE 0 END) AS bias50,
      sum(CASE WHEN close / m10 - 1 >= 0.30 THEN 1 ELSE 0 END) AS bias30,
      count(*) AS n
    FROM (
      SELECT symbol, date, close,
             avg(close) OVER (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) m10,
             count(*) OVER (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) c10
      FROM b
    ) WHERE c10 = 10
    """
).df()
print("BIAS 口径:", bias.to_dict("records"), flush=True)

# ---- 2. 子集搜索 ----
df = pd.read_parquet(
    SLIM,
    columns=["symbol", "date", "y10", "fwd_ret10", "r1", "r5", "r10", "r20", "r10_prev",
             "ma20_d", "dist_hi250", "cv60_pct", "amp20_pct", "amp5_pct", "gap20",
             "to20_pct", "amt_pct", "age", "next_gap"],
)
df["date"] = pd.to_datetime(df["date"])
date_code, uniq = pd.factorize(df["date"], sort=True)
D = len(uniq)
lp = np.where(df["symbol"].str.startswith(("688", "689", "300", "301", "302")), 0.20,
              np.where(df["symbol"].str.startswith(("8", "4", "920")), 0.30, 0.10))
df["untradable"] = df["next_gap"].values >= (lp - 0.005)
valid = (df["age"].values >= 60) & np.isfinite(df["r10"].values) & (df["r10_prev"].values > -0.95)
y = df["y10"].values.astype(np.float64)
bmk = df.loc[valid].groupby("date")["fwd_ret10"].mean()
df["exc"] = df["fwd_ret10"] - df["date"].map(bmk)
exc = df["exc"].values
fr = df["fwd_ret10"].values

r1 = df["r1"].values
r5n = np.nan_to_num(df["r5"].values, nan=1)
r10 = df["r10"].values
base_rate = float(y[valid].mean())


def stats(mask):
    m = mask & valid
    n = int(m.sum())
    if n < 200:
        return None
    hr = float(y[m].mean())
    fm = fr[m]
    fin = np.isfinite(fm)
    hit_ret = float(np.nanmean(fm[fin & (y[m] == 1)])) if (fin & (y[m] == 1)).sum() > 5 else np.nan
    miss_ret = float(np.nanmean(fm[fin & (y[m] == 0)])) if (fin & (y[m] == 0)).sum() > 5 else np.nan
    em = exc[m]
    efin = np.isfinite(em)
    de = date_code[m][efin]
    ev = em[efin]
    ds = np.bincount(de, weights=ev, minlength=D)
    dc = np.bincount(de, minlength=D)
    dok = dc > 10
    et = float((ds[dok] / dc[dok]).mean() / ((ds[dok] / dc[dok]).std(ddof=1) / np.sqrt(dok.sum()))) if dok.sum() > 30 else np.nan
    need = abs(miss_ret) / (0.5 + abs(miss_ret)) if np.isfinite(miss_ret) else np.nan
    return dict(
        n=n, hit_pct=round(hr * 100, 3), lift=round(hr / base_rate, 2),
        hit_ret_pct=round(hit_ret * 100, 2) if np.isfinite(hit_ret) else None,
        miss_ret_pct=round(miss_ret * 100, 2) if np.isfinite(miss_ret) else None,
        breakeven_pct=round(need * 100, 2) if np.isfinite(need) else None,
        exc_pct=round(float(np.nanmean(em)) * 100, 3),
        exc_t=round(et, 2) if np.isfinite(et) else None,
    )


starts = {
    "S1_首板": (r1 > 0.095) & (r5n - r1 < 0.05),
    "S5b_跳空强": (df["gap20"].values >= 3) & (r1 > 0.09),
    "S5_跳空": (df["gap20"].values >= 3) & (r1 > 0.05),
}
pos_bins = {
    "低位_距高<-20%": df["dist_hi250"].values < -0.20,
    "中位_-20~0%": (df["dist_hi250"].values >= -0.20) & (df["dist_hi250"].values < 0),
    "高位_>=0": df["dist_hi250"].values >= 0,
}
to_bins = {
    "低换手<0.5": df["to20_pct"].values < 0.5,
    "中换手0.5-0.8": (df["to20_pct"].values >= 0.5) & (df["to20_pct"].values < 0.8),
    "高换手>=0.8": df["to20_pct"].values >= 0.8,
}
cv_bins = {"低波动<0.5": df["cv60_pct"].values < 0.5, "高波动>=0.5": df["cv60_pct"].values >= 0.5}

grid = {}
for sname, smask in starts.items():
    rows = []
    for pn, pm in pos_bins.items():
        for tn, tm in to_bins.items():
            for vn, vm in cv_bins.items():
                s = stats(smask & pm & tm & vm)
                if s:
                    rows.append(dict(pos=pn, to=tn, vol=vn, **s))
    rows.sort(key=lambda r: -(r["exc_pct"] or -99))
    grid[sname] = rows
    print(f"\n=== {sname} 子集（按超额排序，前 6）===")
    for r in rows[:6]:
        print(f"  {r['pos']:14s} {r['to']:13s} {r['vol']:10s} n={r['n']:>6,} hit={r['hit_pct']:>6.2f}% "
              f"miss={r['miss_ret_pct']:>6.2f}% 需={r['breakeven_pct']:>5.2f}% exc={r['exc_pct']:>7.3f}% t={r['exc_t']}")
    n_tests = len(rows)
    print(f"  （共 {n_tests} 个子集；Bonferroni 0.05 门槛约 |t|>{(2.0 + np.log(n_tests)):.1f} 的粗略参照）")

out = dict(bias_definition=bias.to_dict("records")[0], grid=grid)
Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("saved", OUT, flush=True)
