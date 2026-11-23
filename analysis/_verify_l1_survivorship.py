"""L1 缺口反转: 生存偏差敏感性检验。

L1 口径 (照搬 `decision/l1_cohort.py`):
    T 日收盘  : 全市场按 mom20 = close[T]/close[T-20]-1 **降序**取前 20
    T+1 开盘  : 其中 open <= close[T]*(1-1%) 的才买
    T+1 收盘  : 卖出, 收益 = close[T+1]/open[T+1] - 1

要证的是: 退市股永远不会进入 mom20 前 20, 所以补全**不应**改变 L1 收益。
若 changed ≈ 0, 则该结论对生存偏差免疫; 若显著变化, 说明我漏了什么。
"""
from __future__ import annotations

import duckdb

D = "D:/DSH/tick-stock-panel/data"
con = duckdb.connect()
con.execute("SET threads=4")
con.execute(f"CREATE OR REPLACE TABLE cal AS SELECT date, row_number() OVER (ORDER BY date) rn FROM (SELECT DISTINCT date FROM read_parquet('{D}/kline_daily_enriched/**/*.parquet'))")


def l1(src: str, tag: str) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE l1_{tag} AS
    WITH p AS (
      SELECT symbol, date, close, open,
             row_number() OVER (PARTITION BY symbol ORDER BY date) i,
             lead(open)  OVER (PARTITION BY symbol ORDER BY date) nopen,
             lead(close) OVER (PARTITION BY symbol ORDER BY date) nclose,
             lead(date)  OVER (PARTITION BY symbol ORDER BY date) ndate,
             close / lag(close, 20) OVER (PARTITION BY symbol ORDER BY date) - 1 AS mom20
      FROM {src} WHERE close > 0 AND open > 0
    )
    SELECT p.symbol, p.date, p.ndate, p.mom20,
           p.nclose / p.nopen - 1 AS ret
    FROM p
    JOIN cal c  ON p.date  = c.date
    JOIN cal cn ON p.ndate = cn.date
    WHERE c.rn + 1 = cn.rn          -- 次日必须是日历相邻交易日
      AND p.mom20 IS NOT NULL AND isfinite(p.mom20)
      AND p.nopen > 0 AND p.nclose > 0
      AND p.nopen <= p.close * 0.99     -- 低开 1% 才买
    """)
    con.execute(f"""
    CREATE OR REPLACE TABLE l1sel_{tag} AS
    WITH r AS (SELECT *, row_number() OVER (PARTITION BY date ORDER BY mom20 DESC) rk FROM l1_{tag})
    SELECT * FROM r WHERE rk <= 20
    """)
    r = con.sql(f"""
    WITH d AS (SELECT date, avg(ret) x, count(*) n FROM l1sel_{tag} GROUP BY 1),
         s AS (SELECT avg(x) m, stddev_samp(x) sd, count(*) nd, avg(n) an FROM d)
    SELECT round(m*1e4,2) bp, round(m/sd*sqrt(nd),1) t, nd, round(an,2) per_day,
           (SELECT count(*) FROM l1sel_{tag}) cells FROM s""").fetchone()
    print(f"{tag:6s} {r[2]:5d}日 {r[4]:>8,}笔 均{r[3]:5.2f}笔/日  日等权 {r[0]:+7.2f}bp  t={r[1]:5.1f}")


print("=" * 92)
print("L1 缺口反转: 基线 vs 补全")
print("=" * 92)
l1(f"read_parquet('{D}/kline_daily_enriched/**/*.parquet')", "base")
con.execute(f"CREATE OR REPLACE VIEW full_px AS SELECT symbol,date,open,close FROM read_parquet('{D}/kline_daily_enriched/**/*.parquet') UNION ALL SELECT symbol,date,open,close FROM read_parquet('{D}/backfill_20261117/stock_gap_daily.parquet')")
l1("full_px", "full")

print("\n=== 退市股是否真的没进过前 20 ===")
print(con.sql("""
SELECT (SELECT count(*) FROM l1sel_full) total_picks,
       (SELECT count(*) FROM l1sel_full s WHERE s.symbol IN
          (SELECT DISTINCT symbol FROM read_parquet('D:/DSH/tick-stock-panel/data/backfill_20261117/stock_gap_daily.parquet'))) gap_picks
""").df().to_string(index=False))
