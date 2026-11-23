"""校验 staging 补数据的质量: 覆盖、退市日对齐、价格合法性、与维表一致性。"""
import duckdb
con = duckdb.connect(); con.execute("SET threads=4")
GAP = "D:/DSH/tick-stock-panel/data/_backfill_staging/gap_daily_raw.parquet"
INST = "D:/DSH/tick-stock-panel/data/instruments/instruments.parquet"

print("=== 基本规模 ===")
print(con.sql(f"""SELECT count(*) rows_, count(DISTINCT symbol) ns,
       min(date) mn, max(date) mx, count(DISTINCT date) nd FROM read_parquet('{GAP}')"""))

print("\n=== 每只的行数分布 (抓取完整性) ===")
print(con.sql(f"""WITH s AS (SELECT symbol, count(*) n FROM read_parquet('{GAP}') GROUP BY 1)
SELECT min(n) mn, median(n) md, max(n) mx, count(*) ns FROM s"""))

print("\n=== 价格合法性 (负价/零价/OHLC 不自洽) ===")
print(con.sql(f"""SELECT
  sum(CASE WHEN close<=0 THEN 1 ELSE 0 END) nonpos_close,
  sum(CASE WHEN open<=0 OR high<=0 OR low<=0 THEN 1 ELSE 0 END) nonpos_ohlc,
  sum(CASE WHEN high<low THEN 1 ELSE 0 END) high_lt_low,
  sum(CASE WHEN close>high+1e-6 OR close<low-1e-6 THEN 1 ELSE 0 END) close_out,
  sum(CASE WHEN volume<0 OR amount<0 THEN 1 ELSE 0 END) neg_vol
FROM read_parquet('{GAP}')"""))

print("\n=== 重复行 ===")
print(con.sql(f"""WITH d AS (SELECT symbol,date,count(*) c FROM read_parquet('{GAP}') GROUP BY 1,2)
SELECT count(*) dup_keys, coalesce(sum(c-1),0) dup_rows FROM d WHERE c>1"""))

print("\n=== 末根 K 线分布 (是否为退市日) ===")
print(con.sql(f"""WITH l AS (SELECT symbol, max(date) dlast FROM read_parquet('{GAP}') GROUP BY 1)
SELECT CASE WHEN dlast < DATE '2026-11-01' THEN 'stopped(退市/停牌)' ELSE 'alive' END st,
       count(*) n, min(dlast) mn, max(dlast) mx FROM l GROUP BY 1"""))

print("\n=== 与维表名字一致性 (抽 8 只) ===")
print(con.sql(f"""WITH l AS (SELECT symbol, min(date) dfirst, max(date) dlast, count(*) n FROM read_parquet('{GAP}') GROUP BY 1)
SELECT l.symbol, i.name, l.dfirst, l.dlast, l.n
FROM l JOIN read_parquet('{INST}') i USING (symbol)
WHERE i.name LIKE '%退%' ORDER BY l.dlast LIMIT 8"""))
