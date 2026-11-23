"""5min 落地文件的**内部一致性**体检（不看覆盖，只看「里面有没有错东西」）。

为什么不复用覆盖率审计
----------------------
`_audit_minute_coverage.py` 的分母来自日K，它回答「该有的 只-日 有没有」。
它看不见这些：schema 漂移、OHLC 关系反了、价格为 0/负、同一分钟写了两行、
时间戳不在 48 个标准点上、整日根数只有 3 根（半截文件）。

这些错误的共同点是**不会让覆盖率达到 100% 的时候暴露**——覆盖率照样是 1.0，
但用它算收益会得到 -100% 或 +∞。所以必须单独扫一遍内容。

判据（全部可量化，超阈值就退出 1）
----
1. schema：所有文件列名与 dtype 必须完全一致（polars 跨文件 concat 的前提）
2. 每日根数：标准 48；允许 1~48（半日市/停牌恢复日），但 <40 的日子占比应 <1%
3. OHLC：high >= max(o,c,l) 且 low <= min(o,c,h)，误差 1e-6
4. 价格：四列都 > 0（A 股最小报价 0.01）
5. 成交量：>= 0
6. 唯一性：(symbol, datetime) 不得重复
7. 时间戳：必须落在 48 个标准点上（09:35..11:30, 13:05..15:00）

用法
----
    python analysis/_audit_minute_health.py            # 全量
    python analysis/_audit_minute_health.py --limit 200
    python analysis/_audit_minute_health.py --json     # 结构化输出
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import time as dtime
from pathlib import Path

import polars as pl

ROOT = Path(r"D:/DSH/tick-stock-panel")
F5 = ROOT / "data" / "backfill_bs_minute" / "f5"
EPS = 1e-6

#: baostock 5min 的 48 个标准落点（bar 标的是区间右端）
STD_TIMES: set[dtime] = set()
for h, m0, m1 in ((9, 35, 60), (10, 0, 60), (11, 0, 30)):
    pass
_t = []
for i in range(24):                      # 09:35 .. 11:30
    mins = 9 * 60 + 35 + 5 * i
    _t.append(dtime(mins // 60, mins % 60))
for i in range(24):                      # 13:05 .. 15:00
    mins = 13 * 60 + 5 + 5 * i
    _t.append(dtime(mins // 60, mins % 60))
STD_TIMES = set(_t)


def files(limit: int | None) -> list[Path]:
    with os.scandir(F5) as it:
        fs = sorted((Path(e.path) for e in it if e.name.endswith(".parquet")),
                    key=lambda p: p.name)
    return fs[:limit] if limit else fs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    fs = files(args.limit)
    if not fs:
        print("没有文件")
        return
    print(f"扫 {len(fs):,} 个文件 …", flush=True)

    schema0 = None
    schema_bad: list[tuple[str, str]] = []
    empty: list[str] = []
    rows_total = 0
    days_total = 0
    bars_hist: Counter = Counter()
    bad_ohlc = 0
    bad_price = 0
    bad_vol = 0
    dup_rows = 0
    bad_time = 0
    bad_time_samples: list[tuple[str, str]] = []
    thin_days = 0
    per_file: list[tuple[str, int, str, str]] = []   # symbol, rows, first, last
    ohlc_samples: list[tuple[str, str, dict]] = []

    for i, p in enumerate(fs):
        try:
            d = pl.read_parquet(p)
        except Exception as e:                       # noqa: BLE001
            schema_bad.append((p.stem, f"READ_FAIL: {type(e).__name__}: {e}"))
            continue
        if schema0 is None:
            schema0 = d.schema
        elif d.schema != schema0:
            schema_bad.append((p.stem, str(d.schema)))
        if d.height == 0:
            empty.append(p.stem)
            continue

        # f5 原始文件成交量列名是 `volume_raw`（merge 时才改叫 volume）。
        # 这里兼容两种，否则第一个文件就读崩；同时把「列名混用」当 schema 违规报出来。
        vol = "volume" if "volume" in d.columns else "volume_raw"
        if vol not in d.columns:
            schema_bad.append((p.stem, f"无成交量列: {d.columns}"))
            continue

        rows_total += d.height
        per_file.append((p.stem, d.height, str(d["datetime"][0])[:10], str(d["datetime"][-1])[:10]))

        g = (d.with_columns(pl.col("datetime").dt.date().alias("_d"))
              .group_by("_d").agg(pl.len().alias("n")))
        days_total += g.height
        for n, c in g.group_by("n").agg(pl.len()).iter_rows():
            bars_hist[n] += c
        thin_days += int(g.filter(pl.col("n") < 40).height)

        # OHLC 关系：high 必须是四者最大，low 必须是四者最小
        bad = d.filter(
            (pl.col("high") < pl.max_horizontal("open", "close", "low") - EPS)
            | (pl.col("low") > pl.min_horizontal("open", "close", "high") + EPS)
        )
        if bad.height:
            bad_ohlc += bad.height
            if len(ohlc_samples) < 5:
                ohlc_samples.append((p.stem, str(bad["datetime"][0]), bad.head(1).to_dicts()[0]))

        bad_price += d.filter(
            (pl.col("open") <= 0) | (pl.col("high") <= 0)
            | (pl.col("low") <= 0) | (pl.col("close") <= 0)
        ).height
        bad_vol += d.filter(pl.col(vol) < 0).height
        dup_rows += d.height - d.unique(subset=["symbol", "datetime"]).height

        ts = d["datetime"].dt.time()
        uniq = set(ts.unique().to_list())
        off = uniq - STD_TIMES
        if off:
            bad_time += d.filter(pl.col("datetime").dt.time().is_in(list(off))).height
            if len(bad_time_samples) < 5:
                bad_time_samples.append((p.stem, sorted(str(x) for x in off)[:4]))

        if (i + 1) % 500 == 0:
            print(f"  … {i+1}/{len(fs)}  {rows_total:,} 行", flush=True)

    thin_frac = thin_days / days_total if days_total else 0
    res = {
        "files": len(fs),
        "rows": rows_total,
        "days": days_total,
        "schema_bad": len(schema_bad),
        "empty": len(empty),
        "bad_ohlc": bad_ohlc,
        "bad_price": bad_price,
        "bad_vol": bad_vol,
        "dup_rows": dup_rows,
        "bad_time": bad_time,
        "thin_days": thin_days,
        "thin_frac": thin_frac,
        "bars_hist": dict(sorted(bars_hist.items())),
    }

    if args.json:
        print(json.dumps({**res, "schema": {k: str(v) for k, v in (schema0 or {}).items()},
                          "schema_bad_list": schema_bad[:10],
                          "empty_list": empty[:10],
                          "bad_time_samples": bad_time_samples,
                          "ohlc_samples": ohlc_samples}, ensure_ascii=False, indent=2))
        return

    print("\n=== 内部一致性 ===")
    print(f"文件 / 行 / 只-日      {len(fs):,} / {rows_total:,} / {days_total:,}")
    print(f"schema 不一致          {len(schema_bad)}"
          + (f"  {schema_bad[:3]}" if schema_bad else ""))
    print(f"空文件                 {len(empty)}" + (f"  {empty[:5]}" if empty else ""))
    print(f"OHLC 关系违规          {bad_ohlc:,}")
    print(f"非正价格               {bad_price:,}")
    print(f"负成交量               {bad_vol:,}")
    print(f"重复 (symbol,datetime) {dup_rows:,}")
    print(f"非标准时间戳           {bad_time:,}" + (f"  {bad_time_samples[:3]}" if bad_time else ""))
    print(f"单日根数 <40 的 只-日   {thin_days:,}  ({thin_frac:.4%})")
    top = sorted(bars_hist.items(), key=lambda x: -x[1])[:6]
    print(f"每日根数分布 top        {top}")
    if ohlc_samples:
        print("\nOHLC 反例：")
        for s in ohlc_samples[:3]:
            print("  ", s)

    fails = []
    if res["schema_bad"]:
        fails.append("schema")
    if res["empty"]:
        fails.append("empty")
    if res["bad_ohlc"]:
        fails.append("ohlc")
    if res["bad_price"]:
        fails.append("price")
    if res["bad_vol"]:
        fails.append("volume")
    if res["dup_rows"]:
        fails.append("dup")
    if res["bad_time"]:
        fails.append("timestamp")
    if thin_frac > 0.01:
        fails.append("thin_days>1%")

    print("\n结论：" + ("**通过**（无内部一致性错误）" if not fails
                      else "**不通过** → " + ", ".join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
