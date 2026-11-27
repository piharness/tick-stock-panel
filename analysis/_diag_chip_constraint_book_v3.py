"""存量簿 v3：把「公告口径缺的合约」补回去，再问一次零还在不在。

v2 修的是「同一批合约内部」的口径（复权 / 入场 / 到期），但没修两件更大的事：

  C0 覆盖率：v2 只用 NOTICE_DATE ≥ 2019-01 的明细。2020 年还在簿上的合约，
     有很大一部分是 2015–2018 公告的——它们根本不在表里。
  C1 重复披露：同一份合约会在 2019/2020/2021 的年报或补充公告里被重复披露，
     `PF_TSR` 是**当时的剩余额**（部分解押后递减）。把多期快照同时求和 = 把同一份
     合约数了 3–4 次。比例型指标（near_share / wdist）对完全相同的副本免疫，
     但对「剩余额递减」的副本不免疫。

本脚本把这两件事拆开：

  bw   旧口径 = 仅 2019+ 公告，所有活行求和（= v2）
  bw3  覆盖 = 2005+ 公告，所有活行求和（只补覆盖，不改求和规则）
  sn3  覆盖 + 快照 = 2005+ 公告，每份合约只取「最近一次公告的未解押余额」

对每个变体：C1（near/wdist/breach → f60，受控）与覆盖统计。

判据（写死）
------------
- 若三行的受控 near/wdist 都 |t|<2 → 「存量零」不是覆盖率或重复披露造成的。
- 若只有 bw 为零、bw3/sn3 翻负 → 零是公告口径的 artifact，强版本复活。
- 若只有 sn3 翻负 → 零是重复披露的 artifact（权重被老合约重复放大）。
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
    t = d.to_table(columns=["symbol", "date", "open", "close", "raw_close", "amount", "turnover_rate"],
                   filter=ds.field("date") >= pd.Timestamp(start).date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype(str)
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.9)
    op = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    rc = df.pivot_table(index="date", columns="symbol", values="raw_close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="turnover_rate", aggfunc="last").sort_index()
    return op, cl.reindex(op.index), rc.reindex(op.index), am.reindex(op.index), to.reindex(op.index)


def adjust_factor(sym_start: pd.DataFrame) -> pd.Series:
    """合约起始日的复权因子 C/rawC；起始日早于行情起点时退回最早可用日。"""
    d = ds.dataset(SRC, format="parquet")
    keep = sorted(set(sym_start["symbol"].dropna().astype(str)))
    t = d.to_table(columns=["symbol", "date", "close", "raw_close"],
                   filter=(ds.field("date") >= pd.Timestamp("2009-01-01").date()) &
                          (ds.field("symbol").isin(keep)))
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]"); df["symbol"] = df["symbol"].astype(str)
    df = df[(df["close"] > 0) & (df["raw_close"] > 0)]
    df["f"] = (df["close"] / df["raw_close"]).astype(np.float32)
    df = df[["symbol", "date", "f"]].sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"])
    q = sym_start.copy().reset_index(drop=True)
    q["date"] = pd.to_datetime(q["date"], errors="coerce")
    ok = q["symbol"].notna() & q["date"].notna()
    res = np.full(len(q), np.nan)
    dmap = {s: (g["date"].to_numpy("datetime64[ns]"), g["f"].to_numpy(np.float32))
            for s, g in df.groupby("symbol", sort=False)}
    dts = q["date"].to_numpy("datetime64[ns]")
    for i in np.flatnonzero(ok.to_numpy()):
        v = dmap.get(q["symbol"].iat[i])
        if v is None:
            continue
        j = int(np.searchsorted(v[0], dts[i], side="right")) - 1
        if j >= 0:
            res[i] = v[1][j]
    return res


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


def prep_contracts(dates):
    p = pd.read_parquet(OUT / "pledge_all.parquet").drop_duplicates()
    p["code"] = p["code"].astype(str).str.zfill(6)
    for c in ["NOTICE_DATE", "PF_START_DATE", "UNFREEZE_DATE"]:
        p[c] = pd.to_datetime(p[c], errors="coerce")
    for c in ["PF_TSR", "OPENLINE"]:
        p[c] = pd.to_numeric(p[c], errors="coerce")
    p = p.dropna(subset=["NOTICE_DATE", "PF_START_DATE", "OPENLINE", "PF_TSR"])
    p = p[(p["PF_TSR"] > 0) & (p["OPENLINE"] > 0)]
    p["open_state"] = p["UNFREEZE_STATE"].astype(str).eq("未解押")
    p["ni"] = np.searchsorted(dates.values, p["NOTICE_DATE"].values, side="right") - 1
    # 公告日早于行情窗口：视为「窗口开始前已在簿」，入场 idx = 0（不是丢弃）
    p["ni"] = p["ni"].clip(lower=0)
    p["si"] = np.searchsorted(dates.values, p["PF_START_DATE"].values, side="right") - 1
    return p.copy()


def main():
    print("[1/6] load daily ...", flush=True)
    op_df, cl_df, rc_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32); C = cl_df.to_numpy(np.float32)
    RC = rc_df.to_numpy(np.float32); A = am_df.to_numpy(np.float32)
    TO = to_df.to_numpy(np.float32) / 100.0
    n, k = C.shape
    sym2idx = {s: i for i, s in enumerate(syms)}
    ym = pd.Series(dates).dt.to_period("M")
    last = pd.Series(dates).groupby(ym).max()
    mids = np.array([dates.get_loc(d) for d in last.values])
    mids = mids[(dates[mids] >= pd.Timestamp(START)) & (mids < n - 62)]
    print(f"      {n} x {k} · {len(mids)} 个月末", flush=True)

    print("[2/6] contracts ...", flush=True)
    p = prep_contracts(dates)
    inst = pd.read_parquet(INST)[["code", "symbol", "name"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6); inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)].copy()
    p["idx"] = p["symbol"].map(sym2idx)
    # 复权因子：起始日优先，起始日不在行情内退回公告日
    f0 = adjust_factor(pd.DataFrame({"symbol": p["symbol"].to_numpy(), "date": p["PF_START_DATE"].to_numpy()}))
    bad = ~np.isfinite(f0)
    if bad.any():
        f1 = adjust_factor(pd.DataFrame({"symbol": p.loc[bad, "symbol"].to_numpy(),
                                         "date": p.loc[bad, "NOTICE_DATE"].to_numpy()}))
        f0 = np.asarray(f0, dtype=float); f0[bad] = f1
    p["fac"] = np.clip(np.where(np.isfinite(f0) & (f0 > 0), f0, 1.0), 0.05, 5.0)
    p["line_adj"] = p["OPENLINE"] * p["fac"]
    p["old"] = p["NOTICE_DATE"] < pd.Timestamp("2019-01-01")
    print(f"      合约 {len(p):,} · {p['symbol'].nunique():,} 只 · 2019 前公告 {int(p['old'].sum()):,}", flush=True)

    # 快照键：合约 = 股东 × 起始日（同一合约的多期公告取最近一期）
    p["ck"] = p["code"] + "|" + p["HOLDER_NAME"].astype(str) + "|" + p["PF_START_DATE"].astype(str)

    # 结果与控制
    ret = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    rv20 = pd.DataFrame(ret).rolling(20).std().to_numpy()
    first = np.argmax(np.isfinite(C), axis=0)
    am20 = pd.DataFrame(A).rolling(20).mean().to_numpy()
    am60 = pd.DataFrame(A).rolling(60).mean().to_numpy()
    to20 = pd.DataFrame(TO).rolling(20).mean().to_numpy()
    age = pd.read_parquet(AGE); age["date"] = pd.to_datetime(age["date"])
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age = age[["date", "symbol", "D"]]
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
        r["age"] = m - first
        recs.append(r)
    base = pd.concat(recs, ignore_index=True)
    base = base.merge(age, on=["date", "symbol"], how="left")
    base["nm"] = base["symbol"].map(nmap).fillna("")
    base["ok"] = (~base["nm"].str.contains("ST")) & (base["age"] >= 250) & (base["amt60"] >= 1.5e7)
    base["ym"] = base["date"].dt.to_period("M").astype(str)
    print(f"      基准面板 {len(base):,} · 有效 {int(base['ok'].sum()):,}", flush=True)

    def book(mode, cover):
        rows, cov = [], []
        src = p if cover == "all" else p[~p["old"]]
        for m in mids:
            q = src[src["ni"] <= m]
            if mode == "sum":
                live = q["open_state"] | (q["UNFREEZE_DATE"].notna() & (q["UNFREEZE_DATE"] > dates[m]))
                q = q[live]
            else:
                # snap：每份合约只取「最近一次公告」；同一公告若同时列出多笔未解押，全部保留
                if q.empty:
                    continue
                q = q.copy()
                q["lastn"] = q.groupby("ck")["NOTICE_DATE"].transform("max")
                q = q[(q["NOTICE_DATE"] == q["lastn"]) & q["open_state"]]
            if q.empty:
                continue
            px = C[m, q["idx"].to_numpy()]
            ok = np.isfinite(px) & (px > 0)
            q = q[ok].copy(); q["px"] = px[ok]
            q["ratio"] = q["line_adj"].to_numpy() / q["px"].to_numpy()
            q["tsr_n"] = q["PF_TSR"] * ((q["ratio"] >= 0.8) & (q["ratio"] <= 1.2))
            q["tsr_b"] = q["PF_TSR"] * (q["ratio"] >= 1.2)
            q["tsr_w"] = q["PF_TSR"] * np.clip(q["ratio"], 0.2, 2.0)
            g = q.groupby("symbol").agg(book=("PF_TSR", "sum"), near=("tsr_n", "sum"),
                                        breach=("tsr_b", "sum"), wnum=("tsr_w", "sum"),
                                        cn=("PF_TSR", "size"))
            g["pre19"] = q[q["old"]].groupby("symbol")["PF_TSR"].sum().reindex(g.index).fillna(0.0)
            g["date"] = dates[m]
            rows.append(g.reset_index())
            cov.append({"date": str(dates[m].date()), "contracts": int(len(q)), "codes": int(g.shape[0]),
                        "book_sum": round(float(q["PF_TSR"].sum()), 1),
                        "pre19_share": round(float(q.loc[q["old"], "PF_TSR"].sum() / max(q["PF_TSR"].sum(), 1e-9)), 4)})
        b = pd.concat(rows, ignore_index=True)
        b["near_share"] = b["near"] / b["book"].clip(lower=1e-9)
        b["breach_share"] = b["breach"] / b["book"].clip(lower=1e-9)
        b["wdist"] = b["wnum"] / b["book"].clip(lower=1e-9)
        b["ym"] = b["date"].dt.to_period("M").astype(str)
        return b, cov

    out = {"meta": {"months": len(mids), "contracts": int(len(p)), "stocks": int(p["symbol"].nunique()),
                    "pre2019_notices": int(p["old"].sum()),
                    "note": "bw=2019+公告+逐行求和（旧）；bw3=2005+公告+逐行求和；sn3=2005+公告+每合约最近一期快照"}}
    for tag, mode, cover in [("bw", "sum", "post"), ("bw3", "sum", "all"), ("sn3", "snap", "all")]:
        print(f"[run] {tag} mode={mode} cover={cover}", flush=True)
        b, cov = book(mode, cover)
        d = base.merge(b[["date", "symbol", "book", "near_share", "breach_share", "wdist", "cn", "pre19"]],
                       on=["date", "symbol"], how="left")
        d["has_book"] = d["book"].fillna(0) > 0
        for c in ["near_share", "breach_share", "wdist", "pre19"]:
            d[c] = d[c].fillna(0.0)
        s = d[d["ok"] & d["has_book"] & d["f60"].notna()].copy()
        reg = []
        for ym_, g in s.groupby("ym"):
            if len(g) < 80:
                continue
            X = np.column_stack([z(g["near_share"]), z(g["wdist"]), z(g["breach_share"]), z(g["vol20"]),
                                 z(g["turn20"]), z(g["size"]), z(g["D"]), z(g["ret20"]), z(g["ret60"]),
                                 np.ones(len(g))])
            y = g["f60"].to_numpy(float) * 1e4
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 80:
                continue
            bb, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            reg.append({"ym": ym_, "b_near": bb[0], "b_w": bb[1], "b_b": bb[2]})
        r = pd.DataFrame(reg)
        # 原始五分位（按 wdist）
        qx = d[d["ok"] & d["has_book"] & d["f60"].notna()].copy()
        qx["q"] = qx.groupby("ym")["wdist"].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=False, duplicates="drop"))
        gg = qx.dropna(subset=["q"]).groupby(["ym", "q"])["f60"].mean().unstack()
        sp = (gg[4] - gg[0]) if 4 in gg.columns else pd.Series(dtype=float)
        out[tag] = {
            "n": int(len(s)), "months": int(len(r)),
            "near": round(float(r.b_near.mean()), 2), "near_t": round(float(nw_t(r.b_near)), 2),
            "wdist": round(float(r.b_w.mean()), 2), "wdist_t": round(float(nw_t(r.b_w)), 2),
            "breach": round(float(r.b_b.mean()), 2), "breach_t": round(float(nw_t(r.b_b)), 2),
            "w_q1": round(float(gg[0].mean() * 1e4), 1) if 0 in gg.columns else None,
            "w_q5": round(float(gg[4].mean() * 1e4), 1) if 4 in gg.columns else None,
            "w_spread": round(float(sp.mean() * 1e4), 1) if len(sp) else None,
            "w_spread_t": round(float(nw_t(sp)), 2) if len(sp) else None,
            "book_median": round(float(s["book"].median()), 2),
            "stocks_med": int(s.groupby("ym").size().median()),
            "coverage": cov[::12],
        }
        print("      " + json.dumps({k2: out[tag][k2] for k2 in ["n", "months", "near", "near_t", "wdist", "wdist_t",
                                                                 "book_median", "w_spread", "w_spread_t"]}, ensure_ascii=False), flush=True)

    (OUT / "constraint_book_v3.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
