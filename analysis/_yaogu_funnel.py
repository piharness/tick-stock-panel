"""首板漏斗：20 年全样本的连板条件概率。

问题：给定一个可识别的起点（首板），走到「超级妖股」的概率是多少？
分母必须是「所有首板」，不是「所有成为妖股的股票」。
"""
import duckdb, pandas as pd, numpy as np

con = duckdb.connect(); con.execute("PRAGMA threads=8"); con.execute("PRAGMA memory_limit='6GB'")
con.execute("""
create or replace table d as
select symbol,date,close,raw_close,volume,turnover_rate,consecutive_limit_ups as clu,
       case when consecutive_limit_ups>0 then 1 else 0 end as lu,
       row_number() over (partition by symbol order by date) as k
from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where close>0 and date between date '2005-01-01' and date '2026-11-20'
""")
con.execute("""
create or replace table f as
select *,
  sum(lu) over (partition by symbol order by date rows between 61 preceding and 2 preceding) as lu_pre60,
  avg(turnover_rate) over (partition by symbol order by date rows between 61 preceding and 2 preceding) as to_pre60,
  max(clu) over (partition by symbol order by date rows between 62 preceding and 1 preceding) as clu_prev,
  max(clu) over (partition by symbol order by date rows between current row and 59 following) as clu_next60
from d
""")
con.execute("""
create or replace table first_board as
select *,
  volume*100/(turnover_rate/100)*raw_close/1e8 as fcap
from f
where clu=1 and k>250 and coalesce(lu_pre60,0)=0 and turnover_rate>0
""")
print("首板事件", con.execute("select count(*), count(distinct symbol), count(distinct date) from first_board").fetchone())

q = """
select
  case when fcap<20 then 'a <20亿' when fcap<40 then 'b 20-40亿'
       when fcap<80 then 'c 40-80亿' when fcap<150 then 'd 80-150亿' else 'e >=150亿' end as cap,
  case when to_pre60<0.8 then 'a <0.8%' when to_pre60<1.5 then 'b 0.8-1.5%'
       when to_pre60<3 then 'c 1.5-3%' else 'd >=3%' end as cold,
  count(*) n,
  avg(case when clu_next60>=2 then 1.0 else 0 end) p2,
  avg(case when clu_next60>=3 then 1.0 else 0 end) p3,
  avg(case when clu_next60>=5 then 1.0 else 0 end) p5,
  avg(case when clu_next60>=8 then 1.0 else 0 end) p8,
  avg(case when clu_next60>=12 then 1.0 else 0 end) p12
from first_board group by 1,2 order by 1,2
"""
r = con.execute(q).df()
for c in ["p2","p3","p5","p8","p12"]: r[c] = (r[c]*100).round(2)
print("\n首板 → 60日内到达某连板高度的概率（%）")
print(r.to_string(index=False))

r2 = con.execute("""
select substr(cast(date as varchar),1,4) yr, count(*) n,
  avg(case when clu_next60>=2 then 1.0 else 0 end) p2,
  avg(case when clu_next60>=5 then 1.0 else 0 end) p5,
  avg(case when clu_next60>=8 then 1.0 else 0 end) p8
from first_board group by 1 order by 1
""").df()
for c in ["p2","p5","p8"]: r2[c] = (r2[c]*100).round(2)
print("\n按年（%）")
print(r2.to_string(index=False))

con.execute("create table fb as select * from first_board")
con.execute("copy fb to 'data/.scratch/first_board.parquet' (format parquet)")
