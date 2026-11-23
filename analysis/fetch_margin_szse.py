"""深交所融资融券汇总回填（单市场 · 日频）。

⚠ 已降级为「一手抽样校验工具」，不要用它做全量回填：
   全量约 4,000 次请求，实测 24 分钟未完成（并发会被限流）。全量用
   `analysis/fetch_margin_balance.py`（东财分页，6 次请求拿全 4,041 日）。
   本脚本只在需要交易所一手口径抽查时使用（`--limit N`）。

数据源：深交所官网 ShowReport 接口，CATALOGID=1837_xxpl（「融资融券交易总量」）。
口径：**单日、单市场（深市）**，金额单位亿元；2010-05-04 两融开闸起可用。
产物：`data/margin_szse/daily.parquet`（date + 6 列，date 为 YYYY-MM-DD 字符串）。

为什么只做深市：沪市 `queryMargin.do` 参数未通（返回空 data），而深市占全市场约一半、
更偏中小盘，与本项目「小微盘/科技硬件去杠杆」的论点同侧。用它做事件研究足够，
但所有结论必须标「深市口径」，不能冒充全市场两融余额。

用法：
    python analysis/fetch_margin_szse.py --limit 20     # 只取最近 20 个交易日，验证解析
    python analysis/fetch_margin_szse.py                # 全量回填（断点续传）
    python analysis/fetch_margin_szse.py --verify       # 只校验已落盘的数据

复现口径：交易日历取本地 `data/backfill_20261117/index_daily_full.parquet` 的 date 唯一值，
不猜节假日；非交易日不请求。
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CALENDAR = ROOT / "data" / "backfill_20261117" / "index_daily_full.parquet"
OUT_DIR = ROOT / "data" / "margin_szse"
OUT = OUT_DIR / "daily.parquet"
START = "2010-05-04"
URL = (
    "http://www.szse.cn/api/report/ShowReport/data"
    "?SHOWTYPE=JSON&CATALOGID=1837_xxpl&txtDate={d}&tab1PAGENO=1"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://www.szse.cn/",
}
COLS = {
    "jrrzmr": "fin_buy",        # 融资买入额（亿元）
    "jrrzye": "fin_balance",    # 融资余额（亿元）
    "jrrjmc": "short_sell_vol",  # 融券卖出量
    "jrrjyl": "short_vol",      # 融券余量
    "jrrjye": "short_balance",  # 融券余额（亿元）
    "jrrzrjye": "total_balance",  # 融资融券余额（亿元）
}


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_day(date: str) -> dict | None:
    """单个交易日。非交易日 / 未发布返回 None（不抛）。"""
    req = Request(URL.format(d=date), headers=HEADERS)
    with urlopen(req, timeout=15) as r:
        payload = json.loads(r.read().decode("utf-8", "ignore"))
    rows = (payload[0].get("data") or []) if payload else []
    if not rows:
        return None
    row = rows[0]
    rec = {"date": date}
    for src, dst in COLS.items():
        rec[dst] = _num(row.get(src))
    return rec


def trading_days() -> list[str]:
    cal = pd.read_parquet(CALENDAR, columns=["date"])
    dates = pd.to_datetime(cal["date"]).dt.strftime("%Y-%m-%d")
    out = sorted(d for d in dates.unique() if d >= START)
    # 本地日历滞后于交易所发布（index_daily_full 停在 11-17，深交所已有 11-19）：
    # 尾部按工作日补齐，非交易日由 fetch_day 返回 None 记为 miss —— 不猜节假日，
    # 但要保证「当下这段」不被本地数据的滞后截断。
    last = pd.Timestamp(out[-1])
    today = pd.Timestamp.now().normalize()
    tail = pd.date_range(last + pd.Timedelta(days=1), today, freq="B")
    out += [d.strftime("%Y-%m-%d") for d in tail]
    return out


def load_existing() -> set[str]:
    if not OUT.exists():
        return set()
    return set(pd.read_parquet(OUT, columns=["date"])["date"].astype(str))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只取最近 N 个交易日（验证用）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.verify:
        if not OUT.exists():
            print("no parquet:", OUT)
            return 1
        df = pd.read_parquet(OUT).sort_values("date")
        print(f"rows={len(df)}  {df['date'].min()} → {df['date'].max()}")
        print(df.tail(3).to_string(index=False))
        print("null fin_balance:", int(df["fin_balance"].isna().sum()))
        return 0

    days = trading_days()
    done = load_existing()
    todo = [d for d in days if d not in done]
    if args.limit:
        todo = todo[-args.limit:]
    print(f"calendar={len(days)} 已有={len(done)} 待取={len(todo)}", flush=True)
    if not todo:
        print("up to date")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    got: list[dict] = []
    miss: list[str] = []
    fail: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(fetch_day, d): d for d in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            d = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001 — 单日失败不中断全量
                for attempt in range(2):  # 重试两次
                    try:
                        rec = fetch_day(d)
                        break
                    except Exception:  # noqa: BLE001
                        rec = None
                if rec is None:
                    fail.append(d)
                    if i % 200 == 0:
                        print(f"  {i}/{len(todo)} (fail {len(fail)})", flush=True)
                    continue
            if rec is None:
                miss.append(d)  # 停市/未发布
            else:
                got.append(rec)
            if i % 200 == 0:
                print(f"  {i}/{len(todo)} got={len(got)} miss={len(miss)} fail={len(fail)}", flush=True)

    if got:
        new = pd.DataFrame(got)
        if OUT.exists():
            old = pd.read_parquet(OUT)
            new = pd.concat([old, new], ignore_index=True)
        new = new.drop_duplicates("date").sort_values("date")
        new.to_parquet(OUT, index=False)
        print(f"written {OUT} rows={len(new)}")

    if miss:
        print(f"non-trading/unpublished: {len(miss)} (前 5: {miss[:5]})")
    if fail:
        print(f"FAILED: {len(fail)} (前 5: {fail[:5]})")
    return 0 if not fail else 2


if __name__ == "__main__":
    sys.exit(main())
