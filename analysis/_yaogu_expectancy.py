"""首板次日开盘买入的 20 年期望 + 妖股密度 regime 序列。"""
import duckdb, pandas as pd, numpy as np

con = duckdb.connect(); con.execute("PRAGMA threads=8")
bench = pd.read_parquet("data/.scratch/mkt_bench.parquet").set_index("date")["cum"]

fb = pd.read_parquet("data/.scratch/first_board.parquet")
syms = sorted(fb.symbol.unique()); con.register("syms", pd.DataFrame({"symbol": syms}))
px = con.execute("""
select symbol,date,open,close from read_parquet('data/kline_daily_enriched/date=*/part.parquet')
where symbol in (select symbol from syms)
union all
select symbol,date,open,close from read_parquet('data/backfill_20261117/stock_gap_daily.parquet')
where symbol in (select symbol from syms)
  and symbol not in (select distinct symbol from read_parquet('data/kline_daily_enriched/date=2026-11-25/part.parquet'))
""").df()
px["date"] = pd.to_datetime(px["date"]); px = px.sort_values(["symbol","date"])
g = {k: v.reset_index(drop=True) for k, v in px.groupby("symbol")}

rows = []
for _, e in fb.iterrows():
    df = g.get(e.symbol)
    if df is None: continue
    pos = np.where(df["date"].values == np.datetime64(pd.Timestamp(e.date)))[0]
    if len(pos) == 0: continue
    j = int(pos[0])
    if j+1 >= len(df): continue
    bo = df["open"].iloc[j+1]
    if not (bo > 0): continue
    rec = {"date": e.date, "symbol": e.symbol, "fcap": e.fcap, "to_pre60": e.to_pre60}
    for h in (2, 5, 10, 20, 60):
        if j+1+h < len(df):
            cT = df["close"].iloc[j+1+h]; tT = df["date"].iloc[j+1+h]; bt = df["date"].iloc[j+1]
            rec[f"r{h}"] = cT/bo - 1
            rec[f"b{h}"] = float(np.exp(bench[tT]-bench[bt]) - 1) if (bt in bench.index and tT in bench.index) else np.nan
            rec[f"e{h}"] = rec[f"r{h}"] - rec[f"b{h}"]
        else:
            for c in (f"r{h}", f"b{h}", f"e{h}"): rec[c] = np.nan
    rows.append(rec)
fr = pd.DataFrame(rows)
fr.to_parquet("data/.scratch/first_board_ret.parquet", index=False)
print("首板样本", len(fr))

print("\n=== 首板 T+1 开盘买入，扣同日全市场等权 ===")
for h in (2, 5, 10, 20, 60):
    s = fr[f"e{h}"].dropna()
    # 按日聚合（同日多个首板不能当独立样本）
    d = fr.dropna(subset=[f"e{h}"]).groupby("date")[f"e{h}"].mean()
    t = d.mean()/d.std()*np.sqrt(len(d))
    print(f"{h:>2}日: 笔均 {s.mean()*100:+.2f}% 中位 {s.median()*100:+.2f}% 日聚合 {d.mean()*100:+.2f}% (N日={len(d)}) t={t:+.2f} 胜率 {(s>0).mean()*100:.1f}%")

print("\n=== 分市值/冷启动（20日超额）===")
q = fr.dropna(subset=["e20"]).assign(
    cap=pd.cut(fr.fcap, [0,20,40,80,150,1e9], labels=["<20","20-40","40-80","80-150",">=150"]),
    cold=pd.cut(fr.to_pre60, [0,0.8,1.5,3,1e9], labels=["<0.8","0.8-1.5","1.5-3",">=3"]))
print(q.groupby(["cap","cold"], observed=True).e20.agg(["mean","median","size"]).assign(
    mean=lambda x:(x["mean"]*100).round(2), median=lambda x:(x["median"]*100).round(2)).to_string())

print("\n=== 按年：首板 20 日超额（日聚合）===")
fr["yr"] = fr.date.astype(str).str[:4]
yy = fr.dropna(subset=["e20"]).groupby(["yr","date"]).e20.mean().reset_index().groupby("yr").e20.agg(["mean","size"])
yy["mean"] = (yy["mean"]*100).round(2)
print(yy.to_string())

# ---- regime 序列 ----
an = pd.read_parquet("data/.scratch/yaogu_anatomy.parquet")
an["yr"] = an.peak_date.astype(str).str[:4]
reg = an[~an.susp].groupby("yr").agg(S=("tier", lambda s: (s=="S").sum()),
                                     all_ep=("tier","size")).reset_index()
mkt = bench.diff().fillna(0)
ann_mkt = (np.exp(mkt.groupby(mkt.index.year).sum()) - 1).rename("mkt_ret").reset_index()
ann_mkt.columns = ["yr","mkt_ret"]
ann_mkt["yr"] = ann_mkt["yr"].astype(str)
reg = reg.merge(ann_mkt, on="yr", how="left")
reg["next_ret"] = reg.mkt_ret.shift(-1)
print("\n=== 年度妖股密度 vs 市场 ===")
print(reg.assign(mkt_ret=lambda x:(x.mkt_ret*100).round(1), next_ret=lambda x:(x.next_ret*100).round(1)).to_string(index=False))
print("\ncorr(S_t, ret_t) =", round(reg.S.corr(reg.mkt_ret),3),
      " corr(S_t, ret_t+1) =", round(reg.S.corr(reg.next_ret),3))
reg.to_parquet("data/.scratch/yaogu_regime.parquet", index=False)
