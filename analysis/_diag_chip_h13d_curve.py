"""H13d：把「跌幅」控制到非线性——同日全市场三次多项式残差

H13c 用五分位做对照，near 仍 +239bp（t 3.81）；但 near 是五分位里最深的一端，
五分位太粗，残差可能只是「跌幅曲线更陡」。

本脚本在公告日当天，用全市场横截面拟合 f60 ~ ret250（三次）与 f60 ~ ret60（三次），
取 near / far 事件的残差。三次项能吃掉「越跌越反弹」的凸性；剩下的才是合约增量。
同时报告 near 的 ret250 分位（0–100），确认它不是「普通深跌」。

输出：data/.scratch/leverage/h13d_curve.json
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


def main() -> None:
    ev = load()
    syms = ev["symbol"].unique().tolist()
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "open", "close"],
        filter=(ds.field("date") >= pd.Timestamp("2016-01-01").date()) & ds.field("symbol").isin(syms),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    O = px.pivot_table(index="date", columns="symbol", values="open").sort_index()
    C = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    ret60 = C / C.shift(60) - 1
    ret250 = C / C.shift(250) - 1
    f60 = C.shift(-60) / O.shift(-1) - 1

    di = C.index
    idx = di.searchsorted(pd.to_datetime(ev["notice"]).values)
    ok = (idx < len(di)) & (idx >= 0)
    ev = ev[ok].reset_index(drop=True)
    idx = idx[ok]
    ev["row"] = idx

    cols = list(f60.columns)
    pos = {s: i for i, s in enumerate(cols)}
    out_rows: list[dict] = []

    for row, g in ev.groupby("row"):
        y = f60.iloc[row].values.astype(float)
        for xname, X in [("ret250", ret250), ("ret60", ret60)]:
            x = X.iloc[row].values.astype(float)
            okx = np.isfinite(y) & np.isfinite(x)
            if okx.sum() < 200:
                continue
            yy, xx = y[okx], x[okx]
            A = np.column_stack([np.ones(len(xx)), xx, xx ** 2, xx ** 3])
            b = np.linalg.lstsq(A, yy, rcond=None)[0]
            for _, e in g.iterrows():
                j = pos.get(e["symbol"])
                if j is None:
                    continue
                xv = x[j]
                if not np.isfinite(xv) or not np.isfinite(y[j]):
                    continue
                pred = b[0] + b[1] * xv + b[2] * xv ** 2 + b[3] * xv ** 3
                out_rows.append({
                    "ym": e["ym"], "ratio_c": e["ratio_c"], "bench": xname,
                    "resid": float(y[j] - pred), "pct": float((xx < xv).mean() * 100),
                })

    x = pd.DataFrame(out_rows)
    out: dict = {"events": int(len(x)), "months": int(x["ym"].nunique())}

    def agg(sub: pd.DataFrame) -> dict:
        g = sub.groupby("ym")["resid"].mean()
        return {
            "bp": round(float(sub["resid"].mean() * 1e4), 1),
            "t": round(nw_t(g.to_numpy()), 2),
            "n": int(len(sub)),
            "pct": round(float(sub["pct"].median()), 1),
        }

    for b in ["ret250", "ret60"]:
        s = x[x["bench"] == b]
        near = s[s["ratio_c"] > 0.8]
        far = s[s["ratio_c"] < 0.7]
        out[b] = {"near": agg(near), "far": agg(far), "diff": round(agg(near)["bp"] - agg(far)["bp"], 1)}

    (OUT / "h13d_curve.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
