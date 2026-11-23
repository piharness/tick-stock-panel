"""校验时点股本: 覆盖、最新值 vs 维表、能否构造无前视历史市值。"""
import duckdb
con = duckdb.connect(); con.execute("SET threads=4")
S = "D:/DSH/tick-stock-panel/data/_backfill_staging/fin_shares.parquet"
I = "D:/DSH/tick-stock-panel/data/instruments/instruments.parquet"

print("=== 覆盖 ===")
print(con.sql(f"""SELECT count(*) rows_, count(DISTINCT symbol) ns,
       min(end_date) mn, max(end_date) mx FROM read_parquet('{S}')"""))

print("\n=== 每只事件数 ===")
print(con.sql(f"""WITH s AS (SELECT symbol, count(*) n FROM read_parquet('{S}') GROUP BY 1)
SELECT min(n) mn, median(n) md, max(n) mx, count(*) ns FROM s"""))

print("\n=== 最新股本 vs 维表 total_shares (校验可信度) ===")
print(con.sql(f"""
WITH last AS (
  SELECT symbol, total_shares, end_date,
         row_number() OVER (PARTITION BY symbol ORDER BY end_date DESC) rn
  FROM read_parquet('{S}') WHERE total_shares > 0
)
SELECT count(*) n_compared,
       sum(CASE WHEN abs(l.total_shares - i.total_shares)/i.total_shares < 0.01 THEN 1 ELSE 0 END) within_1pct,
       sum(CASE WHEN abs(l.total_shares - i.total_shares)/i.total_shares >= 0.01 THEN 1 ELSE 0 END) mismatch,
       median(abs(l.total_shares - i.total_shares)/i.total_shares) med_dev
FROM last l JOIN read_parquet('{I}') i USING (symbol)
WHERE l.rn = 1 AND i.total_shares > 0"""))

print("\n=== 偏差最大的 5 只 (找口径差) ===")
print(con.sql(f"""
WITH last AS (
  SELECT symbol, total_shares, end_date, row_number() OVER (PARTITION BY symbol ORDER BY end_date DESC) rn
  FROM read_parquet('{S}') WHERE total_shares > 0)
SELECT l.symbol, i.name, l.total_shares sh_em, i.total_shares sh_inst,
       round(abs(l.total_shares-i.total_shares)/i.total_shares*100,2) pct, l.end_date
FROM last l JOIN read_parquet('{I}') i USING (symbol)
WHERE l.rn=1 AND i.total_shares>0 ORDER BY abs(l.total_shares-i.total_shares)/i.total_shares DESC LIMIT 5"""))

print("\n=== 历史市值可算性: 取 2010-01-04 的时点数 ===")
print(con.sql(f"""
WITH px AS (SELECT symbol, date, raw_close FROM read_parquet('D:/DSH/tick-stock-panel/data/kline_daily_enriched/date=2010-01-04/*.parquet')),
     sh AS (SELECT symbol, total_shares FROM read_parquet('{S}')
            WHERE end_date <= DATE '2010-01-04'
              AND end_date = (SELECT max(end_date) FROM read_parquet('{S}') s2
                              WHERE s2.symbol = fin_shares.symbol AND s2.end_date <= DATE '2010-01-04'))
SELECT count(*) n, count(sh.total_shares) with_shares,
       round(median(px.raw_close * sh.total_shares)/1e8, 2) med_mktcap_yi
FROM px LEFT JOIN sh USING (symbol)"""))
