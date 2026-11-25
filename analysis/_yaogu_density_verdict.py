"""按 docs/yaogu-density-prereg.md §4/§5 逐条判决 + 稳健性。"""
import duckdb, numpy as np, pandas as pd
con=duckdb.connect(); con.execute("PRAGMA threads=8"); con.execute("PRAGMA memory_limit='7GB'")
pan=pd.read_parquet("data/.scratch/yaogu_density_panel.parquet")
hot=pd.read_parquet("data/.scratch/yaogu_density_hot.parquet")

# 需要 r20 版组合：重新取（沿用 v3 的月度表）
con.execute("""
create or replace table d as
select symbol,date,close,turnover_rate,row_number() over (partition by symbol order by date) k
from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where close>0 and date between date '2005-01-01' and date '2026-11-25'
""")
con.execute("""
create or replace table d2 as
select *, date_trunc('month',date) m,
  close/nullif(lag(close,20) over (partition by symbol order by date),0)-1 as r20
from d where k>250 and not (symbol like '920%' or symbol like '8%' or symbol like '4%')
""")
ms=con.execute("""
with x as (select symbol,m,date,close,r20,
  row_number() over (partition by symbol,m order by date desc) rd from d2)
select symbol,m,max(case when rd=1 then close end) c,max(case when rd=1 then r20 end) r20
from x group by symbol,m""").df()
ms=ms.sort_values(["symbol","m"]); ms["c_prev"]=ms.groupby("symbol").c.shift(1)
ms["r"]=ms.c/ms.c_prev-1; ms["r20_lag"]=ms.groupby("symbol").r20.shift(1)
ms=ms[np.isfinite(ms.r)]
MKT=ms.groupby("m").r.mean().rename("MKT").reset_index()

def port(col,q=0.8):
    out=[]
    for m,g in ms.groupby("m"):
        g=g.dropna(subset=[col,"r"])
        if len(g)<200: continue
        thr=g[col].quantile(q); out.append((m,g[g[col]>=thr].r.mean()))
    return pd.DataFrame(out,columns=["m","p"])
C=port("r20_lag").merge(MKT,on="m"); C["EX_C"]=C.p-C.MKT

pan=pan.merge(C[["m_next","EX_C"]].rename(columns={"m_next":"m_next"}).assign(m=pan.m_next) if False else C.rename(columns={"m":"m_next"})[["m_next","EX_C"]],on="m_next",how="left")

def nw_diff(y,d,lag=3):
    """y ~ 1 + d 的 NW t（d 为 0/1）。"""
    X=np.column_stack([np.ones(len(y)),d]); y=np.asarray(y,float)
    b=np.linalg.lstsq(X,y,rcond=None)[0]; e=y-X@b
    S=(X*e[:,None]).T@(X*e[:,None])
    for l in range(1,lag+1):
        w=1-l/(lag+1); A=(X[l:]*e[l:,None]).T@(X[:-l]*e[:-l,None]); S+=w*(A+A.T)
    V=np.linalg.inv(X.T@X)@S@np.linalg.inv(X.T@X)
    return b[1], b[1]/np.sqrt(V[1,1])

def test(df,col,hqcol="HQ",label=""):
    d=df.dropna(subset=[col,hqcol])
    if len(d)<40: return None
    lo=d[d[hqcol]<=0.2]; hi=d[d[hqcol]>=0.8]
    sub=pd.concat([lo,hi]).sort_values("m")
    b,t=nw_diff(sub[col].values,(sub[hqcol]>=0.8).astype(float).values)
    return dict(spec=label,n=len(d),q1=lo[col].mean()*100,q5=hi[col].mean()*100,
                diff=b*100,t=t)

rows=[]
rows.append(test(pan,"EX_A",label="A 主检验：to20 / 未控市值"))
rows.append(test(pan,"EX_B",label="B 事后：to20 / 市值分层内"))
rows.append(test(pan,"EX_C",label="C 换 heat：20日涨幅"))
rows.append(test(pan,"EX_A","HQL",label="D 换密度：涨停占比"))
rows.append(test(pan,"EX_A","HQfull",label="E 全样本分位（含前视，上界）"))
rows.append(test(pan[pan.yr!="2015"],"EX_A",label="F 剔除 2015"))
rows.append(test(pan,"EX_bot",label="G 反向：低换手组合（符号应相反）"))
R=pd.DataFrame([r for r in rows if r])
R["q1"]=R.q1.round(2); R["q5"]=R.q5.round(2); R["diff"]=R["diff"].round(2); R["t"]=R.t.round(2)
print("=== 预注册判据 ===")
m=test(pan,"EX_A",label="主")
print(f"P1 Q5−Q1 NW t ≤ −2.0 : t={m['t']:+.2f}  → {'PASS' if m['t']<=-2.0 else 'FAIL'}")
print(f"P3 控过去市场收益后 b t ≤ −1.5 : 见 v3 输出 (−1.01 / −1.21) → FAIL")
print("P2 bootstrap CI 全负 : [−2.41,+0.37] 含 0 → FAIL")
print("P4 安慰剂 p<0.05 : p=0.004 → PASS")
print("\n=== 稳健性（Q5−Q1，%/月）===")
print(R.to_string(index=False))
pan.to_parquet("data/.scratch/yaogu_density_panel.parquet",index=False)
