"""证伪 step2：真实可成交价 + 事件层统计。

step1 的漏洞：用 T+1 开盘价当买入价，但「开盘涨停、盘中打开」的样本开盘价根本买不到。
这里改成：
  entry = 开盘未涨停 ? 开盘价 : 当日均价(amount/volume/100)
封死（low >= 涨停价）的样本一律剔除，并单独统计占比。

另外区分两个层次：
  板层  每一个涨停日都算一次（描述「第 N 板买」的直观感受，但样本高度重叠）
  事件层 每波连板只在「首次达到 N 板」算一次（用于统计推断，避免同一波重复计数）
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
F = DATA / "_diag_yaogu_f.parquet"
OUT = DATA / "_diag_yaogu_tradability2.json"

cols = [
    "symbol", "date", "seq", "rn", "open", "high", "low", "close", "lp", "is_lu", "streak",
    "q1", "o1", "h1", "l1", "c_1", "v1", "a1", "avg1_adj", "avg2_adj",
    "q2", "o2", "c_2", "q3", "c_3", "q5", "c_5", "q10", "c_10", "q20", "c_20",
]
df = pd.read_parquet(F, columns=cols)
df["date"] = pd.to_datetime(df["date"])
print(f"rows {len(df):,}  limit-up rows {(df.is_lu==1).sum():,}", flush=True)

# 只看涨停日
lu = df[(df["is_lu"] == 1) & (df["streak"] >= 1)].copy()
for c in ("lp", "o1", "h1", "l1", "c_1", "v1", "a1", "avg1_adj", "c_2", "c_3", "c_5", "c_10", "c_20", "close", "open"):
    if c in lu.columns:
        lu[c] = pd.to_numeric(lu[c], errors="coerce").astype("float64")
lu["limit_next"] = lu["close"] * (1 + lu["lp"]) - 0.005
lu["has_next"] = lu["q1"] - lu["seq"] == 1
lu["sealed"] = lu["has_next"] & (lu["l1"] >= lu["limit_next"])
lu["open_at_limit"] = lu["has_next"] & (lu["o1"] >= lu["limit_next"])
lu["px_avg"] = pd.to_numeric(lu["avg1_adj"], errors="coerce").astype("float64")
lu["entry"] = np.where(lu["open_at_limit"], lu["px_avg"], lu["o1"])
lu["entry"] = np.where(np.isfinite(lu["entry"]) & (lu["entry"] > 0), lu["entry"], np.nan)

print(f"涨停日 {len(lu):,}  有次日 {lu.has_next.sum():,}  全天封死 {lu.sealed.sum():,} "
      f"({lu.sealed.mean()*100:.1f}%)  开盘涨停 {lu.open_at_limit.sum():,} ({lu.open_at_limit.mean()*100:.1f}%)", flush=True)

trad = lu[lu["has_next"] & ~lu["sealed"]].copy()
print(f"可成交 {len(trad):,} ({len(trad)/lu.has_next.sum()*100:.1f}% of 有次日)", flush=True)

# 打开幅度：最低价相对涨停价的折价，衡量「能买进多少量」
lu["open_depth"] = np.where(lu["has_next"], 1 - lu["l1"] / lu["limit_next"], np.nan)
lu["entry_close"] = lu["c_1"]

# 收益（从 entry 到 T+k 收盘），在 lu 上算，trad/first 共享
for k, qc, cc in ((1, "q1", "c_1"), (2, "q2", "c_2"), (3, "q3", "c_3"), (5, "q5", "c_5"), (10, "q10", "c_10"), (20, "q20", "c_20")):
    lu[f"r{k}"] = np.where(lu[qc] - lu["seq"] == k, lu[cc] / lu["entry"] - 1, np.nan)
    lu[f"rc{k}"] = np.where(lu[qc] - lu["seq"] == k, lu[cc] / lu["entry_close"] - 1, np.nan)
trad = lu[lu["has_next"] & ~lu["sealed"]].copy()


def agg(g: pd.DataFrame) -> dict:
    out = {"n": int(len(g))}
    for k in (1, 2, 3, 5, 10, 20):
        s = g[f"r{k}"].dropna()
        if len(s) < 50:
            continue
        out[f"r{k}_mean"] = round(float(s.mean()) * 100, 2)
        out[f"r{k}_med"] = round(float(s.median()) * 100, 2)
        out[f"r{k}_win"] = round(float((s > 0).mean()) * 100, 1)
    for k in (5, 10):
        s2 = g[f"rc{k}"].dropna()
        if len(s2) > 50:
            out[f"rc{k}_mean"] = round(float(s2.mean()) * 100, 2)
    return out


lu["bucket"] = np.where(lu["streak"] >= 10, 10, np.where(lu["streak"] >= 8, 8, np.where(lu["streak"] >= 5, 5, lu["streak"])))
trad["bucket"] = np.where(trad["streak"] >= 10, 10, np.where(trad["streak"] >= 8, 8, np.where(trad["streak"] >= 5, 5, trad["streak"])))

rows = []
for b in sorted(trad["bucket"].unique()):
    g = trad[trad["bucket"] == b]
    s = agg(g)
    sub = lu[lu["bucket"] == b]
    s["bucket"] = int(b)
    s["sealed_pct"] = round(float(sub["sealed"].mean()) * 100, 1)
    s["open_limit_pct"] = round(float(sub["open_at_limit"].mean()) * 100, 1)
    rows.append(s)
flow = pd.DataFrame(rows)[["bucket", "n", "sealed_pct", "open_limit_pct", "r1_mean", "r2_mean", "r3_mean", "r5_mean", "r5_med", "r5_win", "r10_mean", "r10_med", "r10_win", "r20_mean"]]
print("\n=== 可成交样本：从 T+1 真实可成交价买入 ===")
print(flow.to_string(index=False), flush=True)

# ---- 事件层：首次达到 N 板 ----
first = lu[lu["streak"].isin([3, 4, 5, 8, 10])].copy()
print("\n=== 事件层（首次达到 N 板）===")
ev_rows = []
for k in (3, 4, 5, 8, 10):
    g = first[first["streak"] == k]
    t = g[g["has_next"] & ~g["sealed"]]
    s = agg(t) if len(t) else {"n": 0}
    s["board"] = k
    s["n_all"] = int(len(g))
    s["sealed_pct"] = round(float(g["sealed"].mean()) * 100, 1)
    ev_rows.append(s)
ev = pd.DataFrame(ev_rows)[["board", "n_all", "sealed_pct", "n", "r1_mean", "r5_mean", "r5_med", "r5_win", "r10_mean", "r10_med", "r10_win"]]
print(ev.to_string(index=False), flush=True)

# ---- 年度（3 板事件层，剔封死）----
yr = first[first["streak"] == 3]
yr = yr[yr["has_next"] & ~yr["sealed"]].copy()
yr["y"] = yr["date"].dt.year
yearly = []
for y, g in yr.groupby("y"):
    if len(g) < 60:
        continue
    yearly.append(dict(year=int(y), n=len(g),
                       r5=round(float(g["r5"].dropna().mean()) * 100, 2),
                       r10=round(float(g["r10"].dropna().mean()) * 100, 2),
                       win10=round(float((g["r10"].dropna() > 0).mean()) * 100, 1)))
print("\n=== 3 板事件逐年 ===")
print(pd.DataFrame(yearly).to_string(index=False), flush=True)

# ---- t 值：按事件日聚类 ----
def daily_t(g: pd.DataFrame, col: str) -> tuple[float, float, int]:
    s = g[["date", col]].dropna()
    if len(s) < 200:
        return (np.nan, np.nan, 0)
    d = s.groupby("date")[col].mean()
    if len(d) < 30:
        return (np.nan, np.nan, 0)
    t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))
    return (float(d.mean()) * 100, float(t), len(d))


tstats = {}
for b in (3, 5, 10):
    g = trad[trad["bucket"] == b]
    tstats[f"board_{b}"] = {f"r{k}": daily_t(g, f"r{k}") for k in (1, 3, 5, 10, 20)}
print("\n=== 日频 t 值（板层，可成交）===")
for k, v in tstats.items():
    print(k, {kk: (round(vv[0], 2) if np.isfinite(vv[0]) else None, round(vv[1], 2) if np.isfinite(vv[1]) else None, vv[2]) for kk, vv in v.items()})

depth = trad[trad["bucket"] >= 3].copy()
depth["d_bin"] = pd.cut(depth["open_depth"], [-1, 0.01, 0.03, 0.06, 1.0],
                        labels=["<1%", "1-3%", "3-6%", ">6%"])
dep_rows = []
for lab, g in depth.groupby("d_bin", observed=True):
    if len(g) < 100:
        continue
    dep_rows.append(dict(bin=str(lab), n=len(g),
                         r5=round(float(g["r5"].dropna().mean()) * 100, 2),
                         r10=round(float(g["r10"].dropna().mean()) * 100, 2),
                         win10=round(float((g["r10"].dropna() > 0).mean()) * 100, 1)))
print("\n=== 3 板以上：按「打开幅度」分层（越大越好买）===")
print(pd.DataFrame(dep_rows).to_string(index=False), flush=True)

out = dict(flow=flow.to_dict("records"), depth=dep_rows, events=ev.to_dict("records"), yearly=yearly, tstats=tstats,
           totals=dict(lu_days=int(len(lu)), has_next=int(lu.has_next.sum()),
                       sealed=int(lu.sealed.sum()), tradable=int(len(trad))))
Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("saved", OUT, flush=True)
