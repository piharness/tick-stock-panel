"""约束簿 · 规格诊断（在 constraint_book_panel.parquet 上重跑，不覆盖主结果）。

主脚本把 near_share 当主变量，但 near_share 是 ratio 的**倒 U 变换**：
股价下跌 → ratio 上升 → 进入 [0.8,1.2] → 继续跌 → 离开（变 breached）。
所以 near_share 的五分位混合了「正在靠近」和「已经跌穿后离开」两种状态，单调性被破坏。
H11 的剂量是 ratio 本身单调（0.3→1.2 越来越差）。这里补三个正确规格：

D1  wdist（簿加权平均 ratio）五分位 → fwd60；这是 H11 剂量反应的连续版。
D2  条件放大：只在「已发生下跌」的月末里看 near/wdist；约束是放大器，不是水平预测器。
D3  波动放大：下跌子样本里 near_share → 未来 20 日波动；理论预测为正。
D4  到期（预定解押日，非提前解押）：near × matured 的交互。
"""
from __future__ import annotations

import json
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


def qmean(df, var, y, q=5, by="ym"):
    d = df.copy()
    d = d[d.groupby(by)[var].transform("std") > 1e-9]
    d["_q"] = d.groupby(by)[var].transform(lambda s: pd.qcut(s.rank(method="first"), q, labels=False, duplicates="drop"))
    g = d.dropna(subset=["_q"]).groupby([by, "_q"])[y].mean().reset_index()
    p = g.pivot(index=by, columns="_q", values=y)
    means = {f"Q{int(c)+1}": round(float(p[c].mean() * 1e4), 1) for c in p.columns}
    sp = p[p.columns[-1]] - p[p.columns[0]]
    return {"q_bp": means, "spread_bp": round(float(sp.mean() * 1e4), 1), "t": round(float(nw_t(sp)), 2),
            "pos_months": round(float((sp > 0).mean()), 2), "n": int(len(d))}


def main():
    d = pd.read_parquet(OUT / "constraint_book_panel.parquet")
    d = d[d["ok"] & d["has_book"]].copy()
    d = d[d["f60"].notna() & d["wdist"].notna()]
    out = {}

    # D1 wdist 五分位（连续剂量）
    out["D1_wdist"] = qmean(d, "wdist", "f60")
    out["D1_near"] = qmean(d, "near_share", "f60")
    # 控制回归：wdist
    reg = []
    for ym, g in d.groupby("ym"):
        if len(g) < 80:
            continue
        X = np.column_stack([z(g["wdist"]), z(g["near_share"]), z(g["breach_share"]), z(g["vol20"]),
                             z(g["turn20"]), z(g["size"]), z(g["D"]), z(g["ret20"]), z(g["ret60"]), np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 80:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym, "b_w": b[0], "b_n": b[1], "b_b": b[2]})
    r = pd.DataFrame(reg)
    out["D1_controlled"] = {"b_wdist_bp": round(float(r["b_w"].mean()), 1), "t_w": round(float(nw_t(r["b_w"])), 2),
                            "b_near_bp": round(float(r["b_n"].mean()), 1), "t_n": round(float(nw_t(r["b_n"])), 2),
                            "b_breach_bp": round(float(r["b_b"].mean()), 1), "t_b": round(float(nw_t(r["b_b"])), 2)}

    # D2 下跌条件：ret20 低于当月截面 p20
    d["shock"] = d["ret20"] < d.groupby("ym")["ret20"].transform(lambda s: s.quantile(0.2))
    s = d[d["shock"] & d["f20"].notna()].copy()
    out["D2_shock_sample"] = {"n": int(len(s)), "avg_ret20": round(float(s["ret20"].mean() * 100), 1)}
    out["D2_wdist_q"] = qmean(s, "wdist", "f20")
    out["D2_near_q"] = qmean(s, "near_share", "f20")
    reg2 = []
    for ym, g in s.groupby("ym"):
        if len(g) < 40:
            continue
        X = np.column_stack([z(g["near_share"]), z(g["wdist"]), z(g["breach_share"]), z(g["vol20"]),
                             z(g["turn20"]), z(g["size"]), z(g["D"]), z(g["ret60"]), np.ones(len(g))])
        y = g["f20"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 40:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg2.append({"ym": ym, "b_n": b[0], "b_w": b[1]})
    r2 = pd.DataFrame(reg2)
    out["D2_controlled"] = {"b_near_bp": round(float(r2["b_n"].mean()), 1), "t_near": round(float(nw_t(r2["b_n"])), 2),
                            "b_wdist_bp": round(float(r2["b_w"].mean()), 1), "t_wdist": round(float(nw_t(r2["b_w"])), 2),
                            "months": int(len(r2))}

    # D3 波动放大（下跌子样本）
    s3 = d[d["shock"] & d["fv20"].notna()].copy()
    reg3 = []
    for ym, g in s3.groupby("ym"):
        if len(g) < 40:
            continue
        X = np.column_stack([z(g["near_share"]), z(g["wdist"]), z(g["vol20"]), z(g["turn20"]),
                             z(g["size"]), np.ones(len(g))])
        y = g["fv20"].to_numpy(float)
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 40:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg3.append({"ym": ym, "b_n": b[0], "b_w": b[1]})
    r3 = pd.DataFrame(reg3)
    out["D3_shock_vol"] = {"b_near_pp": round(float(r3["b_n"].mean() * 100), 2), "t_near": round(float(nw_t(r3["b_n"])), 2),
                          "b_wdist_pp": round(float(r3["b_w"].mean() * 100), 2), "t_wdist": round(float(nw_t(r3["b_w"])), 2)}

    # D4 预定解押：exp_share 是 90 天内到期占比；用 near × exp 交互
    s4 = d[d["f60"].notna()].copy()
    reg4 = []
    for ym, g in s4.groupby("ym"):
        if len(g) < 80:
            continue
        X = np.column_stack([z(g["near_share"]), z(g["exp_share"]), z(g["near_share"] * g["exp_share"]),
                             z(g["wdist"]), z(g["vol20"]), z(g["turn20"]), z(g["size"]), z(g["D"]), np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 80:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg4.append({"ym": ym, "b_int": b[2], "b_exp": b[1]})
    r4 = pd.DataFrame(reg4)
    out["D4_expiry"] = {"b_int_bp": round(float(r4["b_int"].mean()), 1), "t_int": round(float(nw_t(r4["b_int"])), 2),
                        "b_exp_bp": round(float(r4["b_exp"].mean()), 1), "t_exp": round(float(nw_t(r4["b_exp"])), 2)}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    (OUT / "constraint_book_diag.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
