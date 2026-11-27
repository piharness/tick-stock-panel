"""拆 2012–2018：机构专用卖方深折价的负号，是质押/定增驱动的吗？

输入
----
- `block_type_big_events.parquet`：大样本事件（任意大宗日 + 20 日冷却，2013–2026），
  含 inst_deep / broker_deep / prem / other、f60、raw_close。
- `pledge_hist.parquet`：2012–2018 质押明细（code/start/unfreeze/PF_TSR/OPENLINE/notice/state）。
- `seo_hist.parquet`：2012–2018 定增明细（code/ISSUE_DATE/ISSUE_LISTING_DATE/ISSUE_PRICE/LOCKIN_PERIOD/SEO_TYPE）。

暴露定义
--------
P（质押）：
  pledge_live  = 事件日存在活合约（start≤t 且 (unfreeze>t 或 未解押)）；
  pledge_near  = 活合约里 openline/raw_close ∈ [0.8,1.2]；
  pledge_ratio = 活合约 PF_TSR 之和。
S（定增）：
  seo_active = 事件日落在某笔定增的锁定期内（listing_date ≤ t ≤ listing_date + lockup）；
  seo_break  = seo_active 且 raw_close < ISSUE_PRICE；
  seo_recent = 定增发行日在 [t−365, t+30]。

检验
----
M1 桶：2012–2018 的 inst_deep 事件，按 P/S 分四桶（both / P only / S only / neither），
   报 N、f60 均值、与同桶 broker_deep 的差（月聚簇 NW t）。
M2 交互（主检验）：全样本月截面
   f60 ~ inst_deep + P + inst_deep×P + controls；S 同式。
   若 P 是驱动，交互应显著为负；若 S 是驱动，S 交互显著为负。
M3 细粒度：pledge_near vs pledge_far；seo_break vs seo_active_not_break。
M4 时代：2012–2015 vs 2016–2018 的桶均值。

判据（写死）
------------
- M2 的 P 交互 ≤ −100bp 且 |t| ≥ 2 → 质押是驱动；S 交互同理。
- 若两个交互都不显著，或负号集中在 neither → 质押/定增不是驱动，2012–18 的 regime 另有来源。
- M1 桶样本 < 30 的不裁决。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
NW_LAG = 6


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


def lock_days(s):
    s = str(s)
    m = re.search(r"(\d+)\s*(年|个月|月|天)", s)
    if not m:
        return 365
    n = int(m.group(1)); u = m.group(2)
    return n * 365 if u == "年" else (n * 30 if "月" in u else n)


def main():
    ev = pd.read_parquet(OUT / "block_type_big_events.parquet")
    ev["date"] = pd.to_datetime(ev["date"])
    ev["code"] = ev["code"].astype(str).str.zfill(6)
    ev["eid"] = np.arange(len(ev))
    p = pd.read_parquet(OUT / "pledge_hist.parquet")
    s = pd.read_parquet(OUT / "seo_hist.parquet")
    for c in ["start", "unfreeze", "notice"]:
        p[c] = pd.to_datetime(p[c], errors="coerce")
    for c in ["ISSUE_DATE", "ISSUE_LISTING_DATE"]:
        s[c] = pd.to_datetime(s[c], errors="coerce")
    p["PF_TSR"] = pd.to_numeric(p["PF_TSR"], errors="coerce").fillna(0)
    p["openline"] = pd.to_numeric(p["OPENLINE"], errors="coerce")
    s["ISSUE_PRICE"] = pd.to_numeric(s["ISSUE_PRICE"], errors="coerce")
    s = s[s["SEO_TYPE"].astype(str).isin(["1", "1.0"])].copy()  # 定向增发
    s["lock"] = s["LOCKIN_PERIOD"].map(lock_days)
    s["s0"] = s["ISSUE_LISTING_DATE"].fillna(s["ISSUE_DATE"])
    s["s1"] = s["s0"] + pd.to_timedelta(s["lock"], unit="D")

    print(f"events {len(ev):,} · pledge {len(p):,} · seo {len(s):,}", flush=True)

    # ---- P 暴露 ----
    m = ev[["eid", "date", "code", "raw_close"]].merge(
        p[["code", "start", "unfreeze", "notice", "PF_TSR", "openline", "UNFREEZE_STATE"]], on="code", how="left")
    m["start"] = m["start"].fillna(m["notice"])
    live = (m["start"] <= m["date"]) & (
        (m["unfreeze"] > m["date"]) |
        (m["unfreeze"].isna() & (m["notice"] + pd.Timedelta(days=365) > m["date"])) |
        (m["UNFREEZE_STATE"].astype(str).eq("未解押"))
    )
    m = m[live].copy()
    m["ratio"] = m["openline"] / m["raw_close"].replace(0, np.nan)
    pg = m.groupby("eid").agg(
        pledge_live=("PF_TSR", "size"),
        pledge_ratio=("PF_TSR", "sum"),
        pledge_near=("ratio", lambda x: int(((x >= 0.8) & (x <= 1.2)).any())),
        pledge_far=("ratio", lambda x: int((x < 0.8).any())),
    ).reset_index()
    pg["pledge_live"] = 1
    # 近期公告（±180/+30）
    n = ev[["eid", "date", "code"]].merge(p[["code", "notice"]], on="code", how="left")
    n = n[(n["notice"] >= n["date"] - pd.Timedelta(days=180)) & (n["notice"] <= n["date"] + pd.Timedelta(days=30))]
    pg2 = n.groupby("eid").size().rename("pledge_notice").reset_index()

    # ---- S 暴露 ----
    ms = ev[["eid", "date", "code", "raw_close"]].merge(
        s[["code", "s0", "s1", "ISSUE_PRICE", "ISSUE_DATE"]], on="code", how="left")
    act = (ms["s0"] <= ms["date"]) & (ms["date"] <= ms["s1"])
    ms = ms[act].copy()
    ms["brk"] = ms["raw_close"] < ms["ISSUE_PRICE"]
    sg = ms.groupby("eid").agg(seo_active=("ISSUE_PRICE", "size"), seo_break=("brk", "max")).reset_index()
    sg["seo_active"] = 1
    rec = ev[["eid", "date", "code"]].merge(s[["code", "ISSUE_DATE"]], on="code", how="left")
    rec = rec[(rec["ISSUE_DATE"] >= rec["date"] - pd.Timedelta(days=365)) & (rec["ISSUE_DATE"] <= rec["date"] + pd.Timedelta(days=30))]
    sg2 = rec.groupby("eid").size().rename("seo_recent").reset_index()

    ev = ev.merge(pg, on="eid", how="left").merge(pg2, on="eid", how="left").merge(sg, on="eid", how="left").merge(sg2, on="eid", how="left")
    for c in ["pledge_ratio", "pledge_near", "pledge_far", "pledge_notice", "seo_break", "seo_recent"]:
        ev[c] = pd.to_numeric(ev[c], errors="coerce").fillna(0)
    ev["P"] = ev["pledge_live"].fillna(0).astype(int)
    ev["Pn"] = ((ev["P"] == 1) & (ev["pledge_near"] == 1)).astype(int)
    ev["S"] = ev["seo_active"].fillna(0).astype(int)
    ev["Sb"] = ((ev["S"] == 1) & (ev["seo_break"] == 1)).astype(int)
    ev["bucket"] = np.select(
        [(ev["P"] == 1) & (ev["S"] == 1), (ev["P"] == 1) & (ev["S"] == 0), (ev["P"] == 0) & (ev["S"] == 1)],
        ["both", "P_only", "S_only"], default="neither")
    print("      P", int(ev["P"].sum()), "Pn", int(ev["Pn"].sum()), "S", int(ev["S"].sum()), "Sb", int(ev["Sb"].sum()), flush=True)

    # ---- M1 桶 ----
    e18 = ev[(ev["date"].dt.year >= 2012) & (ev["date"].dt.year <= 2018)].copy()
    out = {"meta": {"events_2012_18": int(len(e18)), "inst_deep": int((e18["grp"] == "inst_deep").sum()),
                    "P": int(e18["P"].sum()), "S": int(e18["S"].sum())}}
    m1 = []
    for b in ["both", "P_only", "S_only", "neither"]:
        x = e18[(e18["bucket"] == b) & (e18["grp"] == "inst_deep")]
        y = e18[(e18["bucket"] == b) & (e18["grp"] == "broker_deep")]
        row = {"bucket": b, "n_inst": int(len(x)), "n_broker": int(len(y))}
        if len(x) >= 30:
            row["inst_f60_bp"] = round(float(x["f60"].mean() * 1e4), 1)
        if len(x) >= 30 and len(y) >= 30:
            # 月聚簇 inst − broker
            g = e18[e18["bucket"] == b].groupby(["ym", "grp"])["f60"].mean().unstack()
            if "inst_deep" in g and "broker_deep" in g:
                d = (g["inst_deep"] - g["broker_deep"]).dropna()
                row["diff_bp"] = round(float(d.mean() * 1e4), 1)
                row["t"] = round(float(nw_t(d)), 2)
                row["months"] = int(len(d))
        m1.append(row)
    out["M1_buckets"] = m1

    # M1b：在 inst_deep 内部，S vs 非 S（定增是否在机构卖方内部加分）
    inst_e = e18[e18["grp"] == "inst_deep"]
    gS = inst_e.groupby(["ym", "S"])["f60"].mean().unstack()
    if 0 in gS and 1 in gS:
        dS = (gS[1] - gS[0]).dropna()
        out["M1b_inst_S_vs_not"] = {"bp": round(float(dS.mean() * 1e4), 1), "t": round(float(nw_t(dS)), 2),
                                    "months": int(len(dS)), "n_S": int((inst_e["S"] == 1).sum()),
                                    "n_not": int((inst_e["S"] == 0).sum())}
    inst_late = inst_e[inst_e["date"].dt.year >= 2016]
    gL = inst_late.groupby(["ym", "S"])["f60"].mean().unstack()
    if 0 in gL and 1 in gL:
        dL = (gL[1] - gL[0]).dropna()
        out["M1b_2016_18"] = {"bp": round(float(dL.mean() * 1e4), 1), "t": round(float(nw_t(dL)), 2),
                              "months": int(len(dL)), "n_S": int((inst_late["S"] == 1).sum()),
                              "n_not": int((inst_late["S"] == 0).sum())}

    # ---- M2 交互 ----
    def inter(col):
        reg = []
        for ym_, g in e18.groupby("ym"):
            if len(g) < 30:
                continue
            inst = (g["grp"] == "inst_deep").to_numpy(float)
            ex = g[col].to_numpy(float)
            X = np.column_stack([inst, ex, inst * ex, z(g["ret20"]), z(g["ret60"]), z(g["vol20"]),
                                 z(g["turn20"]), z(g["size"]), z(np.log1p(g["D"])), np.ones(len(g))])
            yy = g["f60"].to_numpy(float) * 1e4
            ok = np.isfinite(yy) & np.isfinite(X).all(axis=1)
            if ok.sum() < 30:
                continue
            bb, *_ = np.linalg.lstsq(X[ok], yy[ok], rcond=None)
            reg.append({"ym": ym_, "b_inst": bb[0], "b_ex": bb[1], "b_int": bb[2]})
        r = pd.DataFrame(reg)
        return {"b_int_bp": round(float(r["b_int"].mean()), 1), "t_int": round(float(nw_t(r["b_int"])), 2),
                "b_inst_bp": round(float(r["b_inst"].mean()), 1), "t_inst": round(float(nw_t(r["b_inst"])), 2),
                "b_ex_bp": round(float(r["b_ex"].mean()), 1), "t_ex": round(float(nw_t(r["b_ex"])), 2),
                "months": int(len(r))}
    out["M2_P"] = inter("P")
    out["M2_S"] = inter("S")
    out["M2_Pn"] = inter("Pn")
    out["M2_Sb"] = inter("Sb")

    # ---- M3 细粒度 ----
    m3 = {}
    for name, mask in [("Pn", e18["Pn"] == 1), ("P_not_near", (e18["P"] == 1) & (e18["Pn"] == 0)),
                       ("Sb", e18["Sb"] == 1), ("S_not_break", (e18["S"] == 1) & (e18["Sb"] == 0))]:
        x = e18[mask & (e18["grp"] == "inst_deep")]
        y = e18[mask & (e18["grp"] == "broker_deep")]
        if len(x) >= 30 and len(y) >= 30:
            g = e18[mask].groupby(["ym", "grp"])["f60"].mean().unstack()
            if "inst_deep" in g and "broker_deep" in g:
                d = (g["inst_deep"] - g["broker_deep"]).dropna()
                m3[name] = {"n_inst": int(len(x)), "n_broker": int(len(y)),
                            "diff_bp": round(float(d.mean() * 1e4), 1), "t": round(float(nw_t(d)), 2),
                            "months": int(len(d))}
        else:
            m3[name] = {"n_inst": int(len(x)), "n_broker": int(len(y)), "note": "样本太薄"}
    out["M3"] = m3

    # ---- M4 时代 ----
    m4 = {}
    for lo, hi in [(2012, 2015), (2016, 2018)]:
        sub = e18[(e18["date"].dt.year >= lo) & (e18["date"].dt.year <= hi)]
        for b in ["both", "P_only", "S_only", "neither"]:
            x = sub[(sub["bucket"] == b) & (sub["grp"] == "inst_deep")]
            if len(x) >= 30:
                m4[f"{lo}-{hi}_{b}"] = {"n": int(len(x)), "f60_bp": round(float(x["f60"].mean() * 1e4), 1)}
    out["M4_era"] = m4

    (OUT / "block_type_mechanism.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
