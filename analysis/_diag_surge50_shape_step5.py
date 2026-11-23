"""step5: 严格样本下的形状比较（剔除次新、停牌、无 10 日历史、买不到的一字板）。

step3/step4 暴露了两个伪信号，必须先清掉再谈「形状」：
1. 次新股：上市 <60 日的票 10 日涨 50% 是常态，且它们把 ma20_d/振幅类特征的
   lift 抬到 13-83 倍。age<=20 只占样本 8.4%，却贡献了 42% 的命中。
2. 一字板：命中样本若 T+1 开盘就涨停，物理上买不到。

严格样本 = age>=60 且 有 10 日历史 且 当日有成交量。
每个形状额外输出收益分解：
  E[fwd10] = P(hit)*E[fwd10|hit] + (1-P)*E[fwd10|miss]
因为命中率高的形状（如新高放量）可能因为 miss 端亏更多而净收益为负。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SRC = str(DATA / "_diag_surge50_features.parquet").replace("\\", "/")
SLIM = str(DATA / "_diag_surge50_slim.parquet").replace("\\", "/")
OUT = DATA / "_diag_surge50_shape_step5.json"

t0 = time.time()
con = duckdb.connect()
con.execute("PRAGMA threads=6")
con.execute("PRAGMA memory_limit='12GB'")

con.execute(
    f"""
    CREATE TABLE s AS
    SELECT symbol, date, y10, fwd_ret10, y10_end_ret, close, open, r1, r5, r10, r20, r60,
           ma20_d, ma60_d, dist_hi250, dist_lo250, dd20, cv20, cv60,
           amp20, amp5, body20, upsh20, dnsh20, gap20, updays20, vol_r5_20,
           to20_pct, amt_pct, age, seq, rn,
           CASE WHEN r10 IS NOT NULL AND r20 IS NOT NULL THEN (1+r20)/(1+r10)-1 END AS r10_prev,
           CASE WHEN fwd_ret10 IS NOT NULL AND y10_end_ret IS NOT NULL
                THEN (1+fwd_ret10)/(1+y10_end_ret)-1 END AS next_gap,
           percent_rank() OVER (PARTITION BY date ORDER BY amp20) AS amp20_pct,
           percent_rank() OVER (PARTITION BY date ORDER BY amp5)  AS amp5_pct,
           percent_rank() OVER (PARTITION BY date ORDER BY cv60)  AS cv60_pct,
           percent_rank() OVER (PARTITION BY date ORDER BY dist_hi250) AS hi_pct,
           percent_rank() OVER (PARTITION BY date ORDER BY dd20)  AS dd_pct,
           percent_rank() OVER (PARTITION BY date ORDER BY r10_prev) AS r10prev_pct
    FROM read_parquet('{SRC}')
    WHERE y10 IS NOT NULL
    """
)
print("slim built", round(time.time() - t0, 1), flush=True)
con.execute(f"COPY s TO '{SLIM}' (FORMAT PARQUET, COMPRESSION ZSTD)")
print("slim written", round(time.time() - t0, 1), flush=True)

df = pd.read_parquet(SLIM)
df["date"] = pd.to_datetime(df["date"])
date_code, uniq = pd.factorize(df["date"], sort=True)
D = len(uniq)

# 涨停幅度按板块
lp = np.where(
    df["symbol"].str.startswith(("688", "689", "300", "301", "302")), 0.20,
    np.where(df["symbol"].str.startswith(("8", "4", "920")), 0.30, 0.10),
)
df["lp"] = lp
df["untradable"] = df["next_gap"].values >= (lp - 0.005)

valid = (df["age"].values >= 60) & np.isfinite(df["r10"].values) & (df["r10_prev"].values > -0.95)
print(f"rows {len(df):,}  strict valid {valid.sum():,} ({valid.mean()*100:.1f}%)", flush=True)

y = df["y10"].values.astype(np.float64)
fr = df["fwd_ret10"].values.astype(np.float64)
ut = df["untradable"].values
r10 = df["r10"].values
r1 = df["r1"].values


def stats(mask: np.ndarray, tag: str = "") -> dict:
    m = mask & valid
    n = int(m.sum())
    if n < 100:
        return dict(n=n)
    hr = float(y[m].mean())
    dc = date_code[m]
    num = np.bincount(dc, weights=y[m], minlength=D)
    cnt = np.bincount(dc, minlength=D)
    ok = cnt > 0
    rate = num[ok] / cnt[ok]
    t = float(rate.mean() / (rate.std(ddof=1) / np.sqrt(ok.sum()))) if ok.sum() > 30 and rate.std(ddof=1) > 0 else np.nan
    fm = fr[m]
    fin = np.isfinite(fm)
    hit_ret = float(np.nanmean(fm[fin & (y[m] == 1)])) if (fin & (y[m] == 1)).sum() > 5 else np.nan
    miss_ret = float(np.nanmean(fm[fin & (y[m] == 0)])) if (fin & (y[m] == 0)).sum() > 5 else np.nan
    m2 = m & ~ut
    return dict(
        n=n, days=int(ok.sum()),
        hit_pct=round(hr * 100, 4), lift=round(hr / base_rate, 2),
        t=round(t, 2) if np.isfinite(t) else None,
        fwd10_pct=round(float(np.nanmean(fm)) * 100, 3),
        ret_hit_pct=round(hit_ret * 100, 2) if np.isfinite(hit_ret) else None,
        ret_miss_pct=round(miss_ret * 100, 2) if np.isfinite(miss_ret) else None,
        n_tradable=int(m2.sum()),
        fwd10_tradable_pct=round(float(np.nanmean(fr[m2])) * 100, 3) if m2.sum() > 50 else None,
        hit_tradable_pct=round(float(y[m2].mean()) * 100, 4) if m2.sum() > 50 else None,
        untradable_share=round(float(ut[m].mean()) * 100, 2) if n else None,
    )


base_rate = float(y[valid].mean())
base = stats(np.ones(len(df), dtype=bool), "base")
print(f"strict base {base['hit_pct']}%  fwd10 {base['fwd10_pct']}%", flush=True)

q = (r10 < 0.05) & np.isfinite(r10)
shapes = {
    "S1_首板_当日涨停且前日<5%": (r1 > 0.095) & (np.nan_to_num(df["r5"].values, nan=1) - r1 < 0.05),
    "S2_旗形_前10日>=15%_近10日<5%_站上MA20": q & (df["r10_prev"].values >= 0.15) & (df["ma20_d"].values > 0),
    "S2b_旗形弱_前10日>=8%": q & (df["r10_prev"].values >= 0.08) & (df["ma20_d"].values > 0),
    "S3_箱体收缩_振幅最低20%_距高10%内": (df["amp20_pct"].values <= 0.20) & (df["dist_hi250"].values > -0.10),
    "S3b_箱体收缩+缩量": (df["amp20_pct"].values <= 0.20) & (df["dist_hi250"].values > -0.10)
    & (df["vol_r5_20"].values < 0.9),
    "S4_缩量加速_已涨>=20%_5日振幅最低20%": (r10 >= 0.20) & (df["amp5_pct"].values <= 0.20),
    "S5_跳空启动_20日跳空>=3_当日>5%": (df["gap20"].values >= 3) & (r1 > 0.05),
    "S5b_跳空启动_当日>9%": (df["gap20"].values >= 3) & (r1 > 0.09),
    "S6_回踩不破_20日>10%_5日<0_站上MA20": (df["r20"].values > 0.10) & (df["r5"].values < 0)
    & (df["ma20_d"].values > 0) & (df["dd20"].values > -0.05),
    "S7_低波动新高_波动最低20%_创60日高": (df["cv60_pct"].values <= 0.20)
    & (df["dist_hi250"].values > -0.02),
    "S8_安静且贴近MA20_前10日涨5-15%": q & (df["r10_prev"].between(0.05, 0.15).values)
    & (df["ma20_d"].between(-0.02, 0.05).values),
    "S9_高位横盘_距250日高5%内_20日振幅最低30%": (df["dist_hi250"].values > -0.05)
    & (df["amp20_pct"].values <= 0.30),
}
res = {}
for k, v in shapes.items():
    res[k] = stats(np.asarray(v, dtype=bool))
    print(f"{k:44s} n={res[k]['n']:>8,} hit={res[k].get('hit_pct')} lift={res[k].get('lift')} "
          f"t={res[k].get('t')} fwd={res[k].get('fwd10_pct')} trad={res[k].get('fwd10_tradable_pct')}", flush=True)

# 月度 regime（严格样本）
ym = df.loc[valid, "date"].dt.to_period("M").astype(str)
monthly = df.loc[valid].groupby(ym)["y10"].agg(["count", "sum", "mean"]).reset_index()
monthly["hit_pct"] = (monthly["mean"] * 100).round(3)
monthly = monthly.astype({"count": int, "sum": int}).drop(columns=["mean"])

# 逐年 S2 / S5b
yr = df["date"].dt.year.values
yearly = {}
for k in ("S2_旗形_前10日>=15%_近10日<5%_站上MA20", "S5_跳空启动_20日跳空>=3_当日>5%", "S1_首板_当日涨停且前日<5%"):
    v = np.asarray(shapes[k], dtype=bool)
    yearly[k] = {
        int(yy): stats(v & (yr == yy)) for yy in range(2010, 2027)
        if (v & (yr == yy) & valid).sum() > 300
    }

out = dict(
    n_rows=int(len(df)), n_valid=int(valid.sum()),
    base_rate_pct=round(base_rate * 100, 4), base=base,
    shapes=res,
    monthly=monthly.to_dict("records"),
    yearly=yearly,
)
Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("saved", OUT, flush=True)
