"""筹码年龄的两个示例切片 + 久期的横截面结构。

产物：
  data/.scratch/chip_age/examples.json
    - 601398.SH / 000002.SZ 的 (价格 × 年龄) 20x20 热力图
    - 最新交易日的 D 与市值/换手/年龄熵的横截面关系
这不是判据，只是给页面提供「同一个中位数，藏着两种市场」的直观证据。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "chip_age"
PANEL = OUT / "age_panel.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"

EXAMPLES = [("601398.SH", "工商银行"), ("000002.SZ", "万科A")]


def load_symbol(sym: str) -> pd.DataFrame:
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "close", "high", "low", "volume", "amount", "turnover_rate"],
                   filter=ds.field("symbol") == sym)
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.99)
    df["vwap"] = np.where(df["volume"] > 0, df["amount"] / (df["volume"] * 100), (df["high"] + df["low"] + df["close"]) / 3)
    df["vwap"] = df["vwap"].replace([np.inf, -np.inf], np.nan).fillna((df["high"] + df["low"] + df["close"]) / 3)
    return df


def heatmap(df: pd.DataFrame, max_age: int = 500, nb_price: int = 20, nb_age: int = 20) -> dict:
    t = len(df) - 1
    close_t = float(df["close"].iloc[t])
    rows = []
    w = 1.0
    for s in range(t, max(t - max_age, -1), -1):
        to = float(df["to"].iloc[s])
        weight = to * w
        if weight > 0:
            rows.append((t - s, float(df["vwap"].iloc[s]) / close_t, weight))
        w *= (1.0 - to)
    arr = np.asarray(rows, dtype=float)
    price_edges = np.linspace(0.60, 1.40, nb_price + 1)
    age_edges = np.linspace(0, max_age, nb_age + 1)
    mat, _, _ = np.histogram2d(arr[:, 1], arr[:, 0], bins=[price_edges, age_edges], weights=arr[:, 2])
    total = mat.sum()
    if total > 0:
        mat = mat / total
    return {
        "symbol": str(df["symbol"].iloc[0]),
        "date": str(df["date"].iloc[t].date()),
        "close": round(close_t, 2),
        "price_edges": [round(float(x), 4) for x in price_edges],
        "age_edges": [round(float(x), 1) for x in age_edges],
        "matrix": [[round(float(v), 6) for v in row] for row in mat.tolist()],
        "age_marginal": [round(float(v), 6) for v in mat.sum(axis=0).tolist()],
        "price_marginal": [round(float(v), 6) for v in mat.sum(axis=1).tolist()],
    }


def main() -> None:
    panel = pd.read_parquet(PANEL)
    last = panel["date"].max()
    cur = panel[panel["date"] == last].copy()
    inst = pd.read_parquet(INST)[["symbol", "float_shares", "name"]]
    cur = cur.merge(inst, on="symbol", how="left")
    cur["float_mktcap"] = cur["float_shares"] * cur["close"]
    cur = cur[(cur["float_mktcap"] > 0) & cur["age_mean"].notna()].copy()
    cur["logcap"] = np.log(cur["float_mktcap"])
    cur["cap_q"] = pd.qcut(cur["logcap"], 5, labels=False, duplicates="drop") + 1

    def corr(a, b):
        x = cur[[a, b]].dropna()
        return round(float(x[a].corr(x[b])), 4) if len(x) > 10 else None

    by_cap = (
        cur.groupby("cap_q")
        .agg(n=("symbol", "nunique"), age=("age_mean", "median"), entropy=("age_entropy", "median"),
             young20=("young20", "median"), old=("old_share", "median"))
        .reset_index()
        .round(3)
        .to_dict(orient="records")
    )

    examples = []
    for sym, name in EXAMPLES:
        df = load_symbol(sym)
        h = heatmap(df)
        h["name"] = name
        row = cur[cur["symbol"] == sym]
        if len(row):
            h["age_mean"] = round(float(row["age_mean"].iloc[0]), 1)
            h["entropy"] = round(float(row["age_entropy"].iloc[0]), 2)
            h["young20"] = round(float(row["young20"].iloc[0]), 3)
            h["old_share"] = round(float(row["old_share"].iloc[0]), 3)
        examples.append(h)

    out = {
        "as_of": str(pd.Timestamp(last).date()),
        "n": int(len(cur)),
        "corr": {
            "D_logcap": corr("age_mean", "logcap"),
            "D_entropy": corr("age_mean", "age_entropy"),
            "D_young20": corr("age_mean", "young20"),
            "D_old": corr("age_mean", "old_share"),
        },
        "by_cap": by_cap,
        "examples": examples,
    }
    (OUT / "examples.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("as_of", "n", "corr", "by_cap")}, ensure_ascii=False, indent=2))
    for e in examples:
        print(e["symbol"], e["name"], e.get("age_mean"), e.get("entropy"), e.get("young20"), e.get("old_share"))


if __name__ == "__main__":
    main()
