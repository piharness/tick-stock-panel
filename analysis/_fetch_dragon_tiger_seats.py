"""抓取东方财富龙虎榜营业部明细，落盘 data/dragon_tiger_seats/date=*.parquet。

数据源：东财 datacenter-web 公开接口（免费、无 token）。
  RPT_BILLBOARD_DAILYDETAILSBUY  买入营业部（每票 top 席位）
  RPT_BILLBOARD_DAILYDETAILSSELL 卖出营业部

每个交易日一个 parquet，含 buy/sell 两方向，side 列区分。
字段含 OPERATEDEPT_NAME（营业部）+ RISE_PROBABILITY_3DAY（该席位 3 日上涨胜率，
东财自己算的）+ TOTAL_BUYER_SALESTIMES_3DAY（该席位 3 日上榜次数）。

用途：席位共现网络 + 席位声誉信号，回候选池验证「谁在买」的增量。
"""
from __future__ import annotations

import glob
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "dragon_tiger_seats"
OUT.mkdir(parents=True, exist_ok=True)

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
COLS = ("SECURITY_CODE,SECUCODE,TRADE_DATE,OPERATEDEPT_CODE,OPERATEDEPT_NAME,"
        "EXPLANATION,CHANGE_RATE,CLOSE_PRICE,BUY,SELL,NET,"
        "RISE_PROBABILITY_3DAY,TOTAL_BUYER_SALESTIMES_3DAY,TOTAL_BUYRIO,TOTAL_SELLRIO")


def _url(report: str, day: str) -> str:
    return (f"{BASE}?reportName={report}&columns={COLS}"
            f"&filter=(TRADE_DATE%3D%27{day}%27)&pageSize=500&pageNumber=1")


def _fetch(report: str, day: str) -> list[dict]:
    url = _url(report, day)
    for attempt in range(4):
        try:
            r = httpx.get(url, timeout=40, headers=HEADERS)
            if r.status_code == 200:
                data = (r.json().get("result") or {}).get("data") or []
                return data
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return []


def _norm(rows: list[dict], side: str, day: str) -> list[dict]:
    out = []
    for x in rows:
        out.append({
            "symbol": x.get("SECUCODE"),
            "date": day,
            "side": side,
            "dept_code": x.get("OPERATEDEPT_CODE"),
            "dept_name": x.get("OPERATEDEPT_NAME"),
            "buy": x.get("BUY"),
            "sell": x.get("SELL"),
            "net": x.get("NET"),
            "rise_prob_3d": x.get("RISE_PROBABILITY_3DAY"),
            "sales_times_3d": x.get("TOTAL_BUYER_SALESTIMES_3DAY"),
            "buy_ratio": x.get("TOTAL_BUYRIO"),
            "sell_ratio": x.get("TOTAL_SELLRIO"),
        })
    return out


def _days() -> list[str]:
    days = set()
    for f in glob.glob(str(ROOT / "data/dragon_tiger/date=*.json")):
        try:
            days.add(json.load(open(f, encoding="utf-8"))["trade_date"])
        except Exception:
            continue
    return sorted(days)


def _fetch_day(day: str) -> tuple[str, list[dict]]:
    buy = _norm(_fetch("RPT_BILLBOARD_DAILYDETAILSBUY", day), "buy", day)
    sell = _norm(_fetch("RPT_BILLBOARD_DAILYDETAILSSELL", day), "sell", day)
    return day, buy + sell


def main() -> None:
    days = _days()
    done = {p.name[5:13] for p in OUT.glob("date=*.parquet")}
    todo = [d for d in days if d not in done]
    print(f"交易日 {len(days)}，已抓 {len(done)}，待抓 {len(todo)}", flush=True)

    if not todo:
        print("已全部落盘，跳过。")
        return

    schema = {
        "symbol": pl.Utf8, "date": pl.Utf8, "side": pl.Utf8, "dept_code": pl.Utf8,
        "dept_name": pl.Utf8, "buy": pl.Float64, "sell": pl.Float64, "net": pl.Float64,
        "rise_prob_3d": pl.Float64, "sales_times_3d": pl.Float64,
        "buy_ratio": pl.Float64, "sell_ratio": pl.Float64,
    }
    ok = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_day, d): d for d in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            day = futs[fut]
            try:
                _, rows = fut.result()
            except Exception as e:
                print(f"[{i}/{len(todo)}] {day} 失败 {e}", flush=True)
                continue
            if not rows:
                print(f"[{i}/{len(todo)}] {day} 空", flush=True)
                continue
            df = pl.DataFrame(rows, schema=schema)
            df.write_parquet(OUT / f"date={day}.parquet")
            ok += 1
            if i % 20 == 0:
                print(f"[{i}/{len(todo)}] {day} 行={df.height} 累计 ok={ok}", flush=True)
    print(f"完成：落盘 {ok}/{len(todo)} 天 → {OUT}")


if __name__ == "__main__":
    main()
