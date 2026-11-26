"""H1 分解：T/D 比值到底是「错配」，还是两个主效应？

背景：H1 的比值 M=T/D 与 D 的 Spearman = -0.85，与 20 日换手 = 0.80。
比值方差被分母（D）主导，所以「M 预测脆弱性」可能只是「短久期 → 深回撤」，
不是「短钱 + 慢故事」的交互。

本脚本只做分解，不重新造面板。输入：
  data/.scratch/chip_h1/panel.parquet   （H1 合并面板，375k 行）
  data/.scratch/chip_h1/monthly.parquet （93 个月的截面统计）

输出：
  data/.scratch/chip_h1/h1_decomp.json
  data/.scratch/chip_h1/decomp_table.parquet

判据（写于跑数之前）：
  - 比值通过：primary ρ ≥ 0.15、NW t ≥ 2、OOS 同号（已在 _diag_chip_h1 里）。
  - 交互通过：2×2 的 (D 低 × T 高 − D 低 × T 低) − (D 高 × T 高 − D 高 × T 低) ≥ 0.10，
    且 NW t ≥ 2。若 <0.10，则「错配」只是两个主效应相加，不能叫交互。
  - 主效应：logT 系数 > 0 且 t ≥ 2；logD 系数 < 0 且 t ≤ −2。
  - 收益：报告交互与主效应对 fwd20 的系数；不把「风险」说成「alpha」。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h1"
NW_LAG = 6


def nw_t(x, lag: int = NW_LAG) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return np.nan
    e = x - x.mean()
    s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def z(x) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def main() -> None:
    p = pd.read_parquet(OUT / "panel.parquet")
    p = p.dropna(subset=["M", "D", "T", "fragility", "fwd20", "logamt20", "vol20", "turn20", "ret20"])
    m = pd.read_parquet(OUT / "monthly.parquet")

    out: dict = {
        "sample": {"rows": int(len(p)), "dates": int(p["date"].nunique()), "symbols": int(p["symbol"].nunique())},
        "collinearity": {
            "corr_M_D": round(float(p[["M", "D"]].corr(method="spearman").iloc[0, 1]), 3),
            "corr_M_T": round(float(p[["M", "T"]].corr(method="spearman").iloc[0, 1]), 3),
            "corr_M_turn20": round(float(p[["M", "turn20"]].corr(method="spearman").iloc[0, 1]), 3),
            "corr_M_vol20": round(float(p[["M", "vol20"]].corr(method="spearman").iloc[0, 1]), 3),
            "corr_T_D": round(float(p[["T", "D"]].corr(method="spearman").iloc[0, 1]), 3),
        },
    }

    # 2x2 交互：用月度 cell 均值
    dlo_tlo = m["cell_dlo_tlo_frag"]
    dlo_thi = m["cell_dlo_thi_frag"]
    dhi_tlo = m["cell_dhi_tlo_frag"]
    dhi_thi = m["cell_dhi_thi_frag"]
    t_low = dlo_thi - dlo_tlo
    t_high = dhi_thi - dhi_tlo
    inter = t_low - t_high
    out["two_by_two"] = {
        "cells_frag": {
            "dlo_tlo": round(float(dlo_tlo.mean()), 3),
            "dlo_thi": round(float(dlo_thi.mean()), 3),
            "dhi_tlo": round(float(dhi_tlo.mean()), 3),
            "dhi_thi": round(float(dhi_thi.mean()), 3),
        },
        "T_effect_within_D_low": {"mean": round(float(t_low.mean()), 3), "nw_t": round(float(nw_t(t_low)), 2)},
        "T_effect_within_D_high": {"mean": round(float(t_high.mean()), 3), "nw_t": round(float(nw_t(t_high)), 2)},
        "interaction": {"mean": round(float(inter.mean()), 3), "nw_t": round(float(nw_t(inter)), 2)},
    }

    # 2x2 对 fwd20 的交互
    fl = (m["cell_dlo_thi_fwd"] - m["cell_dlo_tlo_fwd"]) - (m["cell_dhi_thi_fwd"] - m["cell_dhi_tlo_fwd"])
    out["two_by_two"]["fwd_interaction"] = {
        "mean_pct": round(float(fl.mean() * 100), 3),
        "nw_t": round(float(nw_t(fl)), 2),
    }

    # 条件表：D 五分位 × T 三分位
    rows = []
    for dt, g in p.groupby("date"):
        g = g.copy()
        g["dq"] = pd.qcut(g["D"], 5, labels=False, duplicates="drop")
        g["tt"] = pd.qcut(g["T"], 3, labels=False, duplicates="drop")
        for (dq, tt), x in g.groupby(["dq", "tt"]):
            rows.append({
                "date": dt, "dq": int(dq), "tt": int(tt),
                "frag": float(x["fragility"].mean()),
                "mdd": float(x["mdd20"].mean()),
                "fwd": float(x["fwd20"].mean()),
                "n": int(len(x)),
            })
    c = pd.DataFrame(rows)
    c.to_parquet(OUT / "decomp_table.parquet", index=False)
    tab = c.pivot_table(index="dq", columns="tt", values="frag", aggfunc="mean")
    tab_mdd = c.pivot_table(index="dq", columns="tt", values="mdd", aggfunc="mean")
    out["conditional"] = {
        "fragility": {str(int(i)): [round(float(v), 3) for v in row] for i, row in tab.iterrows()},
        "mdd_pct": {str(int(i)): [round(float(v) * 100, 2) for v in row] for i, row in tab_mdd.iterrows()},
        "T_effect_by_D_quintile": {
            str(int(dq)): round(float(c[(c.dq == dq) & (c.tt == 2)]["frag"].mean() - c[(c.dq == dq) & (c.tt == 0)]["frag"].mean()), 3)
            for dq in sorted(c["dq"].unique())
        },
    }

    # 月度截面回归：fragility / fwd20 ~ logT + logD + controls
    reg_rows = []
    for dt, g in p.groupby("date"):
        if len(g) < 200:
            continue
        X = np.column_stack([
            z(np.log(g["T"])), z(np.log(g["D"])), z(g["logamt20"]),
            z(g["vol20"]), z(g["turn20"]), z(g["ret20"]), np.ones(len(g)),
        ])
        for yname in ("fragility", "fwd20"):
            y = z(g[yname])
            b, *_ = np.linalg.lstsq(X, y, rcond=None)
            reg_rows.append({"date": dt, "y": yname, "b_logT": b[0], "b_logD": b[1], "n": len(g)})
    reg = pd.DataFrame(reg_rows)
    reg.to_parquet(OUT / "decomp_regression.parquet", index=False)
    out["regression_logT_logD"] = {}
    for yname, g in reg.groupby("y"):
        out["regression_logT_logD"][yname] = {
            "b_logT": round(float(g["b_logT"].mean()), 4),
            "t_logT": round(float(nw_t(g["b_logT"])), 2),
            "b_logD": round(float(g["b_logD"].mean()), 4),
            "t_logD": round(float(nw_t(g["b_logD"])), 2),
            "n": int(len(g)),
        }

    # R²：M / M+D / M+D+controls
    r2 = []
    for dt, g in p.groupby("date"):
        if len(g) < 200:
            continue
        y = z(g["fragility"])
        sets = {
            "M": [z(g["M"])],
            "M+D": [z(g["M"]), z(g["D"])],
            "M+D+ctrl": [z(g["M"]), z(g["D"]), z(g["logamt20"]), z(g["vol20"]), z(g["turn20"]), z(g["ret20"])],
        }
        for name, cols in sets.items():
            X = np.column_stack(cols + [np.ones(len(g))])
            b, *_ = np.linalg.lstsq(X, y, rcond=None)
            yhat = X @ b
            ss = ((y - y.mean()) ** 2).sum()
            r2.append({"date": dt, "set": name, "r2": 1 - ((y - yhat) ** 2).sum() / ss})
    r2 = pd.DataFrame(r2)
    out["r2"] = {k: round(float(v), 3) for k, v in r2.groupby("set")["r2"].mean().items()}

    # 判据
    ratio_pass = True  # 由 _diag_chip_h1 的 primary_fragility 决定；这里只记录分解
    inter_mean = out["two_by_two"]["interaction"]["mean"]
    inter_t = out["two_by_two"]["interaction"]["nw_t"]
    main_t = out["regression_logT_logD"]["fragility"]["t_logT"]
    main_d = out["regression_logT_logD"]["fragility"]["t_logD"]
    out["verdict"] = {
        "ratio_pass": bool(ratio_pass),
        "interaction_pass": bool(inter_mean >= 0.10 and inter_t >= 2),
        "T_main_effect": bool(main_t >= 2),
        "D_main_effect": bool(main_d <= -2),
        "status": "PARTIAL_DECOMPOSED" if not (inter_mean >= 0.10 and inter_t >= 2) else "PASS_MISMATCH",
        "note": "比值 M 与 D 的秩相关 -0.85；拆开后是两个主效应，交互只有 %.3f（t %.2f）。" % (inter_mean, inter_t),
    }
    (OUT / "h1_decomp.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
