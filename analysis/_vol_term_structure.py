"""已实现波动的期限结构 → 未来回撤（按 docs/vol-term-structure-prereg.md）。

R = ln(V20/V60)。主假设 A：R 高（短期波动远高于长期=恐慌已发生）⇒ 未来回撤更浅、收益更高。
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
r = r[(r.n>=200) & np.isfinite(r.r)].reset_index(drop=True)
P = (1+r.r).cumprod().values
print("等权指数", len(r), r.date.min().date(), r.date.max().date())

def fwd_mdd(t,k):
    if t+k>=len(P): return np.nan
    w=P[t:t+k+1]; return float((w/np.maximum.accumulate(w)-1).min())
def fwd_ret(t,k):
    return np.nan if t+k>=len(P) else float(P[t+k]/P[t]-1)

rr = r.r.values
def vol(t,k):
    if t<k: return np.nan
    return float(rr[t-k+1:t+1].std(ddof=1)*np.sqrt(252))

r["ym"]=r.date.dt.to_period("M")
idx=r.groupby("ym").tail(1).index
rows=[]
for t in idx:
    rows.append(dict(m=r.date.iloc[t].to_period("M").to_timestamp(), date=r.date.iloc[t],
        V5=vol(t,5), V20=vol(t,20), V60=vol(t,60), V120=vol(t,120),
        mdd21=fwd_mdd(t,21), mdd63=fwd_mdd(t,63), mdd10=fwd_mdd(t,10),
        ret21=fwd_ret(t,21), ret63=fwd_ret(t,63),
        ret20=float(P[t]/P[t-20]-1) if t>=20 else np.nan,
        ret250=float(P[t]/P[t-250]-1) if t>=250 else np.nan))
mm=pd.DataFrame(rows).dropna(subset=["V60","V20","mdd21"])
mm["R"]=np.log(mm.V20/mm.V60); mm["R5"]=np.log(mm.V5/mm.V60); mm["R120"]=np.log(mm.V20/mm.V120)
mm["lv"]=np.log(mm.V60)
mm=mm[np.isfinite(mm.R)].reset_index(drop=True)
print("月末面板", len(mm), mm.m.min().date(), mm.m.max().date())
print(mm[["V5","V20","V60","R","R5","R120"]].describe(percentiles=[.1,.5,.9]).round(3).to_string())

def nw(y,X,lag=3):
    X=np.asarray(X,float); X=X[:,None] if X.ndim==1 else X
    Xd=np.column_stack([np.ones(len(y)),X]); y=np.asarray(y,float)
    b=np.linalg.lstsq(Xd,y,rcond=None)[0]; e=y-Xd@b
    S=(Xd*e[:,None]).T@(Xd*e[:,None])
    for l in range(1,lag+1):
        w=1-l/(lag+1); A=(Xd[l:]*e[l:,None]).T@(Xd[:-l]*e[:-l,None]); S+=w*(A+A.T)
    V=np.linalg.inv(Xd.T@Xd)@S@np.linalg.inv(Xd.T@Xd); return b,b/np.sqrt(np.diag(V))
def boot_diff(a,b,n=3000,seed=5):
    rng=np.random.default_rng(seed); o=[rng.choice(a,len(a)).mean()-rng.choice(b,len(b)).mean() for _ in range(n)]
    return np.percentile(o,[2.5,97.5])
def quint(df,col,tgt):
    q=df.dropna(subset=[col,tgt]).copy()
    q["bin"]=pd.qcut(q[col].rank(method="first"),5,labels=["Q1","Q2","Q3","Q4","Q5"])
    t=q.groupby("bin",observed=True)[tgt].agg(["mean","median","count"])
    t[["mean","median"]]=(t[["mean","median"]]*100).round(2)
    lo=q[q.bin=="Q1"][tgt].values; hi=q[q.bin=="Q5"][tgt].values
    ci=boot_diff(hi,lo); return t,(hi.mean()-lo.mean())*100,ci

print("\n"+"="*70)
for tgt in ["mdd21","ret21","mdd63","ret63"]:
    t,d,ci=quint(mm,"R",tgt)
    print(f"\n### R=ln(V20/V60) → {tgt}")
    print(t.to_string())
    print(f"Q5−Q1 {d:+.2f}pp  CI[{ci[0]*100:+.2f},{ci[1]*100:+.2f}]")

print("\n"+"="*70)
print("=== P2：控制 ln(V60) 与 RET20 ===")
for tgt in ["mdd21","ret21","mdd63"]:
    d=mm.dropna(subset=[tgt,"R","lv","ret20"])
    b,t=nw(d[tgt].values,d[["R","lv","ret20"]].values)
    print(f"{tgt}: R {b[1]*100:+.2f}pp (t {t[1]:+.2f}) | lnV60 {b[2]*100:+.2f} (t {t[2]:+.2f}) | RET20 {b[3]*100:+.2f} (t {t[3]:+.2f})")
    b2,t2=nw(d[tgt].values,d[["R","lv"]].values)
    print(f"      仅控 lnV60: R {b2[1]*100:+.2f} (t {t2[1]:+.2f}) | lnV60 t {t2[2]:+.2f}")
    b3,t3=nw(d[tgt].values,d.R.values)
    print(f"      不控:       R {b3[1]*100:+.2f} (t {t3[1]:+.2f})")

rng=np.random.default_rng(11); N=len(mm); null=[]
for _ in range(2000):
    i=[]
    while len(i)<N:
        s=rng.integers(0,N-6+1); i.extend(range(s,s+6))
    i=np.array(i[:N]); null.append(np.corrcoef(mm.R.values[i],mm.mdd21.values)[0,1])
real=np.corrcoef(mm.R,mm.mdd21)[0,1]
print(f"\n安慰剂 corr(R,MDD21)={real:+.3f}  p(右尾)={(np.array(null)>=real).mean():.3f}  p(左尾)={(np.array(null)<=real).mean():.3f}")

mm.to_parquet("data/.scratch/vol_ts_panel.parquet",index=False)
