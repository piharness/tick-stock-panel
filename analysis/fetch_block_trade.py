"""抓取东方财富大宗交易全历史（RPT_DATA_BLOCKTRADE）。

已验证（2024 全年，24,911 条 A 股）：
  - PREMIUM_RATIO 恒等于 DEAL_PRICE/CLOSE_PRICE-1（误差 5e-13）；
  - CLOSE_PRICE 与本地 kline_daily_enriched.raw_close 逐条相等（24,147/24,147）；
  - px_deal/前收-1 的硬下限恰好卡在主板 -10%、创业板/科创板 -20%，与交易所规则吻合。

口径警告（写进产物列名）：
  premium_dod  = px_deal/前收-1   ← 真正的「让价」，受板块价格笼子约束
  premium_doc  = 东财 PREMIUM_RATIO ← = premium_dod - 当日涨幅，混入了行情
  cage_pos     = (px_deal-lower)/(upper-lower)，价格笼子内的相对位置（0=打到下限）
研究必须用 premium_dod 或 cage_pos，不能用 premium_doc。

用法:
  python analysis/fetch_block_trade.py --years 2005 2026
输出:
  data/block_trade/year=YYYY/part.parquet
  data/.scratch/block_trade_manifest.json
"""
from __future__ import annotations

import argparse
import calendar
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "data" / "block_trade"
SCRATCH = ROOT / "data" / ".scratch"

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BASE = {"reportName": "RPT_DATA_BLOCKTRADE", "columns": "ALL", "source": "WEB", "client": "WEB"}
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}
KEEP = [
    "TRADE_DATE", "SECUCODE", "SECURITY_NAME_ABBR", "SECURITY_TYPE", "TRADE_MARKET_OLD",
    "DEAL_PRICE", "CLOSE_PRICE", "PRE_CLOSE_PRICE", "PREMIUM_RATIO",
    "DEAL_VOLUME", "DEAL_AMT", "TURNOVER_RATE", "FREE_SHARES_RATIO",
    "BUYER_NAME", "SELLER_NAME", "TRADE_UNIT", "DAILY_RANK",
]


def fetch(params: dict, retries: int = 4) -> dict:
    q = urllib.parse.urlencode({**BASE, **params})
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{URL}?{q}", headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed: {last}")


def fetch_range(start: str, end: str, page_size: int = 500) -> pl.DataFrame:
    flt = f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"
    params = {"filter": flt, "pageNumber": 1, "pageSize": page_size,
              "sortColumns": "TRADE_DATE,SECURITY_CODE", "sortTypes": "1,1"}
    first = fetch(params)
    res = first.get("result")
    if not res:
        return pl.DataFrame()
    total, pages = res["count"], res["pages"]
    rows = list(res["data"])
    for p in range(2, pages + 1):
        params["pageNumber"] = p
        d = (fetch(params).get("result") or {}).get("data") or []
        if not d:
            break
        rows.extend(d)
        time.sleep(0.22)
    df = pl.DataFrame(rows, infer_schema_length=None)
    if df.height != total:
        print(f"  ! {start}~{end} api={total} got={df.height}", flush=True)
    return df


def board_of(code: str) -> str:
    c = code[:3]
    if c.startswith("688") or c.startswith("689"):
        return "STAR"
    if c.startswith("30"):
        return "GEM"
    if c.startswith(("60", "00")):
        return "MAIN"
    if c.startswith(("8", "4", "92")):
        return "BSE"
    return "OTHER"


def transform(df: pl.DataFrame) -> pl.DataFrame:
    if df.height == 0:
        return df
    cols = [c for c in KEEP if c in df.columns]
    df = df.select(cols)
    df = df.with_columns(
        pl.col("TRADE_DATE").str.slice(0, 10).str.to_date().alias("date"),
        pl.col("SECUCODE").cast(pl.Utf8).alias("symbol"),
    ).rename({
        "DEAL_PRICE": "px", "CLOSE_PRICE": "close", "PRE_CLOSE_PRICE": "pre_close",
        "PREMIUM_RATIO": "premium_doc", "DEAL_VOLUME": "vol", "DEAL_AMT": "amt",
        "BUYER_NAME": "buyer", "SELLER_NAME": "seller",
        "SECURITY_NAME_ABBR": "name", "SECURITY_TYPE": "sec_type",
        "TURNOVER_RATE": "turnover", "TRADE_MARKET_OLD": "market",
        "TRADE_UNIT": "unit", "DAILY_RANK": "rank",
    })
    # 业务键去重（同一笔被接口重复返回）
    df = df.unique(subset=["date", "symbol", "px", "vol", "buyer", "seller"], keep="first")
    df = df.with_columns(
        (pl.col("px") / pl.col("pre_close") - 1).alias("premium_dod"),
        pl.col("symbol").map_elements(board_of, return_dtype=pl.Utf8).alias("board"),
    )
    # 价格笼子：主板 ±10%，创业板/科创板 ±20%，北交所 ±30%
    cap = pl.when(pl.col("board").is_in(["GEM", "STAR"])).then(0.20) \
           .when(pl.col("board") == "BSE").then(0.30) \
           .otherwise(0.10)
    df = df.with_columns(
        cap.alias("cap"),
    ).with_columns(
        ((pl.col("px") - pl.col("pre_close") * (1 - pl.col("cap")))
         / (pl.col("pre_close") * 2 * pl.col("cap"))).alias("cage_pos")
    )
    return df.sort(["date", "symbol"])


def year_report(df: pl.DataFrame, y: int) -> dict:
    a = df.filter((pl.col("sec_type") == "EQA") & (pl.col("market") != "CNSEBJ"))
    if a.height < 50:
        return {"year": y, "rows_all": df.height, "rows_A": a.height}
    q = lambda p: round(float(a["premium_dod"].quantile(p)), 4)
    return {
        "year": y, "rows_all": df.height, "rows_A": a.height,
        "dates": a["date"].n_unique(), "symbols": a["symbol"].n_unique(),
        "p05": q(0.05), "p25": q(0.25), "med": q(0.5), "p75": q(0.75), "p95": q(0.95),
        "flat_share": round(float((a["premium_dod"].abs() <= 0.005).mean()), 3),
        "by_board": {r["board"]: r["len"] for r in a.group_by("board").len().to_dicts()},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs=2, type=int, default=[2005, 2026])
    args = ap.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    manifest = []
    for y in range(args.years[0], args.years[1] + 1):
        part = OUTDIR / f"year={y}"
        target = part / "part.parquet"
        frames = []
        for m in range(1, 13):
            last = calendar.monthrange(y, m)[1]
            s, e = f"{y}-{m:02d}-01", f"{y}-{m:02d}-{last:02d}"
            raw = fetch_range(s, e)
            if raw.height:
                frames.append(raw)
            time.sleep(0.3)
        if not frames:
            print(f"{y}: 0", flush=True)
            continue
        df = transform(pl.concat(frames, how="diagonal_relaxed"))
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(target)
        rep = year_report(df, y)
        manifest.append(rep)
        print(json.dumps(rep, ensure_ascii=False), flush=True)

    (SCRATCH / "block_trade_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n写出 {len(manifest)} 年 -> {OUTDIR}")


if __name__ == "__main__":
    main()
