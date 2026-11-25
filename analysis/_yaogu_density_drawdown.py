"""密度 → 未来回撤（按 docs/yaogu-density-drawdown-prereg.md 执行）。

等权全 A 指数（含退市股）→ 月末点上的未来 21/63/126 日最大回撤
→ 按 HOT 分位分组 + 控 VOL20/RET12 的增量检验 + P4 区分性检验。
"""
import duckdb, numpy as np, pandas as pd

con = duckdb.connect(); con.execute("PRAGMA threads=8"); con.execute("PRAGMA memory_limit='7GB'")
con.execute("""
create or replace table d as
select symbol,date,close,row_number() over (partition by symbol order by date) k
from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where close>0 and date between date '2004-01-01' and date '2026-11-25'
union all
select symbol,date,close,row_number() over (partition by symbol order by date)
from read_parquet('data/backfill_20261117/stock_gap_daily.parquet')
where close>0 and date between date '2004-01-01' and date '2026-11-25'
  and symbol not in (select distinct symbol from read_parquet('data/kline_daily_enriched/date=2026-11-25/part.parquet'))
""")
r = con.execute("""
with x as (select symbol,date,close/nullif(lag(close) over (partition by symbol order by date),0)-1 as ri
  from d where k>250 and not (symbol like '920%' or symbol like '8%' or symbol like '4%'))
select date, avg(ri) r, count(ri) n from x where ri is not null group by date order by date
""").df()
r["date"] = pd.to_datetime(r.date)
r = r[(r.n >= 200) & np.isfinite(r.r)].reset_index(drop=True)
r["P"] = (1+r.r).cumprod()
print("等权指数", len(r), r.date.min().date(), r.date.max().date(), "终值", round(r.P.iloc[-1],2))

P = r.P.values; D = r.date.values
def fwd_mdd(t, k):
    if t+k >= len(P): return np.nan
    w = P[t:t+k+1]
    rm = np.maximum.accumulate(w)
    return float((w/rm - 1).min())
def fwd_ret(t, k):
    return np.nan if t+k >= len(P) else float(P[t+k]/P[t] - 1)

r["ym"] = pd.to_datetime(r.date).dt.to_period("M")
eom = r.groupby("ym").tail(1).index
rows = []
for t in eom:
    rows.append(dict(m=r.date.iloc[t].to_period("M").to_timestamp(), date=r.date.iloc[t],
                     P=P[t], mdd21=fwd_mdd(t,21), mdd63=fwd_mdd(t,63), mdd126=fwd_mdd(t,126),
                     ret21=fwd_ret(t,21), ret63=fwd_ret(t,63), ret126=fwd_ret(t,126),
                     vol20=float(r.r.iloc[max(0,t-19):t+1].std()*np.sqrt(252)),
                     ret12=float(P[t]/P[t-250]-1) if t >= 250 else np.nan,
                     d250=float(P[t]/P[max(0,t-249):t+1].max()-1)))
mm = pd.DataFrame(rows)
print("月末点", len(mm), mm.m.min().date(), mm.m.max().date())

hot = pd.read_parquet("data/.scratch/yaogu_density_hot.parquet")
pan = mm.merge(hot[["m","HOT","HQ","HQL"]], on="m", how="inner").dropna(subset=["HQ","mdd63","vol20"])
print("面板", len(pan), "HQ 缺省", pan.HQ.isna().sum())
print("\nMDD 描述:")
for c in ["mdd21","mdd63","mdd126"]:
    s=pan[c].dropna(); print(f"  {c}: 均值 {s.mean()*100:.1f}% 中位 {s.median()*100:.1f}% p10 {s.quantile(.1)*100:.1f}% 最深 {s.min()*100:.1f}%")

def nw(y,X,lag=3):
    X=np.asarray(X,float); X=X[:,None] if X.ndim==1 else X
    Xd=np.column_stack([np.ones(len(y)),X]); y=np.asarray(y,float)
    b=np.linalg.lstsq(Xd,y,rcond=None)[0]; e=y-Xd@b
    S=(Xd*e[:,None]).T@(Xd*e[:,None])
    for l in range(1,lag+1):
        w=1-l/(lag+1); A=(Xd[l:]*e[l:,None]).T@(Xd[:-l]*e[:-l,None]); S+=w*(A+A.T)
    V=np.linalg.inv(Xd.T@Xd)@S@np.linalg.inv(Xd.T@Xd)
    return b,b/np.sqrt(np.diag(V))

def boot_diff(a,b,n=3000,seed=5):
    rng=np.random.default_rng(seed); out=[]
    for _ in range(n): out.append(rng.choice(a,len(a)).mean()-rng.choice(b,len(b)).mean())
    return np.percentile(out,[2.5,97.5])

print("\n=== P1：按 HOT 分位（Q5 最热） ===")
for tgt in ["mdd21","mdd63","mdd126","ret63"]:
    q=pan.dropna(subset=[tgt]).copy()
    q["bin"]=pd.cut(q.HQ,[-.01,.2,.4,.6,.8,1.0],labels=["Q1","Q2","Q3","Q4","Q5"])
    t=q.groupby("bin",observed=True)[tgt].agg(["mean","median","count"])
    t["mean"]=(t["mean"]*100).round(2); t["median"]=(t["median"]*100).round(2)
    lo=q[q.bin=="Q1"][tgt]; hi=q[q.bin=="Q5"][tgt]; ci=boot_diff(hi.values,lo.values)
    print(f"\n{tgt}\n{t.to_string()}")
    print(f"  Q5−Q1 均值 {((hi.mean()-lo.mean())*100):+.2f}pp  中位差 {((hi.median()-lo.median())*100):+.2f}pp  CI[{ci[0]*100:+.2f},{ci[1]*100:+.2f}]")

print("\n=== P(MDD63 < −15%) 分位 ===")
pan["big"]=(pan.mdd63<-0.15).astype(float)
q=pan.dropna(subset=["mdd63"]).copy(); q["bin"]=pd.cut(q.HQ,[-.01,.2,.4,.6,.8,1.0],labels=["Q1","Q2","Q3","Q4","Q5"])
print((q.groupby("bin",observed=True).big.agg(["mean","count"]).assign(mean=lambda x:(x["mean"]*100).round(1))).to_string())

print("\n=== P2：控制 VOL20 / RET12 ===")
for tgt in ["mdd63","mdd21","mdd126"]:
    d=pan.dropna(subset=[tgt,"vol20","ret12"]).copy()
    b,t=nw(d[tgt].values, d[["HOT","vol20","ret12"]].values)
    print(f"{tgt}: HOT {b[1]*100:+.4f} (t {t[1]:+.2f}) | vol20 {b[2]*100:+.4f} (t {t[2]:+.2f}) | ret12 {b[3]*100:+.4f} (t {t[3]:+.2f})")
    b2,t2=nw(d[tgt].values, d[["HOT","vol20"]].values)
    print(f"        仅控 vol20: HOT {b2[1]*100:+.4f} (t {t2[1]:+.2f}) | vol20 t {t2[2]:+.2f}")

print("\n=== P4：区分性（HOT 对收益） ===")
for tgt in ["ret63","ret21","ret126"]:
    d=pan.dropna(subset=[tgt,"vol20","ret12"]).copy()
    b,t=nw(d[tgt].values, d[["HOT","vol20","ret12"]].values)
    print(f"{tgt}: HOT {b[1]*100:+.3f} (t {t[1]:+.2f})")
    d2=pan.dropna(subset=[tgt]).copy()
    b2,t2=nw(d2[tgt].values, d2.HOT.values)
    print(f"      不控: HOT {b2[1]*100:+.3f} (t {t2[1]:+.2f})")

rng=np.random.default_rng(11); N=len(pan); null=[]
base=pan.dropna(subset=["mdd63"]).reset_index(drop=True)
for _ in range(2000):
    idx=[]
    while len(idx)<N:
        s=rng.integers(0,N-6+1); idx.extend(range(s,s+6))
    idx=np.array(idx[:N])%len(base)
    null.append(np.corrcoef(base.HQ.values[idx], base.mdd63.values)[0,1])
real=np.corrcoef(base.HQ, base.mdd63)[0,1]
print(f"\n安慰剂 corr(HQ,MDD63)={real:+.3f}  p(左尾)={(np.array(null)<=real).mean():.3f}")
pan.to_parquet("data/.scratch/yaogu_mdd_panel.parquet", index=False)
