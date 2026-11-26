"""H5 网络版：中心度 → 对「共同持有人组合」的下行贝塔（机制检验）。

市场版（h5_market.json）已显示方向反了：高中心度 = 大盘蓝筹，崩盘里更稳。
网络版更贴机制：把市场换成**该股共同持有人组成的组合**，看中心度是否预测
「对邻居组合的下行贝塔 − 平均贝塔」。若网络版也反向或为零，H5 彻底杀死。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h5"
SRC = ROOT / "data" / "kline_daily_enriched"
TOP_FLOAT = ROOT / "data" / "shareholder" / "top_float" / "part.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
HOLDER = ROOT / "data" / ".scratch" / "chip_age" / "holder_panel.parquet"

WINDOW = 250
NOMINEE_PAT = (
    "香港中央结算|HKSCC|中央结算|中国证券金融|中央汇金|证金公司|"
    "高盛|UBS|MORGAN|J.P.|JPMORGAN|BARCLAYS|MERRILL|CITIGROUP|"
    "STATE STREET|BNP|DEUTSCHE|CREDIT SUISSE|UBS AG|"
    "中信证券资产管理(香港)|客户资金"
)


def nw_t(x, lag=6):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return np.nan
    e = x - x.mean()
    s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def zscore(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def main():
    print("[1/4] load daily ...", flush=True)
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "close"], filter=ds.field("date") >= pd.Timestamp("2018-01-01").date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    dates = pd.DatetimeIndex(cl.index)
    syms = np.asarray(cl.columns)
    C = cl.to_numpy(dtype=np.float64)
    R = np.full_like(C, np.nan)
    R[1:] = C[1:] / C[:-1] - 1.0
    n, k = C.shape
    print(f"      {n} x {k}", flush=True)

    print("[2/4] network edges ...", flush=True)
    tf = pd.read_parquet(TOP_FLOAT)
    tf["period_end"] = pd.to_datetime(tf["period_end"])
    tf["code"] = tf["code"].astype(str).str.zfill(6)
    tf["ratio"] = pd.to_numeric(tf["ratio"], errors="coerce").fillna(0.0).clip(0, 100)
    tf["is_nominee"] = tf["holder_name"].astype(str).str.contains(NOMINEE_PAT, case=False, na=False)
    inst = pd.read_parquet(INST)[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    tf = tf.merge(inst, on="code", how="left")
    tf = tf[~tf["is_nominee"]].dropna(subset=["symbol", "ratio"])
    tf = tf[tf["ratio"] > 0]
    sym2idx = {s: i for i, s in enumerate(syms)}
    tf = tf[tf["symbol"].isin(sym2idx)]
    tf["idx"] = tf["symbol"].map(sym2idx)

    hp = pd.read_parquet(HOLDER)
    hp["period_end"] = pd.to_datetime(hp["period_end"]).astype("datetime64[ns]")
    hp["symbol"] = hp["symbol"].astype(str)

    quarters = sorted(tf["period_end"].unique())
    print(f"      quarters={[q.date() for q in quarters]}", flush=True)

    rows = []
    for Q in quarters:
        pos = np.searchsorted(dates.values, np.datetime64(Q, "ns"))
        if pos < WINDOW:
            continue
        w = slice(pos - WINDOW, pos)
        Rw = R[w]  # (250, N)
        # 构建 holder -> members
        g = tf[tf["period_end"] == Q]
        members = {}
        for h, gg in g.groupby("holder_name"):
            if len(gg) < 2:
                continue
            idxs = gg["idx"].to_numpy()
            ws = gg["ratio"].to_numpy() / 100.0
            members[h] = (idxs, ws)
        # 每个股票累计 co-held 组合收益
        r_net = np.zeros((k, WINDOW))
        wt = np.zeros(k)
        for h, (idxs, ws) in members.items():
            W_h = ws.sum()
            if W_h <= 1e-9:
                continue
            # holder 组合收益（长度 250）
            r_h = np.zeros(WINDOW)
            for a, ww in zip(idxs, ws):
                r_h += ww * np.nan_to_num(Rw[:, a], nan=0.0)
            r_h /= W_h
            for a, ww in zip(idxs, ws):
                excl = (r_h * W_h - ww * np.nan_to_num(Rw[:, a], nan=0.0)) / max(W_h - ww, 1e-9)
                r_net[a] += ww * excl
                wt[a] += ww
        wt[wt < 1e-9] = np.nan
        r_net = r_net / wt[:, None]
        # 下行/平均 beta 对 co-held 组合
        beta = np.full(k, np.nan)
        bdn = np.full(k, np.nan)
        for a in range(k):
            rj = Rw[:, a]
            rn = r_net[a]
            good = np.isfinite(rj) & np.isfinite(rn)
            if good.sum() < 100:
                continue
            rj = rj[good]; rn = rn[good]
            rm = rn - rn.mean(); ri = rj - rj.mean()
            var = (rm * rm).sum()
            beta[a] = (ri * rm).sum() / var if var > 0 else np.nan
            d = rn < 0
            if d.sum() >= 30:
                rmd = rn[d] - rn[d].mean(); rid = rj[d] - rj[d].mean()
                vard = (rmd * rmd).sum()
                bdn[a] = (rid * rmd).sum() / vard if vard > 0 else np.nan
        size = np.log(np.nanmean(C[w], axis=0) + 1.0)
        vol = np.nanstd(Rw, axis=0)
        hpq = hp[hp["period_end"] == Q][["symbol", "shared_score"]].copy()
        dfq = pd.DataFrame({"symbol": syms, "beta_net": beta, "beta_net_down": bdn,
                            "size": size, "vol": vol})
        dfq["symbol"] = dfq["symbol"].astype(str)
        dfq = dfq.merge(hpq, on="symbol", how="inner")
        dfq["tail_excess_net"] = dfq["beta_net_down"] - dfq["beta_net"]
        dfq["Q"] = Q
        rows.append(dfq)
    allx = pd.concat(rows, ignore_index=True)
    allx = allx.dropna(subset=["tail_excess_net", "shared_score", "size", "vol", "beta_net"])
    allx["Qnum"] = allx["Q"].astype("int64")
    allx.to_parquet(OUT / "panel_net.parquet", index=False)
    print(f"      panel_net {len(allx):,} rows", flush=True)

    def run_reg(d, ycol):
        d = d.copy()
        dummies = pd.get_dummies(d["Qnum"].astype("category"), prefix="q", drop_first=True).astype(float)
        X = np.column_stack([zscore(d["shared_score"]), zscore(d["size"]), zscore(d["vol"]), dummies.to_numpy()])
        y = zscore(d[ycol])
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        return b[0]

    b_tail = run_reg(allx, "tail_excess_net")
    b_avg = run_reg(allx, "beta_net")
    qc = []
    for Q, g in allx.groupby("Qnum"):
        if len(g) < 300:
            continue
        X = np.column_stack([zscore(g["shared_score"]), zscore(g["size"]), zscore(g["vol"]), np.ones(len(g))])
        y = zscore(g["tail_excess_net"])
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        qc.append(b[0])
    qc = np.asarray(qc)

    summary = {
        "sample": {"rows": int(len(allx)), "quarters": int(allx["Qnum"].nunique())},
        "b_centrality_on_tail_excess_net": round(float(b_tail), 4),
        "t_centrality": round(float(nw_t(qc)), 2),
        "b_centrality_on_avg_beta_net": round(float(b_avg), 4),
        "quarter_coefs": [round(float(x), 3) for x in qc],
        "quarter_same_sign": int((qc > 0).sum()),
        "n_quarters": int(len(qc)),
    }
    (OUT / "h5_net.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
