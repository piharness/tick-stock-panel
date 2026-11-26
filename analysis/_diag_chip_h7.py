"""H7：Θ（过路费率）是滚续成本 → 脆弱性，不是收益。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
对象
  Θ = Π / D0，只在「上穿走廊」（cum5>0）上有定义：
    D0 = Σ_{k=5..19} TO_k · Φ(band)   穿越前已在走廊里的存量筹码（分母）
    Π  = Σ_{k=0..4}  TO_k · Φ(band)   穿越期真正落在走廊里的成交（分子）
  Θ 高 = 走廊里的筹码被换手了好几遍 = 新钱 = 久期短 = 更容易被迫卖。

机制
  旧 P2 问「Θ 高是否收益好」——问错了方向。正确的问题是「Θ 高是否更脆」。

判据（三层）
  L1 分解：Θ = Π/D0 是比值。先报 corr(Θ, D0)、corr(Θ, Π)、corr(Θ, cum5)、corr(Θ, to15)。
      若 Θ 只是分母 D0 的另一种写法（corr 接近 −1），比值不成立。
  L2 脆弱性：fragility ~ z(rank Θ) + z(D0) + z(Π) + controls；Θ 系数 >0 且 t≥2（高 Θ 更脆）。
  L3 不是收益：fwd20 ~ 同式；Θ 系数应 ≈ 0（|t|<2）。

杀死
  L1 比值只是分母；或 L2 不显著；或 L3 Θ 与 fwd20 强相关（旧 P2 只是没测对）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h7"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "data" / "kline_daily_enriched"
PANEL = ROOT / "data" / ".scratch" / "chip_medium" / "panel.parquet"

NW_LAG = 6
THETA_LO, THETA_HI = 0.01, 50.0


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


def zscore(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def skewness(a):
    a = np.asarray(a, dtype=float)
    mu = a.mean(axis=0)
    d = a - mu
    m2 = (d ** 2).mean(axis=0)
    m3 = (d ** 3).mean(axis=0)
    return m3 / np.maximum(m2, 1e-18) ** 1.5


def main():
    print("[1/4] chip_medium events ...", flush=True)
    cols = ["date", "symbol", "THETA", "D0", "PI", "cum5", "to15", "vol20", "ln_mcap", "fwd20", "exc20"]
    p = pd.read_parquet(PANEL, columns=cols)
    p["date"] = pd.to_datetime(p["date"])
    p["symbol"] = p["symbol"].astype(str)
    p = p[(p["THETA"].notna()) & (p["THETA"] > THETA_LO) & (p["THETA"] < THETA_HI)]
    p = p[p["cum5"] > 0]
    p = p.dropna(subset=["D0", "PI", "to15", "vol20", "ln_mcap", "fwd20"])
    print(f"      events={len(p):,}  dates={p['date'].nunique()}  symbols={p['symbol'].nunique()}", flush=True)

    print("[2/4] forward fragility from daily ...", flush=True)
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "open", "close"],
                   filter=ds.field("date") >= pd.Timestamp("2024-10-01").date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    O = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    C = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index().reindex(O.index)
    dates = pd.DatetimeIndex(O.index)
    syms = np.asarray(O.columns)
    Om = O.to_numpy(dtype=np.float64)
    Cm = C.to_numpy(dtype=np.float64)
    # 事件 → (tidx, col)
    p = p[p["symbol"].isin(syms)]
    p["tidx"] = p["date"].map({dt: i for i, dt in enumerate(dates)})
    p = p.dropna(subset=["tidx"])
    p["tidx"] = p["tidx"].astype(int)
    p["cidx"] = p["symbol"].map({s: i for i, s in enumerate(syms)})
    p = p.dropna(subset=["cidx"])
    p["cidx"] = p["cidx"].astype(int)
    print(f"      matched events={len(p):,}", flush=True)

    ti = p["tidx"].to_numpy()
    ci = p["cidx"].to_numpy()
    H = 20
    n = len(ti)
    mdd = np.full(n, np.nan)
    sk = np.full(n, np.nan)
    w5 = np.full(n, np.nan)
    dvol = np.full(n, np.nan)
    for t in np.unique(ti):
        sel = np.nonzero(ti == t)[0]
        idx = ci[sel]
        if t + H + 2 > Cm.shape[0]:
            continue
        p0 = Om[t + 1, idx]
        pp = Cm[t + 1:t + H + 2, idx]
        if pp.shape[0] < H + 1:
            continue
        good = np.isfinite(p0) & (p0 > 0) & np.isfinite(pp).all(axis=0)
        if not good.any():
            continue
        p0 = p0[good]
        pp = pp[:, good]
        r1 = pp[0] / p0 - 1.0
        r = np.vstack([r1, pp[1:] / pp[:-1] - 1.0])
        r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
        path = np.vstack([p0, pp])
        peak = np.maximum.accumulate(path, axis=0)
        dd = path / peak - 1.0
        sw = np.lib.stride_tricks.sliding_window_view(r, 5, axis=0)
        ww = np.prod(1.0 + sw, axis=-1) - 1.0
        mdd[sel[good]] = dd.min(axis=0)
        sk[sel[good]] = skewness(r)
        w5[sel[good]] = ww.min(axis=0)
        dvol[sel[good]] = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2, axis=0))
    p["mdd20"] = mdd
    p["skew20"] = sk
    p["worst5"] = w5
    p["dvol20"] = dvol
    p = p.dropna(subset=["mdd20", "skew20", "worst5", "dvol20"])
    p["ym"] = p["date"].dt.to_period("M").astype(str)
    print(f"      final events={len(p):,}", flush=True)

    print("[3/4] L1 decomposition ...", flush=True)
    p["logD0"] = np.log(p["D0"].clip(lower=1e-6))
    p["logPI"] = np.log(p["PI"].clip(lower=1e-6))
    p["logTHETA"] = np.log(p["THETA"])
    corr = {}
    for a, b in [("THETA", "D0"), ("THETA", "PI"), ("THETA", "cum5"), ("THETA", "to15"),
                 ("logTHETA", "logD0"), ("logTHETA", "logPI"), ("logTHETA", "cum5")]:
        x = p[[a, b]].dropna()
        corr[f"{a}_vs_{b}"] = round(float(x.corr(method="spearman").iloc[0, 1]), 3)

    print("[4/4] monthly regression ...", flush=True)
    # 月度截面 z：fragility 复合
    frag_parts = []
    for ym, g in p.groupby("ym"):
        z = pd.DataFrame(index=g.index)
        z["fragility"] = (zscore(-g["mdd20"].to_numpy()) + zscore(-g["skew20"].to_numpy()) +
                          zscore(-g["worst5"].to_numpy()) + zscore(g["dvol20"].to_numpy())) / 4.0
        frag_parts.append(z)
    p = p.join(pd.concat(frag_parts))

    def monthly_reg(ycol, with_theta=True):
        rows = []
        for ym, g in p.groupby("ym"):
            if len(g) < 100:
                continue
            g = g.copy()
            g["r_theta"] = g.groupby("date")["THETA"].rank(pct=True)
            g["r_D0"] = g.groupby("date")["D0"].rank(pct=True)
            g["r_PI"] = g.groupby("date")["PI"].rank(pct=True)
            cols = [zscore(g["r_theta"]), zscore(g["r_D0"]), zscore(g["r_PI"]),
                    zscore(g["cum5"]), zscore(g["to15"]), zscore(g["vol20"]), zscore(g["ln_mcap"]), np.ones(len(g))]
            X = np.column_stack(cols)
            y = zscore(g[ycol].to_numpy())
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 100:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            rows.append({"ym": ym, "b_theta": b[0], "b_D0": b[1], "b_PI": b[2], "n": int(ok.sum())})
        return pd.DataFrame(rows)

    rf = monthly_reg("fragility")
    rr = monthly_reg("fwd20")
    # 不加 θ 的对照（D0+PI 已经够不够）
    def monthly_reg_noth(ycol):
        rows = []
        for ym, g in p.groupby("ym"):
            if len(g) < 100:
                continue
            g = g.copy()
            g["r_D0"] = g.groupby("date")["D0"].rank(pct=True)
            g["r_PI"] = g.groupby("date")["PI"].rank(pct=True)
            X = np.column_stack([zscore(g["r_D0"]), zscore(g["r_PI"]),
                                 zscore(g["cum5"]), zscore(g["to15"]), zscore(g["vol20"]), zscore(g["ln_mcap"]), np.ones(len(g))])
            y = zscore(g[ycol].to_numpy())
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 100:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            rows.append({"ym": ym, "b_D0": b[0], "b_PI": b[1], "n": int(ok.sum())})
        return pd.DataFrame(rows)

    rf_noth = monthly_reg_noth("fragility")

    summary = {
        "sample": {"events": int(len(p)), "months": int(p["ym"].nunique()),
                   "symbols": int(p["symbol"].nunique()),
                   "start": str(p["date"].min().date()), "end": str(p["date"].max().date()),
                   "theta_median": round(float(p["THETA"].median()), 2),
                   "theta_p90": round(float(p["THETA"].quantile(0.9)), 2)},
        "L1": corr,
        "L2_fragility": {
            "b_theta": round(float(rf["b_theta"].mean()), 4),
            "t_theta": round(float(nw_t(rf["b_theta"])), 2),
            "b_D0": round(float(rf["b_D0"].mean()), 4),
            "t_D0": round(float(nw_t(rf["b_D0"])), 2),
            "b_PI": round(float(rf["b_PI"].mean()), 4),
            "t_PI": round(float(nw_t(rf["b_PI"])), 2),
            "months": int(len(rf)),
        },
        "L2_no_theta_D0": {
            "b_D0": round(float(rf_noth["b_D0"].mean()), 4),
            "t_D0": round(float(nw_t(rf_noth["b_D0"])), 2),
        },
        "L3_return": {
            "b_theta": round(float(rr["b_theta"].mean()), 4),
            "t_theta": round(float(nw_t(rr["b_theta"])), 2),
            "b_D0": round(float(rr["b_D0"].mean()), 4),
            "b_PI": round(float(rr["b_PI"].mean()), 4),
        },
    }
    (OUT / "h7.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
