"""抓取事件腿数据：限售解禁（RPT_LIFT_STAGE）与定向增发（RPT_SEO_DETAIL）。

为什么单独一个脚本：这两个表是**事件**数据，不是行情；口径与更新节奏都跟 K 线不同。
落在 `data/events/`，由 `_diag_narrative_events.py` 消费，页面只读聚合结果。

口径（改数之前先读）：
  - 解禁：`FREE_DATE` 是**可上市流通日**；`LIFT_MARKET_CAP` 单位万元；
    `TOTAL_RATIO` = 本次解禁股数 / 流通股数（页面用这个），`FREE_RATIO` = 解禁股数 / 总股本。
  - 定增：`ISSUE_LISTING_DATE` 是**新增股份上市日**（不是预案日、不是缴款日）；
    `LOCKIN_PERIOD` 是文本（"0.5年" / "1.5年" / "3年"），不解析成数，直接透传。
  - 两个接口都分页（pageSize=500），翻页间 sleep 0.25s；失败重试 3 次。

用法：
    backend/.venv/Scripts/python.exe analysis/_fetch_events.py            # 增量刷新
    backend/.venv/Scripts/python.exe analysis/_fetch_events.py --full     # 全区间重抓

输出：
    data/events/lockup.parquet   columns: symbol,name,free_date,free_shares_wan,
                                 lift_mcap_wan,ratio_float,ratio_total,kind,market,fetched_at
    data/events/seo.parquet      columns: symbol,name,listing_date,issue_date,issue_num,
                                 issue_price,raise_yuan,lockin,seo_type,way,fetched_at
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "events"
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
PAGE = 500


def _get(report: str, filt: str, sort_col: str, page: int, asc: bool = True) -> dict:
    params = {
        "reportName": report,
        "columns": "ALL",
        "pageNumber": str(page),
        "pageSize": str(PAGE),
        "source": "WEB",
        "client": "WEB",
        "sortColumns": sort_col,
        "sortTypes": "1" if asc else "-1",
        "filter": filt,
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not payload.get("success"):
                raise RuntimeError(payload.get("message") or "接口返回失败")
            return payload["result"] or {"pages": 0, "data": []}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"{report} 第 {page} 页失败: {last}")


def fetch_all(report: str, filt: str, sort_col: str) -> list[dict]:
    first = _get(report, filt, sort_col, 1)
    pages = int(first.get("pages") or 0)
    rows = list(first.get("data") or [])
    for p in range(2, pages + 1):
        time.sleep(0.25)
        rows += list(_get(report, filt, sort_col, p).get("data") or [])
        if p % 5 == 0:
            print(f"  {report} {p}/{pages} 页 · {len(rows)} 行")
    return rows


def _d(v: str | None):
    return None if not v else str(v)[:10]


def fetch_lockup(start: str, end: str) -> pl.DataFrame:
    filt = f"(FREE_DATE>='{start}')(FREE_DATE<='{end}')"
    rows = fetch_all("RPT_LIFT_STAGE", filt, "FREE_DATE")
    now = datetime.now().isoformat(timespec="seconds")
    recs = []
    for r in rows:
        sym = r.get("SECUCODE")
        if not sym:
            continue
        recs.append({
            "symbol": sym,
            "name": r.get("SECURITY_NAME_ABBR"),
            "free_date": _d(r.get("FREE_DATE")),
            "free_shares_wan": r.get("CURRENT_FREE_SHARES"),
            "lift_mcap_wan": r.get("LIFT_MARKET_CAP"),
            "ratio_float": r.get("TOTAL_RATIO"),
            "ratio_total": r.get("FREE_RATIO"),
            "kind": r.get("FREE_SHARES_TYPE"),
            "market": r.get("MARKET_TYPE"),
            "fetched_at": now,
        })
    return pl.DataFrame(recs) if recs else pl.DataFrame()


def fetch_seo(start: str, end: str) -> pl.DataFrame:
    filt = f"(ISSUE_LISTING_DATE>='{start}')(ISSUE_LISTING_DATE<='{end}')"
    rows = fetch_all("RPT_SEO_DETAIL", filt, "ISSUE_LISTING_DATE")
    now = datetime.now().isoformat(timespec="seconds")
    recs = []
    for r in rows:
        sym = r.get("SECUCODE")
        if not sym:
            continue
        recs.append({
            "symbol": sym,
            "name": r.get("SECURITY_NAME_ABBR"),
            "listing_date": _d(r.get("ISSUE_LISTING_DATE")),
            "issue_date": _d(r.get("ISSUE_DATE")),
            "issue_num": r.get("ISSUE_NUM"),
            "issue_price": r.get("ISSUE_PRICE"),
            "raise_yuan": r.get("TOTAL_RAISE_FUNDS"),
            "lockin": r.get("LOCKIN_PERIOD"),
            "seo_type": r.get("SEO_TYPE"),
            "way": r.get("ISSUE_WAY"),
            "fetched_at": now,
        })
    return pl.DataFrame(recs) if recs else pl.DataFrame()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="重抓全区间（默认增量：只覆盖近 3 个月）")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    if args.full:
        lk_start, seo_start = "2014-01-01", "2014-01-01"
    else:
        lk_start, seo_start = f"{today.year - 1}-01-01", f"{today.year - 1}-01-01"
    lk_end = f"{today.year + 1}-12-31"

    print(f"解禁 {lk_start} ~ {lk_end}")
    lk = fetch_lockup(lk_start, lk_end)
    if lk.height:
        path = OUT_DIR / "lockup.parquet"
        if path.exists() and not args.full:
            old = pl.read_parquet(path)
            lk = pl.concat([old, lk], how="vertical_relaxed").unique(
                subset=["symbol", "free_date", "kind"], keep="last"
            )
        lk = lk.sort(["free_date", "symbol"])
        lk.write_parquet(path)
        print(f"  → {path} {lk.height} 行 / {lk['symbol'].n_unique()} 只")

    print(f"定增 {seo_start} ~ {lk_end}")
    seo = fetch_seo(seo_start, lk_end)
    if seo.height:
        path = OUT_DIR / "seo.parquet"
        if path.exists() and not args.full:
            old = pl.read_parquet(path)
            seo = pl.concat([old, seo], how="vertical_relaxed").unique(
                subset=["symbol", "listing_date"], keep="last"
            )
        seo = seo.sort(["listing_date", "symbol"])
        seo.write_parquet(path)
        print(f"  → {path} {seo.height} 行 / {seo['symbol'].n_unique()} 只")


if __name__ == "__main__":
    main()
