"""九阳股份(002242) 2026-11 行情结构解剖 + 全市场同构事件回放。

结论口径：
- 事件日 t 收盘后可观测，收益从 t 收盘起算（吃不到 t 日涨幅）
- 日历相邻校验：t+i 必须与 t 的交易日 seq 相差 i，杜绝跨停牌假收益
- 超额 = 个股区间收益 - 同期全市场等权区间收益
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
KLINE = str(DATA / "kline_daily_enriched" / "*" / "part.parquet").replace("\\", "/")
OUT = DATA / "_diag_jiuyang.json"

con = duckdb.connect()
con.execute("PRAGMA threads=4")


def load() -> pd.DataFrame:
    q = f"""
    SELECT symbol, date, open, high, low, close, volume, amount, turnover_rate,
           consecutive_limit_ups
    FROM read_parquet('{KLINE}', hive_partitioning=false)
    WHERE close IS NOT NULL AND close > 0
    """
    df = con.execute(q).df()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["pc"] = g["close"].shift(1)
    df["ret"] = df["close"] / df["pc"] - 1
    df["amp"] = (df["high"] - df["low"]) / df["pc"]
    df["mom20"] = df["close"] / g["close"].shift(21) - 1
    df["lim_up"] = df["ret"] >= 0.095          # 主板 10% 口径，避开 ST 5% 历史
    df["lim_dn"] = df["ret"] <= -0.095
    # 日历 seq：跨停牌保护
    cal = pd.DataFrame({"date": np.sort(df["date"].unique())})
    cal["seq"] = np.arange(len(cal))
    df = df.merge(cal, on="date", how="left")
    df["seq"] = df["seq"].astype("int32")
    return df, cal


def add_fwd(df: pd.DataFrame, horizons=(1, 3, 5, 10, 20)) -> pd.DataFrame:
    g = df.groupby("symbol", sort=False)
    for h in horizons:
        df[f"c{h}"] = g["close"].shift(-h)
        df[f"s{h}"] = g["seq"].shift(-h)
        df[f"f{h}"] = np.where(df[f"s{h}"] - df["seq"] == h, df[f"c{h}"] / df["close"] - 1, np.nan)
    return df


def bench(df: pd.DataFrame, horizons=(1, 3, 5, 10, 20)) -> dict:
    """全市场等权基准：每天截面均值收益，按窗口累乘。"""
    daily = df.groupby("date")["ret"].mean().sort_index()
    daily = daily.dropna()
    idx = daily.index
    out = {}
    for h in horizons:
        vals = []
        for i in range(len(idx) - h):
            w = daily.iloc[i + 1 : i + h + 1]
            vals.append((idx[i], float((1 + w).prod() - 1)))
        out[h] = pd.Series(dict(vals))
    return out


def summarize(ev: pd.DataFrame, bmk: dict, label: str, horizons=(1, 3, 5, 10, 20)) -> list[dict]:
    rows = []
    for h in horizons:
        sub = ev.dropna(subset=[f"f{h}"])
        if len(sub) < 10:
            continue
        fwd = sub[f"f{h}"]
        b = sub["date"].map(bmk[h])
        ex = (1 + fwd) / (1 + b) - 1
        ex = ex.replace([np.inf, -np.inf], np.nan).dropna()
        t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex))) if len(ex) > 2 else np.nan
        rows.append(
            dict(
                event=label,
                horizon=h,
                n=int(len(ex)),
                fwd_mean=round(float(fwd.mean()) * 100, 2),
                fwd_median=round(float(fwd.median()) * 100, 2),
                excess_mean=round(float(ex.mean()) * 100, 2),
                excess_median=round(float(ex.median()) * 100, 2),
                win=round(float((fwd > 0).mean()) * 100, 1),
                excess_win=round(float((ex > 0).mean()) * 100, 1),
                t=round(float(t), 2) if t == t else None,
            )
        )
    return rows


def main() -> None:
    print("[1/4] loading 15M rows ...", flush=True)
    df, cal = load()
    df = add_fwd(df)
    bmk = bench(df)
    print(f"    rows={len(df):,} symbols={df.symbol.nunique()} dates={df.date.nunique()}")

    res: dict = {"generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), "events": []}

    # ---------- E1: 涨停次日「天地板」 ----------
    # 前一日涨停 -> 今日盘中触及涨停价 -> 收盘跌停
    prev_limit = df.groupby("symbol", sort=False)["lim_up"].shift(1).fillna(False)
    e1 = df[prev_limit & (df["high"] >= df["pc"] * 1.095) & (df["close"] <= df["pc"] * 0.905)].copy()
    print(f"[2/4] E1 天地板 n={len(e1)}")

    # ---------- E2: 地天板（跌停开 -> 盘中涨停） ----------
    e2 = df[(df["open"] <= df["pc"] * 0.905) & (df["high"] >= df["pc"] * 1.095) & (df["ret"] > 0)].copy()
    print(f"      E2 地天板 n={len(e2)}")

    # ---------- E3: 高位高换手冲高回落 ----------
    e3 = df[(df["mom20"] >= 1.0) & (df["turnover_rate"] >= 15) & (df["close"] < df["high"] * 0.97)].copy()
    print(f"      E3 高位放量滞涨 n={len(e3)}")

    # ---------- E4: 连板>=3 后首次断板 ----------
    p3 = df.groupby("symbol", sort=False)["consecutive_limit_ups"].shift(1).fillna(0)
    e4 = df[(p3 >= 3) & (df["consecutive_limit_ups"] == 0)].copy()
    print(f"      E4 高位断板 n={len(e4)}")

    for ev, label in [(e1, "涨停次日天地板"), (e2, "地天板"), (e3, "高位放量滞涨"), (e4, "三板以上断板")]:
        res["events"] += summarize(ev, bmk, label)
        if len(ev):
            res.setdefault("by_year", {})[label] = (
                ev.assign(y=ev["date"].dt.year)
                .groupby("y")
                .apply(lambda x: dict(n=int(len(x)), ex20=round(float(((1 + x["f20"]) / (1 + x["date"].map(bmk[20])) - 1).mean()) * 100, 2)))
                .to_dict()
            )

    # ---------- 九阳当前位置的最近邻：60日翻倍 + 换手>=15 + 收阳 ----------
    df["mom60"] = df.groupby("symbol", sort=False)["close"].shift(61)
    df["mom60"] = df["close"] / df["mom60"] - 1
    nn = df[(df["mom60"] >= 1.5) & (df["turnover_rate"] >= 15) & (df["ret"] > 0)].copy()
    res["events"] += summarize(nn, bmk, "60日翻倍+高换手收阳")
    res["nn_n"] = int(len(nn))
    res["nn_dates"] = int(nn["date"].nunique())

    # ---------- 该股自身历史 ----------
    own = df[df.symbol == "002242.SZ"].copy()
    res["own"] = dict(
        rows=int(len(own)),
        first=str(own["date"].min().date()),
        last=str(own["date"].max().date()),
        max_close=float(own["close"].max()),
        max_close_date=str(own.loc[own["close"].idxmax(), "date"].date()),
    )

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("[3/4] table")
    t = pd.DataFrame(res["events"])
    print(t.to_string(index=False))
    print(f"[4/4] -> {OUT}")


if __name__ == "__main__":
    main()
