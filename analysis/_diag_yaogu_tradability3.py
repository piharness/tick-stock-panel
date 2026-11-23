"""证伪 step3：两个可能的反例。

命题「要么买不进，要么买进就亏」如果成立，必须堵死两条路：
  反例 1  封死了不要紧，T+2 打开再买 —— 延迟买入能不能赚？
  反例 2  不在 T+1 追，等 T+1 大跌（>-5% / >-10%）后 T+2 开盘低吸 —— 能不能赚？

历史上唯一验证过的正价差是「换手板 + T+1 日内跌>5% → T+2 开买/收卖 +0.836%」，
这里在连板样本上重测，看它是否还成立。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
F = DATA / "_diag_yaogu_f.parquet"
OUT = DATA / "_diag_yaogu_tradability3.json"

df = pd.read_parquet(
    F,
    columns=["symbol", "date", "seq", "rn", "open", "high", "low", "close", "lp", "is_lu", "streak",
             "q1", "o1", "h1", "l1", "c_1", "avg1_adj", "avg2_adj",
             "q2", "o2", "c_2", "q3", "o3", "c_3", "q5", "c_5", "q10", "c_10", "q20", "c_20"],
)
df["date"] = pd.to_datetime(df["date"])
for c in ("lp", "o1", "l1", "c_1", "avg1_adj", "avg2_adj", "o2", "o3", "c_2", "c_3", "c_5", "c_10", "c_20", "close"):
    df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")

lu = df[(df["is_lu"] == 1) & (df["streak"] >= 3)].copy()
lu["limit1"] = lu["close"] * (1 + lu["lp"]) - 0.005
lu["limit2"] = lu["c_1"] * (1 + lu["lp"]) - 0.005
lu["depth1"] = 1 - lu["l1"] / lu["limit1"]

# 反例 1：T+2 开盘买（若 T+1 封死，T+2 才有机会；若 T+2 开盘涨停也买不到）
lu["can_t2"] = (lu["q2"] - lu["seq"] == 2) & (lu["o2"] < lu["limit2"] - 0.005)
lu["e2"] = lu["o2"]
for k, qc, cc in ((2, "q2", "c_2"), (3, "q3", "c_3"), (5, "q5", "c_5"), (10, "q10", "c_10")):
    lu[f"t2r{k}"] = np.where(lu["can_t2"] & (lu[qc] - lu["seq"] == k), lu[cc] / lu["e2"] - 1, np.nan)
# T+1 约束下的可执行口径：T+2 开盘买，最早 T+3 才能卖
lu["t3o"] = np.where(lu["can_t2"] & (lu["q3"] - lu["seq"] == 3), lu["o3"] / lu["e2"] - 1, np.nan)

# 反例 2：T+1 大跌后 T+2 开盘低吸
lu["t1_ret"] = lu["c_1"] / lu["close"] - 1
for thr in (-0.05, -0.10):
    col = f"dip{int(abs(thr)*100)}"
    lu[col] = lu["t1_ret"] <= thr


def summ(g: pd.DataFrame, cols) -> dict:
    out = {"n": int(len(g))}
    for c in cols:
        s = g[c].dropna()
        if len(s) < 40:
            continue
        d = g.loc[s.index].groupby("date")[c].mean()
        t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 20 and d.std(ddof=1) > 0 else np.nan
        out[c] = dict(mean=round(float(s.mean()) * 100, 2), med=round(float(s.median()) * 100, 2),
                      win=round(float((s > 0).mean()) * 100, 1),
                      t=round(float(t), 2) if np.isfinite(t) else None, ndays=len(d))
    return out


res = {}
res["all_3plus_t2"] = summ(lu[lu["can_t2"]], ["t2r2", "t2r3", "t2r5", "t2r10", "t3o"])
res["sealed_t1_t2"] = summ(lu[(lu["l1"] >= lu["limit1"]) & lu["can_t2"]], ["t2r2", "t2r3", "t2r5", "t2r10"])
res["dip5_t2"] = summ(lu[lu["dip5"] & lu["can_t2"]], ["t2r2", "t2r3", "t2r5", "t2r10", "t3o"])
res["dip10_t2"] = summ(lu[lu["dip10"] & lu["can_t2"]], ["t2r2", "t2r3", "t2r5", "t2r10", "t3o"])

print("=== 反例 1：T+2 开盘买入（连板>=3）===")
for k, v in res.items():
    print(f"\n[{k}] n={v['n']:,}")
    for c, s in v.items():
        if c == "n":
            continue
        print(f"   {c:6s} mean={s['mean']:>7}% med={s['med']:>7}% win={s['win']:>5}% t={s['t']} days={s['ndays']}")

# 逐年（dip5 子集，T+2 当日）
d = lu[lu["dip5"] & lu["can_t2"]].copy()
d["y"] = d["date"].dt.year
yearly = []
for y, g in d.groupby("y"):
    s = g["t2r2"].dropna()
    if len(s) < 40:
        continue
    yearly.append(dict(year=int(y), n=int(len(s)), r2=round(float(s.mean()) * 100, 2),
                       win=round(float((s > 0).mean()) * 100, 1)))
print("\n=== dip5 + T+2 开盘买当日卖：逐年 ===")
print(pd.DataFrame(yearly).to_string(index=False), flush=True)

# 首板（streak==1）同样两条路，作为对照
first = df[(df["is_lu"] == 1) & (df["streak"] == 1)].copy()
first["limit1"] = first["close"] * (1 + first["lp"]) - 0.005
first["limit2"] = first["c_1"] * (1 + first["lp"]) - 0.005
first["t1_ret"] = first["c_1"] / first["close"] - 1
first["can_t2"] = (first["q2"] - first["seq"] == 2) & (first["o2"] < first["limit2"] - 0.005)
first["t2r2"] = np.where(first["can_t2"] & (first["q2"] - first["seq"] == 2), first["c_2"] / first["o2"] - 1, np.nan)
first["dip5"] = first["t1_ret"] <= -0.05
res["first_board_dip5_t2"] = summ(first[first["dip5"] & first["can_t2"]], ["t2r2"])
res["first_board_all_t2"] = summ(first[first["can_t2"]], ["t2r2"])
print("\n=== 对照：首板 ===")
for k in ("first_board_all_t2", "first_board_dip5_t2"):
    print(k, res[k])

out = dict(res=res, dip5_yearly=yearly)
Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("saved", OUT)
