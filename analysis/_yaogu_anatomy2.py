"""妖股 episode 解剖 v2（修正 i0 全局下标、流通市值口径、区间天数）。

流通市值口径：volume 单位为手，turnover_rate 为百分数
  float_shares = volume*100 / (turnover_rate/100)
  fcap = float_shares * raw_close
"""
import duckdb, pandas as pd, numpy as np

con = duckdb.connect(); con.execute("PRAGMA threads=8")
ep = pd.read_parquet("data/.scratch/yaogu_episodes.parquet")
syms = sorted(ep.symbol.unique())
con.register("syms", pd.DataFrame({"symbol": syms}))

px = con.execute("""
select symbol,date,open,close,raw_close,amount,volume,turnover_rate,consecutive_limit_ups
from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where symbol in (select symbol from syms)
union all
select symbol,date,open,close,close as raw_close,amount,volume,
       null::double as turnover_rate, null::uint32 as consecutive_limit_ups
from read_parquet('data/backfill_20261117/stock_gap_daily.parquet')
where symbol in (select symbol from syms)
  and symbol not in (select distinct symbol from read_parquet('data/kline_daily_enriched/date=2026-11-25/part.parquet'))
""").df()
px["date"] = pd.to_datetime(px["date"])
px = px.sort_values(["symbol","date"]).reset_index(drop=True)

bench = pd.read_parquet("data/.scratch/mkt_bench.parquet").set_index("date")["cum"]
g = {k: v.reset_index(drop=True) for k, v in px.groupby("symbol")}

rows = []
for _, e in ep.iterrows():
    df = g.get(e.symbol)
    if df is None: continue
    pos = np.where(df["date"].values == np.datetime64(pd.Timestamp(e.date)))[0]
    if len(pos) == 0: continue
    j = int(pos[0])
    if j < 62: continue
    lo = max(0, j-59)
    w = df.iloc[lo: j+1]
    i0 = lo + int(w["close"].values.argmin())
    t0, c0 = df["date"].iloc[i0], df["close"].iloc[i0]
    t1, c1 = df["date"].iloc[j], df["close"].iloc[j]
    seg = df.iloc[i0: j+1]

    pre = df.iloc[max(0, i0-60): i0]
    pre20 = (c0 / df["close"].iloc[i0-20] - 1) if i0 >= 21 else np.nan
    pre60 = (c0 / df["close"].iloc[i0-60] - 1) if i0 >= 61 else np.nan
    pre250 = (c0 / df["close"].iloc[i0-250] - 1) if i0 >= 251 else np.nan
    pre_to = pre["turnover_rate"].mean() if len(pre) else np.nan
    pre_lu = int((pre["consecutive_limit_ups"] > 0).sum()) if len(pre) else np.nan

    tr0, v0, rc0 = df["turnover_rate"].iloc[i0], df["volume"].iloc[i0], df["raw_close"].iloc[i0]
    fcap = (v0*100/(tr0/100)*rc0/1e8) if (tr0 and tr0 > 0 and rc0) else np.nan

    b_open = df["open"].iloc[j+1] if j+1 < len(df) else np.nan
    bt = df["date"].iloc[j+1] if j+1 < len(df) else pd.NaT
    fwd = {}
    for h in (5, 20, 60, 250):
        if j+1+h < len(df) and b_open == b_open and b_open > 0:
            cT = df["close"].iloc[j+1+h]; tT = df["date"].iloc[j+1+h]
            fwd[f"f{h}"] = cT/b_open - 1
            fwd[f"b{h}"] = float(np.exp(bench[tT] - bench[bt]) - 1) if (bt in bench.index and tT in bench.index) else np.nan
        else:
            fwd[f"f{h}"] = np.nan; fwd[f"b{h}"] = np.nan
    post = df.iloc[j+1: j+251]
    rows.append(dict(
        symbol=e.symbol, name=e.name, peak_date=t1, start_date=t0, tier=e.tier,
        gain=c1/c0-1, ndays=len(seg), lu=len(seg[seg.consecutive_limit_ups > 0]),
        maxlu=seg["consecutive_limit_ups"].max(), avg_to=seg["turnover_rate"].mean(),
        fcap=fcap, pre20=pre20, pre60=pre60, pre250=pre250, pre_to=pre_to, pre_lu=pre_lu,
        gapmax=int(e.gapmax), susp=bool(e.gapmax >= 20), peak_close=c1,
        f5=fwd["f5"], f20=fwd["f20"], f60=fwd["f60"], f250=fwd["f250"],
        b5=fwd["b5"], b20=fwd["b20"], b60=fwd["b60"], b250=fwd["b250"],
        post_dd250=(post["close"].min()/c1 - 1) if len(post) else np.nan,
    ))

a = pd.DataFrame(rows)
a["yr"] = a.peak_date.astype(str).str[:4]
for h in (5, 20, 60, 250): a[f"ex{h}"] = a[f"f{h}"] - a[f"b{h}"]
a.to_parquet("data/.scratch/yaogu_anatomy.parquet", index=False)
print(a.shape, a.yr.min(), a.yr.max())
print(a.groupby("tier")[["gain","ndays","lu","maxlu","avg_to","fcap","pre20","pre60","pre250","pre_to","post_dd250","ex20","ex60","ex250"]].median().round(3).to_string())
print("\n样本完整度：ex250 非空", a.ex250.notna().sum(), "/", len(a))
