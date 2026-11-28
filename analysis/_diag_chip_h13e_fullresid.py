"""H13e：同日全市场「同跌幅 + 同波动/换手/规模」残差

H13d 只控制了跌幅的三次曲线，near 仍 +229bp（t 3.63）。但 near 组可能只是
波动更低/换手更低/规模更大——这些都与反转收益相关。

本脚本在每个公告日做全市场横截面回归：
  f60 ~ ret250 + ret250² + ret250³ + ret60 + vol20 + turn20 + log(amount20)
取 near / far 的残差，按月聚簇。若 near 残差仍显著为正，说明「贴线」在
「同跌幅、同波动、同换手、同规模」之外还有增量；否则它只是这些特征的组合。

输出：data/.scratch/leverage/h13e_fullresid.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_audit_fix import load, nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
KLINE = ROOT / "data" / "kline_daily_enriched"
XS = ["ret250", "ret250_2", "ret250_3", "ret60", "vol20", "turn20", "logamt20"]


def main() -> None:
    ev = load()
    syms = ev["symbol"].unique().tolist()
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "open", "close", "amount", "turnover_rate"],
        filter=(ds.field("date") >= pd.Timestamp("2016-01-01").date()) & ds.field("symbol").isin(syms),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    O = px.pivot_table(index="date", columns="symbol", values="open").sort_index()
    C = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    A = px.pivot_table(index="date", columns="symbol", values="amount").sort_index()
    T = px.pivot_table(index="date", columns="symbol", values="turnover_rate").sort_index()
    R1 = C.pct_change(fill_method=None)
    ret60 = C / C.shift(60) - 1
    ret250 = C / C.shift(250) - 1
    vol20 = R1.rolling(20).std()
    turn20 = T.rolling(20).mean()
    logamt20 = np.log(A.rolling(20).mean().clip(lower=1))
    f60 = C.shift(-60) / O.shift(-1) - 1
    ret250_2 = ret250 ** 2
    ret250_3 = ret250 ** 3

    di = C.index
    idx = di.searchsorted(pd.to_datetime(ev["notice"]).values)
    ok = (idx < len(di)) & (idx >= 0)
    ev = ev[ok].reset_index(drop=True)
    idx = idx[ok]
    ev["row"] = idx

    cols = list(f60.columns)
    pos = {s: i for i, s in enumerate(cols)}
    frames = {"ret250": ret250, "ret250_2": ret250_2, "ret250_3": ret250_3,
              "ret60": ret60, "vol20": vol20, "turn20": turn20, "logamt20": logamt20}
    out_rows = []
    for row, g in ev.groupby("row"):
        y = f60.iloc[row].values.astype(float)
        X = np.column_stack([frames[k].iloc[row].values.astype(float) for k in XS])
        okx = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if okx.sum() < 300:
            continue
        yy, XX = y[okx], X[okx]
        A1 = np.column_stack([np.ones(len(yy)), XX])
        b = np.linalg.lstsq(A1, yy, rcond=None)[0]
        for _, e in g.iterrows():
            j = pos.get(e["symbol"])
            if j is None:
                continue
            xv = X[j]
            if not np.isfinite(xv).all() or not np.isfinite(y[j]):
                continue
            pred = b[0] + float(xv @ b[1:])
            out_rows.append({"ym": e["ym"], "ratio_c": e["ratio_c"], "resid": float(y[j] - pred),
                             "f60": float(y[j]), "ret250": float(X[j][0])})

    x = pd.DataFrame(out_rows)
    out: dict = {"events": int(len(x)), "months": int(x["ym"].nunique())}

    def agg(sub: pd.DataFrame) -> dict:
        g = sub.groupby("ym")["resid"].mean()
        return {"bp": round(float(sub["resid"].mean() * 1e4), 1), "t": round(nw_t(g.to_numpy()), 2), "n": int(len(sub))}

    near = x[x["ratio_c"] > 0.8]
    far = x[x["ratio_c"] < 0.7]
    out["near"] = agg(near)
    out["far"] = agg(far)
    out["diff"] = round(out["near"]["bp"] - out["far"]["bp"], 1)
    # 分年 near 残差
    x["year"] = pd.to_datetime(x["ym"]).dt.year
    out["by_year"] = [
        {"year": int(y), "bp": round(float(g["resid"].mean() * 1e4), 1), "n": int(len(g))}
        for y, g in x[x["ratio_c"] > 0.8].groupby("year") if len(g) >= 20
    ]
    # 分跌幅段：near 的增量是不是只在某一档
    near = near.copy()
    near["q"] = pd.qcut(near["ret250"].rank(method="first"), 4, labels=False)
    out["near_by_drawdown"] = [
        {"q": int(q), "bp": round(float(g["resid"].mean() * 1e4), 1), "n": int(len(g)),
         "ret250_mid": round(float(g["ret250"].median() * 100), 1)}
        for q, g in near.groupby("q")
    ]

    (OUT / "h13e_fullresid.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
