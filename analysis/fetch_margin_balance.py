"""沪深两市融资融券日频回填（全市场口径）。

数据源：东方财富数据中心 `RPTA_RZRQ_LSHJ`（融资融券历史数据，沪深合计）。
一手校验：深交所官网 `CATALOGID=1837_xxpl` 抽查若干日，检查「合计 × 深市占比」是否自洽。

口径（写死在这里，别处不再定义）：
    date            交易日
    fin_balance     融资余额（亿元）        RZYE
    fin_buy         融资买入额（亿元）      RZMRE
    fin_net_buy     融资净买入（亿元）      RZJME
    short_balance   融券余额（亿元）        RQYE
    total_balance   融资融券余额（亿元）    RZRQYE
    float_mktcap    流通市值（亿元）        LTSZ
    fin_pct_float   融资余额占流通市值（%） RZYEZB
    index_level     指数点位                NEW
    index_chg_pct   指数涨跌幅（%）         ZDF

为什么带 float_mktcap / fin_pct_float：绝对余额在 2010-2026 之间受市场规模扩张影响，
事件研究必须能换成「余额/流通市值」再谈高低；否则 2015 年的 2.2 万亿与 2026 年的 2.2 万亿
不是同一件事（母题 #2：跨口径相除先问分子分母同一列）。

产物：`data/margin/daily.parquet`。

用法：
    python analysis/fetch_margin_balance.py            # 全量（分页，约 6 次请求）
    python analysis/fetch_margin_balance.py --verify   # 只校验已落盘数据 + 深市抽样
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "margin"
OUT = OUT_DIR / "daily.parquet"

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://data.eastmoney.com/rzrq/",
}
SZSE = (
    "http://www.szse.cn/api/report/ShowReport/data"
    "?SHOWTYPE=JSON&CATALOGID=1837_xxpl&txtDate={d}&tab1PAGENO=1"
)
SZSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://www.szse.cn/",
}

# 东财字段 → 本地产物列名 + 除数（元 → 亿元）
FIELDS = {
    "RZYE": ("fin_balance", 1e8),
    "RZMRE": ("fin_buy", 1e8),
    "RZJME": ("fin_net_buy", 1e8),
    "RQYE": ("short_balance", 1e8),
    "RZRQYE": ("total_balance", 1e8),
    "LTSZ": ("float_mktcap", 1e8),
    "RZYEZB": ("fin_pct_float", 1.0),
    "NEW": ("index_level", 1.0),
    "ZDF": ("index_chg_pct", 1.0),
}


def fetch_page(page: int) -> tuple[int, list[dict]]:
    q = {
        "reportName": "RPTA_RZRQ_LSHJ",
        "columns": "ALL",
        "source": "WEB",
        "sortColumns": "DIM_DATE",
        "sortTypes": "-1",
        "pageNumber": str(page),
        "pageSize": "800",
        "filter": "",
    }
    req = Request(f"{BASE}?{urlencode(q)}", headers=HEADERS)
    with urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8", "ignore"))
    if not payload.get("success"):
        raise RuntimeError(f"page {page}: {payload.get('message')}")
    result = payload.get("result") or {}
    return int(result.get("pages") or 1), (result.get("data") or [])


def to_row(raw: dict) -> dict:
    row = {"date": str(raw["DIM_DATE"])[:10]}
    for src, (dst, div) in FIELDS.items():
        v = raw.get(src)
        row[dst] = None if v is None else float(v) / div
    return row


def fetch_all() -> pd.DataFrame:
    pages, first = fetch_page(1)
    rows = list(first)
    print(f"pages={pages} first={len(first)}", flush=True)
    for p in range(2, pages + 1):
        _, chunk = fetch_page(p)
        rows += chunk
        print(f"  page {p}/{pages} +{len(chunk)} total={len(rows)}", flush=True)
    df = pd.DataFrame([to_row(r) for r in rows])
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def szse_fin_balance(date: str) -> float | None:
    """深市单边融资余额（亿元），一手源。"""
    req = Request(SZSE.format(d=date), headers=SZSE_HEADERS)
    with urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8", "ignore"))
    rows = (payload[0].get("data") or []) if payload else []
    if not rows:
        return None
    return float(str(rows[0]["jrrzye"]).replace(",", ""))


def verify(df: pd.DataFrame) -> None:
    print(f"rows={len(df)}  {df['date'].min()} → {df['date'].max()}")
    print(df.tail(3).to_string(index=False))
    # 自洽：合计 = 融资 + 融券（容差 1 亿）
    gap = (df["total_balance"] - df["fin_balance"] - df["short_balance"]).abs()
    print(f"total 自洽最大偏差: {gap.max():.2f} 亿")
    # 比率口径：fin_pct_float 应 ≈ fin_balance / float_mktcap × 100
    calc = df["fin_balance"] / df["float_mktcap"] * 100
    diff = (calc - df["fin_pct_float"]).abs()
    print(f"占比口径最大偏差: {diff.max():.3f} pct（应为 0，字段同源）")
    # 一手校验：深市融资余额应占合计 40~60%
    print("深交所抽样（深市单边 / 沪深合计）：")
    sample = df.tail(5)["date"].tolist()
    for d in sample:
        total = float(df.loc[df["date"] == d, "fin_balance"].iloc[0])
        try:
            sz = szse_fin_balance(d)
        except Exception as e:  # noqa: BLE001
            print(f"  {d}  取深交所失败: {type(e).__name__}")
            continue
        if sz is None:
            print(f"  {d}  深交所未发布")
            continue
        ratio = sz / total
        flag = "OK" if 0.40 <= ratio <= 0.60 else "CHECK"
        print(f"  {d}  深市 {sz:,.2f} / 合计 {total:,.2f} = {ratio:.1%}  [{flag}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.verify:
        if not OUT.exists():
            print("no parquet:", OUT)
            return 1
        verify(pd.read_parquet(OUT))
        return 0

    df = fetch_all()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"written {OUT} rows={len(df)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
