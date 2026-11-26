"""H2 分解：合成伽马失败后，拆开持有人分项，看符号地图是否反转。

输入：data/.scratch/chip_h2/panel.parquet（H2 合并面板）
输出：data/.scratch/chip_h2/h2_decomp.json + component_table.parquet

预注册之外的**探索性**分解，必须按探索性读：
  - 7 个分项 × 2 个结果 = 14 个检验；Bonferroni 阈值约 |t| ≥ 2.7。
  - 2025-04 ~ 2026-09，18 个月；top_float 只覆盖前十大；基金/法人份额与个人份额互补。
  - 不把「分项反转」写成结论；它是一个下一轮假设。

分项：
  fund_share / insurance_share / social_share / qfii_share / individual_share / corporate_share
  gamma_flow = −HOLDER_NUM_RATIO（户数下降=集中=正伽马）
合成反转：
  gamma_rev = z(fund + corporate) − z(individual) − z(gamma_flow)
  预期（反转假设）：dens × gamma_rev 对 fragility 为**正**（负伽马放大密度脆弱性）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h2"
NW_LAG = 3
N_PLACEBO = 200


def nw_t(x, lag: int = NW_LAG) -> float:
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


def z(x) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def main() -> None:
    p = pd.read_parquet(OUT / "panel.parquet")
    p = p.dropna(subset=["dens_z", "fwd_bp", "fragility", "to_z", "vol_z", "cap_z", "ret_z", "D_z", "H_z"])
    comps = ["fund_share", "insurance_share", "social_share", "qfii_share", "individual_share", "corporate_share", "gamma_flow"]

    # 分项 rank-z + 交互
    for c in comps:
        p[f"{c}_z"] = p.groupby("date")[c].transform(lambda x: z(x.rank(pct=True)))
    for c in comps:
        p[f"dx_{c}"] = p["dens_z"] * p[f"{c}_z"]

    # 反转合成
    p["gamma_rev"] = (
        z(p["fund_share"].fillna(0) + p["corporate_share"].fillna(0))
        - z(p["individual_share"].fillna(0))
        - z(p["gamma_flow"].fillna(0))
    )
    p["gamma_rev_z"] = p.groupby("date")["gamma_rev"].transform(z)
    p["dx_rev"] = p["dens_z"] * p["gamma_rev_z"]
    p.to_parquet(OUT / "panel_decomp.parquet", index=False)

    def reg(ycol: str, c: str):
        xcol = "gamma_rev_z" if c == "gamma_rev_z" else f"{c}_z"
        dxcol = "dx_rev" if c == "gamma_rev_z" else f"dx_{c}"
        rows = []
        for dt, g in p.groupby("date"):
            if len(g) < 200:
                continue
            cols = ["dens_z", xcol, dxcol,
                    "to_z", "vol_z", "cap_z", "ret_z", "D_z", "H_z"]
            X = np.column_stack([g[c].to_numpy(float) for c in cols] + [np.ones(len(g))])
            y = g[ycol].to_numpy(float)
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 200:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            rows.append({"date": dt, "b_main": b[0], "b_gamma": b[1], "b_int": b[2], "n": int(ok.sum())})
        return pd.DataFrame(rows)

    def summ(reg, col):
        return {"mean": round(float(reg[col].mean()), 4), "nw_t": round(float(nw_t(reg[col])), 2), "n": int(len(reg))}

    table = []
    for c in comps:
        rf = reg("fwd_bp", c)
        rg = reg("fragility", c)
        table.append({
            "comp": c,
            "coverage": round(float(p[c].notna().mean()), 3),
            "nonzero": round(float((p[c].fillna(0) > 0).mean()), 3),
            "fwd_int": summ(rf, "b_int"),
            "frag_int": summ(rg, "b_int"),
        })
    rrev_f = reg("fwd_bp", "gamma_rev_z")
    rrev_g = reg("fragility", "gamma_rev_z")
    table.append({
        "comp": "gamma_rev",
        "coverage": 1.0,
        "nonzero": 1.0,
        "fwd_int": summ(rrev_f, "b_int"),
        "frag_int": summ(rrev_g, "b_int"),
    })
    tab = pd.DataFrame(table)
    tab.to_parquet(OUT / "component_table.parquet", index=False)

    # 分项简单斜率：低/高三分位内 dens Q5-Q1
    slopes = []
    for c in comps + ["gamma_rev_z"]:
        col = f"{c}_z" if c != "gamma_rev_z" else "gamma_rev_z"
        for dt, g in p.groupby("date"):
            if len(g) < 300:
                continue
            g = g.copy()
            try:
                g["gq"] = pd.qcut(g[col], 3, labels=False, duplicates="drop")
                g["dq"] = pd.qcut(g["dens_z"], 5, labels=False, duplicates="drop")
            except Exception:
                continue
            for gq in (0, 2):
                x = g[g["gq"] == gq]
                q = x.groupby("dq").agg(f=("fwd_bp", "mean"), fr=("fragility", "mean"))
                if 0 in q.index and 4 in q.index:
                    slopes.append({"date": dt, "comp": c, "gq": gq,
                                   "fwd": q.loc[4, "f"] - q.loc[0, "f"],
                                   "frag": q.loc[4, "fr"] - q.loc[0, "fr"]})
    sl = pd.DataFrame(slopes)
    sl.to_parquet(OUT / "component_slopes.parquet", index=False)
    slope_out = {}
    for c in comps + ["gamma_rev_z"]:
        x = sl[sl["comp"] == c]
        slope_out[c] = {}
        for gq, name in ((0, "low"), (2, "high")):
            y = x[x["gq"] == gq]
            slope_out[c][name] = {
                "fwd": round(float(y["fwd"].mean()), 1), "fwd_t": round(float(nw_t(y["fwd"])), 2),
                "frag": round(float(y["frag"].mean()), 3), "frag_t": round(float(nw_t(y["frag"])), 2),
            }

    # 反转合成 OOS + placebo
    oos = {}
    for name, mask in (("2025", p["date"] < "2026-01-01"), ("2026", p["date"] >= "2026-01-01")):
        dates = set(p.loc[mask, "date"].unique())
        rf = rrev_f[rrev_f["date"].isin(dates)]
        rg = rrev_g[rrev_g["date"].isin(dates)]
        oos[name] = {"fwd": summ(rf, "b_int"), "frag": summ(rg, "b_int")}

    rng = np.random.default_rng(20261126)
    true_int = float(rrev_g["b_int"].mean())
    placebo = []
    for _ in range(N_PLACEBO):
        vals = []
        for dt, g in p.groupby("date"):
            if len(g) < 200:
                continue
            g = g.copy()
            g["gamma_rev_z"] = rng.permutation(g["gamma_rev_z"].to_numpy())
            g["dx_rev"] = g["dens_z"] * g["gamma_rev_z"]
            X = np.column_stack([g[c].to_numpy(float) for c in
                                 ["dens_z", "gamma_rev_z", "dx_rev", "to_z", "vol_z", "cap_z", "ret_z", "D_z", "H_z"]]
                                + [np.ones(len(g))])
            y = g["fragility"].to_numpy(float)
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 200:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            vals.append(b[2])
        placebo.append(float(np.nanmean(vals)))
    placebo = np.asarray(placebo)
    placebo_out = {"true_int": round(true_int, 4), "mean": round(float(placebo.mean()), 4),
                   "sd": round(float(placebo.std()), 4), "percentile": round(float((placebo < true_int).mean() * 100), 1)}

    out = {
        "sample": {"rows": int(len(p)), "dates": int(p["date"].nunique()), "symbols": int(p["symbol"].nunique())},
        "note": "探索性分解：7 分项 × 2 结果 = 14 个检验；Bonferroni |t|≈2.7。不把分项反转写成结论。",
        "components": tab.to_dict(orient="records"),
        "slopes": slope_out,
        "gamma_rev": {"fwd": summ(rrev_f, "b_int"), "frag": summ(rrev_g, "b_int"), "oos": oos, "placebo": placebo_out},
        "verdict": {
            "composite_fail": True,
            "component_reversal_suggestive": bool(
                tab.set_index("comp").loc["individual_share", "frag_int"]["mean"] < 0
                and tab.set_index("comp").loc["corporate_share", "frag_int"]["mean"] > 0
                and tab.set_index("comp").loc["gamma_flow", "frag_int"]["mean"] < 0
            ),
            "survives_bonferroni": False,
            "status": "EXPLORATORY_REVERSAL",
        },
    }
    (OUT / "h2_decomp.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
