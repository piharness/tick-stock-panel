"""全量回填股权质押明细（2019+），构建股票级质押比例时间序列。

数据源：东财 datacenter RPTA_APP_ACCUMDETAILS（重要股东质押明细）。
每条记录 = 某股东某次质押公告，ACCUM_PLEDGE_TSR = 该股东公告时累计质押占总股本 %，
OPENLINE/WARNING_LINE = 平仓线/预警线，UNFREEZE_STATE = 解押状态。

产物：
  data/.scratch/leverage/pledge_raw.parquet      全量明细（2019+）
  data/.scratch/leverage/pledge_ts.parquet       股票级日频累计质押比例
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://data.eastmoney.com/gpzy/"}


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


def fetch_all(since: str) -> pd.DataFrame:
    rows = []
    page = 1
    while True:
        d = get_json({"reportName": "RPTA_APP_ACCUMDETAILS", "columns": "ALL", "source": "WEB",
                      "sortColumns": "NOTICE_DATE", "sortTypes": "-1", "pageNumber": str(page),
                      "pageSize": "500", "filter": f"(NOTICE_DATE>='{since}')"})
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
    return pd.DataFrame(rows)


def main():
    print("[1/3] 回填质押明细 ...", flush=True)
    p = fetch_all("2019-01-01")
    p["code"] = p["SECURITY_CODE"].astype(str).str.zfill(6)
    p["notice"] = pd.to_datetime(p["NOTICE_DATE"])
    p["accum"] = pd.to_numeric(p["ACCUM_PLEDGE_TSR"], errors="coerce")
    p["openline"] = pd.to_numeric(p["OPENLINE"], errors="coerce")
    p["warnline"] = pd.to_numeric(p["WARNING_LINE"], errors="coerce")
    p["close"] = pd.to_numeric(p["CLOSE_PRICE"], errors="coerce")
    p["control"] = (p["IS_CONTROL_SHAREHOLDER"].astype(str) == "1")
    keep = p.dropna(subset=["code", "notice", "accum"])[
        ["code", "HOLDER_NAME", "notice", "accum", "openline", "warnline", "close", "control",
         "PF_REASON", "UNFREEZE_STATE"]].copy()
    keep.to_parquet(OUT / "pledge_raw.parquet", index=False)
    print(f"      明细 {len(keep):,} 条 / {keep['code'].nunique()} 只", flush=True)

    print("[2/3] 构建股票级日频质押比例 ...", flush=True)
    # 每个 code 内：holder × notice 的累计占比，ffill 到交易日历，再求和
    raw = keep.sort_values("notice")
    # 交易日历（用已有日线）
    import pyarrow.dataset as ds
    d = ds.dataset(ROOT / "data" / "kline_daily_enriched", format="parquet")
    t = d.to_table(columns=["date"], filter=ds.field("date") >= pd.Timestamp("2019-01-01").date())
    cal = pd.DatetimeIndex(pd.unique(pd.to_datetime(t.to_pandas()["date"]))).sort_values()
    print(f"      交易日历 {len(cal)} 天", flush=True)

    series = {}
    for code, g in raw.groupby("code"):
        piv = g.pivot_table(index="notice", columns="HOLDER_NAME", values="accum", aggfunc="last")
        piv = piv.sort_index()
        piv = piv.reindex(cal).ffill()
        series[code] = piv.sum(axis=1)
    ts = pd.DataFrame(series).sort_index()  # 行=日期, 列=code
    ts.columns.name = "code"
    ts.index.name = "date"
    ts_long = ts.stack().rename("pledge_tsr").reset_index()
    ts_long.to_parquet(OUT / "pledge_ts.parquet", index=False)
    print(f"      股票级时间序列 {len(ts_long):,} 行，{ts.shape[1]} 只", flush=True)

    print("[3/3] 描述 ...", flush=True)
    print("      质押比例>0 的股票占比:", round(float((ts_long['pledge_tsr'] > 0).mean()), 3))
    print("      质押比例分位:", ts_long['pledge_tsr'].quantile([.5, .75, .9, .95, .99]).round(2).to_dict())


if __name__ == "__main__":
    main()
