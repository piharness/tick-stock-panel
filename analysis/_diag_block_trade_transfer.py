"""E4a 迁移：大宗交易的「发起」vs「存量」。

假设（写死）
------------
形式可迁移：在任何披露数据里，**发起**（有日期、有对手方、有条款的动作）携带符号；
**存量**（累计量/平均水平）不携带。
符号不迁移：符号由义务方向决定。
  深折价大宗 = 卖方付即时性价格（被迫/急售）→ 负；
  溢价大宗   = 买方付溢价（主动建仓/抢筹）→ 正或零；
  存量（60 日大宗占比、平均折价）→ 零。
负控：若深折价与溢价同号，或存量也显著，则「发起」只是「事件溢价」，不是约束。

数据：东财大宗交易 2019-01 ~ 2026-11，A 股 225,380 条。
事件：某日有 Block，且过去 60 个交易日无 Block（首次），且 min(premium_doc) ≤ −5%（深折价）。
      对照：首次且 max(premium_doc) ≥ +2%（溢价）。
入场：事件次日开盘；出场 t+21/t+61 收盘。
存量：月末 60 日大宗成交额 / 60 日总成交额；60 日成交额加权平均折价。
控制：换手/波动/规模/久期/ret20/ret60；月聚簇 NW t。
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
BT = ROOT / "data" / "block_trade"
NW_LAG = 6
START = "2019-01-01"


def load_daily(start=START):
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "open", "close", "amount", "turnover_rate"],
                   filter=ds.field("date") >= pd.Timestamp(start).date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"]); df["symbol"] = df["symbol"].astype("string")
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
    print("[1/5] 日线 ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index); syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32); C = cl_df.to_numpy(np.float32)
    A = am_df.to_numpy(np.float32); TO = to_df.to_numpy(np.float32)
    n, k = C.shape
    sym2idx = {s: i for i, s in enumerate(syms)}
    ym = pd.Series(dates).dt.to_period("M"); last = pd.Series(dates).groupby(ym).max()
    mids = np.array([dates.get_loc(d) for d in last.values])
    mids = mids[(mids >= 60) & (mids < n - 62)]
    print(f"      {n} x {k} · {len(mids)} 个月末", flush=True)

    print("[2/5] 大宗聚合 ...", flush=True)
    b = ds.dataset(BT, format="parquet", partitioning="hive").to_table(
        columns=["date", "symbol", "sec_type", "market", "premium_doc", "amt", "buyer", "seller"],
        filter=ds.field("date") >= pd.Timestamp(START).date()).to_pandas()
    b = b[b["sec_type"] == "EQA"].copy()
    b["symbol"] = b["symbol"].astype(str)
    b = b[b["symbol"].isin(sym2idx)]
    b["date"] = pd.to_datetime(b["date"])
    b["premium_doc"] = pd.to_numeric(b["premium_doc"], errors="coerce")
    b["amt"] = pd.to_numeric(b["amt"], errors="coerce")
    b = b.dropna(subset=["date", "symbol", "amt"]).drop_duplicates()

    def classify(s):
        s = str(s)
        if "机构专用" in s:
            return "institution"
        if ("证券" in s) or ("营业部" in s) or ("总部" in s) or ("分公司" in s):
            return "broker"
        if "公司" in s:
            return "company"
        return "other"

    b["stype"] = b["seller"].map(classify)
    b["btype"] = b["buyer"].map(classify)
    # 每个股票-日：深折价那一笔的卖方类型；溢价那一笔的买方类型
    deep_row = b.sort_values("premium_doc").drop_duplicates(["date", "symbol"], keep="first")
    prem_row = b.sort_values("premium_doc", ascending=False).drop_duplicates(["date", "symbol"], keep="first")
    g = b.groupby(["date", "symbol"]).agg(bt_amt=("amt", "sum"), pmin=("premium_doc", "min"),
                                          pmax=("premium_doc", "max")).reset_index()
    g = g.merge(deep_row[["date", "symbol", "stype"]].rename(columns={"stype": "stype_deep"}),
                on=["date", "symbol"], how="left")
    g = g.merge(prem_row[["date", "symbol", "btype"]].rename(columns={"btype": "btype_prem"}),
                on=["date", "symbol"], how="left")
    print(f"      股票-日 {len(g):,} · 深折价日 {int((g['pmin'] <= -0.05).sum()):,} · 溢价日 {int((g['pmax'] >= 0.02).sum()):,}", flush=True)
    print("      深折价卖方类型: " + str(g.loc[g["pmin"] <= -0.05, "stype_deep"].value_counts().to_dict()), flush=True)
    print("      溢价买方类型: " + str(g.loc[g["pmax"] >= 0.02, "btype_prem"].value_counts().to_dict()), flush=True)

    # 日频矩阵
    B = np.zeros((n, k), dtype=np.float32); PMIN = np.full((n, k), np.nan, dtype=np.float32)
    PMAX = np.full((n, k), np.nan, dtype=np.float32)
    ti = np.searchsorted(dates.values, g["date"].values, side="left")
    ci = g["symbol"].map(sym2idx).to_numpy()
    ok = (ti >= 0) & (ti < n)
    B[ti[ok], ci[ok]] = g["bt_amt"].to_numpy()[ok]
    PMIN[ti[ok], ci[ok]] = g["pmin"].to_numpy()[ok]
    PMAX[ti[ok], ci[ok]] = g["pmax"].to_numpy()[ok]

    print("[3/5] 首次 + 折价 ...", flush=True)
    has = B > 0
    # 过去 60 日（不含当日）是否有 Block
    prior = pd.DataFrame(has).rolling(60, min_periods=1).max().shift(1).fillna(0).to_numpy() > 0
    first = has & (~prior)
    deep = first & (PMIN <= -0.05)
    prem = first & (PMAX >= 0.02) & (~deep)
    print(f"      first {int(first.sum()):,} · deep {int(deep.sum()):,} · prem {int(prem.sum()):,}", flush=True)

    print("[4/5] 事件收益 ...", flush=True)
    inst = pd.read_parquet(INST)[["symbol", "name"]]
    nmap = inst.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    age = pd.read_parquet(AGE); age["date"] = pd.to_datetime(age["date"])
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age = age[["date", "symbol", "D"]]
    am20 = pd.DataFrame(A).rolling(20).mean().to_numpy()
    am60 = pd.DataFrame(A).rolling(60).mean().to_numpy()
    to20 = pd.DataFrame(TO).rolling(20).mean().to_numpy()
    ret = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    rv20 = pd.DataFrame(ret).rolling(20).std().to_numpy()
    first_valid = np.argmax(np.isfinite(C), axis=0)

    recs = []
    for t, i in zip(*np.nonzero(first)):
        rec = {"date": dates[t], "symbol": syms[i], "t": t, "i": i,
               "grp": "deep" if deep[t, i] else ("prem" if prem[t, i] else "other"),
               "pmin": PMIN[t, i], "pmax": PMAX[t, i], "bt_amt": B[t, i]}
        rec["f20"] = C[t + 21, i] / O[t + 1, i] - 1 if t + 21 < n and O[t + 1, i] > 0 else np.nan
        rec["f60"] = C[t + 61, i] / O[t + 1, i] - 1 if t + 61 < n and O[t + 1, i] > 0 else np.nan
        rec["ret20"] = C[t, i] / C[t - 20, i] - 1 if C[t - 20, i] > 0 else np.nan
        rec["ret60"] = C[t, i] / C[t - 60, i] - 1 if C[t - 60, i] > 0 else np.nan
        rec["vol20"] = rv20[t, i]; rec["turn20"] = to20[t, i]
        rec["size"] = np.log(max(am20[t, i], 1.0)); rec["amt60"] = am60[t, i]
        rec["age"] = t - first_valid[i]
        rec["nm"] = nmap.get(syms[i], "")
        recs.append(rec)
    e = pd.DataFrame(recs)
    e = e.merge(age, on=["date", "symbol"], how="left")
    e = e.merge(g[["date", "symbol", "stype_deep", "btype_prem"]], on=["date", "symbol"], how="left")
    e["ok"] = (~e["nm"].fillna("").str.contains("ST")) & (e["age"] >= 250) & (e["amt60"] >= 1.5e7)
    e = e[e["ok"] & e["f60"].notna()].copy()
    e["ym"] = e["date"].dt.to_period("M").astype(str)

    def fine(r):
        if r["grp"] == "deep":
            return "deep_" + str(r["stype_deep"])
        if r["grp"] == "prem":
            return "prem_" + str(r["btype_prem"])
        return "other"

    e["grp2"] = e.apply(fine, axis=1)
    print(f"      事件样本 {len(e):,} · deep {int((e['grp']=='deep').sum()):,} · prem {int((e['grp']=='prem').sum()):,}", flush=True)
    print("      细分: " + str(e["grp2"].value_counts().to_dict()), flush=True)

    # 事件：月聚簇均值 + 控制回归
    mon = e.groupby(["ym", "grp"]).agg(f20=("f20", "mean"), f60=("f60", "mean")).reset_index()
    piv = mon.pivot(index="ym", columns="grp", values="f60")
    cells = {g: round(float(piv[g].mean() * 1e4), 1) for g in piv.columns}
    tt = {}
    for g in ["deep", "prem"]:
        if g in piv and "other" in piv:
            d = piv[g] - piv["other"]
            tt[f"{g}_minus_other"] = {"bp": round(float(d.mean() * 1e4), 1), "t": round(float(nw_t(d)), 2)}
    reg = []
    for ym_, g in e.groupby("ym"):
        if len(g) < 20:
            continue
        X = np.column_stack([(g["grp"] == "deep").to_numpy(float), (g["grp"] == "prem").to_numpy(float),
                             z(g["vol20"]), z(g["turn20"]), z(g["size"]), z(g["D"]), z(g["ret20"]), z(g["ret60"]),
                             np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 20:
            continue
        bb, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym_, "b_deep": bb[0], "b_prem": bb[1]})
    r = pd.DataFrame(reg)
    ev = {"cells_f60_bp": cells, "diffs": tt,
          "controlled": {"b_deep_bp": round(float(r["b_deep"].mean()), 1), "t_deep": round(float(nw_t(r["b_deep"])), 2),
                         "b_prem_bp": round(float(r["b_prem"].mean()), 1), "t_prem": round(float(nw_t(r["b_prem"])), 2),
                         "months": int(len(r))}}
    print("      " + json.dumps(ev, ensure_ascii=False), flush=True)

    # E4 细分：买卖方类型
    mon2 = e.groupby(["ym", "grp2"]).agg(f60=("f60", "mean"), n=("f60", "size")).reset_index()
    piv2 = mon2.pivot(index="ym", columns="grp2", values="f60")
    cnt2 = e.groupby("grp2").size().to_dict()
    cells2 = {}
    for gname in piv2.columns:
        s = piv2[gname].dropna()
        cells2[gname] = {"f60_bp": round(float(s.mean() * 1e4), 1), "months": int(len(s)),
                         "events": int(cnt2.get(gname, 0))}
    diffs2 = {}
    if "other" in piv2:
        for gname in piv2.columns:
            if gname == "other":
                continue
            d = piv2[gname] - piv2["other"]
            d = d.dropna()
            if len(d) < 12:
                diffs2[gname] = {"bp": round(float(d.mean() * 1e4), 1) if len(d) else None,
                                 "t": None, "months": int(len(d)), "note": "样本太薄"}
            else:
                diffs2[gname] = {"bp": round(float(d.mean() * 1e4), 1), "t": round(float(nw_t(d)), 2),
                                 "months": int(len(d))}
    print("      type_split " + json.dumps({"cells": cells2, "diffs": diffs2}, ensure_ascii=False), flush=True)

    print("[5/5] 存量 ...", flush=True)
    Bs = pd.DataFrame(B).rolling(60, min_periods=20).sum().to_numpy()
    As = pd.DataFrame(A).rolling(60, min_periods=20).sum().to_numpy()
    Bp = pd.DataFrame(B * np.nan_to_num(PMIN, nan=0.0)).rolling(60, min_periods=20).sum().to_numpy()
    share = np.where(As > 0, Bs / As, np.nan)
    prem60 = np.where(Bs > 0, Bp / Bs, np.nan)
    rows = []
    for m in mids:
        r = pd.DataFrame({"symbol": syms, "date": dates[m]})
        r["bt_share60"] = share[m]; r["prem60"] = prem60[m]
        r["f60"] = C[m + 61] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        r["ret20"] = C[m] / np.where(C[m - 20] > 0, C[m - 20], np.nan) - 1
        r["ret60"] = C[m] / np.where(C[m - 60] > 0, C[m - 60], np.nan) - 1
        r["vol20"] = rv20[m]; r["turn20"] = to20[m]; r["size"] = np.log(np.maximum(am20[m], 1.0))
        r["amt60"] = am60[m]; r["age"] = m - first_valid
        r["nm"] = [nmap.get(s, "") for s in syms]
        rows.append(r)
    sp = pd.concat(rows, ignore_index=True).merge(age, on=["date", "symbol"], how="left")
    sp["ok"] = (~sp["nm"].fillna("").str.contains("ST")) & (sp["age"] >= 250) & (sp["amt60"] >= 1.5e7)
    sp = sp[sp["ok"] & sp["f60"].notna() & sp["bt_share60"].notna()].copy()
    sp["ym"] = sp["date"].dt.to_period("M").astype(str)
    sp["q"] = sp.groupby("ym")["bt_share60"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    qm = sp.dropna(subset=["q"]).groupby(["ym", "q"])["f60"].mean().unstack()
    qbp = {f"Q{int(c)+1}": round(float(qm[c].mean() * 1e4), 1) for c in qm.columns}
    sp_d = qm[qm.columns[-1]] - qm[qm.columns[0]]
    reg2 = []
    for ym_, g in sp.groupby("ym"):
        if len(g) < 80:
            continue
        X = np.column_stack([z(g["bt_share60"]), z(g["prem60"].fillna(0.0)), z(g["vol20"]), z(g["turn20"]),
                             z(g["size"]), z(g["D"]), z(g["ret20"]), z(g["ret60"]), np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 80:
            continue
        bb, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg2.append({"ym": ym_, "b_share": bb[0], "b_prem": bb[1]})
    r2 = pd.DataFrame(reg2)
    st = {"n": int(len(sp)), "months": int(sp["ym"].nunique()), "q_f60_bp": qbp,
          "spread_bp": round(float(sp_d.mean() * 1e4), 1), "t": round(float(nw_t(sp_d)), 2),
          "controlled": {"b_share_bp": round(float(r2["b_share"].mean()), 1), "t_share": round(float(nw_t(r2["b_share"])), 2),
                         "b_prem_bp": round(float(r2["b_prem"].mean()), 1), "t_prem": round(float(nw_t(r2["b_prem"])), 2)}}
    print("      " + json.dumps(st, ensure_ascii=False), flush=True)

    out = {"meta": {"events": int(len(e)), "deep": int((e["grp"] == "deep").sum()), "prem": int((e["grp"] == "prem").sum()),
                    "months": int(e["ym"].nunique()), "start": str(e["date"].min().date()), "end": str(e["date"].max().date())},
           "event": ev, "state": st, "type_split": {"cells": cells2, "diffs": diffs2}}
    (OUT / "block_trade_transfer.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[done] block_trade_transfer.json", flush=True)


if __name__ == "__main__":
    main()
