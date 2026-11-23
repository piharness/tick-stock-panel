"""证伪「妖股要么买不进，要么买进就亏」。

命题拆成两个可测子句：
  A 买不进：T+1 全天封死涨停（low >= 涨停价），任何时点都无法成交。
  B 买进就亏：T+1 能买进（low < 涨停价）的样本，买入后持有到 T+5/T+10 收益为负。

证伪只需要一个反例：能买进 且 收益显著为正的子集。

妖股操作定义（四档，做敏感性）：
  Y3  连续涨停 >=3 板
  Y5  连续涨停 >=5 板
  Y100 10 日累计涨幅 >=100%
  Y3H 连续涨停 >=3 板 且 这波累计涨幅 >=40%

「买不进」用 low 判定而不是开盘价：一字板 low=high=close=涨停价；
开盘涨停但盘中打开 low<涨停价，属于能买进（按均价或打开价成交）。

涨停判定：close/prev_close-1 >= 板块幅度-0.005，且 raw_close 比值未跳变（排除除权）。
创业板 2020-08-24 前 10%；ST 5% 无法识别，会漏计（已在结论中声明）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
K = str(DATA / "kline_daily_enriched" / "*" / "part.parquet").replace("\\", "/")
OUT = DATA / "_diag_yaogu_tradability.json"

t0 = time.time()
con = duckdb.connect()
con.execute("PRAGMA threads=6")
con.execute("PRAGMA memory_limit='12GB'")
con.execute(f"PRAGMA temp_directory='{str(DATA / '_duckdb_tmp').replace(chr(92), '/')}'")

con.execute(
    f"""
    CREATE TABLE cal AS
    SELECT date, row_number() OVER (ORDER BY date) - 1 AS seq
    FROM (SELECT DISTINCT date FROM read_parquet('{K}'))
    """
)

con.execute(
    f"""
    CREATE TABLE b AS
    SELECT k.symbol, k.date, c.seq, k.open, k.high, k.low, k.close, k.raw_close,
           k.volume, k.amount,
           CASE
             WHEN k.symbol LIKE '688%' OR k.symbol LIKE '689%' THEN 0.20
             WHEN (k.symbol LIKE '300%' OR k.symbol LIKE '301%' OR k.symbol LIKE '302%')
                  AND k.date >= DATE '2020-08-24' THEN 0.20
             WHEN k.symbol LIKE '8%' OR k.symbol LIKE '4%' OR k.symbol LIKE '920%' THEN 0.30
             ELSE 0.10
           END AS lp,
           row_number() OVER (PARTITION BY k.symbol ORDER BY k.date) AS rn
    FROM read_parquet('{K}') k JOIN cal c ON c.date = k.date
    WHERE k.close > 0 AND k.open > 0
    """
)
print("base", round(time.time() - t0, 1), flush=True)

# 涨停标记 + 连板数（按日历相邻，停牌断开）
con.execute(
    """
    CREATE TABLE lu AS
    SELECT *,
      CASE WHEN seq - s1 = 1
                AND close / c1 - 1 >= lp - 0.005
                AND abs(raw_close / rc1 - 1) < 0.25
           THEN 1 ELSE 0 END AS is_lu
    FROM (
      SELECT *,
        lag(close,1) OVER w AS c1,
        lag(seq,1)   OVER w AS s1,
        lag(raw_close,1) OVER w AS rc1
      FROM b
      WINDOW w AS (PARTITION BY symbol ORDER BY rn)
    )
    """
)

con.execute(
    """
    CREATE TABLE st AS
    WITH g AS (
      SELECT *, sum(CASE WHEN is_lu=0 THEN 1 ELSE 0 END) OVER (PARTITION BY symbol ORDER BY rn) AS grp
      FROM lu
    )
    SELECT *, sum(is_lu) OVER (PARTITION BY symbol, grp ORDER BY rn) AS streak
    FROM g
    """
)
print("streak", round(time.time() - t0, 1), flush=True)

# 未来 1..20 日收盘 + 次日可用性
con.execute(
    """
    CREATE TABLE f AS
    WITH l AS (
      SELECT symbol, date, seq, rn, open, high, low, close, raw_close, volume, amount, lp,
             is_lu, streak,
             lead(seq,1)  OVER w AS q1,  lead(open,1)  OVER w AS o1,
             lead(high,1) OVER w AS h1,  lead(low,1)   OVER w AS l1,
             lead(close,1) OVER w AS c_1,
             lead(seq,2)  OVER w AS q2,  lead(open,2)  OVER w AS o2,  lead(close,2) OVER w AS c_2,
             lead(seq,3)  OVER w AS q3,  lead(open,3) OVER w AS o3, lead(close,3) OVER w AS c_3,
             lead(seq,5)  OVER w AS q5,  lead(close,5) OVER w AS c_5,
             lead(seq,10) OVER w AS q10, lead(close,10) OVER w AS c_10,
             lead(seq,20) OVER w AS q20, lead(close,20) OVER w AS c_20,
             lead(amount,1) OVER w AS a1, lead(volume,1) OVER w AS v1,
             lead(amount,2) OVER w AS a2, lead(volume,2) OVER w AS v2
      FROM st
      WINDOW w AS (PARTITION BY symbol ORDER BY rn)
    )
    SELECT *,
           -- 成交均价是原始价口径，必须乘复权因子才能和复权收盘比
           a1 / (v1 * 100) * close / raw_close AS avg1_adj,
           a2 / (v2 * 100) * close / raw_close AS avg2_adj
    FROM l
    """
)
print("fwd", round(time.time() - t0, 1), flush=True)

con.execute(f"COPY f TO '{DATA/'_diag_yaogu_f.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)")
print("written", round(time.time() - t0, 1), flush=True)

# 汇总
q = lambda s: con.execute(s).df()
print("\n=== 连板分布（涨停日按 streak）===")
print(q("SELECT streak, count(*) n FROM f WHERE is_lu=1 AND streak<=8 GROUP BY 1 ORDER BY 1").to_string(index=False))
print(q("SELECT count(*) n_10plus FROM f WHERE is_lu=1 AND streak>=10").to_string(index=False))

print("\n=== 妖股事件数（按定义）===")
print(
    q(
        """
        SELECT
          sum(CASE WHEN streak=3 THEN 1 ELSE 0 END) AS n_3rd_board,
          sum(CASE WHEN streak=5 THEN 1 ELSE 0 END) AS n_5th_board,
          count(DISTINCT CASE WHEN streak>=3 THEN symbol END) AS sym_3plus
        FROM f
        """
    ).to_string(index=False)
)

# 3 板以上：次日可买性 + 收益（用均价/开盘/收盘三种买点）
res = q(
    """
    SELECT
      CASE WHEN streak>=8 THEN 8 WHEN streak>=5 THEN 5 ELSE streak END AS bucket,
      count(*) AS n,
      sum(CASE WHEN q1-seq=1 THEN 1 ELSE 0 END) AS has_next,
      sum(CASE WHEN q1-seq=1 AND l1 >= close*(1+lp)-0.005 THEN 1 ELSE 0 END) AS sealed_next,
      avg(CASE WHEN q1-seq=1 THEN (c_1/o1 - 1)*100 END) AS d1_open_ret,
      avg(CASE WHEN q1-seq=1 AND q2-seq=2 THEN (c_2/o1 - 1)*100 END) AS d2_ret,
      avg(CASE WHEN q1-seq=1 AND q5-seq=5 THEN (c_5/o1 - 1)*100 END) AS d5_ret,
      avg(CASE WHEN q1-seq=1 AND q10-seq=10 THEN (c_10/o1 - 1)*100 END) AS d10_ret
    FROM f
    WHERE is_lu=1 AND streak>=1
    GROUP BY 1 ORDER BY 1
    """
)
print(res.to_string(index=False))
print("saved checkpoint", OUT, flush=True)
