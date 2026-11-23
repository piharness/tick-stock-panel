"""最终定稿：τ = 0.5 固定（半个最小报价单位），只拟合 β。含样本外与残差检验。"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "analysis" / "_cache_cd.parquet"
OUT = ROOT / "analysis" / "_diag_friction_final.json"
COST_FIXED_BP = 8.0
ALPHA = 0.5  # 理论值：半价差 = 半个最小报价单位，不拟合


def prep(df):
    df = df.copy()
    df["rho1"] = df["c1"] / df["v"]
    df["sd_bp"] = np.sqrt(df["v"] / df["n"]) * 1e4
    df["h_bp"] = df.sd_bp * np.sqrt(-df.rho1)
    df["gross_bp"] = df.g / df.n * 1e4
    df["tick_bp"] = 0.01 / df.mpx * 1e4
    df["h_quant"] = ALPHA * df.tick_bp
    return df


def fit_beta(df):
    best = None
    for b in np.arange(0.05, 0.401, 0.0005):
        h = np.sqrt((ALPHA * df.tick_bp.values) ** 2 + (b * df.sd_bp.values) ** 2)
        pr = -((h / df.sd_bp.values) ** 2)
        sse = float(((pr - df.rho1.values) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, b)
    return best[1]


def r2(pr, y):
    return float(1 - ((pr - y) ** 2).sum() / ((y - y.mean()) ** 2).sum())


def main() -> None:
    con = duckdb.connect()
    con.execute(f"CREATE VIEW cd AS SELECT * FROM '{CACHE.as_posix()}'")
    base = con.sql(
        """
        SELECT tq, pq, strftime(date,'%Y') AS yr,
               sum(v) AS v, sum("c1") AS c1, sum(g) AS g, sum(n) AS n, avg(zf) AS zf, count(*) AS nd
        FROM cd GROUP BY 1,2,3
        """
    ).df()
    px = con.sql(
        """
        SELECT tq, pq, median(mpx) AS mpx, median(mturn) AS mturn FROM (
          SELECT tq, pq, date, 0 AS mpx, 0 AS mturn FROM cd LIMIT 0) GROUP BY 1,2
        """
    ).df()
    # mpx/mturn 需从 symq 重建（cd 未存），改由 law2 的 JSON 取
    law2 = json.loads((ROOT / "analysis" / "_diag_intraday_friction_law2.json").read_text(encoding="utf-8"))
    meta = {(c["tq"], c["pq"]): (c["mpx"], c["mturn"]) for c in law2["cells"]}
    base["mpx"] = [meta[(a, b)][0] for a, b in zip(base.tq, base.pq)]
    base["mturn"] = [meta[(a, b)][1] for a, b in zip(base.tq, base.pq)]
    base = prep(base)

    tr = base[base.yr.isin(["2024", "2025"])]
    te = base[base.yr == "2026"]
    allp = (base.groupby(["tq", "pq"], as_index=False)
            .agg(v=("v", "sum"), c1=("c1", "sum"), g=("g", "sum"), n=("n", "sum"), zf=("zf", "mean")))
    allp["mpx"] = [meta[(a, b)][0] for a, b in zip(allp.tq, allp.pq)]
    allp["mturn"] = [meta[(a, b)][1] for a, b in zip(allp.tq, allp.pq)]
    allp = prep(allp)

    def model(df, b):
        h = np.sqrt((ALPHA * df.tick_bp.values) ** 2 + (b * df.sd_bp.values) ** 2)
        return -((h / df.sd_bp.values) ** 2)

    # 训练期拟合（按格子聚合）
    trc = tr.groupby(["tq", "pq"], as_index=False).agg(v=("v", "sum"), c1=("c1", "sum"),
                                                       g=("g", "sum"), n=("n", "sum"))
    trc["mpx"] = [meta[(a, b)][0] for a, b in zip(trc.tq, trc.pq)]
    trc = prep(trc)
    beta = fit_beta(trc)
    pr_tr, pr_all, pr_te = model(trc, beta), model(allp, beta), model(te, beta)

    res_all = allp.rho1.values - pr_all
    allp["pred"] = pr_all
    allp["resid"] = res_all
    allp["cost_bp"] = 2 * allp.h_bp + COST_FIXED_BP
    allp["g_over_c"] = allp.gross_bp / allp.cost_bp

    # α=0.5 是否可接受：与自由拟合 α 的 R² 对比
    best = None
    for a in np.arange(0.30, 0.91, 0.01):
        for b in np.arange(0.05, 0.401, 0.002):
            h = np.sqrt((a * allp.tick_bp.values) ** 2 + (b * allp.sd_bp.values) ** 2)
            sse = float(((-((h / allp.sd_bp.values) ** 2) - allp.rho1.values) ** 2).sum())
            if best is None or sse < best[0]:
                best = (sse, a, b)
    hf = np.sqrt((best[1] * allp.tick_bp.values) ** 2 + (best[2] * allp.sd_bp.values) ** 2)
    r2_free = r2(-((hf / allp.sd_bp.values) ** 2), allp.rho1.values)

    # 残差 vs 换手
    lt = np.log(allp.mturn.values)
    X = np.column_stack([np.ones(len(lt)), lt])
    bb, *_ = np.linalg.lstsq(X, res_all, rcond=None)
    rr = res_all - X @ bb
    s2 = rr @ rr / (len(lt) - 2)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))

    out = {
        "model": {"alpha": ALPHA, "beta": float(beta),
                  "r2_train": r2(pr_tr, trc.rho1.values),
                  "r2_all": r2(pr_all, allp.rho1.values),
                  "r2_test_2026": r2(pr_te, te.rho1.values),
                  "alpha_free": float(best[1]), "beta_free": float(best[2]),
                  "r2_alpha_free": r2_free,
                  "rho1_floor_impact": float(-beta ** 2),
                  "resid_sd": float(res_all.std()),
                  "resid_max_abs": float(np.abs(res_all).max())},
        "residual": {
            "coef_ln_turn": float(bb[1]), "t_ln_turn": float(bb[1] / se[1]),
            "span_ln_turn": float(lt.max() - lt.min()),
            "range_effect": float(bb[1] * (lt.max() - lt.min())),
            "share_of_rho_range": float(abs(bb[1] * (lt.max() - lt.min())) / (allp.rho1.max() - allp.rho1.min())),
            "by_tq": allp.groupby("tq").resid.mean().round(5).to_dict(),
            "by_pq": allp.groupby("pq").resid.mean().round(5).to_dict(),
        },
        "economics": {
            "g_over_c_min": float(allp.g_over_c.min()), "g_over_c_max": float(allp.g_over_c.max()),
            "g_over_c_median": float(allp.g_over_c.median()),
            "n_over_0_15": int((allp.g_over_c > 0.15).sum()),
            "cost_bp_min": float(allp.cost_bp.min()), "cost_bp_max": float(allp.cost_bp.max()),
        },
        "cells": allp[["tq", "pq", "mpx", "mturn", "sd_bp", "rho1", "pred", "resid",
                       "h_bp", "gross_bp", "cost_bp", "g_over_c", "zf"]].round(6).to_dict("records"),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    m = out["model"]
    print(f"α={ALPHA}(固定) β={beta:.4f}")
    print(f"R²: 训练 {m['r2_train']:.4f} / 全样本 {m['r2_all']:.4f} / 2026样本外 {m['r2_test_2026']:.4f}")
    print(f"α 自由拟合: {m['alpha_free']:.2f} β={m['beta_free']:.3f} → R² {m['r2_alpha_free']:.4f}")
    print(f"残差 sd {m['resid_sd']:.5f}，最大 {m['resid_max_abs']:.5f}")
    print(f"冲击项隐含 ρ₁ 下限 = {m['rho1_floor_impact']:.4f}")
    r = out["residual"]
    print(f"残差 ~ ln(换手): β={r['coef_ln_turn']:+.5f} t={r['t_ln_turn']:.2f} "
          f"跨度效应 {r['range_effect']:+.5f} = ρ₁ 全距的 {r['share_of_rho_range']*100:.1f}%")
    print("残差按价格分位:", r["by_pq"])
    print("残差按换手分位:", r["by_tq"])
    e = out["economics"]
    print(f"毛/成本 中位 {e['g_over_c_median']:.3f} 范围 {e['g_over_c_min']:.3f}-{e['g_over_c_max']:.3f}"
          f" 超0.15的格子 {e['n_over_0_15']}")
    print(f"往返成本 {e['cost_bp_min']:.1f}-{e['cost_bp_max']:.1f}bp")


if __name__ == "__main__":
    main()
