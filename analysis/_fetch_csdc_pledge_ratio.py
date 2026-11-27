"""抓中登（东财 RPT_CSDC_LIST）个股质押比例，用于校验合约簿的覆盖率。

用途：合约簿（公告明细）只覆盖「重要股东」这一层，与中登的全口径质押比例一定不等。
本脚本抓季末快照，给出 book/official 的比值，作为「补合约」能补到哪的天花板。

产物：data/.scratch/leverage/csdc_pledge_ratio.parquet
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/gpzy/"}
TARGETS = ["2020-06-30", "2021-06-30", "2022-06-30", "2023-06-30", "2024-06-28", "2025-06-27", "2026-06-26"]


def get(params):
    for _ in range(3):
        try:
            with urlopen(Request(BASE + "?" + urlencode(params), headers=H), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print("   retry", e, flush=True); time.sleep(2)
    raise RuntimeError("fetch failed")


def pick_date(target: str) -> str | None:
    p = {"reportName": "RPT_CSDC_LIST", "columns": "TRADE_DATE", "source": "WEB", "client": "WEB",
         "sortColumns": "TRADE_DATE", "sortTypes": "1", "pageNumber": "1", "pageSize": "1",
         "filter": f"(TRADE_DATE>='{target}')"}
    d = get(p)
    rows = (d.get("result") or {}).get("data") or []
    return str(rows[0]["TRADE_DATE"])[:10] if rows else None


def fetch_date(date: str) -> pd.DataFrame:
    rows, page = [], 1
    while True:
        p = {"reportName": "RPT_CSDC_LIST", "columns": "ALL", "source": "WEB", "client": "WEB",
             "sortColumns": "TRADE_DATE", "sortTypes": "-1", "pageNumber": str(page), "pageSize": "500",
             "filter": f"(TRADE_DATE='{date}')"}
        d = get(p)
        data = (d.get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        total = (d.get("result") or {}).get("count", 0)
        if page * 500 >= total:
            break
        page += 1
        time.sleep(0.2)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["code"] = df["SECURITY_CODE"].astype(str).str.zfill(6)
    df["ratio"] = pd.to_numeric(df["PLEDGE_RATIO"], errors="coerce")
    df["date"] = pd.to_datetime(df["TRADE_DATE"])
    return df[["date", "code", "ratio", "PLEDGE_MARKET_CAP"]]


def main():
    frames = []
    for t in TARGETS:
        d = pick_date(t)
        if not d:
            print(f"{t} -> 无数据", flush=True); continue
        df = fetch_date(d)
        print(f"{t} -> {d} {len(df)} 行 / {df['code'].nunique()} 只", flush=True)
        frames.append(df)
        time.sleep(0.3)
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(OUT / "csdc_pledge_ratio.parquet", index=False)
    print("saved", out.shape, flush=True)


if __name__ == "__main__":
    main()
