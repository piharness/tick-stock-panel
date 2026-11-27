"""回填 2012–2018 的质押明细与定增明细（东财 datacenter）。

用途：拆 2012–2018 机构专用卖方深折价的机制——是质押、定增，还是都不是。
产物：
  data/.scratch/leverage/pledge_hist.parquet
  data/.scratch/leverage/seo_hist.parquet
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
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}


def fetch(report: str, filter_: str, sort: str, since: str, until: str) -> pd.DataFrame:
    rows, page = [], 1
    while True:
        p = {"reportName": report, "columns": "ALL", "source": "WEB", "client": "WEB",
             "sortColumns": sort, "sortTypes": "-1", "pageNumber": str(page), "pageSize": "500",
             "filter": f"({sort}>='{since}')({sort}<='{until}')"}
        for _ in range(3):
            try:
                with urlopen(Request(BASE + "?" + urlencode(p), headers=H), timeout=30) as r:
                    d = json.loads(r.read().decode("utf-8"))
                break
            except Exception as e:
                print("retry", e, flush=True); time.sleep(2)
        else:
            raise RuntimeError("fetch failed")
        data = (d.get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        total = (d.get("result") or {}).get("count", 0)
        if page % 10 == 0:
            print(f"      {report} page {page} / {len(rows):,}", flush=True)
        if page * 500 >= total:
            break
        page += 1
        time.sleep(0.25)
    return pd.DataFrame(rows)


def main():
    print("[1/2] pledge 2012-2018 ...", flush=True)
    p = fetch("RPTA_APP_ACCUMDETAILS", "", "NOTICE_DATE", "2012-01-01", "2018-12-31")
    p["code"] = p["SECURITY_CODE"].astype(str).str.zfill(6)
    p["notice"] = pd.to_datetime(p["NOTICE_DATE"], errors="coerce")
    p["start"] = pd.to_datetime(p["PF_START_DATE"], errors="coerce")
    p["unfreeze"] = pd.to_datetime(p["UNFREEZE_DATE"], errors="coerce")
    for c in ["ACCUM_PLEDGE_TSR", "PF_TSR", "OPENLINE", "WARNING_LINE", "CLOSE_PRICE"]:
        p[c] = pd.to_numeric(p[c], errors="coerce")
    keep = p.dropna(subset=["code", "notice"])[
        ["code", "notice", "start", "unfreeze", "PF_TSR", "ACCUM_PLEDGE_TSR", "OPENLINE", "WARNING_LINE",
         "CLOSE_PRICE", "IS_CONTROL_SHAREHOLDER", "UNFREEZE_STATE"]].copy()
    keep.to_parquet(OUT / "pledge_hist.parquet", index=False)
    print(f"      {len(keep):,} 条 / {keep['code'].nunique():,} 只", flush=True)

    print("[2/2] seo 2012-2018 ...", flush=True)
    s = fetch("RPT_SEO_DETAIL", "", "ISSUE_DATE", "2012-01-01", "2018-12-31")
    s["code"] = s["SECURITY_CODE"].astype(str).str.zfill(6)
    for c in ["ISSUE_DATE", "ISSUE_LISTING_DATE", "LOT_DATE"]:
        s[c] = pd.to_datetime(s[c], errors="coerce")
    for c in ["ISSUE_PRICE", "ISSUE_NUM"]:
        s[c] = pd.to_numeric(s[c], errors="coerce")
    keep = s.dropna(subset=["code", "ISSUE_DATE"])[
        ["code", "ISSUE_DATE", "ISSUE_LISTING_DATE", "ISSUE_PRICE", "ISSUE_NUM", "SEO_TYPE",
         "LOCKIN_PERIOD", "ISSUE_OBJECT"]].copy()
    keep.to_parquet(OUT / "seo_hist.parquet", index=False)
    print(f"      {len(keep):,} 条 / {keep['code'].nunique():,} 只", flush=True)


if __name__ == "__main__":
    main()
