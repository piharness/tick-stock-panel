"""T-3 离场判据的精确率与代价。

问三个问题：
1. 判据触发后，20 个交易日内真的发生天地板的概率是多少？（精确率 vs 基率）
2. 没发生的那些（误杀），持有 20 天的收益是多少？（砍掉的右尾）
3. 判据是否在每一年都成立？（分年）

不做任何未来信息：判据只用 T 日收盘可观测值，卖出假设为次日开盘。
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
OUT = DATA / "_diag_precursor_cost.json"

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
df["mom20"] = df["close"] / g["close"].shift(21) - 1

# 未来收益（用于「不卖」的对照）
for h in (5, 20):
    df[f"c{h}"] = g["close"].shift(-h)
    df[f"s{h}"] = g["seq"].shift(-h)
    df[f"f{h}"] = np.where(df[f"s{h}"] - df["seq"] == h, df[f"c{h}"] / df["close"] - 1, np.nan)

daily = df.groupby("date")["ret"].mean().sort_index().dropna()
idx = daily.index
bmk = {h: pd.Series({idx[i]: float((1 + daily.iloc[i + 1 : i + h + 1]).prod() - 1) for i in range(len(idx) - h)}) for h in (5, 20)}

# 天地板标记（T 日）
prev_lim = g["lim_up"].shift(1).fillna(False)
df["is_tiandi"] = (prev_lim & (df["high"] >= df["pc"] * 1.095) & (df["close"] <= df["pc"] * 0.905)).astype(int)

# 未来 20 日内是否出现天地板（不含当日）
g2 = df.groupby("symbol", sort=False)
df["td_fwd20"] = g2["is_tiandi"].transform(
    lambda s: s[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)
).fillna(0)

base_rate = df["is_tiandi"].mean()
print(f"全市场天地板基率: {base_rate*100:.4f}%  (n={int(df['is_tiandi'].sum())} / {len(df):,})")

# ---------- 判据 ----------
rules = {
    "连板>=5": df["consecutive_limit_ups"] >= 5,
    "连板>=3": df["consecutive_limit_ups"] >= 3,
    "20日涨幅>=100%": df["mom20"] >= 1.0,
    "20日涨幅>=150%": df["mom20"] >= 1.5,
    "连板>=5 且 换手>=5%": (df["consecutive_limit_ups"] >= 5) & (df["turnover_rate"] >= 5),
    "20日>=100% 且 换手>=15%": (df["mom20"] >= 1.0) & (df["turnover_rate"] >= 15),
}


def dedup_mask(mask: pd.Series, gap: int = 20) -> pd.Series:
    """同一 symbol gap 日内只保留首个触发。"""
    idxs = np.where(mask.values)[0]
    keep = np.zeros(len(df), dtype=bool)
    last = {}
    syms = df["symbol"].values
    seqs = df["seq"].values
    for i in idxs:
        s = syms[i]
        if s not in last or seqs[i] - last[s] >= gap:
            keep[i] = True
            last[s] = seqs[i]
    return pd.Series(keep, index=df.index)


res = {}
print("\n" + "=" * 118)
print(f"{'判据':<24}{'触发n':>7}{'20日内天地板率':>14}{'提升倍数':>9}{'T+20超额':>10}{'T+20胜率':>9}{'误杀占比':>9}")
print("=" * 118)
for name, mask in rules.items():
    m = dedup_mask(mask.fillna(False))
    sub = df[m].dropna(subset=["f20"])
    if not len(sub):
        continue
    hit = float(sub["td_fwd20"].mean())
    b = sub["date"].map(bmk[20])
    ex = ((1 + sub["f20"]) / (1 + b) - 1).replace([np.inf, -np.inf], np.nan).dropna()
    res[name] = dict(
        n=int(len(sub)),
        td_rate_20d=round(hit * 100, 2),
        lift=round(hit / base_rate, 1) if base_rate > 0 else None,
        ex20=round(float(ex.mean()) * 100, 2),
        ex20_win=round(float((ex > 0).mean()) * 100, 1),
        miss_rate=round((1 - hit) * 100, 2),
        years=int(sub["date"].dt.year.nunique()),
    )
    r = res[name]
    print(f"{name:<24}{r['n']:>7}{r['td_rate_20d']:>13.2f}%{r['lift']:>9.0f}x{r['ex20']:>9.2f}%{r['ex20_win']:>8.1f}%{r['miss_rate']:>8.2f}%")

# ---------- 分年：连板>=5 ----------
m = dedup_mask((df["consecutive_limit_ups"] >= 5).fillna(False))
sub = df[m].dropna(subset=["f20"]).copy()
sub["y"] = sub["date"].dt.year
b = sub["date"].map(bmk[20])
sub["ex20"] = (1 + sub["f20"]) / (1 + b) - 1
by = sub.groupby("y").agg(n=("ex20", "size"), ex20=("ex20", lambda s: round(s.mean() * 100, 1)),
                          td=("td_fwd20", lambda s: round(s.mean() * 100, 2)))
print("\n连板>=5 分年（T+20 超额 / 20日内天地板率）：")
print(by.to_string())
res["by_year_lu5"] = by.reset_index().to_dict("records")

# ---------- 如果只在 T 日开盘卖：滑点 ----------
combo = df[df["is_tiandi"] == 1].copy()
combo["open_gap"] = combo["open"] / combo["pc"] - 1
print(f"\n天地板当日开盘缺口（相对前收）：中位 {combo['open_gap'].median()*100:.2f}%  "
      f"均值 {combo['open_gap'].mean()*100:.2f}%  "
      f"开盘即跌停占比 {(combo['open_gap']<=-0.095).mean()*100:.1f}%")
print(f"天地板当日收盘相对前收：中位 {combo['ret'].median()*100:.2f}%")
print(f"⇒ T 日开盘卖 vs 收盘卖 的差价中位：{(combo['open_gap']-combo['ret']).median()*100:.2f}pct")
res["t_day_execution"] = dict(
    open_gap_median=round(float(combo["open_gap"].median()) * 100, 2),
    open_gap_mean=round(float(combo["open_gap"].mean()) * 100, 2),
    open_limit_down_pct=round(float((combo["open_gap"] <= -0.095).mean()) * 100, 1),
    close_median=round(float(combo["ret"].median()) * 100, 2),
    gain_vs_close=round(float((combo["open_gap"] - combo["ret"]).median()) * 100, 2),
)

OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("\n->", OUT)
