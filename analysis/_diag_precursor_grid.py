"""判据稳健性：参数网格 + 样本外 + 扣成本。

上一轮给的判据是 (20日涨幅>=50%, 换手>=15%) -> T+20 超额 -6.59%。
一个点不构成判据。这里检查：
1. 22 个阈值组合，看它是一块平原还是一根尖峰；
2. 2015-2020 / 2021-2026 两段样本外；
3. 剔除 2015、ST、次新后的纯净子样本；
4. 扣双边成本（印花税 5bp + 佣金 2.5bp + 滑点 10bp）。
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
OUT = DATA / "_diag_precursor_grid.json"

con = duckdb.connect()
con.execute("PRAGMA threads=4")

df = con.execute(
    f"""SELECT symbol,date,open,close,turnover_rate,consecutive_limit_ups
        FROM read_parquet('{K}', hive_partitioning=false) WHERE close>0"""
).df()
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
g = df.groupby("symbol", sort=False)
df["ret"] = df["close"] / g["close"].shift(1) - 1
df["mom20"] = df["close"] / g["close"].shift(21) - 1
cal = pd.DataFrame({"date": sorted(df["date"].unique())})
cal["seq"] = range(len(cal))
df = df.merge(cal, on="date", how="left")
g = df.groupby("symbol", sort=False)
df["f20"] = np.where(g["seq"].shift(-20) - df["seq"] == 20, g["close"].shift(-20) / df["close"] - 1, np.nan)
df["listed_days"] = g.cumcount()

daily = df.groupby("date")["ret"].mean().sort_index().dropna()
idx = daily.index
bmk = pd.Series({idx[i]: float((1 + daily.iloc[i + 1 : i + 21]).prod() - 1) for i in range(len(idx) - 20)})
df["bmk20"] = df["date"].map(bmk)
df["ex20"] = (1 + df["f20"]) / (1 + df["bmk20"]) - 1

syms = df["symbol"].values
seqs = df["seq"].values


def dedup_mask(mask: np.ndarray, gap: int = 20) -> np.ndarray:
    idxs = np.where(mask)[0]
    keep = np.zeros(len(df), dtype=bool)
    last: dict[str, int] = {}
    for i in idxs:
        s = syms[i]
        if s not in last or seqs[i] - last[s] >= gap:
            keep[i] = True
            last[s] = seqs[i]
    return keep


def run(mask: np.ndarray, gap: int = 20) -> dict:
    keep = dedup_mask(np.asarray(mask, dtype=bool), gap)
    sub = df.loc[keep].dropna(subset=["ex20"])
    if len(sub) < 30:
        return dict(n=int(len(sub)), ex=np.nan, win=np.nan)
    ex = sub["ex20"]
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex)))
    return dict(n=int(len(ex)), ex=round(float(ex.mean()) * 100, 2),
                med=round(float(ex.median()) * 100, 2),
                win=round(float((ex > 0).mean()) * 100, 1), t=round(float(t), 1))


# ---------- 1. 参数网格 ----------
MOMP = [0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]
TURN = [5, 10, 15, 20, 25]
grid = {}
print("=" * 96)
print("1. 参数网格：T+20 超额（%），行=20日涨幅阈值，列=当日换手率阈值")
print("=" * 96)
header = "mom\\turn " + "".join(f"{t:>9}%" for t in TURN)
print(header)
for m in MOMP:
    cells = []
    for t in TURN:
        msk = (df["mom20"] >= m) & (df["turnover_rate"] >= t)
        r = run(msk.values)
        grid[f"m{m}_t{t}"] = r
        cells.append(f"{r['ex']:>9.2f}" if r["ex"] == r["ex"] else f"{'--':>9}")
    print(f"{m:<9.2f}" + "".join(cells))
print(f"\\n参考点 m0.5_t15: {grid['m0.5_t15']}")

# ---------- 2. 样本外 ----------
print("\n" + "=" * 96)
print("2. 样本外：同一组参数在前后两段的表现")
print("=" * 96)
seg = {"2015-2020": (df["date"] < "2021-01-01"), "2021-2026": (df["date"] >= "2021-01-01")}
oos = {}
for m, t in [(0.4, 10), (0.5, 10), (0.5, 15), (0.6, 15), (0.8, 15)]:
    row = {}
    for name, cond in seg.items():
        msk = ((df["mom20"] >= m) & (df["turnover_rate"] >= t) & cond).values
        row[name] = run(msk)
    oos[f"m{m}_t{t}"] = row
    print(f"mom>={m:.1f} turn>={t}%  " +
          "  ".join(f"{k}: n={v['n']} ex={v['ex']}%win={v['win']}%" for k, v in row.items()))

# ---------- 3. 纯净子样本 ----------
print("\n" + "=" * 96)
print("3. 剔除混杂：2015 杠杆牛 / ST / 上市<60日次新")
print("=" * 96)
base = (df["mom20"] >= 0.5) & (df["turnover_rate"] >= 15)
clean = base & (df["date"].dt.year != 2015) & (~df["symbol"].str.contains("ST", na=False)) & (df["listed_days"] >= 60)
print("原始     :", run(base.values))
print("剔2015   :", run((base & (df["date"].dt.year != 2015)).values))
print("剔ST     :", run((base & (~df["symbol"].str.contains("ST", na=False))).values))
print("剔次新   :", run((base & (df["listed_days"] >= 60)).values))
print("全剔除   :", run(clean.values))

# ---------- 4. 扣成本 ----------
print("\n" + "=" * 96)
print("4. 扣成本后的剩余超额（单边 17.5bp：印花税 5 + 佣金 2.5 + 滑点 10）")
print("=" * 96)
cost = 0.00175 * 2
r = run(base.values)
print(f"毛超额 {r['ex']}%  ->  双边成本 {cost*100:.2f}%  ->  净 {r['ex'] - cost*100:.2f}%")
print(f"若只承担卖出单边 0.175%：净 {r['ex'] - 0.175:.2f}%")
print(f"年化视角：该判据一年触发约 {r['n']/max(df['date'].dt.year.nunique(),1):.0f} 次/年（去重后）")

out = dict(grid=grid, oos=oos,
           clean=dict(raw=run(base.values), no2015=run((base & (df["date"].dt.year != 2015)).values),
                      noST=run((base & (~df["symbol"].str.contains("ST", na=False))).values),
                      noIPO=run((base & (df["listed_days"] >= 60)).values), all_clean=run(clean.values)),
           cost_note="单边 17.5bp = 印花税5 + 佣金2.5 + 滑点10")
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("\n->", OUT)
