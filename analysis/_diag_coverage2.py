"""细查: 指数/ETF/财务/主档/龙虎榜/因子 的覆盖结构, 以及生存偏差可见性。"""
import duckdb
from pathlib import Path

D = Path(r"D:/DSH/tick-stock-panel/data")
con = duckdb.connect(); con.execute("SET threads=4")

def q(sql, title):
    print(f"\n--- {title} ---")
    try:
        print(con.sql(sql))
    except Exception as e:
        print("ERR", type(e).__name__, str(e)[:200])

def P(rel):
    return (D / rel).as_posix()

q(f"SELECT count(*) n, count(DISTINCT symbol) ns, min(date) mn, max(date) mx, count(DISTINCT date) nd FROM read_parquet('{P('kline_index_daily/**/*.parquet')}')", "index_daily")
q(f"SELECT substr(symbol,1,3) pre, count(DISTINCT symbol) ns FROM read_parquet('{P('kline_index_daily/**/*.parquet')}') GROUP BY 1 ORDER BY ns DESC LIMIT 12", "index 前缀")

q(f"DESCRIBE SELECT * FROM read_parquet('{P('adj_factor/all.parquet')}')", "adj_factor schema")
q(f"SELECT count(*) n, count(DISTINCT symbol) ns, min(date) mn, max(date) mx, count(DISTINCT date) nd FROM read_parquet('{P('adj_factor/all.parquet')}')", "adj_factor 覆盖")

q(f"DESCRIBE SELECT * FROM read_parquet('{P('instruments/instruments.parquet')}')", "instruments schema")
q(f"SELECT count(*) n FROM read_parquet('{P('instruments/instruments.parquet')}')", "instruments 行数")

for t in ["balance_sheet", "income", "cash_flow", "metrics", "shares"]:
    q(f"SELECT count(*) n, count(DISTINCT symbol) ns, count(DISTINCT report_date) nd, min(report_date) mn, max(report_date) mx FROM read_parquet('{P('financials/'+t+'/**/*.parquet')}')", f"financials/{t}")
