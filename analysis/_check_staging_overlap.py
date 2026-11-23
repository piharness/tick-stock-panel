"""校验 staging 与本地既有数据的重叠段一致性（指数 / ETF / 缺口股）。"""
import duckdb
con = duckdb.connect(); con.execute("SET threads=4")
D = "D:/DSH/tick-stock-panel/data"

def check(name, new_path, local_dir, key="close"):
    q = f"""
    WITH new AS (SELECT symbol, CAST(date AS DATE) d, {key} v FROM read_parquet('{new_path}')),
         loc AS (SELECT symbol, date d, {key} v FROM read_parquet('{local_dir}/**/*.parquet'))
    SELECT count(*) n_overlap,
           sum(CASE WHEN abs(new.v-loc.v) <= 0.01 THEN 1 ELSE 0 END) within_1f,
           round(100.0*sum(CASE WHEN abs(new.v-loc.v) <= 0.01 THEN 1 ELSE 0 END)/count(*), 3) pct,
           round(max(abs(new.v-loc.v)), 4) max_abs,
           round(median(abs(new.v-loc.v)), 6) med_abs
    FROM new JOIN loc USING (symbol, d)"""
    r = con.sql(q).fetchone()
    print(f"{name:14s} 重叠 {r[0]:>9,}  差≤0.01元 {r[1]:>9,} ({r[2]:6.2f}%)  中位 {r[4]:.6f}  最大 {r[3]}")

print("=" * 90)
print("重叠段一致性（同一 symbol + 同一交易日）")
print("=" * 90)
check("index", f"{D}/_backfill_staging/index_daily_raw.parquet", f"{D}/kline_index_daily")
check("etf", f"{D}/_backfill_staging/etf_daily_raw.parquet", f"{D}/kline_etf_daily")

print("\n=== 指数覆盖提升 ===")
print(con.sql(f"""
SELECT 'before' src, count(DISTINCT date) nd, count(DISTINCT symbol) ns, min(date) mn FROM read_parquet('{D}/kline_index_daily/**/*.parquet')
UNION ALL
SELECT 'after', count(DISTINCT date), count(DISTINCT symbol), min(date) FROM read_parquet('{D}/_backfill_staging/index_daily_raw.parquet')""").df().to_string(index=False))

print("\n=== ETF 覆盖提升 ===")
print(con.sql(f"""
SELECT 'before' src, count(DISTINCT date) nd, count(DISTINCT symbol) ns, min(date) mn FROM read_parquet('{D}/kline_etf_daily/**/*.parquet')
UNION ALL
SELECT 'after', count(DISTINCT date), count(DISTINCT symbol), min(date) FROM read_parquet('{D}/_backfill_staging/etf_daily_raw.parquet')""").df().to_string(index=False))
