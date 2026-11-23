"""2026 年「1 元附近」ST 撬板案例：最接近 *ST华幸 的对照组。"""
from __future__ import annotations
import os
from pathlib import Path
import polars as pl
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"data"
files=[str(Path(e.path)/"part.parquet") for e in os.scandir(DATA/"kline_daily_enriched")
       if e.is_dir() and e.name.startswith("date=") and e.name[5:]>="2025-10-01"]
files.sort()
d=pl.read_parquet(files,columns=["symbol","date","low","close","amount"]).filter((pl.col("close")>0)&(pl.col("low")>0)).sort(["symbol","date"])
inst=pl.read_parquet(DATA/"instruments/instruments.parquet").select("symbol","name")
nm=pl.col("name").fill_null("")
inst=inst.filter(nm.str.starts_with("ST")|nm.str.starts_with("*ST"))
d=d.with_columns(pl.col("close").shift(1).over("symbol").alias("prev"),
    pl.col("date").shift(1).over("symbol").alias("pd"),
    pl.col("close").shift(-1).over("symbol").alias("c1"),
    pl.col("close").shift(-5).over("symbol").alias("c5"),
    pl.col("close").shift(-10).over("symbol").alias("c10")).filter(pl.col("prev").is_not_null())
d=d.with_columns(((pl.col("date")-pl.col("pd")).dt.total_days()<=7).alias("adj"),
    (pl.col("low")/pl.col("prev")-1).alias("low_ret"),(pl.col("close")/pl.col("prev")-1).alias("ret"),
    (pl.col("close")/pl.col("low")-1).alias("rebound"))
d=d.filter(pl.col("adj"))
q=d.join(inst,on="symbol",how="inner").filter(
    (pl.col("date")>=pl.date(2026,1,1)) & (pl.col("close")<1.3) & (pl.col("low_ret")<=-0.045) & (pl.col("rebound")>=0.005))
q=q.with_columns((pl.col("c1")/pl.col("close")-1).alias("r1"),(pl.col("c5")/pl.col("close")-1).alias("r5"),
    (pl.col("c10")/pl.col("close")-1).alias("r10"),(pl.col("amount")/1e8).alias("amt"))
print("1.3 元以下 ST 撬板样本",q.height)
print("\n日期        代码       名称      收盘   最低%   收盘%  反弹%  成交亿  次日%   5日%   10日%")
for r in q.sort("amount",descending=True).head(20).iter_rows(named=True):
    f=lambda x: f"{x*100:>6.2f}" if x is not None else "   n/a"
    print(f"{r['date']} {r['symbol']:<10} {r['name']:<7} {r['close']:>5.2f} {r['low_ret']*100:>6.2f} {r['ret']*100:>6.2f} {r['rebound']*100:>5.2f} {r['amt']:>6.2f} {f(r['r1'])} {f(r['r5'])} {f(r['r10'])}")
print("\n=== 该子集后续 ===")
for c,l in [("r1","次日"),("r5","5日"),("r10","10日")]:
    s=q[c].drop_nulls(); print(f"{l}: n={s.len()} 均值 {s.mean()*100:>6.2f}% 中位 {s.median()*100:>6.2f}% 胜率 {(s>0).mean()*100:>5.1f}%")
print("\n不同股票数",q["symbol"].n_unique())
