"""东财股东结构数据拉取 —— 股东户数 / 十大流通股东 / 十大股东 / 限售解禁。

为什么是这四个（而不是"筹码分布"）：
  - 筹码分布（CYQ）是 close/volume/turnover 的确定性函数，不含新信息；
    本项目 `indicators/levels.py` 已实现该模型。
  - 股东户数是**唯一真实的持仓集中度数据**，不是价格衍生。
  - 十大流通股东带 IS_HOLDORG / HOLDER_TYPE，能区分机构/个人，是"谁在持有"。
  - 十大股东（占总股本）是「锁仓=前两大股东占比」判据的数据源（短线雷达「重点关注」）。
  - 解禁是**未来抛压的确定性事件**（日期已知、市值可算），与历史量价正交。

落盘：data/shareholder/{holder_num,top_float,top_holders,lift}/part.parquet
  —— 独立目录，不进 kline_daily_enriched/date=*/ 通配范围（见 MEMORY 母题 15）。

口径注意：
  - END_DATE 是**报告期**，不是公告日。披露有滞后（季报 ~1 个月、年报 ~3 个月）。
    回测必须用公告日（NOTICE_DATE / HOLD_NOTICE_DATE）做可得性对齐，否则是未来函数。
  - HOLDER_NUM 单位＝户；AVG_MARKET_CAP 单位＝元（东财字段虽叫 CAP）。
  - top_holders 的 ratio = HOLD_NUM_RATIO = **占总股本比例**（不是占流通股）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
OUT = DATA / "shareholder"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
API = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def get(params: dict, retries: int = 3) -> dict:
    url = f"{API}?{urllib.parse.urlencode(params)}"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"fetch failed: {last}")


def fetch_paged(report: str, filt: str, columns: str = "ALL",
                page_size: int = 500, sort: str = "", sort_type: str = "") -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        p = {"reportName": report, "columns": columns, "pageNumber": page,
             "pageSize": page_size, "filter": filt, "source": "WEB", "client": "WEB"}
        if sort:
            p["sortColumns"] = sort
            p["sortTypes"] = sort_type
        d = get(p)
        res = d.get("result")
        if not res or not res.get("data"):
            break
        rows.extend(res["data"])
        if page >= int(res.get("pages") or 1):
            break
        page += 1
        time.sleep(0.05)
    return rows


def quarters(start_year: int = 2014, end: tuple[str, ...] = ()) -> list[str]:
    """报告期列表。end 为空则取到"不晚于今天"的最近一期。

    边界用完整 `YYYY-MM-DD` 字符串比较 —— 早先按 (年, 月) 比，`{"06"}` 转 int 后
    与 (2026,3) 比会漏掉当年 Q2/Q3，第 49 期停在 Q1 而页面上最新一期看起来是旧的。
    """
    from datetime import date

    today = date.today().isoformat()
    out = []
    for y in range(start_year, date.today().year + 1):
        for md in ("03-31", "06-30", "09-30", "12-31"):
            s = f"{y}-{md}"
            if s <= today:
                out.append(s)
    return out


def pull_holder_num() -> int:
    cols = ("SECURITY_CODE,END_DATE,HOLDER_NUM,PRE_HOLDER_NUM,HOLDER_NUM_RATIO,"
            "AVG_HOLD_NUM,AVG_MARKET_CAP,TOTAL_A_SHARES,HOLD_NOTICE_DATE,CLOSE_PRICE")
    allrows: list[dict] = []
    periods = quarters()
    for i, p in enumerate(periods, 1):
        try:
            rows = fetch_paged("RPT_HOLDERNUM_DET", f"(END_DATE='{p}')", columns=cols,
                               sort="HOLDER_NUM", sort_type="-1")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(periods)}] {p} FAIL {e}", flush=True)
            continue
        allrows.extend(rows)
        print(f"  [{i}/{len(periods)}] {p} n={len(rows)}", flush=True)
    if not allrows:
        return 0
    df = pl.DataFrame(allrows, infer_schema_length=None)
    keep = [c for c in ["SECURITY_CODE", "END_DATE", "HOLDER_NUM", "PRE_HOLDER_NUM",
                        "HOLDER_NUM_RATIO", "AVG_HOLD_NUM", "AVG_MARKET_CAP",
                        "TOTAL_A_SHARES", "HOLD_NOTICE_DATE", "CLOSE_PRICE"] if c in df.columns]
    df = df.select(keep).rename({"SECURITY_CODE": "code", "END_DATE": "period_end",
                                "HOLD_NOTICE_DATE": "notice_date"})
    df = df.with_columns(
        pl.col("period_end").str.slice(0, 10).alias("period_end"),
        pl.col("notice_date").str.slice(0, 10).alias("notice_date"),
    ).unique(subset=["code", "period_end"], keep="last").sort(["code", "period_end"])
    path = OUT / "holder_num" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    print(f"holder_num -> {path}  rows={df.height}  symbols={df['code'].n_unique()}  "
          f"periods={df['period_end'].n_unique()}")
    return df.height


def pull_top_float(periods: list[str]) -> int:
    """十大流通股东（最近若干期，用于页面展示；历史回测用户数已足够）。

    必须按 `HOLDER_RANK` 分片：东财单次查询硬上限 5 万行，而一期全市场是
    5400 只 × 10 档 ≈ 5.4 万行 ⇒ 不分片会静默截掉尾部 ~900 只（实测 688 板块
    缺 269 只、600 缺 128 只），且接口不报错、只返回"成功"。按 rank<=5 / >5
    切开后每片 2.8 万行，安全在线内。
    """
    cols = ("SECURITY_CODE,END_DATE,HOLDER_NAME,HOLD_NUM,FREE_HOLDNUM_RATIO,"
            "HOLDER_RANK,IS_HOLDORG,HOLDER_TYPE,HOLD_NUM_CHANGE,NOTICE_DATE")
    shards = ["(HOLDER_RANK<=5)", "(HOLDER_RANK>5)"]
    allrows: list[dict] = []
    for i, p in enumerate(periods, 1):
        got = 0
        for sh in shards:
            try:
                rows = fetch_paged("RPT_F10_EH_FREEHOLDERS", f"(END_DATE='{p}'){sh}",
                                   columns=cols, sort="SECURITY_CODE", sort_type="1")
            except Exception as e:  # noqa: BLE001
                print(f"  [top {i}/{len(periods)}] {p}{sh} FAIL {e}", flush=True)
                continue
            allrows.extend(rows)
            got += len(rows)
        print(f"  [top {i}/{len(periods)}] {p} n={got}", flush=True)
    if not allrows:
        return 0
    df = pl.DataFrame(allrows, infer_schema_length=None)
    keep = [c for c in ["SECURITY_CODE", "END_DATE", "HOLDER_NAME", "HOLD_NUM",
                        "FREE_HOLDNUM_RATIO", "HOLDER_RANK", "IS_HOLDORG",
                        "HOLDER_TYPE", "HOLD_NUM_CHANGE", "NOTICE_DATE"] if c in df.columns]
    df = df.select(keep).rename({"SECURITY_CODE": "code", "END_DATE": "period_end",
                                 "HOLD_NUM": "hold_num", "FREE_HOLDNUM_RATIO": "ratio",
                                 "HOLDER_RANK": "rank", "IS_HOLDORG": "is_org",
                                 "HOLDER_TYPE": "holder_type", "HOLDER_NAME": "holder_name",
                                 "NOTICE_DATE": "notice_date"})
    df = df.with_columns(
        pl.col("period_end").str.slice(0, 10).alias("period_end"),
        pl.col("notice_date").str.slice(0, 10).alias("notice_date"),
        pl.col("holder_name").str.strip_chars().alias("holder_name"),
    ).unique(subset=["code", "period_end", "holder_name"], keep="last").sort(
        ["code", "period_end", "rank"])
    path = OUT / "top_float" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    print(f"top_float -> {path} rows={df.height} symbols={df['code'].n_unique()}")
    return df.height


def pull_lift() -> int:
    """限售解禁时间表（全市场，未来与历史都给）。"""
    cols = ("SECURITY_CODE,FREE_DATE,CURRENT_FREE_SHARES,LIFT_MARKET_CAP,FREE_RATIO,"
            "TOTAL_RATIO,FREE_SHARES_TYPE,BATCH_HOLDER_NUM")
    start = "2014-01-01"
    rows = fetch_paged("RPT_LIFT_STAGE", f"(FREE_DATE>='{start}')", columns=cols,
                       sort="FREE_DATE", sort_type="1", page_size=500)
    if not rows:
        return 0
    df = pl.DataFrame(rows, infer_schema_length=None)
    keep = [c for c in ["SECURITY_CODE", "FREE_DATE", "CURRENT_FREE_SHARES",
                        "LIFT_MARKET_CAP", "FREE_RATIO", "TOTAL_RATIO",
                        "FREE_SHARES_TYPE", "BATCH_HOLDER_NUM"] if c in df.columns]
    df = df.select(keep).rename({"SECURITY_CODE": "code", "FREE_DATE": "free_date",
                                 "CURRENT_FREE_SHARES": "free_shares",
                                 "LIFT_MARKET_CAP": "lift_mcap", "FREE_RATIO": "free_ratio",
                                 "TOTAL_RATIO": "total_ratio",
                                 "FREE_SHARES_TYPE": "shares_type",
                                 "BATCH_HOLDER_NUM": "batch_holders"})
    df = df.with_columns(pl.col("free_date").str.slice(0, 10).alias("free_date")).sort(
        ["code", "free_date"])
    path = OUT / "lift" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    print(f"lift -> {path} rows={df.height} symbols={df['code'].n_unique()} "
          f"range={df['free_date'].min()}..{df['free_date'].max()}")
    return df.height


def pull_top_holders(periods: list[str]) -> int:
    """十大股东（占总股本）—— 「锁仓=前两大股东占比」判据的数据源。

    与 pull_top_float（十大流通股东）的区别：本函数 `ratio` 是 HOLD_NUM_RATIO，
    **占总股本**，直接对应「前两大股东占 60%+」；流通股东口径含 HKSCC 托管户
    会虚高，不能用来判锁仓。

    同样按 `HOLDER_RANK` 分片（一期全市场 ~5.6 万行 > 5 万硬上限）。后端
    `citizenship._top2_concentration` 只读**最新一期**，这里拉多期是给
    稳定性复核/回测留余地。
    """
    cols = ("SECURITY_CODE,END_DATE,HOLDER_NAME,HOLD_NUM,HOLD_NUM_RATIO,"
            "HOLDER_RANK,IS_HOLDORG,HOLDER_STATE,SHARES_TYPE,TOTAL_SHARES_NUM")
    shards = ["(HOLDER_RANK<=5)", "(HOLDER_RANK>5)"]
    allrows: list[dict] = []
    for i, p in enumerate(periods, 1):
        got = 0
        for sh in shards:
            try:
                rows = fetch_paged("RPT_F10_EH_HOLDERS", f"(END_DATE='{p}'){sh}",
                                   columns=cols, sort="SECURITY_CODE", sort_type="1")
            except Exception as e:  # noqa: BLE001
                print(f"  [holders {i}/{len(periods)}] {p}{sh} FAIL {e}", flush=True)
                continue
            allrows.extend(rows)
            got += len(rows)
        print(f"  [holders {i}/{len(periods)}] {p} n={got}", flush=True)
    if not allrows:
        return 0
    df = pl.DataFrame(allrows, infer_schema_length=None)
    keep = [c for c in ["SECURITY_CODE", "END_DATE", "HOLDER_NAME", "HOLD_NUM",
                        "HOLD_NUM_RATIO", "HOLDER_RANK", "IS_HOLDORG",
                        "HOLDER_STATE", "SHARES_TYPE", "TOTAL_SHARES_NUM"] if c in df.columns]
    df = df.select(keep).rename({"SECURITY_CODE": "code", "END_DATE": "period_end",
                                 "HOLDER_NAME": "holder_name", "HOLD_NUM": "hold_num",
                                 "HOLD_NUM_RATIO": "ratio", "HOLDER_RANK": "rank",
                                 "IS_HOLDORG": "is_org", "HOLDER_STATE": "holder_state",
                                 "SHARES_TYPE": "shares_type",
                                 "TOTAL_SHARES_NUM": "total_shares"})
    df = df.with_columns(
        pl.col("period_end").str.slice(0, 10).alias("period_end"),
        pl.col("holder_name").str.strip_chars().alias("holder_name"),
        pl.col("code").cast(pl.Utf8),
    ).unique(subset=["code", "period_end", "rank"], keep="last").sort(
        ["code", "period_end", "rank"])
    path = OUT / "top_holders" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    print(f"top_holders -> {path} rows={df.height} symbols={df['code'].n_unique()} "
          f"periods={df['period_end'].n_unique()}")
    return df.height


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "num"
    if what == "num":
        pull_holder_num()
    elif what == "top":
        pull_top_float(quarters(2024)[-8:])  # 最近 ~16 期
    elif what == "holders":
        pull_top_holders(quarters(2024)[-4:])  # 最近 ~4 期（后端只用最新一期）
    elif what == "lift":
        pull_lift()


if __name__ == "__main__":
    main()
