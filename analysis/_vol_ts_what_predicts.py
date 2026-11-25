"""期限结构到底在预测什么：波动、收益、还是回撤。"""
import duckdb, numpy as np, pandas as pd

con = duckdb.connect(); con.execute("PRAGMA threads=8")
con.execute("""create or replace table d as
select symbol,date,close,row_number() over (partition by symbol order by date) k
from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where close>0 and date between date '2004-01-01' and date '2026-11-25'""")
mkt = con.execute("""with x as (select symbol,date,close/nullif(lag(close) over (partition by symbol order by date),0)-1 as ri
from d where k>250 and not (symbol like '920%' or symbol like '8%' or symbol like '4%'))
select date, avg(ri) as mr, count(ri) as nn from x where ri is not null group by date order by date""").df()
mkt["date"] = pd.to_datetime(mkt.date)
mkt = mkt[(mkt.nn >= 200) & np.isfinite(mkt.mr)].reset_index(drop=True)
ret = mkt.mr.to_numpy(float)
lvl = np.cumprod(1 + ret)
pos = {d: i for i, d in enumerate(mkt.date)}
N = len(ret)
print("交易日", N, mkt.date.min().date(), mkt.date.max().date())

X = pd.read_parquet("data/.scratch/vol_ts_daily.parquet")
X["t"] = X.date.map(pos).astype(int)

def fvol(t, k):
    return float(ret[t+1:t+1+k].std(ddof=1)*np.sqrt(252)) if t+1+k <= N else np.nan
def fret(t, k):
    return float(lvl[t+k]/lvl[t]-1) if t+k < N else np.nan

X["fV21"] = [fvol(t, 21) for t in X.t]
X["fV63"] = [fvol(t, 63) for t in X.t]
X["ret63"] = [fret(t, 63) for t in X.t]

def nw(y, xx, lag=21):
    xx = np.asarray(xx, float); xx = xx[:, None] if xx.ndim == 1 else xx
    Xd = np.column_stack([np.ones(len(y)), xx]); y = np.asarray(y, float)
    b = np.linalg.lstsq(Xd, y, rcond=None)[0]; e = y - Xd @ b
    S = (Xd*e[:, None]).T @ (Xd*e[:, None])
    for l in range(1, lag+1):
        w = 1-l/(lag+1); A = (Xd[l:]*e[l:, None]).T @ (Xd[:-l]*e[:-l, None]); S += w*(A+A.T)
    V = np.linalg.inv(Xd.T @ Xd) @ S @ np.linalg.inv(Xd.T @ Xd)
    return b, b/np.sqrt(np.diag(V))

print("\n=== A. R 预测未来 21 日已实现波动 ===")
d = X.dropna(subset=["fV21", "R", "lv"])
b, t = nw(d.fV21.values, d[["R", "lv"]].values)
print(f"  R {b[1]:+.3f} (t {t[1]:+.2f}) | lnV60 {b[2]:+.3f} (t {t[2]:+.2f})")
q = d.copy(); q["bin"] = pd.qcut(q.R.rank(method="first"), 5, labels=list("12345"))
s = q.groupby("bin", observed=True).agg(V60=("V60","mean"), fV21=("fV21","mean"), R=("R","mean"))
s["抬升"] = (s.fV21/s.V60).round(2)
print(s.round(3).to_string())

print("\n=== B. 收益 / 回撤比 ===")
q = X.dropna(subset=["mdd21","ret21"]).copy()
q["bin"] = pd.qcut(q.R.rank(method="first"), 5, labels=list("12345"))
g = q.groupby("bin", observed=True).agg(ret=("ret21","mean"), mdd=("mdd21","mean"), R=("R","mean"))
g["ret%"] = (g.ret*100).round(2); g["mdd%"] = (g.mdd*100).round(2)
g["收益/回撤"] = (g.ret/g.mdd.abs()).round(3)
print(g[["R","ret%","mdd%","收益/回撤"]].to_string())

print("\n=== C. 收益侧 ===")
for tgt in ["ret21","ret63"]:
    dd = X.dropna(subset=[tgt, "R", "lv"])
    b, t = nw(dd[tgt].values, dd[["R","lv"]].values)
    b2, t2 = nw(dd[tgt].values, dd.R.values)
    print(f"  {tgt}: R(控lv) {b[1]*100:+.2f}% t={t[1]:+.2f} | R(不控) {b2[1]*100:+.2f}% t={t2[1]:+.2f}")
q2 = X.dropna(subset=["ret63"]).copy(); q2["bin"] = pd.qcut(q2.R.rank(method="first"), 5, labels=list("12345"))
print("  ret63 分位均值(%):", (q2.groupby("bin", observed=True).ret63.mean()*100).round(2).tolist())

print("\n=== D. 月度 Q5-Q1 的符号一致性（分年）===")
mm = pd.read_parquet("data/.scratch/vol_ts_panel.parquet"); mm["yr"] = mm.m.astype(str).str[:4]
def q5q1(d):
    d = d.dropna(subset=["R","mdd21"])
    if len(d) < 24: return np.nan
    lo = d[d.R <= d.R.quantile(.2)]; hi = d[d.R >= d.R.quantile(.8)]
    return (hi.mdd21.mean()-lo.mdd21.mean())*100
rows = []
for y, g in mm.groupby("yr"):
    rows.append((y, len(g), q5q1(g)))
t = pd.DataFrame(rows, columns=["yr","n","d"]).dropna()
print(t.round(2).to_string(index=False))
print(f"负号年数 {int((t.d<0).sum())}/{len(t)}")
X.to_parquet("data/.scratch/vol_ts_daily.parquet", index=False)
