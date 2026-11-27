"""补质押公告口径：回填 2005–2018 的明细（带 HOLDER_NAME），与 2019+ 合并成一张全历史合约表。

背景
----
存量合约簿原来只用 `pledge_raw_v2.parquet`（NOTICE_DATE ≥ 2019-01）。
但 2020 年还在簿上的合约，很大一部分是 2015–2018 年公告的；它们不在 v2 里，
所以 v2 的 `book` 系统性偏小、`near_share` 的分母偏小。
2026-11-27 的 D2 只修了「同一批合约内部」的口径（复权/入场/到期），没修「合约本身缺一批」。

本脚本
------
1. 拉 2005-01-01 ~ 2018-12-31 的 RPTA_APP_ACCUMDETAILS（含 HOLDER_NAME）；
2. 与 pledge_raw_v2 合并、按 (code, holder, start, PF_TSR, OPENLINE) 去重；
3. 输出 pledge_all.parquet：一份从 2005 开始的合约表，供 v3 重建合约簿。

产物
----
data/.scratch/leverage/pledge_all.parquet
data/.scratch/leverage/pledge_pre2019_stats.json
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
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://data.eastmoney.com/gpzy/"}


def fetch(since: str, until: str) -> pd.DataFrame:
    rows, page = [], 1
    while True:
        p = {"reportName": "RPTA_APP_ACCUMDETAILS", "columns": "ALL", "source": "WEB", "client": "WEB",
             "sortColumns": "NOTICE_DATE", "sortTypes": "-1", "pageNumber": str(page), "pageSize": "500",
             "filter": f"(NOTICE_DATE>='{since}')(NOTICE_DATE<='{until}')"}
        d = None
        for _ in range(3):
            try:
                with urlopen(Request(BASE + "?" + urlencode(p), headers=H), timeout=30) as r:
                    d = json.loads(r.read().decode("utf-8"))
                break
            except Exception as e:
                print("      retry", e, flush=True)
                time.sleep(2)
        if d is None:
            raise RuntimeError("fetch failed")
        data = (d.get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        total = (d.get("result") or {}).get("count", 0)
        if page % 20 == 0:
            print(f"      page {page} / {len(rows):,}", flush=True)
        if page * 500 >= total:
            break
        page += 1
        time.sleep(0.2)
    return pd.DataFrame(rows)


def main():
    print("[1/3] fetch 2005-2018 ...", flush=True)
    raw = fetch("2005-01-01", "2018-12-31")
    print(f"      {len(raw):,} 条", flush=True)

    raw["code"] = raw["SECURITY_CODE"].astype(str).str.zfill(6)
    raw["holder"] = raw["HOLDER_NAME"].astype(str)
    for c in ["NOTICE_DATE", "PF_START_DATE", "UNFREEZE_DATE"]:
        raw[c] = pd.to_datetime(raw[c], errors="coerce")
    for c in ["PF_TSR", "ACCUM_PLEDGE_TSR", "OPENLINE", "WARNING_LINE", "CLOSE_PRICE"]:
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    keep = raw.dropna(subset=["code", "NOTICE_DATE"])[
        ["code", "holder", "NOTICE_DATE", "PF_START_DATE", "UNFREEZE_DATE", "PF_TSR",
         "ACCUM_PLEDGE_TSR", "OPENLINE", "WARNING_LINE", "CLOSE_PRICE", "IS_CONTROL_SHAREHOLDER",
         "UNFREEZE_STATE", "PF_REASON"]].copy()
    keep = keep.rename(columns={"holder": "HOLDER_NAME"})
    keep.to_parquet(OUT / "pledge_pre2019.parquet", index=False)

    print("[2/3] merge with v2 ...", flush=True)
    v = pd.read_parquet(OUT / "pledge_raw_v2.parquet").drop_duplicates()
    v["code"] = v["code"].astype(str).str.zfill(6)
    for c in ["NOTICE_DATE", "PF_START_DATE", "UNFREEZE_DATE"]:
        v[c] = pd.to_datetime(v[c], errors="coerce")
    for c in ["PF_TSR", "ACCUM_PLEDGE_TSR", "OPENLINE"]:
        v[c] = pd.to_numeric(v[c], errors="coerce")
    v["HOLDER_NAME"] = v["HOLDER_NAME"].astype(str)
    cols = ["code", "HOLDER_NAME", "NOTICE_DATE", "PF_START_DATE", "UNFREEZE_DATE", "PF_TSR",
            "ACCUM_PLEDGE_TSR", "OPENLINE", "WARNING_LINE", "CLOSE_PRICE", "IS_CONTROL_SHAREHOLDER",
            "UNFREEZE_STATE", "PF_REASON"]
    keep["WARNING_LINE"] = pd.to_numeric(keep["WARNING_LINE"], errors="coerce")
    for c in cols:
        if c not in keep.columns:
            keep[c] = pd.NA
        if c not in v.columns:
            v[c] = pd.NA
    a = pd.concat([keep[cols], v[cols]], ignore_index=True)
    # 同一份合约可能在 2018 公告一次、2019 又公告一次（补充质押/展期）；按合约指纹去重
    a["_fp"] = (a["code"].astype(str) + "|" + a["HOLDER_NAME"].astype(str) + "|" +
                a["PF_START_DATE"].astype(str) + "|" + a["PF_TSR"].round(4).astype(str) + "|" +
                a["OPENLINE"].round(4).astype(str) + "|" + a["UNFREEZE_DATE"].astype(str))
    before = len(a)
    a = a.sort_values("NOTICE_DATE").drop_duplicates("_fp", keep="first").drop(columns="_fp")
    print(f"      合并 {before:,} → 去重 {len(a):,}", flush=True)
    a.to_parquet(OUT / "pledge_all.parquet", index=False)

    print("[3/3] stats ...", flush=True)
    st = {
        "rows_2005_2018": int(len(keep)),
        "rows_v2": int(len(v)),
        "rows_all_dedup": int(len(a)),
        "codes_all": int(a["code"].nunique()),
        "notice_min": str(a["NOTICE_DATE"].min().date()),
        "notice_max": str(a["NOTICE_DATE"].max().date()),
        "by_year": {str(k): int(n) for k, n in a.groupby(a["NOTICE_DATE"].dt.year).size().items()},
        "openline_null_by_era": {
            "pre2019": round(float(keep["OPENLINE"].isna().mean()), 4),
            "post2019": round(float(v["OPENLINE"].isna().mean()), 4),
        },
    }
    (OUT / "pledge_pre2019_stats.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: st[k] for k in ["rows_2005_2018", "rows_all_dedup", "codes_all", "notice_min", "notice_max"]},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
