"""国资入股 / 纾困基金：候选事件抓取（巨潮资讯公告检索）。

为什么是巨潮：国资入股必须披露《权益变动报告书》/《收购报告书》，纾困基金受让也走同一套披露；
东财的并购重组库（RPTA_WEB_BGCZMX）是**资产**交易，不覆盖上市公司股东层面的股份转让，不能替代。

检索式（标题 AND，2018-01-01 至今）：
  权益变动 国资 / 权益变动 国有 / 权益变动 纾困 / 收购 国资 / 表决权 国资 / 纾困基金 / 无偿划转 / 重整投资
输出：data/.scratch/state/state_injection_candidates.parquet
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "state"
URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}
QUERIES = [
    "权益变动 国资",
    "权益变动 国有",
    "权益变动 纾困",
    "收购 国资",
    "表决权 国资",
    "纾困基金",
    "无偿划转",
    "重整投资",
]
START, END = "2018-01-01", "2026-11-27"


def fetch(key: str, page: int) -> dict:
    data = urllib.parse.urlencode(
        {
            "pageNum": str(page),
            "pageSize": "30",
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": key,
            "secdate": f"{START}~{END}",
            "category": "",
            "trade": "",
            "seDate": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
    ).encode()
    req = urllib.request.Request(URL, data=data, headers=HEADERS, method="POST")
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            print(f"    retry {e}", flush=True)
            time.sleep(2)
    raise RuntimeError(f"fetch failed: {key} p{page}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for key in QUERIES:
        first = fetch(key, 1)
        total = int(first.get("totalAnnouncement") or 0)
        pages = min((total + 29) // 30, 60)
        print(f"{key:>14}  {total:>6} 条 / 拉 {pages} 页", flush=True)
        items = first.get("announcements") or []
        for p in range(2, pages + 1):
            time.sleep(0.35)
            items.extend(fetch(key, p).get("announcements") or [])
        for a in items:
            rows.append(
                {
                    "query": key,
                    "code": a.get("secCode"),
                    "name": a.get("secName"),
                    "title": (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""),
                    "date": pd.to_datetime(a.get("announcementTime"), unit="ms").date(),
                    "url": "http://static.cninfo.com.cn/" + (a.get("adjunctUrl") or ""),
                    "id": a.get("announcementId"),
                }
            )
        time.sleep(0.35)
    df = pd.DataFrame(rows).drop_duplicates(subset=["code", "id"])
    df = df.sort_values(["date", "code"], ascending=[False, True])
    df.to_parquet(OUT / "state_injection_candidates.parquet", index=False)
    print(f"\n去重后 {len(df):,} 条 / {df['code'].nunique():,} 只")
    print(df.groupby("query").size().sort_values(ascending=False).to_string())
    print("\n最近 8 条：")
    print(df.head(8)[["date", "code", "name", "title"]].to_string(index=False))


if __name__ == "__main__":
    main()
