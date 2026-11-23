"""验证: 用 adj_factor 的 ex_factor 能否重算出本地的复权比例 (决定补数后能否自动生成复权价)。"""
import duckdb
con = duckdb.connect(); con.execute("SET threads=4")

print("=== 600000 除权事件样本 ===")
print(con.sql("""
SELECT symbol, trade_date, ex_factor, dividend, bonus, allot, allot_price, prev_close
FROM read_parquet('D:/DSH/tick-stock-panel/data/adj_factor/all.parquet')
WHERE symbol='600000.SH' ORDER BY trade_date DESC LIMIT 8"""))

# ex_factor 的定义假设: 每单位老股的除权后价值 / 除权前价 = 1/ex_factor 或类似
# 校验: 用 ex_factor 连乘得到 2005-01-04 相对最新日的比例, 与 0.801788/6.88 比
print("\n=== 连乘校验 ===")
print(con.sql("""
WITH ev AS (
  SELECT symbol, trade_date, ex_factor FROM read_parquet('D:/DSH/tick-stock-panel/data/adj_factor/all.parquet')
  WHERE symbol='600000.SH' AND trade_date > '2005-01-04'
), agg AS (SELECT symbol, exp(sum(ln(ex_factor))) AS cum, count(*) n FROM ev GROUP BY 1)
SELECT symbol, n, cum, 1.0/cum AS ratio_if_div, cum AS ratio_if_mul,
       0.801787778010396/6.88 AS local_ratio
FROM agg"""))

print("\n=== 全市场 ex_factor 连乘后与本地 ratio 的偏差分布 ===")
print(con.sql("""
WITH ev AS (
  SELECT symbol, exp(sum(ln(ex_factor))) cum FROM read_parquet('D:/DSH/tick-stock-panel/data/adj_factor/all.parquet')
  WHERE ex_factor > 0 GROUP BY 1
), loc AS (
  SELECT symbol, close/raw_close r FROM read_parquet('D:/DSH/tick-stock-panel/data/kline_daily_enriched/date=2005-01-04/*.parquet') WHERE raw_close>0
)
SELECT count(*) n,
       sum(CASE WHEN abs(ev.cum*loc.r - 1) < 0.02 THEN 1 ELSE 0 END) within_2pct,
       median(abs(ev.cum*loc.r - 1)) med_dev
FROM ev JOIN loc USING (symbol)"""))
