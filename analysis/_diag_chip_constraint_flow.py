"""约束簿 · 流量 vs 存量（第二轮，写于跑数之前）。

第一轮的教训
------------
把「贴线」做成状态变量（存量）后，C1 的受控系数是 −0.1bp / t −0.01；
原始五分位甚至是正的（+91bp）。而 H11 的事件研究（新公告、贴线）是 −131bp / t −4.02。
两者不矛盾，但说明**信息不在存量里**：
  新合约贴线 = 大股东在股价已跌很多之后，仍按当前价附近借到钱 → 新鲜的流动性约束 + 新鲜的信息；
  老合约贴线 = 同一个约束已经存在很久，可能已展期/补充，信息早被定价。
若如此，筹码的对象不是「约束的余额」，而是「约束的发行流量」——一个授信发起过程，不是一张资产负债表。

检验（写死）
------------
F1 四组：new_near>0 / 存量near>0.3但无新合约 / 有簿但远 / 无簿。预测 new_near 组最差。
F2 控制回归（簿内）：new_near 应负且显著；near_share 存量应归零。
F3 控制人：new_near × IS_CONTROL_SHAREHOLDER。信息/代理假说预测控制人更强；纯强平假说不要求。
F4 年龄：near 合约的簿加权年龄越长，效应越弱（信息衰减）。

杀死
----
F1 new_near 组不差于「有簿但远」；F2 new_near 系数为零；F3/F4 无结构。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
SRC = ROOT / "data" / "kline_daily_enriched"
NW_LAG = 6
START = "2020-01-01"


def load_daily(start="2019-01-01"):
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "open", "close", "amount", "turnover_rate"],
                   filter=ds.field("date") >= pd.Timestamp(start).date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.9)
    op = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="to", aggfunc="last").sort_index()
    return op, cl.reindex(op.index), am.reindex(op.index), to.reindex(op.index)


def nw_t(x, lag=NW_LAG):
    x = np.asarray(x, dtype=float); x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return np.nan
    e = x - x.mean(); s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def z(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def main():
    print("[1/5] load ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32); C = cl_df.to_numpy(np.float32)
    A = am_df.to_numpy(np.float32); TO = to_df.to_numpy(np.float32)
    n, k = C.shape
    sym2idx = {s: i for i, s in enumerate(syms)}
    ym = pd.Series(dates).dt.to_period("M")
    last = pd.Series(dates).groupby(ym).max()
    mids = np.array([dates.get_loc(d) for d in last.values])
    mids = mids[(dates[mids] >= pd.Timestamp(START)) & (mids < n - 62)]

    p = pd.read_parquet(OUT / "pledge_raw_v2.parquet").drop_duplicates()
    p["code"] = p["code"].astype(str).str.zfill(6)
    p["s"] = pd.to_datetime(p["PF_START_DATE"], errors="coerce")
    p["u"] = pd.to_datetime(p["UNFREEZE_DATE"], errors="coerce")
    p["tsr"] = pd.to_numeric(p["PF_TSR"], errors="coerce")
    p["line"] = pd.to_numeric(p["OPENLINE"], errors="coerce")
    p["ctl"] = p["IS_CONTROL_SHAREHOLDER"].astype(str).isin(["1", "True", "true"])
    p["open"] = p["UNFREEZE_STATE"].astype(str).eq("未解押")
    p = p.dropna(subset=["s", "tsr", "line"])
    p = p[(p["tsr"] > 0) & (p["line"] > 0)]
    p = p[p["u"].notna() | p["open"]]
    inst = pd.read_parquet(INST)[["code", "symbol", "name", "listing_date"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6); inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    p = p.groupby(["symbol", "idx", "HOLDER_NAME", "s", "u", "line", "ctl"], dropna=False, as_index=False)["tsr"].sum()
    print(f"      合约 {len(p):,} / {p['symbol'].nunique():,} 只", flush=True)

    print("[2/5] 月末（含流量）...", flush=True)
    rows = []
    for m in mids:
        me = dates[m]
        live = (p["s"] <= me) & (p["u"].isna() | (p["u"] > me))
        q = p[live]
        if q.empty:
            continue
        px = C[m, q["idx"].to_numpy()]
        ok = np.isfinite(px) & (px > 0)
        if not ok.any():
            continue
        q = q.assign(px=px)[ok]
        q["ratio"] = q["line"] / q["px"]
        q["near"] = (q["ratio"] >= 0.8) & (q["ratio"] <= 1.2)
        q["fresh"] = (me - q["s"]).dt.days <= 90
        q["age"] = (me - q["s"]).dt.days.clip(0, 3000)
        q["tsr_n"] = q["tsr"] * q["near"]
        q["tsr_fn"] = q["tsr"] * q["near"] * q["fresh"]
        q["tsr_ct"] = q["tsr"] * q["near"] * q["ctl"]
        q["tsr_wn"] = q["tsr"] * np.clip(q["ratio"], 0.2, 2.0)
        g = q.groupby("symbol").apply(lambda x: pd.Series({
            "book": x["tsr"].sum(),
            "near": x["tsr_n"].sum(),
            "new_near": x["tsr_fn"].sum(),
            "near_ctrl": x["tsr_ct"].sum(),
            "wdist": x["tsr_wn"].sum() / max(x["tsr"].sum(), 1e-9),
            "age_near": float((x.loc[x["near"], "age"] * x.loc[x["near"], "tsr"]).sum() / max(x.loc[x["near"], "tsr"].sum(), 1e-9)),
            "n_near": int(x["near"].sum()),
            "n_new": int((x["near"] & x["fresh"]).sum()),
        }), include_groups=False)
        g["date"] = me
        rows.append(g.reset_index())
    book = pd.concat(rows, ignore_index=True)
    book["near_share"] = book["near"] / book["book"].clip(lower=1e-9)
    book["new_share"] = book["new_near"] / book["book"].clip(lower=1e-9)
    book["ym"] = book["date"].dt.to_period("M").astype(str)
    print(f"      活簿 {len(book):,} 行 · fresh>0 {int((book['new_near']>0).sum()):,}", flush=True)

    print("[3/5] 结果 ...", flush=True)
    ret = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    rv20 = pd.DataFrame(ret).rolling(20).std().to_numpy()
    first = np.argmax(np.isfinite(C), axis=0)
    am20 = pd.DataFrame(A).rolling(20).mean().to_numpy()
    to20 = pd.DataFrame(TO).rolling(20).mean().to_numpy()
    am60 = pd.DataFrame(A).rolling(60).mean().to_numpy()
    nmap = inst.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    recs = []
    for m in mids:
        r = pd.DataFrame({"symbol": syms, "date": dates[m], "close": C[m]})
        r["ret20"] = C[m] / np.where(C[m - 20] > 0, C[m - 20], np.nan) - 1
        r["ret60"] = C[m] / np.where(C[m - 60] > 0, C[m - 60], np.nan) - 1
        r["vol20"] = rv20[m]; r["turn20"] = to20[m]
        r["size"] = np.log(np.maximum(am20[m], 1.0)); r["amt60"] = am60[m]
        r["f20"] = C[m + 21] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        r["f60"] = C[m + 61] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        r["fv20"] = np.sqrt(np.nanmean(np.square(ret[m + 1:m + 21]), axis=0))
        r["age"] = m - first
        recs.append(r)
    panel = pd.concat(recs, ignore_index=True)
    panel = panel.merge(book, on=["date", "symbol"], how="left")
    panel["has_book"] = panel["book"].fillna(0) > 0
    for c in ["new_near", "near", "near_share", "new_share", "near_ctrl", "wdist", "age_near", "n_near", "n_new"]:
        panel[c] = panel[c].fillna(0.0)
    age = pd.read_parquet(AGE); age["date"] = pd.to_datetime(age["date"]); age["D"] = age["age_mean"] + age["old_share"] * 500.0
    panel = panel.merge(age[["date", "symbol", "D"]], on=["date", "symbol"], how="left")
    panel["nm"] = panel["symbol"].map(nmap).fillna("")
    panel["ok"] = (~panel["nm"].str.contains("ST")) & (panel["age"] >= 250) & (panel["amt60"] >= 1.5e7) & (panel["close"] > 0)
    panel.to_parquet(OUT / "constraint_flow_panel.parquet", index=False)
    print(f"      面板 {len(panel):,} · 有效 {int(panel['ok'].sum()):,}", flush=True)

    print("[4/5] F1 四组 ...", flush=True)
    w = panel[panel["ok"] & panel["f60"].notna()].copy()
    w["grp"] = np.where(w["new_near"] > 0, "new_near",
               np.where(w["has_book"] & (w["near_share"] >= 0.3), "stale_near",
               np.where(w["has_book"], "book_far", "no_book")))
    gm = w.groupby(["ym", "grp"]).agg(f60=("f60", "mean"), ret60=("ret60", "mean"), n=("f60", "size")).reset_index()
    piv = gm.pivot(index="ym", columns="grp", values="f60")
    cnt = gm.pivot(index="ym", columns="grp", values="n")
    cells = {g: {"f60_bp": round(float(piv[g].mean() * 1e4), 1), "ret60_pct": round(float(w[w["grp"] == g]["ret60"].mean() * 100), 1),
                 "months": int(piv[g].notna().sum()), "avg_n": int(cnt[g].mean())} for g in piv.columns}
    diffs = {}
    for a in ["new_near", "stale_near", "book_far"]:
        if a in piv and "no_book" in piv:
            d = piv[a] - piv["no_book"]
            diffs[f"{a}_minus_no_book"] = {"bp": round(float(d.mean() * 1e4), 1), "t": round(float(nw_t(d)), 2)}
    if "new_near" in piv and "book_far" in piv:
        d = piv["new_near"] - piv["book_far"]
        diffs["new_minus_far"] = {"bp": round(float(d.mean() * 1e4), 1), "t": round(float(nw_t(d)), 2)}
    if "new_near" in piv and "stale_near" in piv:
        d = piv["new_near"] - piv["stale_near"]
        diffs["new_minus_stale"] = {"bp": round(float(d.mean() * 1e4), 1), "t": round(float(nw_t(d)), 2)}
    print("      " + json.dumps({"cells": cells, "diffs": diffs}, ensure_ascii=False), flush=True)

    print("[5/5] F2/F3/F4 ...", flush=True)
    v = panel[panel["ok"] & panel["has_book"] & panel["f60"].notna()].copy()
    reg = []
    for ym_, g in v.groupby("ym"):
        if len(g) < 80:
            continue
        X = np.column_stack([z(g["new_share"]), z(g["near_share"]), z(g["near_ctrl"] / g["book"].clip(lower=1e-9)),
                             z(g["vol20"]), z(g["turn20"]), z(g["size"]), z(g["D"]), z(g["ret20"]), z(g["ret60"]),
                             np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 80:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym_, "b_new": b[0], "b_stale": b[1], "b_ctrl": b[2], "n": int(ok.sum())})
    r = pd.DataFrame(reg)
    f2 = {"b_new_bp": round(float(r["b_new"].mean()), 1), "t_new": round(float(nw_t(r["b_new"])), 2),
          "b_stale_bp": round(float(r["b_stale"].mean()), 1), "t_stale": round(float(nw_t(r["b_stale"])), 2),
          "b_ctrl_bp": round(float(r["b_ctrl"].mean()), 1), "t_ctrl": round(float(nw_t(r["b_ctrl"])), 2),
          "months": int(len(r))}
    print("      F2 " + json.dumps(f2, ensure_ascii=False), flush=True)

    # F4：近合约年龄 → 效应（只在 near_share>0 内）
    v4 = v[v["near_share"] > 0.05].copy()
    reg4 = []
    for ym_, g in v4.groupby("ym"):
        if len(g) < 50:
            continue
        X = np.column_stack([z(np.log1p(g["age_near"])), z(g["near_share"]), z(g["vol20"]), z(g["turn20"]),
                             z(g["size"]), z(g["D"]), np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 50:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg4.append({"ym": ym_, "b_age": b[0], "n": int(ok.sum())})
    r4 = pd.DataFrame(reg4)
    f4 = {"b_log_age_bp": round(float(r4["b_age"].mean()), 1), "t": round(float(nw_t(r4["b_age"])), 2), "months": int(len(r4))}
    print("      F4 " + json.dumps(f4, ensure_ascii=False), flush=True)

    out = {"F1": {"cells": cells, "diffs": diffs}, "F2": f2, "F4": f4,
           "F3_note": "b_ctrl 已在 F2 中（near_ctrl/book 的独立系数）"}
    (OUT / "constraint_flow.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
