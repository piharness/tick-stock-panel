"""面值倒计时中段的救回率：连续<1元到第5日时，剩余15日能否回到>=1元。"""
from __future__ import annotations
import datetime as dt, os
from pathlib import Path
import polars as pl

ROOT = Path(__file__).resolve().parents[1]; DATA = ROOT/"data"
files=[str(Path(e.path)/"part.parquet") for e in os.scandir(DATA/"kline_daily_enriched")
       if e.is_dir() and e.name.startswith("date=") and e.name[5:]>="2016-01-01"]
files.sort()
live=pl.read_parquet(files,columns=["symbol","date","raw_close"]).filter(pl.col("raw_close")>0).with_columns(pl.lit(True).alias("listed_now"))
gap=pl.read_parquet(DATA/"backfill_20261117"/"stock_gap_daily.parquet").select("symbol","date","close").rename({"close":"raw_close"}).filter(pl.col("raw_close")>0,pl.col("date")>=dt.date(2016,1,1)).with_columns(pl.lit(False).alias("listed_now"))
ls=set(live["symbol"].unique().to_list()); gap=gap.filter(~pl.col("symbol").is_in(list(ls)))
allx=pl.concat([live,gap],how="vertical").sort(["symbol","date"]).with_columns((pl.col("raw_close")<1.0).alias("sub1"))

rows=[]
for k,g in allx.group_by("symbol"):
    sym=k[0]; g=g.sort("date")
    f=g["sub1"].to_list(); c=g["raw_close"].to_list(); d=g["date"].to_list(); ln=g["listed_now"][0]
    i=0; n=len(f)
    while i<n:
        if not f[i]: i+=1; continue
        j=i
        while j<n and f[j]: j+=1
        L=j-i
        if L>=5:
            k5=i+4                      # 倒计时第 5 日
            rest=c[k5+1:k5+16]          # 之后 15 个交易日
            rows.append({"symbol":sym,"start":d[i],"run_len":L,"listed_now":ln,
                         "rest_max":max(rest) if rest else None,
                         "recover15":bool(rest and max(rest)>=1.0)})
        i=j
r=pl.DataFrame(rows)
r=r.filter(pl.col("rest_max").is_not_null())
print("样本（连续<1元 >=5 日的段）",r.height)
print(r.group_by("listed_now").agg(pl.len().alias("n"),pl.col("recover15").mean().alias("rate"),
      pl.col("run_len").median().alias("med_len")).sort("listed_now"))
print("\n按段长分组：")
print(r.group_by((pl.col("run_len")//5*5).alias("len_bucket")).agg(
    pl.len().alias("n"),pl.col("recover15").mean().alias("rate")).sort("len_bucket"))
print("\n最终退市样本里，第5日之后15日内回到1元以上的比例：",
      round(r.filter(~pl.col("listed_now"))["recover15"].mean()*100,2),"%")
print("当前仍在上市样本：",round(r.filter(pl.col("listed_now"))["recover15"].mean()*100,2),"%")
