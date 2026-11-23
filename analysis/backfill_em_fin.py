"""补全财务历史: 时点股本 + 三表, 覆盖全股票池 (含退市股)。

## 为什么先抓股本

`instruments.float_shares` 是单一截面, 用它算历史市值必然前视 —— 这是本仓
「市值因子」长期缺席的根因。`RPT_F10_EH_EQUITY` 给的是**股本变动流水**
(END_DATE + TOTAL_SHARES + CHANGE_REASON, 600000 从 1999-09-23 上市前股本起 69 条),
按 as-of 前推即可得到任意交易日的时点股本, 从而得到无前视的历史市值。

## 前视偏差

三表都带 `NOTICE_DATE`。早期数据的 NOTICE_DATE 可能晚于真实首次披露日
(600000 的 1996 年报 NOTICE_DATE=1999-11-06), 方向是**保守**的 —— 只会晚用,
不会早用, 因此不引入前视。回测 join 一律用 announce_date, 禁止用 period_end。

用法:
    python analysis/backfill_em_fin.py --dataset shares
    python analysis/backfill_em_fin.py --dataset all --workers 6
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb
import pandas as pd

DATA = Path(r"D:/DSH/tick-stock-panel/data")
STAGING = DATA / "backfill_20261117"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# reportName, 排序日期字段, 源字段->本地字段 映射
SPEC = {
    "shares": ("RPT_F10_EH_EQUITY", "END_DATE", {
        "TOTAL_SHARES": "total_shares",
        "LIMITED_SHARES": "limited_shares",
        "UNLIMITED_SHARES": "unlimited_shares",
        "LISTED_A_SHARES": "listed_a_shares",
        "LIMITED_A_SHARES": "limited_a_shares",
        "CHANGE_REASON": "change_reason",
    }),
    "balance": ("RPT_DMSK_FN_BALANCE", "REPORT_DATE", {
        "TOTAL_ASSETS": "total_assets",
        "TOTAL_LIABILITIES": "total_liabilities",
        "TOTAL_EQUITY": "total_equity",
        "MONETARYFUNDS": "cash_and_equivalents",
        "ACCOUNTS_RECE": "accounts_receivable",
        "ACCOUNTS_PAYABLE": "accounts_payable",
        "INVENTORY": "inventory",
        "FIXED_ASSET": "fixed_asset",
        "CURRENT_RATIO": "current_ratio",
        "DEBT_ASSET_RATIO": "debt_to_asset_ratio",
    }),
    "income": ("RPT_DMSK_FN_INCOME", "REPORT_DATE", {
        "TOTAL_OPERATE_INCOME": "revenue",
        "OPERATE_COST": "operating_cost",
        "TOTAL_OPERATE_COST": "total_operating_cost",
        "SALE_EXPENSE": "selling_expense",
        "MANAGE_EXPENSE": "admin_expense",
        "RESEARCH_EXPENSE": "rd_expense",
        "FINANCE_EXPENSE": "financial_expense",
        "OPERATE_PROFIT": "operating_profit",
        "TOTAL_PROFIT": "total_profit",
        "INCOME_TAX": "income_tax",
        "NETPROFIT": "net_income",
        "PARENT_NETPROFIT": "net_income_attributable",
        "BASIC_EPS": "basic_eps",
    }),
    "cashflow": ("RPT_DMSK_FN_CASHFLOW", "REPORT_DATE", {
        "NETCASH_OPERATE": "net_operating_cash_flow",
        "NETCASH_INVEST": "net_investing_cash_flow",
        "NETCASH_FINANCE": "net_financing_cash_flow",
        "CONSTRUCT_LONG_ASSET": "capex",
        "CCE_ADD": "net_cash_change",
    }),
}

_lock = threading.Lock()
_stat = {"ok": 0, "empty": 0, "err": 0}


def fetch_one(symbol: str, rn: str, date_col: str, mapping: dict, retries: int = 3) -> pd.DataFrame:
    code = symbol.split(".")[0]
    q = (f"{BASE}?reportName={rn}&columns=ALL"
         f"&filter=(SECURITY_CODE%3D%22{code}%22)"
         f"&pageSize=500&pageNumber=1&sortColumns={date_col}&sortTypes=1")
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(q, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode())
            rows = (d.get("result") or {}).get("data") or []
            if not rows:
                return pd.DataFrame()
            src = pd.DataFrame(rows)
            out = pd.DataFrame({"symbol": [symbol] * len(src)})
            out["end_date"] = pd.to_datetime(src[date_col], errors="coerce").dt.date
            if date_col != "END_DATE":
                out["period_end"] = out["end_date"]
                out["announce_date"] = (pd.to_datetime(src["NOTICE_DATE"], errors="coerce").dt.date
                                        if "NOTICE_DATE" in src.columns else None)
            for s, t in mapping.items():
                out[t] = pd.to_numeric(src[s], errors="coerce") if s in src.columns else None
            return out
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="all", choices=["shares", "balance", "income", "cashflow", "all"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    con = duckdb.connect()
    syms = [r[0] for r in con.sql(
        "SELECT symbol FROM read_parquet('"
        + (DATA / "instruments/instruments.parquet").as_posix()
        + "') ORDER BY 1").fetchall()]
    if args.limit:
        syms = syms[: args.limit]

    targets = list(SPEC) if args.dataset == "all" else [args.dataset]
    for name in targets:
        rn, date_col, mapping = SPEC[name]
        _stat.update(ok=0, empty=0, err=0)
        frames, fails, t0 = [], [], time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(fetch_one, s, rn, date_col, mapping): s for s in syms}
            for i, fut in enumerate(as_completed(futs), 1):
                s = futs[fut]
                try:
                    df = fut.result()
                    if df.empty:
                        with _lock:
                            _stat["empty"] += 1
                    else:
                        frames.append(df)
                        with _lock:
                            _stat["ok"] += 1
                except Exception as e:
                    fails.append((s, type(e).__name__))
                    with _lock:
                        _stat["err"] += 1
                if i % 500 == 0:
                    print(f"  [{name}] {i}/{len(syms)} ok={_stat['ok']} empty={_stat['empty']} "
                          f"err={_stat['err']} {time.time()-t0:.0f}s", flush=True)

        if frames:
            all_df = pd.concat(frames, ignore_index=True)
            dst = STAGING / f"fin_{name}.parquet"
            all_df.to_parquet(dst, index=False)
            print(f"[{name}] 落盘 {dst.name}: {len(all_df):,} 行 / "
                  f"{all_df.symbol.nunique()} 只 / {time.time()-t0:.0f}s")
            print(f"   日期跨度 {all_df.end_date.min()} ~ {all_df.end_date.max()}")
        if fails:
            print(f"[{name}] 失败 {len(fails)}: {fails[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
