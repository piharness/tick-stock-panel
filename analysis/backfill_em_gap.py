"""按「广度缺口」补全日K: 找 instruments 里有、kline 里没有的标的, 用东财原始价补齐。

设计约束 (实测得出, 勿改):
1. 只取 fqt=0 (原始价)。东财 fqt=1 是减法式前复权, 600000 在 2005-01-04 返回 -4.29 (负价),
   不可用。前复权必须由 原始价 + adj_factor.ex_factor 自算 (已验证到 1e-16)。
2. 不改动 data/kline_daily*，只写 staging 目录, 由人工确认后再合并。
3. 退市股是核心目标: 本地 5573 只全部活到最近交易日, 154 只含「退」的标的零行情。

用法:
    python analysis/backfill_em_gap.py --limit 20            # 试跑 20 只
    python analysis/backfill_em_gap.py --only-delisted       # 只补含「退」的
    python analysis/backfill_em_gap.py --all                 # 全量 312 只
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
    """600000.SH -> 1.600000 ; 000001.SZ -> 0.000001"""
    code, mkt = symbol.split(".")
    return f"{1 if mkt == 'SH' else 0}.{code}"


def fetch(symbol: str, retries: int = 3) -> pd.DataFrame:
    secid = to_secid(symbol)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1&fields2=f51,f52,f53,f54,f55,f56,f57"
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
            time.sleep(0.5 * (i + 1))
    return pd.DataFrame(columns=["symbol"] + COLS)


def find_gap(only_delisted: bool) -> pd.DataFrame:
    con = duckdb.connect()
    q = """
    WITH inst AS (SELECT symbol, name FROM read_parquet('{inst}')),
         kd AS (SELECT DISTINCT symbol FROM read_parquet('{kd}/**/*.parquet'))
    SELECT inst.symbol, inst.name FROM inst LEFT JOIN kd USING (symbol)
    WHERE kd.symbol IS NULL {extra} ORDER BY inst.symbol
    """
    extra = "AND inst.name LIKE '%退%'" if only_delisted else ""
    return con.sql(q.format(inst=(DATA / "instruments/instruments.parquet").as_posix(),
                            kd=(DATA / "kline_daily_enriched").as_posix(),
                            extra=extra)).df()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--only-delisted", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=str(STAGING))
    args = ap.parse_args()

    gap = find_gap(args.only_delisted)
    print(f"缺口标的: {len(gap)} 只 (名单来源 instruments, 行情来源 东财 fqt=0)")
    todo = gap if args.all else gap.head(args.limit)

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    frames, fails, t0 = [], [], time.time()
    print(f"{'symbol':11s} {'name':10s} {'rows':>6s} {'first':11s} {'last':11s} {'sec':>5s}")
    for _, row in todo.iterrows():
        t = time.time()
        try:
            df = fetch(row.symbol)
            if df.empty:
                fails.append((row.symbol, row["name"], "empty"))
                print(f"{row.symbol:11s} {row['name']:10s} {'EMPTY':>6s}")
            else:
                frames.append(df)
                print(f"{row.symbol:11s} {row['name']:10s} {len(df):6d} "
                      f"{str(df.date.min()):11s} {str(df.date.max()):11s} {time.time()-t:5.2f}")
        except Exception as e:
            fails.append((row.symbol, row["name"], f"{type(e).__name__}: {e}"))
            print(f"{row.symbol:11s} {row['name']:10s} ERR {type(e).__name__}")
        time.sleep(0.08)

    if frames:
        all_df = pd.concat(frames, ignore_index=True)
        dst = out / "stock_gap_daily.parquet"
        all_df.to_parquet(dst, index=False)
        el = time.time() - t0
        print(f"\n落盘 {dst}")
        print(f"合计 {len(all_df):,} 行 / {len(frames)} 只 / 耗时 {el:.1f}s "
              f"(均值 {el/max(len(todo),1):.2f}s/只)")
        print(f"外推剩余 {len(gap)-len(frames)} 只: {(len(gap)-len(frames))*el/max(len(todo),1)/60:.1f} 分钟")
    if fails:
        print(f"\n失败 {len(fails)} 只: {fails[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
