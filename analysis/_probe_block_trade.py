"""探针：东方财富大宗交易（RPT_DATA_BLOCKTRADE）可得性、口径与本地对账。

只回答四个问题，不做研究：
  1. 接口能否按月批量取、分页是否稳定（有无重复/遗漏）；
  2. 折价率 PREMIUM_RATIO 是否恒等于 DEAL_PRICE/CLOSE_PRICE-1；
  3. CLOSE_PRICE 是否与本地 kline_daily_enriched 的 close 一致（能否直接 join）；
  4. 覆盖与分布：交易日覆盖、A股占比、折价率分位。

用法:
  python analysis/_probe_block_trade.py --probe      # 单月 2024-01，检查分页与字段
  python analysis/_probe_block_trade.py --year 2024  # 全年，落 parquet
输出:
  data/.scratch/block_trade_probe.json
  data/block_trade/part.parquet（--year 时）
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
SCRATCH = ROOT / "data" / ".scratch"
OUTDIR = ROOT / "data" / "block_trade"

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BASE = {
    "reportName": "RPT_DATA_BLOCKTRADE",
    "columns": "ALL",
    "source": "WEB",
    "client": "WEB",
}
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}


def fetch(params: dict, retries: int = 3) -> dict:
    q = urllib.parse.urlencode({**BASE, **params})
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{URL}?{q}", headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"fetch failed: {last}")


def month_ranges(year: int) -> list[tuple[str, str]]:
    out = []
    for m in range(1, 13):
        last = calendar.monthrange(year, m)[1]
        out.append((f"{year}-{m:02d}-01", f"{year}-{m:02d}-{last:02d}"))
    return out


def fetch_range(start: str, end: str, page_size: int = 500) -> pl.DataFrame:
    flt = f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"
    first = fetch({
        "filter": flt, "pageNumber": 1, "pageSize": page_size,
        "sortColumns": "TRADE_DATE,SECURITY_CODE", "sortTypes": "1,1",
    })
    res = first.get("result")
    if not res:
        return pl.DataFrame()
    total, pages = res["count"], res["pages"]
    rows = list(res["data"])
    for p in range(2, pages + 1):
        r = fetch({
            "filter": flt, "pageNumber": p, "pageSize": page_size,
            "sortColumns": "TRADE_DATE,SECURITY_CODE", "sortTypes": "1,1",
        })
        d = (r.get("result") or {}).get("data") or []
        if not d:
            break
        rows.extend(d)
        time.sleep(0.25)
    df = pl.DataFrame(rows, infer_schema_length=None)
    if df.height != total:
        print(f"  !! {start}~{end} count={total} 实收={df.height}", flush=True)
    return df


KEEP = [
    "TRADE_DATE", "SECUCODE", "SECURITY_CODE", "SECURITY_NAME_ABBR", "SECURITY_TYPE",
    "DEAL_PRICE", "CLOSE_PRICE", "PREMIUM_RATIO", "DEAL_VOLUME", "DEAL_AMT",
    "BUYER_NAME", "SELLER_NAME", "TURNOVER_RATE", "FREE_SHARES_RATIO",
    "PREMIUM_TURNOVER", "DISCOUNT_TURNOVER", "TRADE_MARKET_OLD",
]


def slim(df: pl.DataFrame) -> pl.DataFrame:
    if df.height == 0:
        return df
    cols = [c for c in KEEP if c in df.columns]
    df = df.select(cols).with_columns(
        pl.col("TRADE_DATE").str.slice(0, 10).str.to_date().alias("date"),
        pl.col("SECUCODE").cast(pl.Utf8).alias("symbol"),
    )
    df = df.rename({
        "DEAL_PRICE": "px_deal", "CLOSE_PRICE": "px_close",
        "PREMIUM_RATIO": "premium", "DEAL_VOLUME": "vol", "DEAL_AMT": "amt",
        "BUYER_NAME": "buyer", "SELLER_NAME": "seller",
        "SECURITY_NAME_ABBR": "name", "SECURITY_TYPE": "sec_type",
        "TURNOVER_RATE": "turnover", "TRADE_MARKET_OLD": "market",
    })
    return df


def audit(df: pl.DataFrame) -> dict:
    out: dict = {}
    out["rows"] = df.height
    out["date_min"] = str(df["date"].min())
    out["date_max"] = str(df["date"].max())
    out["n_dates"] = df["date"].n_unique()
    out["n_symbols"] = df["symbol"].n_unique()
    out["by_sec_type"] = df.group_by("sec_type").len().sort("len", descending=True).rows()

    # 折价率恒等式：premium == px_deal/px_close-1
    chk = df.filter(
        pl.col("px_deal").is_not_null() & pl.col("px_close").is_not_null()
        & (pl.col("px_close") > 0) & pl.col("premium").is_not_null()
    ).with_columns(
        (pl.col("px_deal") / pl.col("px_close") - 1 - pl.col("premium")).abs().alias("d")
    )
    out["identity_rows"] = chk.height
    out["identity_max_abs_err"] = float(chk["d"].max())
    out["identity_p99_abs_err"] = float(chk["d"].quantile(0.99))

    # 折价率分布（A股）
    a = df.filter(pl.col("sec_type") == "EQA").filter(pl.col("premium").is_not_null())
    qs = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    if a.height >= 100:
        out["premium_A_quantiles"] = {str(q): round(float(a["premium"].quantile(q)), 5) for q in qs}
        out["premium_A_mean"] = round(float(a["premium"].mean()), 5)
        out["premium_A_share_discount"] = round(float((a["premium"] < 0).mean()), 4)
        out["premium_A_n"] = a.height
    else:
        out["premium_A_n"] = a.height
        out["premium_A_note"] = "样本不足 100，跳过分布"

    # 折价幅度（discount = -(premium)），分组
    out["premium_A_by_year"] = (
        a.with_columns(pl.col("date").dt.year().alias("y"))
        .group_by("y").agg(
            pl.len().alias("n"),
            pl.col("premium").median().alias("med"),
            pl.col("premium").mean().alias("mean"),
        ).sort("y").rows()
    )
    return out


def reconcile(df: pl.DataFrame, year: int) -> dict:
    files = sorted((ROOT / "data" / "kline_daily_enriched").glob("date=*/part.parquet"))
    files = [str(f) for f in files if f"date={year}" in str(f)]
    if not files:
        return {"error": f"no kline for {year}"}
    kl = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", pl.col("raw_close").alias("close")])
        .collect()
    )
    j = df.filter(pl.col("sec_type") == "EQA").join(
        kl, on=["symbol", "date"], how="left"
    )
    miss = j.filter(pl.col("close").is_null())
    both = j.filter(pl.col("close").is_not_null() & pl.col("px_close").is_not_null())
    both = both.with_columns((pl.col("px_close") / pl.col("close") - 1).alias("rel"))
    if both.height == 0:
        return {"trade_rows_A": j.height, "kline_matched": 0,
                "kline_missing": miss.height, "error": "no match"}
    return {
        "trade_rows_A": j.height,
        "kline_matched": both.height,
        "kline_missing": miss.height,
        "kline_missing_sample": miss.select(["symbol", "name", "date"]).head(8).rows(),
        "close_rel_median": round(float(both["rel"].median()), 6),
        "close_rel_p99": round(float(both["rel"].abs().quantile(0.99)), 6),
        "close_rel_max": round(float(both["rel"].abs().max()), 6),
        "close_mismatch_gt_1pct": int((both["rel"].abs() > 0.01).sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--year", type=int)
    args = ap.parse_args()

    SCRATCH.mkdir(parents=True, exist_ok=True)
    if args.probe:
        df = fetch_range("2024-01-01", "2024-01-31")
        df = slim(df)
        rep = {"probe_month": "2024-01", **audit(df)}
        # 分页稳定性：symbol+date+px_deal+vol 重复率
        key = df.group_by(["date", "symbol", "px_deal", "vol"]).len()
        rep["dup_groups"] = int((key["len"] > 1).sum())
        rep["max_dup"] = int(key["len"].max() or 0)
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        (SCRATCH / "block_trade_probe.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return

    if args.year:
        frames = []
        for start, end in month_ranges(args.year):
            d = fetch_range(start, end)
            print(f"{start}~{end}: {d.height}", flush=True)
            if d.height:
                frames.append(slim(d))
            time.sleep(0.4)
        df = pl.concat(frames, how="diagonal_relaxed").sort(["date", "symbol"])
        OUTDIR.mkdir(parents=True, exist_ok=True)
        part = OUTDIR / f"year={args.year}"
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(part / "part.parquet")
        rep = {"year": args.year, **audit(df), "recon": reconcile(df, args.year)}
        # 分页重复检查：完全相同的业务键
        key = df.group_by(["date", "symbol", "px_deal", "vol", "buyer", "seller"]).len()
        rep["exact_dup_groups"] = int((key["len"] > 1).sum())
        rep["exact_dup_rows"] = int(key.filter(pl.col("len") > 1)["len"].sum() or 0)
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        (SCRATCH / f"block_trade_probe_{args.year}.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
