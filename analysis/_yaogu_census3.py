"""近20年超级妖股：完整普查 v2。

用「60 个交易日内最大涨幅」而不是「固定 60 行前向涨幅」：
    rmax60(t) = close(t) / min(close, t-59..t) - 1
理由：妖股大量诞生在停牌复牌（重组/借壳）之后，固定窗口会因 span60>90 被整段丢掉
（四川双马 2016 就是这样被漏掉的）。取窗口内最低收盘，停牌不会制造假涨幅，
因为起点和终点都是真实交易日。

口径：
  宇宙   2005-01 ~ 2026-11-20，全部 A 股（含退市/吸收合并 307 只）
  剔除   上市后前 250 个交易日（次新连板是发行制度产物）
  形态   lu60 = 过去 60 行涨停天数；clu60 = 过去 60 行最高连板；gapmax = 窗口内最长停牌间隔
  去重   同一 symbol 120 行内只留 rmax60 最高的峰
分级：
  S  rmax60>=3.0 & lu60>=8      A  [2.0,3.0) & lu60>=8
  B  [1.5,2.0) & lu60>=8        C  >=1.5 & lu60<8（非涨停驱动，作对照）
"""
import duckdb, pandas as pd, numpy as np

con = duckdb.connect()
con.execute("PRAGMA threads=8"); con.execute("PRAGMA memory_limit='6GB'")

con.execute("""
create or replace table main as
select symbol, date, close, raw_close, amount, volume, turnover_rate,
       consecutive_limit_ups as clu,
       case when consecutive_limit_ups > 0 then 1 else 0 end as lu
from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where close is not null and close > 0 and date between date '2005-01-01' and date '2026-11-20'
""")
con.execute("""
create or replace table gap as
select symbol, date, close, close as raw_close, amount, volume, null::double as turnover_rate,
       null::uint32 as clu, 0 as lu
from read_parquet('data/backfill_20261117/stock_gap_daily.parquet')
where close is not null and close > 0 and date between date '2005-01-01' and date '2026-11-20'
  and symbol not in (select distinct symbol from main)
""")
con.execute("create or replace table d as select * from main union all select * from gap")

con.execute("""
create or replace table d1 as
select *, date - lag(date) over (partition by symbol order by date) as gaps,
       row_number() over (partition by symbol order by date) as k
from d
""")
con.execute("""
create or replace table w as
select *,
  close / nullif(min(close) over (partition by symbol order by date rows between 59 preceding and current row),0) - 1 as rmax60,
  close / nullif(min(close) over (partition by symbol order by date rows between 19 preceding and current row),0) - 1 as rmax20,
  sum(lu) over (partition by symbol order by date rows between 59 preceding and current row) as lu60,
  max(clu) over (partition by symbol order by date rows between 59 preceding and current row) as clu60,
  max(gaps) over (partition by symbol order by date rows between 59 preceding and current row) as gapmax
from d1
""")
print("rows", con.execute("select count(*) from w").fetchone())

inst = pd.read_parquet("data/instruments/instruments.parquet")[["symbol","name","listing_date"]]
inst["listing_date"] = pd.to_datetime(inst["listing_date"], errors="coerce")
con.register("inst", inst)
con.execute("""
create or replace table ep as
select w.*, i.name, i.listing_date
from w left join inst i using(symbol)
where rmax60 >= 1.5 and lu60 >= 5 and k > 250
order by symbol, date
""")
print("candidates", con.execute("select count(*) from ep").fetchone())

df = con.execute("select * from ep order by symbol, k").df()
kept = []
for sym, g in df.groupby("symbol", sort=False):
    chosen = []
    for idx, row in g.sort_values("rmax60", ascending=False).iterrows():
        if all(abs(row["k"] - c["k"]) > 120 for c in chosen):
            chosen.append(row)
    kept.append(g[g["k"].isin([c["k"] for c in chosen])])
out = pd.concat(kept).sort_values("date").reset_index(drop=True)
print("episodes", len(out))
out["tier"] = np.select(
    [(out.rmax60 >= 3.0) & (out.lu60 >= 8),
     (out.rmax60 >= 2.0) & (out.rmax60 < 3.0) & (out.lu60 >= 8),
     (out.rmax60 >= 1.5) & (out.rmax60 < 2.0) & (out.lu60 >= 8)],
    ["S","A","B"], default="C")
out["yr"] = out.date.astype(str).str[:4]
print(out.tier.value_counts().to_string())
print(out.groupby(["yr","tier"]).size().unstack(fill_value=0).to_string())
print("\n停牌(>=20日)驱动的占比 by tier:")
print(out.assign(susp=(out.gapmax >= 20)).groupby("tier").susp.agg(["mean","size"]).to_string())
out.to_parquet("data/.scratch/yaogu_episodes.parquet", index=False)
