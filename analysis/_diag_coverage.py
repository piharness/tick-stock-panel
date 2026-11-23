"""本地数据覆盖体检: 每个数据集的 行数 / 标的数 / 日期跨度 / 交易日数。

只读 parquet 必要列, 避免 9p 上做全列扫描。
"""
import sys, time, json
import duckdb
from pathlib import Path

ROOT = Path("/d/DSH/tick-stock-panel") if sys.platform != "win32" else Path(r"D:/DSH/tick-stock-panel")
D = ROOT / "data"
con = duckdb.connect()
con.execute("SET threads=4")

TARGETS = [
    ("kline_daily",              "kline_daily/**/*.parquet",        "date", "symbol"),
    ("archive_qfq",              "archive/kline_daily_qfq/**/*.parquet", "date", "symbol"),
    ("kline_daily_enriched",     "kline_daily_enriched/**/*.parquet", "date", "symbol"),
    ("adj_factor",               "adj_factor/all.parquet",          None,   None),
    ("kline_index_daily",        "kline_index_daily/**/*.parquet",  "date", "symbol"),
    ("kline_etf_daily",          "kline_etf_daily/**/*.parquet",    "date", "symbol"),
    ("kline_minute_1m",          "kline_minute/**/*.parquet",       "date", "symbol"),
    ("kline_5min",               "kline_5min/**/*.parquet",         "date", "symbol"),
    ("kline_15min",              "kline_15min/**/*.parquet",        "date", "symbol"),
    ("kline_30min",              "kline_30min/**/*.parquet",        "date", "symbol"),
    ("kline_60min",              "kline_60min/**/*.parquet",        "date", "symbol"),
]

print(f"{'dataset':22s} {'rows':>12s} {'symbols':>8s} {'days':>6s}  {'min':10s} {'max':10s} {'sec':>6s}")
for name, pat, dcol, scol in TARGETS:
    p = D / pat
    t0 = time.time()
    try:
        # 先用 schema 探列名
        rel = con.sql(f"SELECT * FROM read_parquet('{p.as_posix()}') LIMIT 0")
        cols = [c for c in rel.columns]
        dc = dcol or ("date" if "date" in cols else None)
        sc = scol or ("symbol" if "symbol" in cols else None)
        if dc is None:
            n = con.sql(f"SELECT count(*) FROM read_parquet('{p.as_posix()}')").fetchone()[0]
            print(f"{name:22s} {n:12,d} {'-':>8s} {'-':>6s}  {'-':10s} {'-':10s} {time.time()-t0:6.1f}")
            continue
        if sc:
            q = f"SELECT count(*), count(DISTINCT {sc}), count(DISTINCT {dc}), min({dc}), max({dc}) FROM read_parquet('{p.as_posix()}')"
        else:
            q = f"SELECT count(*), NULL, count(DISTINCT {dc}), min({dc}), max({dc}) FROM read_parquet('{p.as_posix()}')"
        n, ns, nd, dmin, dmax = con.sql(q).fetchone()
        print(f"{name:22s} {n:12,d} {str(ns):>8s} {str(nd):>6s}  {str(dmin)[:10]:10s} {str(dmax)[:10]:10s} {time.time()-t0:6.1f}")
    except Exception as e:
        print(f"{name:22s} ERROR {type(e).__name__}: {str(e)[:90]}")
