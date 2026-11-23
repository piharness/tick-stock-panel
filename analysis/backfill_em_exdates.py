"""抓 306 只缺口标的的除权除息日, 用于在收益率计算里剔除除权污染。

为什么需要: 东财 fqt=1 是减法式(600000 2005 给 -4.29), fqt=2 的复权比值逐日漂移
(9.82->10.10->9.91), 两种复权口径都不可用。唯一可靠路径是原始价收益率 + 剔除除权日。
enriched 侧的除权日来自本地 adj_factor(2005+, 5409 只); gap 侧必须单独抓。
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb
import pandas as pd

DATA = Path(r"D:/DSH/tick-stock-panel/data")
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def fetch(symbol: str, retries: int = 3) -> pd.DataFrame:
    code = symbol.split(".")[0]
    q = (f"{BASE}?reportName=RPT_SHAREBONUS_DET&columns=ALL"
         f"&filter=(SECURITY_CODE%3D%22{code}%22)"
         "&pageSize=300&pageNumber=1&sortColumns=EX_DIVIDEND_DATE&sortTypes=1")
    for i in range(retries):
        try:
            req = urllib.request.Request(q, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode())
            rows = (d.get("result") or {}).get("data") or []
            if not rows:
                return pd.DataFrame()
            src = pd.DataFrame(rows)
            out = pd.DataFrame({"symbol": [symbol] * len(src)})
            out["ex_date"] = pd.to_datetime(src.get("EX_DIVIDEND_DATE"), errors="coerce").dt.date
            out = out.dropna(subset=["ex_date"])
            return out
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(0.5 * (i + 1))
    return pd.DataFrame()


def main() -> int:
    con = duckdb.connect()
    syms = [r[0] for r in con.sql(
        "SELECT DISTINCT symbol FROM read_parquet('"
        + (DATA / "backfill_20261117/stock_gap_daily.parquet").as_posix()
        + "') ORDER BY 1").fetchall()]
    print(f"抓 {len(syms)} 只的除权日")

    frames, t0 = [], time.time()
    n_empty = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch, s): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                df = fut.result()
                if df.empty:
                    n_empty += 1
                else:
                    frames.append(df)
            except Exception as e:
                print(f"  ERR {futs[fut]}: {type(e).__name__}", flush=True)
            if i % 100 == 0:
                print(f"  {i}/{len(syms)} {time.time()-t0:.0f}s ok={len(frames)} empty={n_empty}", flush=True)

    print(f"空返回 {n_empty} 只 (无分红记录), 有除权日 {len(frames)} 只")

    if frames:
        all_df = pd.concat(frames, ignore_index=True).drop_duplicates()
        dst = DATA / "_backfill_staging/gap_ex_dates.parquet"
        all_df.to_parquet(dst, index=False)
        print(f"落盘 {dst.name}: {len(all_df):,} 个除权日 / {all_df.symbol.nunique()} 只 / {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
