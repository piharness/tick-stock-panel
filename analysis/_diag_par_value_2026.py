"""面值保卫战的历史赔率：跌破 1 元后 20 日内能否回到 1 元以上。"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# 1) 现存股：raw_close（面值退市看实际价格，不看复权）
files = []
for e in os.scandir(DATA / "kline_daily_enriched"):
    if e.is_dir() and e.name.startswith("date=") and e.name[5:] >= "2016-01-01":
        files.append(str(Path(e.path) / "part.parquet"))
files.sort()
live = pl.read_parquet(files, columns=["symbol", "date", "raw_close"]).filter(
    pl.col("raw_close") > 0
)
live = live.with_columns(pl.lit(True).alias("listed_now"))

# 2) 退市股
gap = pl.read_parquet(DATA / "backfill_20261117" / "stock_gap_daily.parquet").select(
    "symbol", "date", "close"
).rename({"close": "raw_close"}).filter(pl.col("raw_close") > 0)
gap = gap.filter(pl.col("date") >= dt.date(2016, 1, 1)).with_columns(
    pl.lit(False).alias("listed_now")
)
# 去掉与现存股重复的 symbol（gap 包里可能含未退市的缺口股）
live_syms = set(live["symbol"].unique().to_list())
gap = gap.filter(~pl.col("symbol").is_in(list(live_syms)))

allx = pl.concat([live, gap], how="vertical").sort(["symbol", "date"])
print("rows", allx.height, "symbols", allx["symbol"].n_unique())

# 3) 找「首次跌破 1 元」
allx = allx.with_columns((pl.col("raw_close") < 1.0).alias("sub1"))
allx = allx.with_columns(
    (pl.col("sub1") & ~pl.col("sub1").shift(1).fill_null(False)).alias("first_break")
)
fb = allx.filter(pl.col("first_break")).select("symbol", "date", "listed_now")
print("首次跌破 1 元的事件数", fb.height)

# 4) 事件后 20 个交易日内最高收盘
res = []
by_sym = {s[0]: g for s, g in allx.group_by("symbol")}
for row in fb.iter_rows(named=True):
    g = by_sym[row["symbol"]].sort("date")
    idx = g.with_row_index("i").filter(pl.col("date") == row["date"])["i"][0]
    fwd = g.slice(idx + 1, 20)
    if fwd.height == 0:
        continue
    res.append({
        "symbol": row["symbol"],
        "date": row["date"],
        "listed_now": row["listed_now"],
        "max20": float(fwd["raw_close"].max()),
        "recover": bool(fwd["raw_close"].max() >= 1.0),
        "n_fwd": fwd.height,
    })

r = pl.DataFrame(res)
print("有效事件", r.height)
print("\n=== 全部样本 ===")
print(r.group_by("recover").len().sort("recover"))
print("救回率", round(r["recover"].mean() * 100, 2), "%")

print("\n=== 按「现在是否还上市」分组 ===")
print(
    r.group_by("listed_now").agg(
        pl.len().alias("n"), pl.col("recover").mean().alias("recover_rate"),
        pl.col("max20").median().alias("med_max20"),
    ).sort("listed_now")
)

# 5) 更严格：跌破后有「连续 20 日 <1 元」的，最终结局
print("\n=== 连续 20 日 <1 元的样本 ===")
runs = []
for s_key, g in allx.group_by("symbol"):
    s = s_key[0]
    g = g.sort("date")
    flags = g["sub1"].to_list()
    dates = g["date"].to_list()
    closes = g["raw_close"].to_list()
    listed = g["listed_now"][0]
    i = 0
    n = len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j < n and flags[j]:
            j += 1
        if j - i >= 20:
            trigger = dates[i + 19]
            fwd = closes[j:j + 20]
            runs.append({
                "symbol": s, "trigger": trigger, "listed_now": listed,
                "run_len": j - i,
                "back_over_1": bool(fwd and max(fwd) >= 1.0),
            })
            break
        i = j

rr = pl.DataFrame(runs) if runs else pl.DataFrame()
print("触发连续20日<1元的股票数", rr.height)
if rr.height:
    print(rr.group_by(["listed_now", "back_over_1"]).len().sort(["listed_now", "back_over_1"]))
    for k, sub in rr.group_by("listed_now"):
        print(f"listed_now={k[0]}: n={sub.height} 之后回到1元以上={sub['back_over_1'].mean()*100:.1f}%")
