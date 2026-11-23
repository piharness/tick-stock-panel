"""step6: 形状的「超额」收益与年度稳定性，以及事件路径分类。

step5 用的是绝对 10 日收益，牛市年份天然高，会把 beta 误读成 alpha。
这里改成超额：fwd10 - 当日全市场等权 fwd10（同口径、同起点）。

另外回答一个 step5 没答的问题：12,657 个 50% 事件，上涨路径长什么样？
  A 连板型   事件日已 >=3 连板（一步到位）
  B 加速型   无连板但 10 日涨幅 >=50%（双创/北交为主）
  C 阶梯型   1-2 连板
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SLIM = DATA / "_diag_surge50_slim.parquet"
OUT = DATA / "_diag_surge50_shape_step6.json"

df = pd.read_parquet(
    SLIM,
    columns=[
        "symbol", "date", "y10", "fwd_ret10", "r1", "r5", "r10", "r20", "r10_prev",
        "ma20_d", "dist_hi250", "dd20", "cv60", "cv60_pct", "amp20_pct", "amp5_pct",
        "gap20", "vol_r5_20", "to20_pct", "amt_pct", "age", "next_gap",
    ],
)
df["date"] = pd.to_datetime(df["date"])
date_code, uniq = pd.factorize(df["date"], sort=True)
D = len(uniq)

lp = np.where(df["symbol"].str.startswith(("688", "689", "300", "301", "302")), 0.20,
              np.where(df["symbol"].str.startswith(("8", "4", "920")), 0.30, 0.10))
df["untradable"] = df["next_gap"].values >= (lp - 0.005)

valid = (df["age"].values >= 60) & np.isfinite(df["r10"].values) & (df["r10_prev"].values > -0.95)
y = df["y10"].values.astype(np.float64)
fr = df["fwd_ret10"].values.astype(np.float64)
ut = df["untradable"].values

# 当日等权基准（超额口径）
bmk = df.loc[valid].groupby("date")["fwd_ret10"].mean()
df["exc"] = df["fwd_ret10"] - df["date"].map(bmk)
exc = df["exc"].values
print(f"valid {valid.sum():,}  bmk mean {bmk.mean()*100:.3f}% / 10d  days {len(bmk)}", flush=True)

r10 = df["r10"].values
r1 = df["r1"].values
q = (r10 < 0.05) & np.isfinite(r10)

shapes = {
    "S1_首板": (r1 > 0.095) & (np.nan_to_num(df["r5"].values, nan=1) - r1 < 0.05),
    "S2_旗形": q & (df["r10_prev"].values >= 0.15) & (df["ma20_d"].values > 0),
    "S3_箱体收缩": (df["amp20_pct"].values <= 0.20) & (df["dist_hi250"].values > -0.10),
    "S4_缩量加速": (r10 >= 0.20) & (df["amp5_pct"].values <= 0.20),
    "S5_跳空启动": (df["gap20"].values >= 3) & (r1 > 0.05),
    "S5b_跳空启动强": (df["gap20"].values >= 3) & (r1 > 0.09),
    "S6_回踩不破": (df["r20"].values > 0.10) & (df["r5"].values < 0) & (df["ma20_d"].values > 0)
    & (df["dd20"].values > -0.05),
    "S7_低波动新高": (df["cv60_pct"].values <= 0.20) & (df["dist_hi250"].values > -0.02),
    "S9_高位横盘": (df["dist_hi250"].values > -0.05) & (df["amp20_pct"].values <= 0.30),
}


def stats(mask: np.ndarray) -> dict:
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
    em = exc[m]
    efin = np.isfinite(em)
    # 超额 t（日频：桶内当日超额均值序列）
    de = date_code[m][efin]
    ev = em[efin]
    dsum = np.bincount(de, weights=ev, minlength=D)
    dcnt = np.bincount(de, minlength=D)
    dok = dcnt > 10
    et = float((dsum[dok] / dcnt[dok]).mean() / ((dsum[dok] / dcnt[dok]).std(ddof=1) / np.sqrt(dok.sum()))) if dok.sum() > 30 else np.nan
    m2 = m & ~ut
    return dict(
        n=n, hit_pct=round(hr * 100, 4),
        exc_pct=round(float(np.nanmean(em)) * 100, 3),
        exc_t=round(et, 2) if np.isfinite(et) else None,
        exc_tradable_pct=round(float(np.nanmean(exc[m2])) * 100, 3) if m2.sum() > 50 else None,
        untradable_share=round(float(ut[m].mean()) * 100, 2),
        amt_pct_median=round(float(np.nanmedian(df["amt_pct"].values[m])), 3),
        to_pct_median=round(float(np.nanmedian(df["to20_pct"].values[m])), 3),
    )


base = stats(np.ones(len(df), dtype=bool))
res = {k: stats(np.asarray(v, dtype=bool)) for k, v in shapes.items()}

print(f"base hit {base['hit_pct']}%  exc {base['exc_pct']}%", flush=True)
print(f"{'shape':16s} {'n':>9s} {'hit%':>7s} {'exc%':>7s} {'exc_t':>6s} {'exc_trad':>8s} {'untrad%':>7s} {'amtp':>5s} {'top':>5s}")
for k, v in sorted(res.items(), key=lambda kv: -(kv[1].get("exc_pct") or -99)):
    print(f"{k:16s} {v['n']:>9,} {v['hit_pct']:>7.3f} {v['exc_pct']:>7.3f} {str(v['exc_t']):>6s} "
          f"{str(v['exc_tradable_pct']):>8s} {v['untradable_share']:>7.2f} {v['amt_pct_median']:>5.2f} {v['to_pct_median']:>5.2f}",
          flush=True)

# 年度超额
yr = df["date"].dt.year.values
yearly = {}
for k, v in shapes.items():
    v = np.asarray(v, dtype=bool)
    yearly[k] = {
        int(yy): stats(v & (yr == yy)) for yy in range(2012, 2027)
        if (v & (yr == yy) & valid).sum() > 300
    }

# 波动率分层内的超额（控制低波动/高波动暴露）
cvp = df["cv60_pct"].values
ds = {}
for g, lo, hi in (("low", 0, 1 / 3), ("mid", 1 / 3, 2 / 3), ("high", 2 / 3, 1.01)):
    sub = (cvp >= lo) & (cvp < hi)
    ds[f"cv_{g}"] = {k: stats(np.asarray(v, dtype=bool) & sub) for k, v in shapes.items()}
    ds[f"cv_{g}"]["_base"] = stats(sub)

# ---- 事件路径分类（复用 step1 事件表）----
ev = pd.read_parquet(DATA / "_diag_surge50_events.parquet")
post = pd.read_parquet(DATA / "_diag_surge50_post.parquet")


def path_class(r: pd.Series) -> str:
    cl = int(r["consecutive_limit_ups"] or 0)
    if cl >= 3:
        return "A_连板型"
    if cl >= 1:
        return "C_阶梯型"
    return "B_加速型"


ev["path"] = ev.apply(path_class, axis=1)
ev["year"] = pd.to_datetime(ev["date"]).dt.year
pc = ev.groupby(["path", "board"]).size().unstack(fill_value=0)
pc_all = ev.groupby("path").agg(
    n=("symbol", "size"),
    r10_med=("r10", lambda s: round(float(s.median()) * 100, 2)),
    to_med=("turnover_rate", lambda s: round(float(s.median()), 2)),
    share=("symbol", lambda s: round(len(s) / len(ev) * 100, 1)),
)
# 每类的后续收益（来自 post 表）
ev_key = ev[["symbol", "date", "path"]].copy()
ev_key["ev_date"] = pd.to_datetime(ev_key["date"])
post["ev_date"] = pd.to_datetime(post["ev_date"])
post = post.merge(ev_key[["symbol", "ev_date", "path"]], on=["symbol", "ev_date"], how="left")

post_long = post.melt(
    id_vars=["symbol", "ev_date", "path"],
    value_vars=["ret1", "ret3", "ret5", "ret10", "ret20"],
    var_name="h", value_name="ret",
).dropna(subset=["ret"])
post_long["h"] = post_long["h"].str.replace("ret", "", regex=False).astype(int)
path_ret = post_long.groupby(["path", "h"])["ret"].agg(["count", "mean", "median"]).reset_index()
path_ret["mean"] = (path_ret["mean"] * 100).round(2)
path_ret["median"] = (path_ret["median"] * 100).round(2)

out = dict(
    base=base,
    shapes=res,
    yearly=yearly,
    double_sort=ds,
    path_class=pc_all.reset_index().to_dict("records"),
    path_by_board=pc.to_dict(),
    path_ret=path_ret.to_dict("records"),
    bmk_mean_pct=round(float(bmk.mean()) * 100, 3),
)
Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("saved", OUT, flush=True)
print("\npath_class\n", pc_all)
print("\npath_ret\n", path_ret.to_string())
