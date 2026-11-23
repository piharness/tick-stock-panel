"""续: financials 实际列名 + 覆盖; adj_factor; 生存偏差(退市股)。"""
import duckdb
from pathlib import Path

D = Path(r"D:/DSH/tick-stock-panel/data")
con = duckdb.connect(); con.execute("SET threads=4")
def P(rel): return (D / rel).as_posix()
def q(sql, title):
    print(f"\n--- {title} ---")
    try: print(con.sql(sql))
    except Exception as e: print("ERR", type(e).__name__, str(e)[:160])

q(f"DESCRIBE SELECT * FROM read_parquet('{P('financials/balance_sheet/**/*.parquet')}')", "bs schema")
for t in ["balance_sheet", "income", "cash_flow", "metrics", "shares"]:
    q(f"SELECT count(*) n, count(DISTINCT symbol) ns, min(period_end) mn, max(period_end) mx, count(DISTINCT period_end) np FROM read_parquet('{P('financials/'+t+'/**/*.parquet')}')", f"financials/{t}")

q(f"SELECT count(*) n, count(DISTINCT symbol) ns, min(trade_date) mn, max(trade_date) mx FROM read_parquet('{P('adj_factor/all.parquet')}')", "adj_factor")
q(f"SELECT symbol, count(*) c, min(trade_date) mn, max(trade_date) mx FROM read_parquet('{P('adj_factor/all.parquet')}') GROUP BY 1 ORDER BY c DESC LIMIT 5", "adj_factor 每只条数")

# 生存偏差: instruments 中退市/更名可见性
q("SELECT type, count(*) n FROM read_parquet('" + P('instruments/instruments.parquet') + "') GROUP BY 1 ORDER BY n DESC", "instruments type 分布")
q("SELECT count(*) n FROM read_parquet('" + P('instruments/instruments.parquet') + "') WHERE name LIKE '%退%'", "名字含退")
q("SELECT count(*) n FROM read_parquet('" + P('instruments/instruments.parquet') + "') WHERE name LIKE 'ST%' OR name LIKE '%ST%'", "名字含ST")

# 日K 里出现过的标的 vs 主档标的
q(f"SELECT count(DISTINCT symbol) FROM read_parquet('{P('kline_daily/**/*.parquet')}')", "日K 标的数")
