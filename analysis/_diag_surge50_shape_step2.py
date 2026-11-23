"""step2: 全市场「未来 10 日能否涨 50%」的特征矩阵。

与 step1 的关系：step1 回答「涨完 50% 之后追进去赚不赚」（答案：不赚），
step2 把镜头前移，回答「在涨之前，什么形状能提前看出来」。

label（严格只用未来 10 个交易日，且要求日历相邻）：
  y10      = max(close_{t+1..t+10}) / close_t - 1 >= 0.50   （10 日内任意收盘见到 50%）
  y10_end_ret = close_{t+10} / close_t - 1                  （10 日后收盘仍剩多少）
  fwd_ret10   = close_{t+10} / open_{t+1} - 1               （T+1 开盘买入的可执行收益）

特征只用 t 日收盘及以前的信息。绝对量（换手率/成交额）一律转当日截面分位，
因为换手率年代漂移 25 倍（2005 中位 0.085% vs 2026 中位 2.135%），原始值不可跨年比较。
"""
from __future__ import annotations

import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
K = str(DATA / "kline_daily_enriched" / "*" / "part.parquet").replace("\\", "/")
OUT = str(DATA / "_diag_surge50_features.parquet").replace("\\", "/")
TMP = str(DATA / "_duckdb_tmp").replace("\\", "/")

t0 = time.time()
con = duckdb.connect()
con.execute("PRAGMA threads=6")
con.execute("PRAGMA memory_limit='12GB'")
con.execute(f"PRAGMA temp_directory='{TMP}'")

con.execute(
    f"""
    CREATE TABLE cal AS
    SELECT date, row_number() OVER (ORDER BY date) - 1 AS seq
    FROM (SELECT DISTINCT date FROM read_parquet('{K}'))
    """
)
print("cal", round(time.time() - t0, 1), flush=True)

con.execute(
    f"""
    CREATE TABLE b AS
    SELECT k.symbol, k.date, c.seq, k.open, k.high, k.low, k.close, k.volume, k.amount,
           k.turnover_rate,
           row_number() OVER (PARTITION BY k.symbol ORDER BY k.date) AS rn
    FROM read_parquet('{K}') k JOIN cal c ON c.date = k.date
    WHERE k.close > 0 AND k.open > 0
    """
)
print("base", round(time.time() - t0, 1), flush=True)

# 第一层：纯滞后列，不含窗口套窗口
con.execute(
    """
    CREATE TABLE f1 AS
    SELECT symbol, date, seq, rn, open, high, low, close, volume, amount, turnover_rate,
      lag(close,1)  OVER w AS c1,
      lag(close,5)  OVER w AS c5,
      lag(close,10) OVER w AS c10,
      lag(close,20) OVER w AS c20,
      lag(close,60) OVER w AS c60,
      lag(high,1)   OVER w AS h1,
      lag(seq,1)    OVER w AS s1,
      lag(seq,5)    OVER w AS q5,
      lag(seq,10)   OVER w AS q10,
      lag(seq,20)   OVER w AS q20,
      lag(seq,60)   OVER w AS q60,
      lead(close,10) OVER w AS fwd_close10,
      lead(open,1)   OVER w AS next_open,
      lead(seq,10)   OVER w AS fwd_seq10,
      max(close) OVER wf10 AS fwd_max10,
      count(*)   OVER wf10 AS fwd_cnt10,
      avg(close)  OVER w20 AS m20,
      avg(close)  OVER w60 AS m60,
      stddev_samp(close) OVER w20 AS s20,
      stddev_samp(close) OVER w60 AS s60,
      avg((high - low) / close) OVER w20 AS amp20,
      avg((high - low) / close) OVER w5  AS amp5,
      avg(abs(close - open) / nullif(high - low, 0)) OVER w20 AS body20,
      avg((high - greatest(open, close)) / nullif(high - low, 0)) OVER w20 AS upsh20,
      avg((least(open, close) - low) / nullif(high - low, 0)) OVER w20 AS dnsh20,
      avg(volume) OVER w5  AS v5,
      avg(volume) OVER w20 AS v20,
      avg(turnover_rate) OVER w20 AS to20,
      avg(amount) OVER w5  AS a5,
      avg(amount) OVER w20 AS a20,
      max(high) OVER w250 AS hi250,
      min(low)  OVER w250 AS lo250,
      max(close) OVER w20 AS hc20
    FROM b
    WINDOW
      w    AS (PARTITION BY symbol ORDER BY rn),
      w5   AS (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
      w20  AS (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
      w60  AS (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
      w250 AS (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 249 PRECEDING AND CURRENT ROW),
      wf10 AS (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND 10 FOLLOWING)
    """
)
print("f1", round(time.time() - t0, 1), flush=True)

# 第二层：依赖滞后列的派生形状 + 计数窗口 + 截面分位 + label
con.execute(
    f"""
    CREATE TABLE feat AS
    WITH g AS (
      SELECT *,
        CASE WHEN s1 IS NOT NULL AND seq - s1 = 1 THEN close / c1 - 1 END AS r1,
        CASE WHEN open > h1 THEN 1 ELSE 0 END AS is_gap,
        CASE WHEN s1 IS NOT NULL AND seq - s1 = 1 AND close > c1 THEN 1 ELSE 0 END AS is_up,
        sum(CASE WHEN open > h1 THEN 1 ELSE 0 END) OVER w20 AS gap20,
        sum(CASE WHEN s1 IS NOT NULL AND seq - s1 = 1 AND close > c1 THEN 1 ELSE 0 END) OVER w20 AS updays20
      FROM f1
      WINDOW w20 AS (PARTITION BY symbol ORDER BY rn ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
    )
    SELECT symbol, date, seq, rn, open, high, low, close, volume, amount, turnover_rate,
      r1,
      CASE WHEN seq - q5  = 5  THEN close / c5  - 1 END AS r5,
      CASE WHEN seq - q10 = 10 THEN close / c10 - 1 END AS r10,
      CASE WHEN seq - q20 = 20 THEN close / c20 - 1 END AS r20,
      CASE WHEN seq - q60 = 60 THEN close / c60 - 1 END AS r60,
      close / m20 - 1 AS ma20_d,
      close / m60 - 1 AS ma60_d,
      m20 / m60 - 1   AS ma_spread,
      close / hi250 - 1 AS dist_hi250,
      close / lo250 - 1 AS dist_lo250,
      close / hc20 - 1  AS dd20,
      s20 / m20 AS cv20,
      s60 / m60 AS cv60,
      s20 / nullif(s60, 0) AS comp_ratio,
      amp20, amp5, body20, upsh20, dnsh20, gap20, updays20,
      v5 / nullif(v20,0) AS vol_r5_20,
      to20,
      a5 / nullif(a20,0) AS amt_r5_20,
      to20 AS to20_raw,
      CASE WHEN r1 > 0.095 THEN 1 ELSE 0 END AS limit_like,
      rn AS age,
      CASE WHEN fwd_cnt10 = 10 AND fwd_seq10 - seq = 10 AND close > 0
           THEN CASE WHEN fwd_max10 / close - 1 >= 0.50 THEN 1 ELSE 0 END END AS y10,
      CASE WHEN fwd_seq10 - seq = 10 THEN fwd_close10 / close - 1 END AS y10_end_ret,
      CASE WHEN fwd_seq10 - seq = 10 THEN fwd_close10 / next_open - 1 END AS fwd_ret10,
      percent_rank() OVER (PARTITION BY date ORDER BY to20) AS to20_pct,
      percent_rank() OVER (PARTITION BY date ORDER BY amount) AS amt_pct,
      percent_rank() OVER (PARTITION BY date ORDER BY s60 / m60) AS cv60_pct,
      percent_rank() OVER (PARTITION BY date ORDER BY close / hi250 - 1) AS hi250_pct
    FROM g
    """
)
print("feat", round(time.time() - t0, 1), flush=True)

con.execute(f"COPY feat TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD)")
print("written", round(time.time() - t0, 1), flush=True)

print(
    con.execute(
        f"""
        SELECT count(*) n, count(y10) n_label, sum(y10) n_hit,
               round(100.0*avg(y10), 4) base_rate_pct,
               round(100.0*avg(CASE WHEN y10_end_ret>=0.5 THEN 1 ELSE 0 END), 4) end_hit_pct,
               round(100.0*avg(CASE WHEN fwd_ret10 IS NOT NULL THEN 1 ELSE 0 END),2) fwd_cov
        FROM read_parquet('{OUT}')
        """
    ).df()
)
print("done", round(time.time() - t0, 1), flush=True)
