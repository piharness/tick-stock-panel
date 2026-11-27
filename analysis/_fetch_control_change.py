"""控制权变更类公告：全量索引抓取（巨潮）。

比《权益变动报告书》更适合做「民营→国资」：
一条公告对应一个事件，标题就说明控制权要变，正文第一段必写「变更前为 X，变更后为 Y」。

检索式（标题短语，空格会变成 AND，所以这里用整串）：
  实际控制人变更 / 实际控制人发生变更 / 实际控制人拟变更
  控制权变更 / 控制权发生变更 / 控制权拟发生变更 / 控制权转让

切片：按年（单年最多约 500 条 = 17 页，远低于 pageNum 100 的上限）。
输出：data/.scratch/state/control_change_index.parquet
"""

from __future__ import annotations

import json
import math
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
    "实际控制人变更",
    "实际控制人发生变更",
    "实际控制人拟变更",
    "控制权变更",
    "控制权发生变更",
    "控制权拟发生变更",
    "控制权转让",
]
YEARS = list(range(2006, 2027))
PAGE_SIZE = 30
SLEEP = 0.25

PROGRESS = OUT / "control_change_progress.json"
TARGET = OUT / "control_change_index.parquet"


def fetch(query: str, page: int, s: str, e: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "pageNum": str(page),
            "pageSize": str(PAGE_SIZE),
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": query,
            "secdate": "",
            "category": "",
            "trade": "",
            "seDate": f"{s}~{e}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
    ).encode()
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(URL, data=data, headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"fail {query} {s} p{page}: {last}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    prog = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {"done": []}
    done = set(prog["done"])
    rows: list[dict] = pd.read_parquet(TARGET).to_dict("records") if TARGET.exists() else []

    todo = [(q, y) for q in QUERIES for y in YEARS if f"{q}|{y}" not in done]
    print(f"待抓 {len(todo)} 个切片（已完成 {len(done)}）", flush=True)

    for i, (q, y) in enumerate(todo, 1):
        s, e = f"{y}-01-01", f"{y}-12-31"
        first = fetch(q, 1, s, e)
        total = int(first.get("totalAnnouncement") or 0)
        pages = min(max(math.ceil(total / PAGE_SIZE), 1), 100)
        items = first.get("announcements") or []
        for p in range(2, pages + 1):
            time.sleep(SLEEP)
            items.extend(fetch(q, p, s, e).get("announcements") or [])
        for a in items:
            rows.append(
                {
                    "query": q,
                    "code": a.get("secCode"),
                    "name": a.get("secName"),
                    "title": (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""),
                    "date": pd.to_datetime(a.get("announcementTime"), unit="ms").date(),
                    "url": "http://static.cninfo.com.cn/" + (a.get("adjunctUrl") or ""),
                    "id": a.get("announcementId"),
                    "year": y,
                }
            )
        done.add(f"{q}|{y}")
        if i % 20 == 0 or i == len(todo):
            pd.DataFrame(rows).to_parquet(TARGET, index=False)
            PROGRESS.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")
            print(f"  [{i:>3}/{len(todo)}] {q} {y} total={total:>3} 累计 {len(rows):,}", flush=True)
        time.sleep(SLEEP)

    df = pd.DataFrame(rows).drop_duplicates(subset=["id"])
    df = df.sort_values(["date", "code"], ascending=[False, True])
    df.to_parquet(TARGET, index=False)
    df.to_csv(OUT / "control_change_index.csv", index=False, encoding="utf-8-sig")

    print(f"\n去重 {len(df):,} 条 / {df['code'].nunique():,} 只 / {df['date'].min()} ~ {df['date'].max()}")
    print("\n按检索式（去重后单独计数，可重复）：")
    print(df.groupby("query").size().sort_values(ascending=False).to_string())
    print("\n按年：")
    print(pd.to_datetime(df["date"]).dt.year.value_counts().sort_index().to_string())


if __name__ == "__main__":
    main()
