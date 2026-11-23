"""生存偏差量化 v2: 严格日历相邻 + 原始价收益率, 基线与补全同口径对比。

## v1 的三个错, 都已修

1. **跨停牌假收益**: 退市股在退市整理期常长期停牌后一次性复牌, `lag()` 数行不数天,
   会把累计跌幅记成单日 -90%。728 个「单日跌超 25%」里绝大多数是这个。
   ⇒ 必须要求 date 与 lag(date) 在交易日历上**相邻**。
2. **东财两种复权都不可用**: fqt=1 是减法式(600000 2005 年 -4.29), fqt=2 的比值逐日漂移
   (9.82→10.10→9.91)。⇒ 全程用**原始价**收益率。
3. **306 只退市股无任何分红记录**(RPT_SHAREBONUS_DET 全部返回空), 原始价即真实价,
   不存在除权污染。enriched 侧则剔除 adj_factor 事件日。

## 样本边界

只做 2005-01-04 起: 1990-2004 段日历只能由 306 只退市股自己构成, 横截面不可靠。
"""
from __future__ import annotations

import duckdb

D = "D:/DSH/tick-stock-panel/data"
con = duckdb.connect()
con.execute("SET threads=4")

ENC = f"read_parquet('{D}/kline_daily_enriched/**/*.parquet')"
GAP = f"read_parquet('{D}/backfill_20261117/stock_gap_daily.parquet')"
ADJ = f"read_parquet('{D}/adj_factor/all.parquet')"

# 交易日历: enriched 全市场日期 (2005+)
con.execute(f"CREATE OR REPLACE TABLE cal AS SELECT date, row_number() OVER (ORDER BY date) rn FROM (SELECT DISTINCT date FROM {ENC})")


def make_px(src: str, tag: str):
    """原始价 + 日历相邻过滤。"""
    con.execute(f"""
    CREATE OR REPLACE TABLE px_{tag} AS
    WITH p AS (
      SELECT symbol, date, raw_close,
             lag(date)      OVER (PARTITION BY symbol ORDER BY date) pdate,
             lag(raw_close) OVER (PARTITION BY symbol ORDER BY date) praw
      FROM {src} WHERE raw_close > 0
    )
    SELECT p.symbol, p.date, p.raw_close,
           p.raw_close / p.praw - 1 AS ret1
    FROM p
    JOIN cal c  ON p.date  = c.date
    JOIN cal cp ON p.pdate = cp.date
    WHERE c.rn - cp.rn = 1          -- 日历相邻, 排除跨停牌
      AND p.praw > 0
    """)


make_px(ENC, "base")
# gap: 东财 fqt=0 的 close 就是原始价, 改名对齐
con.execute(f"CREATE OR REPLACE VIEW gap_raw AS SELECT symbol, date, close AS raw_close FROM {GAP} WHERE close > 0")
make_px("gap_raw", "gap")

# 剔除 enriched 侧除权日 (adj_factor 有事件的交易日)
con.execute(f"CREATE OR REPLACE TABLE adj_d AS SELECT DISTINCT symbol, trade_date AS date FROM {ADJ}")
con.execute("CREATE OR REPLACE TABLE px_base_clean AS SELECT p.* FROM px_base p ANTI JOIN adj_d a USING (symbol, date)")
con.execute("CREATE OR REPLACE TABLE px_gap_clean AS SELECT * FROM px_gap")


def build_cells(tag: str, src: str):
    con.execute(f"""
    CREATE OR REPLACE TABLE cell_{tag} AS
    WITH p AS (
      SELECT symbol, date, ret1,
             raw_close / lag(raw_close, 20) OVER (PARTITION BY symbol ORDER BY date) - 1 AS mom20
      FROM {src}
    )
    SELECT date, symbol, mom20, ret1,
           ntile(10) OVER (PARTITION BY date ORDER BY mom20) AS g
    FROM p
    WHERE mom20 IS NOT NULL AND ret1 IS NOT NULL AND isfinite(mom20) AND isfinite(ret1)
      AND date >= DATE '2005-01-04'
    """)


build_cells("base", "px_base_clean")
build_cells("full", "(SELECT * FROM px_base_clean UNION ALL SELECT * FROM px_gap_clean)")


def report(tag: str, label: str):
    r = con.sql(f"""
    WITH d AS (SELECT date, avg(ret1) r, count(*) n FROM cell_{tag} GROUP BY 1),
         s AS (SELECT avg(r) m, stddev_samp(r) sd, count(*) nd, avg(n) an FROM d)
    SELECT round(m*1e4,2) daily_bp, round(m/sd*sqrt(nd),1) t_daily, nd,
           round(an,1) avg_names
    FROM s""").fetchone()
    cells = con.sql(f"SELECT count(*) FROM cell_{tag}").fetchone()[0]
    print(f"{label:22s} {r[2]:5d}日 {cells:>10,}样本 每日等权 {r[0]:+7.2f}bp  t={r[1]:5.1f}  均{r[3]:.0f}只")
    return r


print("=" * 96)
print("全市场等权日收益（原始价, 日历相邻, 剔除权日）")
print("=" * 96)
b = report("base", "基线(仅现存标的)")
f = report("full", "补全(+306退市股)")
print(f"\n差异: {f[0]-b[0]:+.2f}bp/日  → 年化 {(f[0]-b[0])*244/100:+.2f}%")

print("\n" + "=" * 96)
print("按 mom20 十组（g1=跌最惨, g10=涨最猛）日等权 bp")
print("=" * 96)
print(con.sql("""
SELECT coalesce(b.g, f.g) AS g,
       round(b.bp,2) base_bp, round(f.bp,2) full_bp, round(f.bp-b.bp,2) diff_bp
FROM (SELECT g, avg(ret1)*1e4 bp FROM cell_base GROUP BY 1) b
FULL JOIN (SELECT g, avg(ret1)*1e4 bp FROM cell_full GROUP BY 1) f USING (g)
ORDER BY 1""").df().to_string(index=False))

print("\n" + "=" * 96)
print("退市股在补全样本中的权重与自身收益")
print("=" * 96)
print(con.sql("""
SELECT count(*) cells, count(DISTINCT symbol) ns,
       round(avg(ret1)*1e4,2) bp, round(median(ret1)*1e4,2) med_bp
FROM cell_full WHERE symbol IN (SELECT DISTINCT symbol FROM px_gap_clean)""").df().to_string(index=False))
