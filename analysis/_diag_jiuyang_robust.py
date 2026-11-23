"""九阳结构分析的稳健性层：事件去重、分年、地天板当日涨幅分层、估值反算。"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
KLINE = str(DATA / "kline_daily_enriched" / "*" / "part.parquet").replace("\\", "/")
OUT = DATA / "_diag_jiuyang_robust.json"

con = duckdb.connect()
con.execute("PRAGMA threads=4")

df = con.execute(
    f"""SELECT symbol,date,open,high,low,close,volume,amount,turnover_rate,consecutive_limit_ups
        FROM read_parquet('{KLINE}', hive_partitioning=false)
        WHERE close IS NOT NULL AND close>0"""
).df()
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
g = df.groupby("symbol", sort=False)
df["pc"] = g["close"].shift(1)
df["ret"] = df["close"] / df["pc"] - 1
df["lim_up"] = df["ret"] >= 0.095
cal = pd.DataFrame({"date": np.sort(df["date"].unique())})
cal["seq"] = np.arange(len(cal))
df = df.merge(cal, on="date", how="left")
g = df.groupby("symbol", sort=False)

for h in (1, 5, 20):
    df[f"c{h}"] = g["close"].shift(-h)
    df[f"s{h}"] = g["seq"].shift(-h)
    df[f"f{h}"] = np.where(df[f"s{h}"] - df["seq"] == h, df[f"c{h}"] / df["close"] - 1, np.nan)

daily = df.groupby("date")["ret"].mean().sort_index().dropna()
idx = daily.index
bmk = {}
for h in (1, 5, 20):
    bmk[h] = pd.Series(
        {idx[i]: float((1 + daily.iloc[i + 1 : i + h + 1]).prod() - 1) for i in range(len(idx) - h)}
    )


def dedup(ev: pd.DataFrame, gap: int = 20) -> pd.DataFrame:
    """同一 symbol 在 gap 个交易日内只保留首个事件。"""
    ev = ev.sort_values(["symbol", "date"])
    keep = []
    last = {}
    for sym, d, s in zip(ev["symbol"], ev["date"], ev["seq"]):
        if sym not in last or s - last[sym] >= gap:
            keep.append(True)
            last[sym] = s
        else:
            keep.append(False)
    return ev[keep]


def stat(ev: pd.DataFrame, h: int) -> dict:
    sub = ev.dropna(subset=[f"f{h}"])
    b = sub["date"].map(bmk[h])
    ex = ((1 + sub[f"f{h}"]) / (1 + b) - 1).replace([np.inf, -np.inf], np.nan).dropna()
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex))) if len(ex) > 2 else np.nan
    return dict(
        n=int(len(ex)),
        fwd=round(float(sub[f"f{h}"].median()) * 100, 2),
        ex=round(float(ex.mean()) * 100, 2),
        ex_med=round(float(ex.median()) * 100, 2),
        ex_win=round(float((ex > 0).mean()) * 100, 1),
        t=round(float(t), 2),
    )


res = {}

# ---- E1 天地板（前一日涨停）----
prev = g["lim_up"].shift(1).fillna(False)
e1 = df[prev & (df["high"] >= df["pc"] * 1.095) & (df["close"] <= df["pc"] * 0.905)].copy()
e1_raw = e1.copy()
e1d = dedup(e1)
res["E1_天地板"] = {
    "raw": {f"T+{h}": stat(e1_raw, h) for h in (1, 5, 20)},
    "dedup20": {f"T+{h}": stat(e1d, h) for h in (1, 5, 20)},
    "years": int(e1d["date"].dt.year.nunique()),
}
# 分年（去重后）
yr = []
for y, sub in e1d.groupby(e1d["date"].dt.year):
    if len(sub) >= 5:
        yr.append(dict(y=int(y), **stat(sub, 20)))
res["E1_by_year"] = yr

# ---- E2 地天板 + 当日涨幅分层 ----
e2 = df[(df["open"] <= df["pc"] * 0.905) & (df["high"] >= df["pc"] * 1.095) & (df["ret"] > 0)].copy()
e2d = dedup(e2)
res["E2_地天板"] = {"raw": {f"T+{h}": stat(e2, h) for h in (1, 5, 20)}, "n_raw": int(len(e2))}
bins = [(0, 0.03), (0.03, 0.06), (0.06, 0.09), (0.09, 1.0)]
lay = []
for lo, hi in bins:
    sub = e2[(e2["ret"] >= lo) & (e2["ret"] < hi)]
    if len(sub) >= 20:
        lay.append(dict(bucket=f"{lo:.0%}~{hi:.0%}", **stat(sub, 5)))
res["E2_by_day_gain_T5"] = lay

# ---- E5: 天地板次日再地天板 ----
nxt = e2[["symbol", "seq"]].copy()
nxt["seq"] = nxt["seq"] - 1          # 事件日 t 的次日 t+1 命中地天板
nxt["_hit"] = 1
_e1 = e1.merge(nxt, on=["symbol", "seq"], how="left")
combo = _e1[_e1["_hit"] == 1].copy()
res["E5_天地板后次日地天板"] = {"n": int(len(combo)), **{f"T+{h}": stat(combo, h) for h in (1, 5, 20)}}

# ---- 九阳自身 ----
own = df[(df.symbol == "002242.SZ") & (df["date"] >= "2026-01-01")].copy()
low = own.loc[own["close"].idxmin()]
res["own"] = dict(
    low_date=str(low["date"].date()),
    low_close=round(float(low["close"]), 2),
    peak_date=str(own.loc[own["high"].idxmax(), "date"].date()),
    peak_high=round(float(own["high"].max()), 2),
    gain=round(float(own["high"].max() / low["close"] - 1) * 100, 1),
)
# 2025-11 那轮
r25 = own[(own["date"] >= "2025-11-01") & (own["date"] <= "2026-07-31")]
hi25 = r25.loc[r25["high"].idxmax()]
after = r25[r25["date"] > hi25["date"]]
res["own_2025_round"] = dict(
    peak_date=str(hi25["date"].date()),
    peak=round(float(hi25["high"]), 2),
    low_after=round(float(after["close"].min()), 2),
    drawdown=round(float(after["close"].min() / hi25["high"] - 1) * 100, 1),
    days_to_low=int(len(after)),
)

# ---- 估值反算 ----
SHARES = 763_017_000
px_now, px_low, px_peak = 30.36, float(low["close"]), float(own["high"].max())
mc_now, mc_low = px_now * SHARES / 1e8, px_low * SHARES / 1e8
np_9m = 0.719895  # 亿元，2026 前三季归母
np_annual = np_9m / 3 * 4
industry_pe = 19.24
main_biz = np_annual * industry_pe
main_biz_30 = np_annual * 30
delta = mc_now - mc_low
implied = delta / 0.0792
res["valuation"] = dict(
    shares_yi=round(SHARES / 1e8, 2),
    mc_now_yi=round(mc_now, 1),
    mc_low_yi=round(mc_low, 1),
    mc_delta_yi=round(delta, 1),
    np_annual_yi=round(np_annual, 2),
    main_biz_at_industry_pe=round(main_biz, 1),
    main_biz_at_30pe=round(main_biz_30, 1),
    implied_shensikao_yi=round(implied, 0),
    story_share_of_mc=round((1 - main_biz / mc_now) * 100, 1),
)

OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
print("\n->", OUT)
