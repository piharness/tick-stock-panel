"""通用序列补全: 指数 / ETF 全历史日K (东财 fqt=0)。

只补 staging, 不碰生产目录。symbol 后缀 -> 东财 secid: .SH->1. .SZ/BJ->0.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

import duckdb
import pandas as pd

DATA = Path(r"D:/DSH/tick-stock-panel/data")
STAGING = DATA / "backfill_20261117"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
COLS = ["date", "open", "close", "high", "low", "volume", "amount"]


def to_secid(symbol: str) -> str:
    code, mkt = symbol.split(".")
    return f"{1 if mkt == 'SH' else 0}.{code}"


def fetch(symbol: str, retries: int = 3) -> pd.DataFrame:
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={to_secid(symbol)}&fields1=f1&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&klt=101&fqt=0&beg=19901219&end=20500101&lmt=100000")
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())["data"]
            if not d or not d.get("klines"):
                return pd.DataFrame(columns=["symbol"] + COLS)
            df = pd.DataFrame([k.split(",") for k in d["klines"]], columns=COLS)
            df.insert(0, "symbol", symbol)
            df["date"] = pd.to_datetime(df["date"]).dt.date
            for c in COLS[1:]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(0.6 * (i + 1))
    return pd.DataFrame(columns=["symbol"] + COLS)


def symbols_of(dataset: str) -> list[str]:
    con = duckdb.connect()
    d = "kline_index_daily" if dataset == "index" else "kline_etf_daily"
    rows = con.sql(f"SELECT DISTINCT symbol FROM read_parquet('{(DATA/d).as_posix()}/**/*.parquet') ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["index", "etf"], required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    syms = symbols_of(args.dataset)
    if args.limit:
        syms = syms[: args.limit]
    print(f"{args.dataset}: {len(syms)} 只待补")

    frames, fails, t0 = [], [], time.time()
    for i, s in enumerate(syms, 1):
        try:
            df = fetch(s)
            if df.empty:
                fails.append((s, "empty"))
            else:
                frames.append(df)
        except Exception as e:
            fails.append((s, f"{type(e).__name__}"))
        if i % 200 == 0:
            print(f"  {i}/{len(syms)}  {time.time()-t0:.0f}s  ok={len(frames)} fail={len(fails)}", flush=True)
        time.sleep(0.06)

    dst = STAGING / (f"{args.dataset}_daily_full.parquet" if args.dataset == "index"
                     else "etf_daily_full.parquet")
    if frames:
        all_df = pd.concat(frames, ignore_index=True)
        all_df.to_parquet(dst, index=False)
        el = time.time() - t0
        print(f"\n落盘 {dst}")
        print(f"合计 {len(all_df):,} 行 / {len(frames)} 只 / {el:.0f}s")
        print(f"日期跨度 {all_df.date.min()} ~ {all_df.date.max()}")
    if fails:
        print(f"失败 {len(fails)}: {fails[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
