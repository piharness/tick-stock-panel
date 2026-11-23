"""第 3 阶段 · 收口：把最后一刀（行业中性后 +30.9bp）也测掉。

两问：
  Q1 行业中性后的 +30.9bp 是不是集中在少数几个行业？（若是，N 只有 2-3，不可执行）
  Q2 在**同一行业内**做特征孪生匹配，路径增量还剩多少？（若归零，则超额全部是
     低波/低换手/行业的组合，没有形态信息）

复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_final.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_p3_residualize import nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "p3"
FEATS = ["vol20", "turn20", "idio_vol60", "log_amt20", "log_mcap"]
KNN = 3


def main() -> None:
    pdf = (pl.read_parquet(OUT / "resid_table.parquet",
                           columns=["date", "sig", "ex15", "industry"] + FEATS)
           .filter(pl.col("date") >= pl.date(2018, 9, 1))
           .to_pandas())
    pdf = pdf[np.isfinite(pdf.ex15)]
    for c in FEATS:
        pdf[c] = pdf[c].astype(float)

    # ---------- Q1 行业中性后的贡献分布 ----------
    ind_mean = pdf.groupby(["date", "industry"]).ex15.transform("mean")
    pdf["ex_ind"] = pdf.ex15 - ind_mean
    sig = pdf[pdf.sig == 1]
    daily = sig.groupby("date").ex_ind.mean()
    mu, t = nw_t(daily.to_numpy())
    contrib = (sig.groupby("industry")
               .agg(n=("ex_ind", "size"), sum_bp=("ex_ind", lambda x: x.sum() * 1e4),
                    mean_bp=("ex_ind", lambda x: x.mean() * 1e4))
               .sort_values("sum_bp", ascending=False))
    total = contrib.sum_bp.sum()
    top3 = float(contrib.sum_bp.head(3).sum() / total)
    pos = contrib[contrib.sum_bp > 0]
    # 去掉贡献最大的 2 个行业后重算
    drop = contrib.index[:2].tolist()
    d2 = sig[~sig.industry.isin(drop)].groupby("date").ex_ind.mean()
    mu_d2, t_d2 = nw_t(d2.to_numpy())

    # ---------- Q2 同行业内孪生 ----------
    rows = []
    for d, g in pdf.groupby("date", sort=True):
        g = g.dropna(subset=FEATS)
        if len(g) < 300:
            continue
        s = g.sig.to_numpy() == 1
        if s.sum() < 1:
            continue
        ex = g.ex15.to_numpy()
        ind = g.industry.to_numpy()
        Z = g[FEATS].to_numpy()
        mu0, sd0 = np.nanmean(Z, axis=0), np.nanstd(Z, axis=0)
        sd0 = np.where(sd0 > 1e-12, sd0, 1.0)
        Z = np.clip((Z - mu0) / sd0, -3, 3)
        if not np.isfinite(Z).all():
            continue
        diffs = []
        for i in np.flatnonzero(s):
            pool = (~s) & (ind == ind[i])
            if pool.sum() < 15:
                continue
            zp, zs = Z[pool], Z[i]
            dd = ((zp - zs) ** 2).sum(axis=1)
            k = min(KNN, dd.size)
            nn = np.argpartition(dd, k - 1)[:k]
            diffs.append(ex[i] - ex[pool][nn].mean())
        if diffs:
            rows.append({"date": d, "n": len(diffs), "diff": float(np.mean(diffs))})
    dl = pd.DataFrame(rows)
    mu_w, t_w = nw_t(dl["diff"].to_numpy())

    res = {
        "industry_neutral": {"bp": mu * 1e4, "t": t, "days": int(daily.size)},
        "industry_table_top": contrib.head(8).round(1).reset_index().to_dict("records"),
        "industries": int(len(contrib)),
        "industries_with_n100": int((contrib.n >= 100).sum()),
        "top3_share_of_total": top3,
        "drop_top2": {"dropped": drop, "bp": mu_d2 * 1e4, "t": t_d2},
        "within_industry_twin": {"bp": mu_w * 1e4, "t": t_w,
                                 "days": int(dl.date.nunique()) if len(dl) else 0,
                                 "signals": int(dl.n.sum()) if len(dl) else 0},
    }
    json.dump(res, open(OUT / "final_close.json", "w"), ensure_ascii=False, indent=1)

    print(f"Q1 行业中性后: {mu*1e4:.1f}bp  t={t:.2f}  (天数 {daily.size})")
    print(contrib.head(8).round(1).to_string())
    print(f"   行业数 {len(contrib)}，n>=100 的 {int((contrib.n>=100).sum())} 个，"
          f"top3 占总贡献 {top3*100:.0f}%")
    print(f"   去掉 {drop} 后: {mu_d2*1e4:.1f}bp  t={t_d2:.2f}")
    print(f"Q2 同行业内孪生: 增量 {mu_w*1e4:.1f}bp  t={t_w:.2f}  "
          f"(信号 {int(dl.n.sum()) if len(dl) else 0})")


if __name__ == "__main__":
    main()
