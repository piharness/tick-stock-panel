"""2018 前：贴线的符号是不是反的？

卷五/卷六的解释是「A 股的解除以救助为主，所以符号为正」。
这句话有一个直接的历史预测（P4，写在 chip-ap.md §8.3）：

  2018 前为负或零，2019 后为正。

2018-10 的纾困基金是制度分界；此前质押处置以强平/诉讼为主，此后以展期/纾困/重组为主。
如果 2013–2018 的 near−far 不是负的，那「符号由解除方式给」这条解释就少一条腿。

口径（与 _diag_chip_audit_fix.py 完全一致，只换事件表）：
  drift_c = adj_close(公告日) / adj_close(质押起始日) − 1
  zhesuan = OPENLINE / adj_close(起始日)
  ratio_c = zhesuan / (1 + drift_c) = 平仓线 / 公告日复权价
  f60 = close[t+61] / open[t+1] − 1
  near = ratio > 0.8；far = ratio < 0.7（与 _diag_chip_audit_fix.py 同口径）；按月聚簇 NW(6)。

输出：data/.scratch/leverage/pre2019_sign.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
KLINE = ROOT / "data" / "kline_daily_enriched"


def nw_t(x: np.ndarray, lag: int = 6) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan")
    e = x - x.mean()
    s = float((e * e).mean())
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * float((e[l:] * e[:-l]).mean())
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else float("nan")


def main() -> None:
    raw = pd.read_parquet(OUT / "pledge_pre2019.parquet")
    ev = raw.copy()
    ev["notice"] = pd.to_datetime(ev["NOTICE_DATE"], errors="coerce")
    ev["start"] = pd.to_datetime(ev["PF_START_DATE"], errors="coerce")
    ev = ev[(ev["notice"] >= "2013-01-01") & (ev["notice"] <= "2018-12-31")]
    ev = ev[ev["start"].notna() & (ev["start"] < ev["notice"])]
    ev["code"] = ev["code"].astype(str).str.zfill(6)
    ev["line"] = pd.to_numeric(ev["OPENLINE"], errors="coerce")
    ev["tsr"] = pd.to_numeric(ev["PF_TSR"], errors="coerce")
    ev = ev.dropna(subset=["line", "tsr"])
    ev = ev[(ev["line"] > 0) & (ev["tsr"] > 0)]

    inst = pd.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    ev = ev.merge(inst.drop_duplicates("code"), on="code", how="left").dropna(subset=["symbol"])
    syms = sorted(ev["symbol"].unique().tolist())
    print(f"events {len(ev):,} / symbols {len(syms):,}", flush=True)

    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "open", "close"],
        filter=(ds.field("date") >= pd.Timestamp("2011-01-01").date()) & ds.field("symbol").isin(syms),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    px["symbol"] = px["symbol"].astype("string")
    C = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    O = px.pivot_table(index="date", columns="symbol", values="open").sort_index()
    di = C.index
    j = C.columns.get_indexer(ev["symbol"])
    pn = di.searchsorted(ev["notice"].values)
    ps = di.searchsorted(ev["start"].values)
    ok = (j >= 0) & (pn + 61 < len(di)) & (pn > 0) & (ps >= 0) & (ps < pn)
    ev, j, pn, ps = ev[ok].reset_index(drop=True), j[ok], pn[ok], ps[ok]

    Cs = C.values[ps, j]
    Cn = C.values[pn, j]
    Co = O.values[pn + 1, j]
    Cf = C.values[pn + 61, j]
    ev["drift_c"] = Cn / Cs - 1.0
    ev["ratio_c"] = (ev["line"].values / Cs) / (1.0 + ev["drift_c"].values)
    ev["f60"] = Cf / Co - 1.0
    ev = ev[ev["drift_c"].between(-0.9, 5.0) & ev["f60"].notna()].copy()
    ev["ym"] = ev["notice"].dt.to_period("M").astype(str)
    print(f"valid {len(ev):,} / {ev['ym'].nunique()} months / {ev['notice'].min().date()}–{ev['notice'].max().date()}", flush=True)

    def agg(sub: pd.DataFrame) -> dict:
        if sub.empty:
            return {"n": 0}
        g = sub.groupby("ym")["f60"].mean()
        return {
            "n": int(len(sub)),
            "bp": round(float(sub["f60"].mean() * 1e4), 1),
            "median_bp": round(float(sub["f60"].median() * 1e4), 1),
            "t": round(nw_t(g.to_numpy()), 2),
            "months": int(len(g)),
        }

    near = ev[ev["ratio_c"] > 0.8]
    far = ev[ev["ratio_c"] < 0.7]
    out: dict = {
        "events": int(len(ev)),
        "symbols": int(ev["symbol"].nunique()),
        "window": f"{ev['notice'].min().date()} ~ {ev['notice'].max().date()}",
        "near": agg(near),
        "far": agg(far),
        "spread": {
            "bp": round(float(near["f60"].mean() - far["f60"].mean()) * 1e4, 1),
            "t": round(nw_t((near.groupby("ym")["f60"].mean() - far.groupby("ym")["f60"].mean()).dropna().to_numpy()), 2),
        },
        "by_year": [],
        "bucket": [],
    }
    for y, g in ev.groupby(ev["notice"].dt.year):
        n = g[g["ratio_c"] > 0.8]["f60"]
        f = g[g["ratio_c"] < 0.7]["f60"]
        if len(n) >= 20 and len(f) >= 20:
            out["by_year"].append({
                "year": int(y), "near_bp": round(float(n.mean() * 1e4), 0),
                "far_bp": round(float(f.mean() * 1e4), 0),
                "spread_bp": round(float(n.mean() - f.mean()) * 1e4, 0), "n_near": int(len(n)),
            })
    edges = [0, 0.4, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 2.0]
    ev["b"] = pd.cut(ev["ratio_c"], edges, right=False)
    for k, g in ev.groupby("b", observed=True):
        if len(g) >= 50:
            out["bucket"].append({"bucket": str(k), "n": int(len(g)), "bp": round(float(g["f60"].mean() * 1e4), 0)})

    (OUT / "pre2019_sign.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
