"""补字段回填：质押日/公告日的前复权价 + 质押开始日，用于拆「雪崩 vs 券商定价」因果。

数据源：东财 RPTA_APP_ACCUMDETAILS，补 CLOSE_FORWARD_ADJPRICE（质押日前复权价）、
CLOSE_FORWARD_ADJPRICE_TODAY（公告日前复权价）、PF_START_DATE（质押开始日）。

产物：data/.scratch/leverage/pledge_raw_v2.parquet
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
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://data.eastmoney.com/gpzy/"}

KEEP = ["SECURITY_CODE", "HOLDER_NAME", "NOTICE_DATE", "PF_START_DATE", "UNFREEZE_DATE",
        "ACCUM_PLEDGE_TSR", "PF_TSR", "OPENLINE", "WARNING_LINE",
        "CLOSE_PRICE", "CLOSE_FORWARD_ADJPRICE", "CLOSE_FORWARD_ADJPRICE_TODAY",
        "PF_REASON", "UNFREEZE_STATE", "IS_CONTROL_SHAREHOLDER"]


def get_json(params):
    url = BASE + "?" + urlencode(params)
    for _ in range(3):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"      retry {e}", flush=True)
            time.sleep(2)
    raise RuntimeError("fetch failed")


def main():
    rows = []
    page = 1
    while True:
        d = get_json({"reportName": "RPTA_APP_ACCUMDETAILS", "columns": "ALL", "source": "WEB",
                      "sortColumns": "NOTICE_DATE", "sortTypes": "-1", "pageNumber": str(page),
                      "pageSize": "500", "filter": "(NOTICE_DATE>='2019-01-01')"})
        data = d.get("result", {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        total = d.get("result", {}).get("count", 0)
        if page % 20 == 0:
            print(f"      {page} 页 / {len(rows):,} 条", flush=True)
        if page * 500 >= total:
            break
        page += 1
        time.sleep(0.25)
    df = pd.DataFrame(rows)[KEEP]
    df["code"] = df["SECURITY_CODE"].astype(str).str.zfill(6)
    df["notice"] = pd.to_datetime(df["NOTICE_DATE"])
    df["pstart"] = pd.to_datetime(df["PF_START_DATE"])
    for c in ["ACCUM_PLEDGE_TSR", "PF_TSR", "OPENLINE", "WARNING_LINE", "CLOSE_PRICE",
              "CLOSE_FORWARD_ADJPRICE", "CLOSE_FORWARD_ADJPRICE_TODAY"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.to_parquet(OUT / "pledge_raw_v2.parquet", index=False)
    print(f"      回填 {len(df):,} 条 / {df['code'].nunique()} 只", flush=True)


if __name__ == "__main__":
    main()
