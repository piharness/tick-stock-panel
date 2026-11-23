"""第 3 阶段 · 匹配对照：同一个特征空间里，换个路径还赚不赚。

回归控制是线性的、27 个一起上，可能过度控制（vol20 / idio_vol60 本身就含 5 日窗口的
标准差，与 snr5/eff5 机械重叠）。这里改用非参匹配：

  对每个信号股 i，在同日、非信号、basic_ok 池里，按 [vol20, turn20, idio_vol60,
  log_amt20, log_mcap] 的当日 z 值找 3 个最近邻，取它们 15 日超额的均值作为"特征孪生"。
  增量 = ex15(i) − mean(ex15(孪生))，逐日聚合后 NW(15)。

若增量 ≈ 0 ⇒ 分类器只是特征的重新组合；若增量 ≈ +40bp ⇒ 路径本身有信息，回归是过度控制。
复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_twin.py
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
                           columns=["date", "sig", "ex15"] + FEATS)
           .filter(pl.col("date") >= pl.date(2018, 9, 1))
           .to_pandas())
    pdf = pdf[np.isfinite(pdf.ex15)]
    for c in FEATS:
        pdf[c] = pdf[c].astype(float)

    rows = []
    for d, g in pdf.groupby("date", sort=True):
        g = g.dropna(subset=FEATS)
        if len(g) < 300:
            continue
        sig = g.sig.to_numpy() == 1
        if sig.sum() < 1 or (~sig).sum() < 200:
            continue
        Z = g[FEATS].to_numpy()
        mu = np.nanmean(Z, axis=0)
        sd = np.nanstd(Z, axis=0)
        sd = np.where(sd > 1e-12, sd, 1.0)
        Z = np.clip((Z - mu) / sd, -3, 3)
        ex = g.ex15.to_numpy()
        zp, zs = Z[~sig], Z[sig]
        if not np.isfinite(zp).all() or not np.isfinite(zs).all():
            continue
        D = ((zp[:, None, :] - zs[None, :, :]) ** 2).sum(axis=2)   # pool × sig
        k = min(KNN, zp.shape[0])
        idx = np.argpartition(D, k - 1, axis=0)[:k]                  # k × sig
        twin = ex[~sig][idx].mean(axis=0)
        rows.append({"date": d, "n": int(sig.sum()),
                     "raw": float(ex[sig].mean()), "twin": float(twin.mean()),
                     "diff": float((ex[sig] - twin).mean())})

    dl = pd.DataFrame(rows)
    dl.to_csv(OUT / "twin_daily.csv", index=False)
    out = {}
    for col in ("raw", "twin", "diff"):
        mu, t = nw_t(dl[col].to_numpy())
        out[col] = {"bp": mu * 1e4, "t": t}
    out["days"] = int(dl.date.nunique())
    out["signals"] = int(dl.n.sum())
    # 分年
    dl["year"] = pd.to_datetime(dl.date).dt.year
    yr = []
    for y, g in dl.groupby("year"):
        mu, t = nw_t(g["diff"].to_numpy())
        yr.append({"year": int(y), "diff_bp": mu * 1e4, "t": t, "days": int(len(g))})
    out["yearly_diff"] = yr
    json.dump(out, open(OUT / "twin.json", "w"), ensure_ascii=False, indent=1)

    print(json.dumps({k: v for k, v in out.items() if k != "yearly_diff"}, ensure_ascii=False, indent=1))
    print(pd.DataFrame(yr).round(1).to_string(index=False))


if __name__ == "__main__":
    main()
