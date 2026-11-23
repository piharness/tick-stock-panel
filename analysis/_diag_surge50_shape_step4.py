"""step4: 把「形状」写成可检验的形态定义，横向比较谁最强。

候选形状（全部只用 t 日收盘及以前的信息）：
  F1 旗形      前 10 日（t-20..t-10）涨 >=15%，近 10 日横盘 <5%，仍站上 MA20
  F2 箱体      20 日振幅极小（amp20 最低 10%），且距 250 日高 <10%
  F3 深跌反转  距 250 日低 <15%，且 20 日波动率最高 20%
  F4 缩量加速  已涨 >=20%（HOT），但 5 日均振幅最低 20%（分歧最小）
  F5 跳空启动  近 20 日跳空 >=3 次，且当日涨幅 >5%
  F6 新高放量  创 250 日新高，且成交额分位 >0.9

每个形状报：命中率、lift、日频 t、T+1 开盘买入 10 日平均收益，
以及剔除「T+1 一字/涨停开盘」（买不到）之后的版本。
另做波动率分层（cv60 三分位）内的增量，检验形状是不是只是波动率暴露。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
F = DATA / "_diag_surge50_features.parquet"
OUT = DATA / "_diag_surge50_shape_step4.json"

cols = [
    "symbol", "date", "y10", "y10_end_ret", "fwd_ret10", "close", "open", "high", "low",
    "r1", "r5", "r10", "r20", "r60", "ma20_d", "ma60_d", "ma_spread",
    "dist_hi250", "dist_lo250", "dd20", "cv20", "cv60", "comp_ratio",
    "amp20", "amp5", "body20", "upsh20", "dnsh20", "gap20", "updays20",
    "vol_r5_20", "to20_pct", "amt_pct", "cv60_pct", "hi250_pct", "age", "rn",
]
df = pd.read_parquet(F, columns=cols)
df = df[df["y10"].notna()].reset_index(drop=True)
df["y10"] = df["y10"].astype(np.int8)
df["date"] = pd.to_datetime(df["date"])
date_code, uniq = pd.factorize(df["date"], sort=True)
D = len(uniq)
y = df["y10"].values.astype(np.float64)
fr = df["fwd_ret10"].values.astype(np.float64)

# 派生：前 10 日涨幅（t-20 -> t-10），T+1 开盘涨幅
r10 = df["r10"].values
r20 = df["r20"].values
with np.errstate(invalid="ignore", divide="ignore"):
    df["r10_prev"] = (1 + r20) / (1 + r10) - 1
    df["next_gap"] = (1 + fr) / (1 + df["y10_end_ret"].values) - 1


# 涨跌停近似（按板块判断开盘可成交）
def limit_pct(sym: str) -> float:
    if sym.startswith(("688", "689")):
        return 0.20
    if sym.startswith(("300", "301", "302")):
        return 0.20
    if sym.startswith(("8", "4", "920")):
        return 0.30
    return 0.10


lp = df["symbol"].map(limit_pct).values
df["untradable"] = df["next_gap"].values >= (lp - 0.005)

base_all = float(y.mean())
print(f"rows {len(df):,} base {base_all*100:.4f}%  fwd10 mean {np.nanmean(fr)*100:.3f}%", flush=True)


def daily_stats(mask: np.ndarray) -> dict:
    m = mask & np.isfinite(y)
    n = int(m.sum())
    if n < 100:
        return dict(n=0)
    hr = float(y[m].mean())
    dc = date_code[m]
    num = np.bincount(dc, weights=y[m], minlength=D)
    cnt = np.bincount(dc, minlength=D)
    ok = cnt > 0
    rate = num[ok] / cnt[ok]
    t = float(rate.mean() / (rate.std(ddof=1) / np.sqrt(ok.sum()))) if ok.sum() > 30 and rate.std(ddof=1) > 0 else np.nan
    # 收益口径：全部买 / 剔除 T+1 涨停开盘
    tr = df["untradable"].values
    fr_ok = fr[m]
    fwd = float(np.nanmean(fr_ok)) if np.isfinite(fr_ok).any() else np.nan
    m2 = m & ~tr
    fwd_tr = float(np.nanmean(fr[m2])) if m2.sum() > 0 else np.nan
    return dict(
        n=n,
        hit_pct=round(hr * 100, 4),
        lift=round(hr / base_all, 2),
        t=round(t, 2) if np.isfinite(t) else None,
        days=int(ok.sum()),
        fwd10_pct=round(fwd * 100, 3) if np.isfinite(fwd) else None,
        n_tradable=int(m2.sum()),
        fwd10_tradable_pct=round(fwd_tr * 100, 3) if np.isfinite(fwd_tr) else None,
        hit_tradable_pct=round(float(y[m2].mean()) * 100, 4) if m2.sum() > 0 else None,
    )


# ---- 基线 ----
baselines = {
    "ALL": daily_stats(np.ones(len(df), dtype=bool)),
    "QUIET_r10<5%": daily_stats(r10 < 0.05),
    "HOT_r10>=20%": daily_stats(r10 >= 0.20),
}
print(json.dumps(baselines, ensure_ascii=False, indent=1), flush=True)

q = r10 < 0.05
shapes = {
    "F1_旗形_前期涨15_近10日横盘_站上MA20": q & (df["r10_prev"].values > 0.15) & (df["ma20_d"].values > 0),
    "F1a_旗形_放宽_前期涨10": q & (df["r10_prev"].values > 0.10) & (df["ma20_d"].values > 0),
    "F1b_旗形_加缩量": q & (df["r10_prev"].values > 0.15) & (df["ma20_d"].values > 0)
    & (df["amp5"].values / np.where(df["amp20"].values > 0, df["amp20"].values, np.nan) < 0.8),
    "F2_箱体_振幅最低10%_近250日高": (df["amp20"].values <= np.nanquantile(df["amp20"].values, 0.10))
    & (df["dist_hi250"].values > -0.10),
    "F3_深跌_距250日低15%内_高波动": (df["dist_lo250"].values < 0.15) & (df["cv60_pct"].values > 0.80),
    "F4_缩量加速_HOT_振幅最低20%": (r10 >= 0.20) & (df["amp5"].values <= np.nanquantile(df["amp5"].values[r10 >= 0.20], 0.20)),
    "F5_跳空启动_20日跳空>=3_当日>5%": (df["gap20"].values >= 3) & (df["r1"].values > 0.05),
    "F6_新高放量": (df["dist_hi250"].values > -0.005) & (df["amt_pct"].values > 0.90),
    "F0_纯MA20上方_QUIET": q & (df["ma20_d"].values > 0),
    "F0b_纯MA20最高十分位_QUIET": q & (df["ma20_d"].values >= np.nanquantile(df["ma20_d"].values[q], 0.90)),
}
res = {k: daily_stats(v) for k, v in shapes.items()}

# ---- 波动率分层内的增量（double sort）----
cv = df["cv60"].values
cv_bins = np.nanquantile(cv, [0, 1 / 3, 2 / 3, 1])
cv_grp = np.digitize(cv, cv_bins[1:-1])
ds = {}
for g in range(3):
    sub = cv_grp == g
    d = {}
    for name in ("F1_旗形_前期涨15_近10日横盘_站上MA20", "F2_箱体_振幅最低10%_近250日高", "F0b_纯MA20最高十分位_QUIET"):
        m = shapes[name] & sub
        d[name] = daily_stats(m)
    d["_base"] = daily_stats(sub)
    ds[f"cv60_grp{g}"] = d

# ---- 旗形的时间稳定性 ----
yr = df["date"].dt.year.values
yearly = {}
for yy in range(2010, 2027):
    m = shapes["F1_旗形_前期涨15_近10日横盘_站上MA20"] & (yr == yy)
    s = daily_stats(m)
    if s.get("n", 0) >= 50:
        yearly[int(yy)] = s

# ---- 旗形按日截面的横截面宽度 ----
per_day = pd.Series(shapes["F1_旗形_前期涨15_近10日横盘_站上MA20"]).groupby(date_code).sum()
print(
    f"旗形 每日可选数: median={per_day.median():.0f} p90={per_day.quantile(0.9):.0f} max={per_day.max()}",
    flush=True,
)

out = dict(
    baselines=baselines,
    shapes=res,
    double_sort_by_cv60=ds,
    flag_yearly=yearly,
    flag_per_day=dict(median=float(per_day.median()), p90=float(per_day.quantile(0.9)), max=int(per_day.max())),
)
Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("saved", OUT, flush=True)

hdr = f"{'shape':46s} {'n':>9s} {'hit%':>7s} {'lift':>5s} {'t':>6s} {'fwd10':>7s} {'fwd可成交':>8s}"
print("\n" + hdr)
for k, v in sorted(res.items(), key=lambda kv: -(kv[1].get("lift") or 0)):
    print(
        f"{k:46s} {v['n']:>9,} {v.get('hit_pct', 0):>7.3f} {v.get('lift', 0):>5.2f} "
        f"{str(v.get('t')):>6s} {str(v.get('fwd10_pct')):>7s} {str(v.get('fwd10_tradable_pct')):>8s}"
    )
print("\n--- double sort by cv60 ---")
for g, d in ds.items():
    print(g, "base", d["_base"].get("hit_pct"), d["_base"].get("n"))
    for k, v in d.items():
        if k == "_base":
            continue
        print(f"   {k[:28]:28s} n={v.get('n',0):>8,} hit={v.get('hit_pct')} lift={v.get('lift')} t={v.get('t')}")
print("\n--- flag yearly ---")
for yy, v in yearly.items():
    print(f"{yy} n={v['n']:>7,} hit={v['hit_pct']:.3f}% fwd10={v['fwd10_pct']}% t={v['t']}")
