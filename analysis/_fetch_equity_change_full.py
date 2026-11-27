"""全量拉取《权益变动报告书》公告索引（巨潮资讯）。

为什么要这个脚本：`_fetch_state_injection.py` 用 `secdate` 传日期，但巨潮只认 `seDate`，
所以那 8 条检索式实际拉的是**全历史**（42,536 条），不是 2018 年起；而且被 60 页截断。
本脚本修正两件事：

  1. 参数：`seDate=YYYY-MM-DD~YYYY-MM-DD`（实测 2 天 15 条，`secdate` 无效）。
  2. 切片：`pageSize` 硬顶 30、`pageNum` 超过 100 会静默退回第 1 页；
     所以按月切片，每月 1–12 页，永远在边界内。

输出：data/.scratch/state/equity_change_index.parquet
进度：data/.scratch/state/equity_change_progress.json（断点续跑）

用法：
    backend/.venv/Scripts/python.exe analysis/_fetch_equity_change_full.py
"""

from __future__ import annotations

import calendar
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

QUERY = "权益变动报告书"
START = (2004, 1)
END = (2026, 11)
PAGE_SIZE = 30
MAX_PAGES = 60  # 单月 1,800 条上限；实测单月最多约 400 条，留足余量
SLEEP = 0.28

PROGRESS = OUT / "equity_change_progress.json"
TARGET = OUT / "equity_change_index.parquet"


def months() -> list[tuple[int, int]]:
    out = []
    y, m = START
    while (y, m) <= END:
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_range(y: int, m: int) -> str:
    last = calendar.monthrange(y, m)[1]
    return f"{y}-{m:02d}-01~{y}-{m:02d}-{last:02d}"


def fetch(page: int, se_date: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "pageNum": str(page),
            "pageSize": str(PAGE_SIZE),
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": QUERY,
            "secdate": "",
            "category": "",
            "trade": "",
            "seDate": se_date,
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
    ).encode()
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(URL, data=data, headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed page={page} seDate={se_date}: {last_err}")


def load_progress() -> dict:
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    return {"done": [], "rows": 0, "months": {}}


def save_progress(p: dict) -> None:
    PROGRESS.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    prog = load_progress()
    done = set(prog["done"])
    rows: list[dict] = []
    if TARGET.exists() and done:
        rows = pd.read_parquet(TARGET).to_dict("records")
        print(f"续跑：已有 {len(rows):,} 条 / {len(done)} 个月", flush=True)

    todo = [(y, m) for y, m in months() if f"{y}-{m:02d}" not in done]
    print(f"待拉 {len(todo)} 个月（{len(done)} 已完成）", flush=True)

    for i, (y, m) in enumerate(todo, 1):
        key = f"{y}-{m:02d}"
        rng = month_range(y, m)
        first = fetch(1, rng)
        total = int(first.get("totalAnnouncement") or 0)
        pages = min(max(math.ceil(total / PAGE_SIZE), 1), MAX_PAGES)
        items = first.get("announcements") or []
        for p in range(2, pages + 1):
            time.sleep(SLEEP)
            items.extend(fetch(p, rng).get("announcements") or [])
        for a in items:
            rows.append(
                {
                    "code": a.get("secCode"),
                    "name": a.get("secName"),
                    "title": (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""),
                    "date": pd.to_datetime(a.get("announcementTime"), unit="ms").date(),
                    "url": "http://static.cninfo.com.cn/" + (a.get("adjunctUrl") or ""),
                    "id": a.get("announcementId"),
                    "org_id": a.get("orgId"),
                    "month": key,
                }
            )
        done.add(key)
        prog["done"] = sorted(done)
        prog["months"][key] = {"total": total, "pages": pages, "got": len(items)}
        prog["rows"] = len(rows)
        if i % 6 == 0 or i == len(todo):
            save_progress(prog)
            pd.DataFrame(rows).to_parquet(TARGET, index=False)
            print(
                f"  [{i:>3}/{len(todo)}] {key} total={total:>4} pages={pages:>2} 累计 {len(rows):,}",
                flush=True,
            )
        time.sleep(SLEEP)

    df = pd.DataFrame(rows)
    before = len(df)
    df = df.drop_duplicates(subset=["id"]).sort_values(["date", "code"], ascending=[False, True])
    df.to_parquet(TARGET, index=False)
    df.to_csv(OUT / "equity_change_index.csv", index=False, encoding="utf-8-sig")
    save_progress(prog)

    print(f"\n原始 {before:,} → 去重 {len(df):,} 条 / {df['code'].nunique():,} 只")
    print(f"区间 {df['date'].min()} ~ {df['date'].max()} / {df['month'].nunique()} 个月")
    by_year = df.assign(y=pd.to_datetime(df["date"]).dt.year).groupby("y").size()
    print("\n按年：")
    print(by_year.to_string())
    empty = [k for k, v in prog["months"].items() if v["got"] == 0]
    short = [k for k, v in prog["months"].items() if v["got"] < v["total"]]
    print(f"\n空月份 {len(empty)} 个；拉取不足的月份 {len(short)} 个")
    if short:
        print("  " + ", ".join(f"{k}({prog['months'][k]['got']}/{prog['months'][k]['total']})" for k in short[:12]))


if __name__ == "__main__":
    main()
