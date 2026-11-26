"""H6：记忆相变，不是线性衰减。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
机制
  当市场久期 D_m 低于阈值，库存机制被流量机制压倒，筹码结构失去预测力。
  这是「相变」（不连续），不是「线性衰减」（连续）。

测量
  月频：D_m = 月中位久期；ρ_m = 截面 Spearman(D, fragility) —— 筹码结构对脆弱性的预测力。
  fragility = z(−mdd20)、z(−skew20)、z(−worst5)、z(+dvol20) 的均值（H1 同口径）。

判据（三层）
  L1 存在阈值：ρ_m ~ 1[D_m < θ] 的 BIC 比 ρ_m ~ D_m 线性至少低 10（阈值模型显著更优）。
  L2 方向：低 D_m 段 ρ_m 显著低于高 D_m 段（结构被流量压倒，ρ 掉下来）。
  L3 不是牛熊：控制当月市场收益后，低 D_m 段的 ρ_m 下降仍在。

杀死
  L1 无阈值（BIC 差 < 10），或 L2 方向相反，或 L3 控制后消失（阈值只是牛熊日历）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h6"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "data" / "kline_daily_enriched"
H1 = ROOT / "data" / ".scratch" / "chip_h1" / "panel.parquet"


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


def spearman(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 30:
        return np.nan
    xr = pd.Series(x[m]).rank().to_numpy()
    yr = pd.Series(y[m]).rank().to_numpy()
    return float(np.corrcoef(xr, yr)[0, 1])


def bic_from_rss(rss, n, p):
    return n * np.log(rss / n) + p * np.log(n)


def main():
    print("[1/3] H1 panel → monthly ...", flush=True)
    p = pd.read_parquet(H1)
    p["date"] = pd.to_datetime(p["date"])
    p["ym"] = p["date"].dt.to_period("M").astype(str)
    rows = []
    for ym, g in p.groupby("ym"):
        if len(g) < 100:
            continue
        Dm = g["D"].median()
        rho_frag = spearman(g["D"], g["fragility"])
        rho_mdd = spearman(g["D"], g["mdd20"])
        rho_skew = spearman(g["D"], g["skew20"])
        rho_fwd = spearman(g["D"], g["fwd20"])
        rows.append({"ym": ym, "date": g["date"].max(), "D_m": Dm,
                     "rho_frag": rho_frag, "rho_mdd": rho_mdd,
                     "rho_skew": rho_skew, "rho_fwd": rho_fwd,
                     "mkt_fwd": g["fwd20"].mean()})
    mon = pd.DataFrame(rows).dropna(subset=["D_m", "rho_frag"]).sort_values("date")
    print(f"      months={len(mon)}  D_m [{mon['D_m'].min():.0f}, {mon['D_m'].max():.0f}]", flush=True)

    print("[2/3] market return (daily) ...", flush=True)
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "close"], filter=ds.field("date") >= pd.Timestamp("2018-06-01").date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    C = cl.to_numpy(dtype=np.float64)
    r = np.vstack([np.zeros((1, C.shape[1])), C[1:] / C[:-1] - 1.0])
    mkt_d = np.nanmean(r, axis=1)
    mkt_s = pd.Series(mkt_d, index=cl.index)
    monthly = (1 + mkt_s).groupby(mkt_s.index.to_period("M")).prod() - 1
    monthly.index = monthly.index.astype(str)
    mon["mkt_ret"] = mon["ym"].map(monthly)
    mon = mon.dropna(subset=["mkt_ret"])
    mon.to_parquet(OUT / "monthly.parquet", index=False)
    print(f"      months with mkt={len(mon)}", flush=True)

    print("[3/3] threshold test ...", flush=True)
    y = mon["rho_frag"].to_numpy()
    x = mon["D_m"].to_numpy()
    n = len(mon)

    def ols_nw_t(X, y, idx, lag=6):
        X = np.column_stack([X, np.ones(len(y))])
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        e = y - X @ b
        # NW 方差
        n2 = len(y)
        s = (e * e).mean()
        sxx = (X.T @ X) / n2
        for l in range(1, lag + 1):
            g = (e[l:] * e[:-l])
            sxx_l = (X[l:].T @ X[:-l]) / n2
            s += 2 * (1 - l / (lag + 1)) * g.mean()
        v = np.linalg.inv(sxx) * s / n2
        se = np.sqrt(np.diag(v))
        return b[idx], b[idx] / se[idx] if se[idx] > 0 else np.nan

    # 线性
    Xl = np.column_stack([x, np.ones(n)])
    bl, *_ = np.linalg.lstsq(Xl, y, rcond=None)
    rss_l = ((y - Xl @ bl) ** 2).sum()
    bic_l = bic_from_rss(rss_l, n, 2)
    # 阈值网格
    best = {"bic": np.inf}
    for q in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        th = float(np.quantile(x, q))
        d = (x < th).astype(float)
        Xt = np.column_stack([d, np.ones(n)])
        bt, *_ = np.linalg.lstsq(Xt, y, rcond=None)
        rss_t = ((y - Xt @ bt) ** 2).sum()
        bic_t = bic_from_rss(rss_t, n, 3)
        if bic_t < best["bic"]:
            best = {"theta": th, "bic": bic_t, "b_low": float(bt[0] + bt[1]), "b_high": float(bt[1]), "q": q}
    # 简单二分 + 控制市场
    med = np.median(x)
    dd = (x < med).astype(float)
    lo = mon[mon["D_m"] < med]
    hi = mon[mon["D_m"] >= med]
    b_ind, t_ind = ols_nw_t(np.column_stack([dd, mon["mkt_ret"].to_numpy()]), y, 0)
    # 线性关系 + 市场控制
    b_lin, t_lin = ols_nw_t(np.column_stack([x, mon["mkt_ret"].to_numpy()]), y, 0)

    summary = {
        "sample": {"months": int(n), "D_m_range": [round(float(mon['D_m'].min()), 1), round(float(mon['D_m'].max()), 1)]},
        "L1": {
            "bic_linear": round(float(bic_l), 1),
            "bic_threshold": round(float(best["bic"]), 1),
            "bic_diff": round(float(best["bic"] - bic_l), 1),
            "best_theta": round(float(best["theta"]), 1),
            "best_quantile": best["q"],
        },
        "L2": {
            "rho_low_Dm": round(float(lo["rho_frag"].mean()), 3),
            "rho_high_Dm": round(float(hi["rho_frag"].mean()), 3),
            "diff": round(float(lo["rho_frag"].mean() - hi["rho_frag"].mean()), 3),
            "low_Dm_months": int(len(lo)),
            "high_Dm_months": int(len(hi)),
        },
        "L3": {
            "b_lowDm_controlling_mkt": round(float(b_ind), 3),
            "t_lowDm": round(float(t_ind), 2),
            "b_Dm_linear_controlling_mkt": round(float(b_lin), 3),
            "t_Dm_linear": round(float(t_lin), 2),
        },
        "corr_Dm_mkt": round(float(mon[["D_m", "mkt_ret"]].corr(method="spearman").iloc[0, 1]), 3),
        "rho_frag_by_year": {str(yy): round(float(g["rho_frag"].mean()), 3)
                             for yy, g in mon.groupby(mon["date"].dt.year)},
    }
    (OUT / "h6.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
