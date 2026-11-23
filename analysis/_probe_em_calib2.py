"""二次口径校验: 分别对齐 raw_close↔fqt=0 与 close↔fqt=1。"""
import duckdb
con = duckdb.connect(); con.execute("SET threads=4")
EM = r"D:/DSH/tick-stock-panel/data/_probe_em/probe.parquet"
ENR = r"D:/DSH/tick-stock-panel/data/kline_daily_enriched/**/*.parquet"
syms = ["600000.SH","000001.SZ","601318.SH","300750.SZ","688981.SH"]
inlist = ",".join(f"'{s}'" for s in syms)

print("=== A: 本地 raw_close  vs  东财 fqt=0 (原始价) ===")
print(con.sql(f"""
WITH em AS (SELECT symbol, CAST(date AS DATE) d, close em_close, volume em_vol FROM read_parquet('{EM}') WHERE symbol IN ({inlist})),
     lo AS (SELECT symbol, date d, raw_close lo_raw, close lo_adj, volume lo_vol FROM read_parquet('{ENR}') WHERE symbol IN ({inlist}))
SELECT em.symbol, count(*) n,
       max(abs(em.em_close-lo.lo_raw)/nullif(lo.lo_raw,0)) max_rel_raw,
       max(abs(em.em_vol-lo.lo_vol)/nullif(lo.lo_vol,0)) max_rel_vol
FROM em JOIN lo USING (symbol,d) GROUP BY 1 ORDER BY 1"""))

print("\n=== B: 本地 close (前复权) vs 东财 fqt=1 (前复权) ===")
# 需要先抓 fqt=1 对照
import json, urllib.request, time
import pandas as pd
UA = {"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"}
def fetch(secid, fqt):
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt={fqt}"
           f"&beg=19901219&end=20500101&lmt=100000")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        d = json.loads(r.read().decode())["data"]
    rows = [k.split(",") for k in d["klines"]]
    df = pd.DataFrame(rows, columns=["date","open","close","high","low","volume","amount"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ["open","close","high","low","volume","amount"]: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df
MAP = {"600000.SH":"1.600000","000001.SZ":"0.000001","601318.SH":"1.601318","300750.SZ":"0.300750","688981.SH":"1.688981"}
frames=[]
for sym, secid in MAP.items():
    df = fetch(secid, 1); df.insert(0,"symbol",sym); frames.append(df); time.sleep(0.08)
pd.concat(frames).to_parquet(r"D:/DSH/tick-stock-panel/data/_probe_em/probe_qfq.parquet", index=False)

print(con.sql(f"""
WITH em AS (SELECT symbol, CAST(date AS DATE) d, close em_adj FROM read_parquet('D:/DSH/tick-stock-panel/data/_probe_em/probe_qfq.parquet') WHERE symbol IN ({inlist})),
     lo AS (SELECT symbol, date d, close lo_adj FROM read_parquet('{ENR}') WHERE symbol IN ({inlist}))
SELECT em.symbol, count(*) n, max(abs(em.em_adj-lo.lo_adj)/nullif(lo.lo_adj,0)) max_rel_qfq
FROM em JOIN lo USING (symbol,d) GROUP BY 1 ORDER BY 1"""))
