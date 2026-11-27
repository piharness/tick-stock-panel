"""P1：锚定安慰剂（修正版）——合约锚到底有没有超出「一段近期跌幅」的信息。

背景
----
旧 P1 写成「同一长度窗口随机重锚 1,000 次」。这是不可识别的：长度固定、端点固定在公告日，
锚就唯一确定了——随机重锚等于没重锚。审计后（drift_c = 公告日/质押日−1）重做，拆成三条可识别的检验：

  A 位移曲线：窗口长度 L 不变，整体往回挪 k 天（k=0/5/10/20/40/80/120）。
     若 k=0 明显高于 k=5，说明「锚在合约上」有锐利的信息；若平滑衰减，只是「近期跌幅」的陈旧效应。
  B 高点锚：同一端点，把锚换成过去 250 日最高价（一个通用的参照点）。
     若高点锚一样有效，合约锚只是「距高点回撤」的代理。
  C 残差：把 drift_c 对 ret20/60/120/250 + 高点锚做月内正交，残差还剩多少预测力。
     残差活 = 合约锚有独立信息；残差死 = 合约只是测量点。

输出：data/.scratch/leverage/p1_anchor.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
EVENTS = OUT / "pledge_drift_events.parquet"
KLINE = ROOT / "data" / "kline_daily_enriched"
SHIFTS = [0, 5, 10, 20, 40, 80, 120]


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


def zmonth(d: pd.DataFrame, col: str) -> pd.Series:
    return d.groupby("ym")[col].transform(lambda x: (x - x.mean()) / x.std())


def reg(d: pd.DataFrame, xs: list[str], y: str = "f60") -> list[dict]:
    co = []
    need = ["z_" + c for c in xs] + [y]
    for _, g in d.groupby("ym"):
        g = g.dropna(subset=need)
        if len(g) < 20:
            continue
        X = np.column_stack([np.ones(len(g))] + [g["z_" + c].values for c in xs])
        b = np.linalg.lstsq(X, g[y].values, rcond=None)[0]
        co.append(b[1:])
    co = np.array(co)
    return [{"var": c, "bp": round(float(co[:, i].mean() * 1e4), 1), "t": round(nw_t(co[:, i]), 2)} for i, c in enumerate(xs)]


def long_short(d: pd.DataFrame, col: str, y: str = "f60") -> dict:
    x = d.dropna(subset=[col, y]).copy()
    x["q"] = x.groupby("ym")[col].transform(lambda s: pd.qcut(s.rank(method="first"), 3, labels=[0, 1, 2]).astype(float))
    g = x.groupby(["ym", "q"])[y].mean().unstack()
    diff = (g[2] - g[0]).dropna()
    return {"bp": round(float(diff.mean() * 1e4), 1), "t": round(nw_t(diff.to_numpy()), 2)}


def main() -> None:
    ev = pd.read_parquet(EVENTS)
    ev = ev[ev["f60"].notna()].copy()
    ev["notice"] = pd.to_datetime(ev["NOTICE_DATE"])
    ev["start"] = pd.to_datetime(ev["PF_START_DATE"])
    syms = ev["symbol"].unique().tolist()
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "close"],
        filter=(ds.field("date") >= pd.Timestamp("2015-01-01").date()) & ds.field("symbol").isin(syms),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    C = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    di = C.index
    n = di.searchsorted(ev["notice"].values)
    s = di.searchsorted(ev["start"].values)
    L = n - s
    keep = (s >= 0) & (n < len(di)) & (L >= 60) & (L <= 500) & (s - 130 >= 0)
    ev = ev[keep].reset_index(drop=True)
    n, s, L = n[keep], s[keep], L[keep]
    j = C.columns.get_indexer(ev["symbol"])
    if (j < 0).any():
        m = j >= 0
        ev, n, s, L, j = ev[m].reset_index(drop=True), n[m], s[m], L[m], j[m]
    M = C.values
    cN = M[n, j]
    for k in [20, 60, 120, 250]:
        idx = n - k
        r = np.full(len(ev), np.nan)
        ok = idx >= 0
        r[ok] = M[idx[ok], j[ok]] / M[n[ok], j[ok]] - 1.0
        ev[f"ret{k}"] = r

    out: dict = {"events": int(len(ev)), "months": int(ev["ym"].nunique()), "window": f"{ev['notice'].min().date()} ~ {ev['notice'].max().date()}"}

    # A 位移曲线
    curve = []
    for k in SHIFTS:
        idx = n - k
        r = M[idx, j] / M[idx - L, j] - 1.0
        ev[f"r{k}"] = r
        ev["z_r"] = zmonth(ev, f"r{k}")
        fit = reg(ev, ["r"])
        ls = long_short(ev, f"r{k}")
        curve.append({"k": k, "slope": fit[0]["bp"], "t": fit[0]["t"], "ls": ls["bp"], "lsT": ls["t"]})
    out["shiftCurve"] = curve

    # B 高点锚
    hi = np.array([M[max(0, i - 250):i + 1, jj].max() for i, jj in zip(n, j)])
    ev["rhigh"] = cN / hi - 1.0
    ev["z_rhigh"] = zmonth(ev, "rhigh")
    out["highAnchor"] = {"slope": reg(ev, ["rhigh"])[0], "ls": long_short(ev, "rhigh")}

    # 合约锚 vs 高点锚：同一样本、月内标准化
    ev["drift_c"] = cN / M[s, j] - 1.0
    ev["z_drift_c"] = zmonth(ev, "drift_c")
    for c in ["drift_c", "rhigh", "ret20", "ret60", "ret120", "ret250"]:
        if c in ev:
            ev["z_" + c] = zmonth(ev, c)
    out["race"] = reg(ev, ["drift_c", "rhigh"])
    out["raceMomentum"] = reg(ev, ["drift_c", "rhigh", "ret250", "ret60"])

    # C 残差：drift_c 对通用锚 + 动量正交
    rcols = ["z_rhigh", "z_ret20", "z_ret60", "z_ret120", "z_ret250"]
    dd = ev.dropna(subset=["z_drift_c"] + rcols).copy()
    X = np.column_stack([np.ones(len(dd))] + [dd[c].values for c in rcols])
    y = dd["z_drift_c"].values
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    resid = y - X @ beta
    ev["resid"] = np.nan
    ev.loc[dd.index, "resid"] = resid
    ev["z_resid"] = zmonth(ev, "resid")
    ss_tot = float(((y - y.mean()) ** 2).sum())
    ss_res = float((resid**2).sum())
    out["residual"] = {
        "r2_explained": round(1 - ss_res / ss_tot, 3),
        "slope": reg(ev, ["resid"])[0],
        "ls": long_short(ev, "resid"),
    }

    # 参考：合约锚单变量
    out["anchorOnly"] = {"slope": reg(ev, ["drift_c"])[0], "ls": long_short(ev, "drift_c")}

    (OUT / "p1_anchor.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
