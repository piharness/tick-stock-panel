"""step3: 形状特征对未来 10 日「涨 50%」的判别力。

口径：
- base rate = 全样本 P(y10=1)，约 0.49%（1/203）。
- 每个特征按全局 10 分位分桶；lift = 桶内命中率 / 基率。
- 显著性用「日频序列」而不是股票-日：同一天全市场高度相关，
  把每天当成一个观测（桶内当日命中率），t = mean/std*sqrt(天数)。
  股票-日口径的 z 值会虚高 10-30 倍。
- 分三个子样本，因为「启动前」和「启动中」是两种完全不同的预测问题：
    ALL   全样本
    QUIET r10 < 5%（还没启动，真正的「提前抓」）
    HOT   r10 >= 20%（已经在涨，检验「追不追」）
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
F = DATA / "_diag_surge50_features.parquet"
OUT = DATA / "_diag_surge50_shape_step3.json"

FEATS = {
    "r1": "当日涨幅",
    "r5": "5日涨幅",
    "r10": "10日涨幅",
    "r20": "20日涨幅",
    "r60": "60日涨幅",
    "ma20_d": "距MA20",
    "ma60_d": "距MA60",
    "ma_spread": "MA20/MA60",
    "dist_hi250": "距250日高",
    "dist_lo250": "距250日低",
    "dd20": "距20日高(回撤)",
    "cv20": "20日波动率",
    "cv60": "60日波动率",
    "comp_ratio": "波动压缩比20/60",
    "amp20": "20日均振幅",
    "amp5": "5日均振幅",
    "body20": "实体占比",
    "upsh20": "上影占比",
    "dnsh20": "下影占比",
    "gap20": "20日跳空次数",
    "updays20": "20日上涨天数",
    "vol_r5_20": "量比5/20",
    "to20_pct": "换手分位",
    "amt_pct": "成交额分位",
    "cv60_pct": "波动分位",
    "hi250_pct": "高位分位",
    "limit_like": "当日近涨停",
    "age": "上市天数",
}

cols = ["symbol", "date", "y10", "y10_end_ret", "fwd_ret10", "close"] + list(FEATS)
df = pd.read_parquet(F, columns=cols)
df = df[df["y10"].notna()].reset_index(drop=True)
df["y10"] = df["y10"].astype(np.int8)
date_code, uniq = pd.factorize(df["date"], sort=True)
D = len(uniq)
y = df["y10"].values.astype(np.float64)
fr = df["fwd_ret10"].values.astype(np.float64)
print(f"rows {len(df):,}  days {D}  base {y.mean()*100:.4f}%", flush=True)

r10 = df["r10"].fillna(0.0).values
subs = {
    "ALL": np.ones(len(df), dtype=bool),
    "QUIET": r10 < 0.05,
    "HOT": r10 >= 0.20,
}


def bucket_daily(b: np.ndarray, nb: int, mask: np.ndarray):
    """按 (日期, 桶) 聚合命中数与样本数 → (D, nb) 两个矩阵。"""
    m = mask & (b >= 0)
    key = date_code[m].astype(np.int64) * nb + b[m]
    num = np.bincount(key, weights=y[m], minlength=D * nb).reshape(D, nb)
    cnt = np.bincount(key, minlength=D * nb).reshape(D, nb)
    return num, cnt


def tstat(num: np.ndarray, cnt: np.ndarray) -> tuple[float, float, int]:
    ok = cnt > 0
    nd = int(ok.sum())
    if nd < 30:
        return (np.nan, np.nan, nd)
    rate = num[ok] / cnt[ok]
    mu, sd = rate.mean(), rate.std(ddof=1)
    return (float(mu), float(mu / (sd / np.sqrt(nd))) if sd > 0 else np.nan, nd)


report = {}
for sub_name, sub_mask in subs.items():
    base = float(y[sub_mask].mean())
    feats_out = {}
    for feat, label in FEATS.items():
        x = df[feat].values.astype(np.float64)
        valid = np.isfinite(x)
        if valid.sum() < 10000:
            continue
        qs = np.unique(np.nanquantile(x[valid], np.linspace(0, 1, 11)))
        if len(qs) < 4:
            continue
        nb = len(qs) - 1
        b = np.digitize(x, qs[1:-1], right=False)
        b[~valid] = -1
        num, cnt = bucket_daily(b, nb, sub_mask)
        rows = []
        for k in range(nb):
            n = int(cnt[:, k].sum())
            if n < 200:
                continue
            mu, t, nd = tstat(num[:, k], cnt[:, k])
            mm = (b == k) & sub_mask
            hr = float(y[mm].mean())
            rows.append(
                dict(
                    bucket=k, lo=float(qs[k]), hi=float(qs[k + 1]), n=n,
                    hit=round(hr * 100, 4),
                    lift=round(hr / base, 3) if base > 0 else None,
                    t=round(t, 2) if np.isfinite(t) else None,
                    days=nd,
                    fwd_ret=round(float(np.nanmean(fr[mm])) * 100, 3),
                )
            )
        if not rows:
            continue
        best = max(rows, key=lambda r: r["hit"])
        worst = min(rows, key=lambda r: r["hit"])
        feats_out[feat] = dict(
            label=label, buckets=rows, best=best, worst=worst,
            spread_lift=round(best["hit"] / worst["hit"], 2) if worst["hit"] > 0 else None,
        )
    report[sub_name] = dict(base_rate_pct=round(base * 100, 4), n=int(sub_mask.sum()), feats=feats_out)
    print(f"[{sub_name}] n={int(sub_mask.sum()):,} base={base*100:.4f}%", flush=True)

ym = pd.to_datetime(df["date"]).dt.to_period("M").astype(str)
monthly = df.groupby(ym)["y10"].agg(["count", "sum", "mean"]).reset_index()
monthly["hit_pct"] = (monthly["mean"] * 100).round(3)
monthly = monthly.drop(columns=["mean"]).astype({"count": int, "sum": int})
report["monthly"] = monthly.to_dict("records")
nz = monthly[monthly["sum"] > 0]
report["regime"] = {
    "max_month": monthly.loc[monthly["hit_pct"].idxmax()].to_dict(),
    "min_month_nonzero": nz.nsmallest(1, "hit_pct").to_dict("records")[0],
    "ratio_max_min": round(float(monthly["hit_pct"].max() / nz["hit_pct"].min()), 1),
    "median_month_pct": round(float(monthly["hit_pct"].median()), 3),
}

Path(OUT).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("saved", OUT, flush=True)

for sub_name in subs:
    r = report[sub_name]
    print(f"\n==== {sub_name}  base={r['base_rate_pct']}%  n={r['n']:,} ====")
    order = sorted(r["feats"].items(), key=lambda kv: -(kv[1]["best"]["lift"] or 0))
    for feat, d in order[:10]:
        b = d["best"]
        w = d["worst"]
        print(
            f"{feat:12s} {d['label']:12s} best={b['bucket']} hit={b['hit']:6.3f}% "
            f"lift={b['lift']:5.2f} t={str(b['t']):>6s} n={b['n']:>8,} fwd10={b['fwd_ret']:6.2f}% "
            f"|| worst hit={w['hit']:6.3f}% fwd10={w['fwd_ret']:6.2f}%"
        )
print("\nregime:", report["regime"], flush=True)
