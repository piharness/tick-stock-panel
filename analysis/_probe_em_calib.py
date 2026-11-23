"""口径校验: 东财全历史 vs 本地 enriched 重叠段逐字段比对。

判定: 若 close/volume/amount 最大相对偏差 < 1e-6 且行集合差为空, 则同源同口径, 可直接补。
"""
import duckdb
from pathlib import Path

con = duckdb.connect(); con.execute("SET threads=4")
EM = r"D:/DSH/tick-stock-panel/data/_probe_em/probe.parquet"
ENR = r"D:/DSH/tick-stock-panel/data/kline_daily_enriched/**/*.parquet"

syms = ["600000.SH", "000001.SZ", "601318.SH", "300750.SZ", "688981.SH"]
inlist = ",".join(f"'{s}'" for s in syms)

q = f"""
WITH em AS (
  SELECT symbol, CAST(date AS DATE) d, close, volume, amount FROM read_parquet('{EM}')
  WHERE symbol IN ({inlist})
), lo AS (
  SELECT symbol, date d, close, volume, amount FROM read_parquet('{ENR}')
  WHERE symbol IN ({inlist})
)
SELECT em.symbol,
       count(*) n_em, count(lo.d) n_matched,
       count(*) - count(lo.d) n_only_em,
       max(abs(em.close - lo.close) / nullif(lo.close,0)) max_rel_close,
       max(abs(em.volume - lo.volume) / nullif(lo.volume,0)) max_rel_vol,
       max(abs(em.amount - lo.amount) / nullif(lo.amount,0)) max_rel_amt
FROM em LEFT JOIN lo USING (symbol, d)
GROUP BY 1 ORDER BY 1
"""
print("=== 重叠段逐字段偏差 (相对) ===")
print(con.sql(q))

# 本地独有的交易日 (东财缺)
q2 = f"""
WITH em AS (SELECT symbol, CAST(date AS DATE) d FROM read_parquet('{EM}') WHERE symbol IN ({inlist})),
     lo AS (SELECT DISTINCT symbol, date d FROM read_parquet('{ENR}') WHERE symbol IN ({inlist}))
SELECT lo.symbol, count(*) n_local_only, min(lo.d) mn, max(lo.d) mx
FROM lo ANTI JOIN em USING (symbol, d) GROUP BY 1 ORDER BY 1
"""
print("\n=== 本地有、东财缺的交易日 ===")
print(con.sql(q2))
