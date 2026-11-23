"""12 个「天地板→次日地天板」样本逐个复盘 + T-3 可卖性 + 与 441 个对照组的判别力检验。

关键口径修正（上一轮犯过的错）：
- 上一轮 turn_slope 用了 T 日换手 / T-3 换手 ⇒ 含未来信息，不能用于「T-3 离场」。
- 本轮所有「前兆」只允许使用 T-3 收盘前可观测的量。
- 一字板判定：open==high==low==close（容差 0.2%），且 ret>=9.5%。
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
OUT = DATA / "_diag_jiuyang_precursors.json"

con = duckdb.connect()
con.execute("PRAGMA threads=4")

df = con.execute(
    f"""SELECT symbol,date,open,high,low,close,volume,amount,turnover_rate,consecutive_limit_ups
        FROM read_parquet('{K}', hive_partitioning=false) WHERE close>0"""
).df()
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
g = df.groupby("symbol", sort=False)
df["pc"] = g["close"].shift(1)
df["ret"] = df["close"] / df["pc"] - 1
df["lim_up"] = df["ret"] >= 0.095
cal = pd.DataFrame({"date": sorted(df["date"].unique())})
cal["seq"] = range(len(cal))
df = df.merge(cal, on="date", how="left")
g = df.groupby("symbol", sort=False)

for h in (1, 5, 20):
    df[f"c{h}"] = g["close"].shift(-h)
    df[f"s{h}"] = g["seq"].shift(-h)
    df[f"f{h}"] = np.where(df[f"s{h}"] - df["seq"] == h, df[f"c{h}"] / df["close"] - 1, np.nan)
df["mom20"] = df["close"] / g["close"].shift(21) - 1

daily = df.groupby("date")["ret"].mean().sort_index().dropna()
idx = daily.index
bmk = {h: pd.Series({idx[i]: float((1 + daily.iloc[i + 1 : i + h + 1]).prod() - 1) for i in range(len(idx) - h)}) for h in (1, 5, 20)}

# 一字板：四价合一 + 涨停
df["one_word"] = (df["ret"] >= 0.095) & (
    (df[["open", "high", "low", "close"]].max(axis=1) - df[["open", "high", "low", "close"]].min(axis=1)).abs()
    <= df["close"] * 0.002
)

prev = g["lim_up"].shift(1).fillna(False)
e1 = df[prev & (df["high"] >= df["pc"] * 1.095) & (df["close"] <= df["pc"] * 0.905)].copy()
e2 = df[(df["open"] <= df["pc"] * 0.905) & (df["high"] >= df["pc"] * 1.095) & (df["ret"] > 0)][["symbol", "seq"]].copy()
e2["seq"] -= 1
e2["_hit"] = 1
e1 = e1.merge(e2, on=["symbol", "seq"], how="left")
e1["is_combo"] = (e1["_hit"] == 1).astype(int)
e1.attrs = {}

inst = con.execute("SELECT symbol,name FROM read_parquet('data/instruments/instruments.parquet')").df()
nm = dict(zip(inst["symbol"], inst["name"]))

# ---------- A. 12 样本逐个复盘 ----------
combo = e1[e1["is_combo"] == 1].sort_values("date").copy()
rows = []
for _, r in combo.iterrows():
    s, t = r["symbol"], int(r["seq"])
    w = df[(df.symbol == s) & (df.seq >= t - 3) & (df.seq <= t + 20)].sort_values("seq").reset_index(drop=True)
    t3 = w[w.seq == t - 3].iloc[0]
    t1 = w[w.seq == t - 1].iloc[0]
    nxt = w[w.seq == t + 1]
    d = dict(
        name=nm.get(s), sym=s, date=str(r["date"].date()),
        t3_lu=int(t3["consecutive_limit_ups"]), t3_turn=round(float(t3["turnover_rate"]), 2),
        t3_one_word=bool(t3["one_word"]), t3_mom20=round(float(t3["mom20"]) * 100, 1) if pd.notna(t3["mom20"]) else None,
        t1_lu=int(t1["consecutive_limit_ups"]), t1_turn=round(float(t1["turnover_rate"]), 2),
        t_open=round(float(r["open"]), 2), t_high=round(float(r["high"]), 2),
        t_low=round(float(r["low"]), 2), t_close=round(float(r["close"]), 2), t_turn=round(float(r["turnover_rate"]), 2),
        t1_ret=round(float(nxt["ret"].iloc[0]) * 100, 2) if len(nxt) else None,
        f20=round(float(r["f20"]) * 100, 1) if pd.notna(r["f20"]) else None,
        # T-3 能不能卖：换手率是流动性代理，<1% 基本无法出货
        t3_tradable="yes" if t3["turnover_rate"] >= 3 else ("marginal" if t3["turnover_rate"] >= 1 else "no"),
    )
    rows.append(d)
rep = pd.DataFrame(rows)
print("=" * 110)
print("A. 12 样本逐个复盘（T-3 全部为收盘前可观测）")
print("=" * 110)
print(rep.to_string(index=False))

# ---------- B. T-3 可卖性分布 ----------
print("\n" + "=" * 110)
print("B. 全部 453 个天地板样本：T-3 的换手率分布（决定「T-3 离场」是否物理可行）")
print("=" * 110)
allt3 = []
for _, r in e1.iterrows():
    s, t = r["symbol"], int(r["seq"])
    x = df[(df.symbol == s) & (df.seq == t - 3)]
    if len(x):
        allt3.append(dict(turn=float(x.iloc[0]["turnover_rate"]), ow=bool(x.iloc[0]["one_word"]),
                          lu=int(x.iloc[0]["consecutive_limit_ups"])))
A3 = pd.DataFrame(allt3)
qs = A3["turn"].quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(2).to_dict()
print(f"n={len(A3)}  T-3 换手率: p10={qs[0.1]} p25={qs[0.25]} 中位={qs[0.5]} p75={qs[0.75]} p90={qs[0.9]}")
print(f"T-3 一字板占比: {A3['ow'].mean()*100:.1f}%")
print(f"T-3 换手<1%（几乎无法出货）: {(A3['turn']<1).mean()*100:.1f}%")
print(f"T-3 换手<3%: {(A3['turn']<3).mean()*100:.1f}%")
print(f"T-3 连板数中位: {A3['lu'].median():.0f}  连板>=2 占比: {(A3['lu']>=2).mean()*100:.1f}%")

# ---------- C. 12 vs 441 判别力 ----------
print("\n" + "=" * 110)
print("C. 12 vs 441：T-3 特征能否区分「次日会不会地天板」")
print("=" * 110)


def auc(x_pos, x_neg):
    x_pos, x_neg = np.asarray(x_pos), np.asarray(x_neg)
    if len(x_pos) == 0 or len(x_neg) == 0:
        return np.nan
    allv = np.concatenate([x_pos, x_neg])
    r = pd.Series(allv).rank().values
    return (r[: len(x_pos)].sum() - len(x_pos) * (len(x_pos) + 1) / 2) / (len(x_pos) * len(x_neg))


feat = {}
for _, r in e1.iterrows():
    s, t = r["symbol"], int(r["seq"])
    x = df[(df.symbol == s) & (df.seq == t - 3)]
    if not len(x):
        continue
    x = x.iloc[0]
    feat.setdefault("is_combo", []).append(int(r["is_combo"]))
    feat.setdefault("t3_turn", []).append(float(x["turnover_rate"]))
    feat.setdefault("t3_lu", []).append(float(x["consecutive_limit_ups"]))
    feat.setdefault("t3_mom20", []).append(float(x["mom20"]) if pd.notna(x["mom20"]) else np.nan)
    feat.setdefault("t3_one_word", []).append(float(bool(x["one_word"])))
F = pd.DataFrame(feat).dropna()
res_c = []
for c in ["t3_turn", "t3_lu", "t3_mom20", "t3_one_word"]:
    a = auc(F.loc[F.is_combo == 1, c], F.loc[F.is_combo == 0, c])
    res_c.append(dict(feature=c, auc=round(float(a), 3),
                      combo_med=round(float(F.loc[F.is_combo == 1, c].median()), 2),
                      ctrl_med=round(float(F.loc[F.is_combo == 0, c].median()), 2)))
C = pd.DataFrame(res_c)
print(C.to_string(index=False))
print("AUC=0.5 表示毫无判别力；n=12 时 AUC 的 95% 区间约 ±0.15，任何单特征都不足以支撑交易。")

# ---------- D. 可回答的替代问题：T-3 特征 -> T+20 亏损幅度 ----------
print("\n" + "=" * 110)
print("D. 全部 453 个天地板：T-3 特征分组后的 T+20 超额（这个有统计功效）")
print("=" * 110)
rows_d = []
for _, r in e1.iterrows():
    s, t = r["symbol"], int(r["seq"])
    x = df[(df.symbol == s) & (df.seq == t - 3)]
    if not len(x) or pd.isna(r["f20"]):
        continue
    x = x.iloc[0]
    b = bmk[20].get(r["date"])
    if b is None:
        continue
    rows_d.append(dict(t3_turn=float(x["turnover_rate"]), t3_lu=float(x["consecutive_limit_ups"]),
                       t3_one_word=int(bool(x["one_word"])),
                       ex20=float((1 + r["f20"]) / (1 + b) - 1), year=int(r["date"].year)))
D = pd.DataFrame(rows_d)
D["turn_bucket"] = pd.cut(D["t3_turn"], [-0.01, 1, 3, 10, 100], labels=["<1%", "1-3%", "3-10%", ">10%"])
D["lu_bucket"] = pd.cut(D["t3_lu"], [-0.01, 0.5, 1.5, 4.5, 100], labels=["0板", "1板", "2-4板", ">=5板"])
g1 = D.groupby("turn_bucket", observed=True).agg(n=("ex20", "size"), ex20=("ex20", lambda s: round(s.mean() * 100, 2)),
                                                 win=("ex20", lambda s: round((s > 0).mean() * 100, 1)))
g2 = D.groupby("lu_bucket", observed=True).agg(n=("ex20", "size"), ex20=("ex20", lambda s: round(s.mean() * 100, 2)),
                                                win=("ex20", lambda s: round((s > 0).mean() * 100, 1)))
g3 = D.groupby("t3_one_word", observed=True).agg(n=("ex20", "size"), ex20=("ex20", lambda s: round(s.mean() * 100, 2)),
                                                  win=("ex20", lambda s: round((s > 0).mean() * 100, 1)))
print("\n按 T-3 换手分组："); print(g1.to_string())
print("\n按 T-3 连板数分组："); print(g2.to_string())
print("\n按 T-3 是否一字板："); print(g3.to_string())

out = dict(
    samples=rep.to_dict("records"),
    t3_liquidity=dict(n=int(len(A3)), q=qs, one_word_pct=round(float(A3["ow"].mean()) * 100, 1),
                      lt1_pct=round(float((A3["turn"] < 1).mean()) * 100, 1),
                      lt3_pct=round(float((A3["turn"] < 3).mean()) * 100, 1),
                      lu_median=float(A3["lu"].median())),
    discrimination=C.to_dict("records"),
    ex20_by_turn=g1.reset_index().to_dict("records"),
    ex20_by_lu=g2.reset_index().to_dict("records"),
    ex20_by_oneword=g3.reset_index().to_dict("records"),
)
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("\n->", OUT)
