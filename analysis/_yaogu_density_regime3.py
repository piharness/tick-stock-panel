"""月度妖股密度 regime 回测 v3 —— 修正前视偏差。

v1：个股面板 merge 进月度面板 ⇒ 68 万行，t 算爆。
v2：`heat`（月末换手）与 `r`（当月收益）同期 ⇒ 用月末状态选当月已涨的股票，
    +3.1%/月、17 年全正，是前视偏差；日频正确口径（t 日 to20 → t+21 收益）是 −1.19%。
v3：`heat_lag` = 上月末 heat，`r` = 当月收益。与日频口径一致才算对。
"""
import duckdb, numpy as np, pandas as pd

con = duckdb.connect(); con.execute("PRAGMA threads=8"); con.execute("PRAGMA memory_limit='7GB'")
con.execute("""
create or replace table d as
select symbol,date,open,close,raw_close,volume,turnover_rate,
  consecutive_limit_ups as clu, case when consecutive_limit_ups>0 then 1 else 0 end as lu,
  row_number() over (partition by symbol order by date) k
from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where close>0 and date between date '2005-01-01' and date '2026-11-25'
union all
select symbol,date,open,close,close,volume,null::double,null::uint32,0,
  row_number() over (partition by symbol order by date)
from read_parquet('data/backfill_20261117/stock_gap_daily.parquet')
where close>0 and date between date '2005-01-01' and date '2026-11-25'
  and symbol not in (select distinct symbol from read_parquet('data/kline_daily_enriched/date=2026-11-25/part.parquet'))
""")
con.execute("""
create or replace table d2 as
select *, date_trunc('month',date) m,
  close/nullif(min(close) over (partition by symbol order by date rows between 59 preceding and current row),0)-1 as rmax60,
  avg(turnover_rate) over (partition by symbol order by date rows between 19 preceding and current row) as to20
from d where k>250 and not (symbol like '920%' or symbol like '8%' or symbol like '4%')
""")
dag = con.execute("""
select date, date_trunc('month',date) m, count(*) n,
  avg(case when rmax60>=1.5 then 1.0 else 0 end) frac_hot,
  avg(case when rmax60>=1.0 then 1.0 else 0 end) frac_dbl,
  avg(lu) frac_lu
from d2 group by date, m order by date
""").df()

ms = con.execute("""
with x as (
  select symbol,m,date,open,close,raw_close,volume,turnover_rate,to20,
    row_number() over (partition by symbol,m order by date) ra,
    row_number() over (partition by symbol,m order by date desc) rd,
    count(*) over (partition by symbol,m) nd
  from d2
)
select symbol,m,
  max(case when ra=1 then open end) o,
  max(case when rd=1 then close end) c,
  max(case when rd=1 then to20 end) heat,
  max(case when rd=1 then volume*1e4/nullif(turnover_rate,0)*raw_close/1e8 end) fcap,
  max(nd) nd
from x group by symbol,m
""").df()
ms = ms.sort_values(["symbol","m"]).reset_index(drop=True)
ms["c_prev"] = ms.groupby("symbol").c_last_ = ms.groupby("symbol")["c"].shift(1)
ms["r"] = ms.c/ms.c_prev - 1
ms["heat_lag"] = ms.groupby("symbol")["heat"].shift(1)
ms["sz"] = ms.groupby("m").fcap.transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))
ms = ms[np.isfinite(ms.r)]
print("个股-月", ms.shape)

HOT = dag.groupby("m").agg(HOT=("frac_hot","mean"), HOTdbl=("frac_dbl","mean"),
                           HOTlu=("frac_lu","mean"), ndays=("date","count")).reset_index()
HOT = HOT[HOT.ndays>=10].reset_index(drop=True)
MKT = ms.groupby("m").r.mean().rename("MKT").reset_index()

def port(col, q=0.8, within=None):
    out=[]
    for m,g in ms.groupby("m"):
        g=g.dropna(subset=[col,"r"])
        if len(g)<200: continue
        if within is None:
            thr=g[col].quantile(q); s=g[g[col]>=thr]; out.append((m,s.r.mean(),len(s)))
        else:
            ps=[]
            for _,gg in g.groupby(within,observed=True):
                if len(gg)<30: continue
                thr=gg[col].quantile(q); ps.append(gg[gg[col]>=thr].r.mean())
            out.append((m,float(np.mean(ps)),len(g)))
    return pd.DataFrame(out,columns=["m","prt","n"])

A = port("heat_lag",0.8)                    # 换手 top20%（前月状态）
B = port("heat_lag",0.8,within="sz")        # 市值分层内 top20%
R = port("heat_lag",0.2)                    # 反向
M = A.merge(B,on="m",suffixes=("","_sz")).merge(R,on="m",suffixes=("","_bot")).merge(MKT,on="m")
M = M.rename(columns={"prt":"A_top","prt_sz":"B_top","prt_bot":"bot"})
M["EX_A"]=M.A_top-M.MKT; M["EX_B"]=M.B_top-M.MKT; M["EX_bot"]=M.bot-M.MKT
print(f"月度 {len(M)} 个月  全期  A_top {M.A_top.mean()*100:+.2f}%/月  MKT {M.MKT.mean()*100:+.2f}%  EX_A {M.EX_A.mean()*100:+.2f}%")
print(f"                        B_top {M.B_top.mean()*100:+.2f}%  EX_B {M.EX_B.mean()*100:+.2f}%  bot {M.bot.mean()*100:+.2f}%")

def roll_rank(s,win=120,minp=36):
    a=s.values; out=np.full(len(a),np.nan)
    for i in range(len(a)):
        if i+1<minp: continue
        h=a[max(0,i-win+1):i+1]; out[i]=(h<a[i]).mean()+0.5*(h==a[i]).mean()
    return pd.Series(out,index=s.index)
HOT=HOT.sort_values("m").reset_index(drop=True)
for c,nm in [("HOT","HQ"),("HOTlu","HQL"),("HOTdbl","HQd")]: HOT[nm]=roll_rank(HOT[c])
HOT["HQfull"]=HOT.HOT.rank(pct=True)
HOT["mkp"]=HOT.m.map(MKT.set_index("m").MKT); HOT["mk3"]=HOT.mkp.rolling(3).mean()
HOT["m_next"]=HOT.m.shift(-1)
pan = HOT.merge(M.rename(columns={"m":"m_next"}), on="m_next").dropna(subset=["HQ"]).reset_index(drop=True)
pan["m"]=pan.m_next
print("面板月数",len(pan))

def nw(y,X,lag=3):
    X=np.asarray(X,float); X=X[:,None] if X.ndim==1 else X
    Xd=np.column_stack([np.ones(len(y)),X]); y=np.asarray(y,float)
    b=np.linalg.lstsq(Xd,y,rcond=None)[0]; e=y-Xd@b
    S=(Xd*e[:,None]).T@(Xd*e[:,None])
    for l in range(1,lag+1):
        w=1-l/(lag+1); A=(Xd[l:]*e[l:,None]).T@(Xd[:-l]*e[:-l,None]); S+=w*(A+A.T)
    V=np.linalg.inv(Xd.T@Xd)@S@np.linalg.inv(Xd.T@Xd)
    return b,b/np.sqrt(np.diag(V))

def quint(col,y):
    q=pan.dropna(subset=[col]).copy()
    q["bin"]=pd.qcut(q[col].rank(method="first"),5,labels=["Q1","Q2","Q3","Q4","Q5"])
    t=q.groupby("bin",observed=True)[y].agg(["mean","count"]); t["mean"]=(t["mean"]*100).round(2); return t,q

def diff_ci(a,b,n=3000,seed=5):
    rng=np.random.default_rng(seed); out=[]
    for _ in range(n):
        out.append(rng.choice(a,len(a)).mean()-rng.choice(b,len(b)).mean())
    return np.percentile(out,[2.5,97.5])

print("\n"+"="*66)
for tag,col in [("A 未控市值","EX_A"),("B 市值分层内","EX_B")]:
    t,q=quint("HQ",col); lo=q[q.bin=="Q1"][col]; hi=q[q.bin=="Q5"][col]
    ci=diff_ci(hi.values,lo.values); b,tt=nw(pan[col].values,(pan.HQ-0.5).values)
    print(f"\n### {tag}  按上月 HOT 分位")
    print(t.to_string())
    print(f"Q5−Q1 {((hi.mean()-lo.mean())*100):+.2f}%/月  95%CI[{ci[0]*100:+.2f},{ci[1]*100:+.2f}]  "
          f"斜率 {b[1]*100:+.2f}%/100%  NW t={tt[1]:+.2f}")

print("\n=== 增量检验 ===")
for col in ["EX_A","EX_B"]:
    b,tt=nw(pan[col].values,pan[["HQ","mkp","mk3"]].assign(c=pan.HQ-0.5)[["c","mkp","mk3"]].values)
    print(f"{col}: HQ {b[1]*100:+.2f}% t={tt[1]:+.2f} | mkt_prev {b[2]:+.3f} t={tt[2]:+.2f} | mkt3 {b[3]:+.3f} t={tt[3]:+.2f}")

rng=np.random.default_rng(11); N=len(pan); null=[]
for _ in range(2000):
    idx=[]
    while len(idx)<N:
        s=rng.integers(0,N-6+1); idx.extend(range(s,s+6))
    idx=np.array(idx[:N]); null.append(np.corrcoef(pan.HQ.values[idx],pan.EX_A.values)[0,1])
real=np.corrcoef(pan.HQ,pan.EX_A)[0,1]
print(f"安慰剂 corr(HQ,EX_A)={real:+.3f} p(左尾)={(np.array(null)<=real).mean():.3f}")

pan["yr"]=pan.m.astype(str).str[:4]
print("\n年度 EX_A(%)："); print((pan.groupby("yr").EX_A.mean()*100).round(2).T.to_string())
print("年度 HQ："); print(pan.groupby("yr").HQ.mean().round(2).T.to_string())
pan.to_parquet("data/.scratch/yaogu_density_panel.parquet",index=False)
HOT.to_parquet("data/.scratch/yaogu_density_hot.parquet",index=False)
