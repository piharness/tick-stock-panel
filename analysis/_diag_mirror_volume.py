"""判据的镜像：低位 + 首次放量 是否正期望。

上一轮证明「高位高换手」T+20 超额 -6.59%。若这只是「换手率极端 = 负」，
那低位放量也该是负的；若它有方向性，则位置与换手应当交互。

设计（全部只用 T 日收盘可观测）：
- 位置：close / close[60] - 1（mom60）
- 缩量：过去 20 日（不含当日）平均换手 avg20
- 首次放量：turn >= k * avg20 且 turn >= 地板值
对照：同样放量但不在低位（把 mom60 分档），用于分离「位置」的贡献。
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
OUT = DATA / "_diag_mirror_volume.json"

con = duckdb.connect()
con.execute("PRAGMA threads=4")

df = con.execute(
    f"""SELECT symbol,date,close,turnover_rate FROM read_parquet('{K}', hive_partitioning=false) WHERE close>0"""
).df()
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
g = df.groupby("symbol", sort=False)
df["ret"] = df["close"] / g["close"].shift(1) - 1
df["mom60"] = df["close"] / g["close"].shift(61) - 1
df["avg20"] = g["turnover_rate"].transform(lambda s: s.shift(1).rolling(20, min_periods=15).mean())
df["vr"] = df["turnover_rate"] / df["avg20"]
cal = pd.DataFrame({"date": sorted(df["date"].unique())})
cal["seq"] = range(len(cal))
df = df.merge(cal, on="date", how="left")
g = df.groupby("symbol", sort=False)
df["f20"] = np.where(g["seq"].shift(-20) - df["seq"] == 20, g["close"].shift(-20) / df["close"] - 1, np.nan)

daily = df.groupby("date")["ret"].mean().sort_index().dropna()
idx = daily.index
bmk = pd.Series({idx[i]: float((1 + daily.iloc[i + 1 : i + 21]).prod() - 1) for i in range(len(idx) - 20)})
df["ex20"] = (1 + df["f20"]) / (1 + df["date"].map(bmk)) - 1

syms, seqs = df["symbol"].values, df["seq"].values


def dedup(mask: np.ndarray, gap: int = 20) -> np.ndarray:
    idxs = np.where(mask)[0]
    keep = np.zeros(len(df), dtype=bool)
    last: dict[str, int] = {}
    for i in idxs:
        s = syms[i]
        if s not in last or seqs[i] - last[s] >= gap:
            keep[i] = True
            last[s] = seqs[i]
    return keep


def run(mask: np.ndarray, label: str) -> dict:
    sub = df.loc[dedup(np.asarray(mask, dtype=bool))].dropna(subset=["ex20"])
    if len(sub) < 30:
        return {"label": label, "n": int(len(sub)), "ex20": None}
    ex = sub["ex20"]
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex)))
    return {"label": label, "n": int(len(ex)),
            "ex20": round(float(ex.mean()) * 100, 2),
            "med": round(float(ex.median()) * 100, 2),
            "win": round(float((ex > 0).mean()) * 100, 1),
            "t": round(float(t), 1)}


vol = (df["vr"] >= 3) & (df["turnover_rate"] >= 3)   # 首次放量：3 倍均量且绝对换手≥3%
pos_bins = [(-9, -0.3, "低位(60日<=-30%)"), (-0.3, 0.0, "偏弱(-30%~0)"),
            (0.0, 0.3, "偏强(0~30%)"), (0.3, 9, "高位(>30%)")]

rows = [run(vol.values, "全部放量(基准)")]
for lo, hi, name in pos_bins:
    m = vol & (df["mom60"] > lo) & (df["mom60"] <= hi)
    rows.append(run(m.values, f"放量 × {name}"))

# 缩量低位（真正的「无人问津」）
quiet_low = (df["vr"] <= 0.8) & (df["mom60"] <= -0.3) & (df["turnover_rate"] >= 0.3)
rows.append(run(quiet_low.values, "低位 × 缩量(<=0.8x)"))
# 低位放量 + 当日收阳（有承接）
yang = vol & (df["mom60"] <= -0.3) & (df["ret"] > 0.03)
rows.append(run(yang.values, "低位放量且当日涨>3%"))
# 高位放量的镜像复核
high_vol = vol & (df["mom60"] > 1.0)
rows.append(run(high_vol.values, "高位(60日>100%)放量"))

T = pd.DataFrame(rows)
print(T.to_string(index=False))

# 分年：低位放量
m = (vol & (df["mom60"] <= -0.3)).values
sub = df.loc[dedup(m)].dropna(subset=["ex20"]).copy()
sub["y"] = sub["date"].dt.year
by = sub.groupby("y").agg(n=("ex20", "size"), ex20=("ex20", lambda s: round(s.mean() * 100, 1)),
                          win=("ex20", lambda s: round((s > 0).mean() * 100, 1)))
print("\n低位放量 分年：")
print(by.to_string())

OUT.write_text(json.dumps(dict(table=T.to_dict("records"), by_year=by.reset_index().to_dict("records")),
                          ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("\n->", OUT)
