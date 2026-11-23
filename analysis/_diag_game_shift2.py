"""游资 × 量化 博弈结构 —— 第二轮：条件结构与期限结构.

回答三个问题：
  Q1 涨停溢价是「一次性」还是「有持续性」→ ex1 / ex3 / ex5 / ex10 的期限结构
  Q2 「点火方式」在日线上可测的代理人（开盘涨幅）是否区分次日溢价
  Q3 炸板（触板未封）之后的定价 —— 「被砸」的另一侧有没有钱

口径同 _diag_game_shift.py：raw_* 判涨跌停，close/open 算收益，剔除 ST 与上市<90天次新。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "analysis"))

from app.price_limits import polars_limit_price, polars_price_limit_pct  # noqa: E402

DAILY = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "game_shift2.json"
COLS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "raw_close",
    "raw_high",
    "amount",
    "turnover_rate",
    "consecutive_limit_ups",
]
YEARS = list(range(2015, 2027))
PAD = 18
HORIZONS = (1, 3, 5, 10)
ERAS = {
    "2015-2017": (2015, 2017),
    "2018-2020": (2018, 2020),
    "2021-2023": (2021, 2023),
    "2024-2026": (2024, 2026),
}


def load_year(dates: list[str]) -> pl.DataFrame:
    frames = []
    for d in dates:
        p = DAILY / f"date={d}" / "part.parquet"
        if p.exists():
            frames.append(pl.read_parquet(p, columns=COLS))
    return pl.concat(frames).sort(["symbol", "date"])


def prep(df: pl.DataFrame, names: dict, listing: dict) -> pl.DataFrame:
    df = df.with_columns(
        [
            pl.col("symbol").replace_strict(names, default="").alias("name"),
            pl.col("symbol").replace_strict(listing, default=None).alias("ld"),
        ]
    )
    df = df.filter(~pl.col("name").str.to_uppercase().str.contains("ST", literal=True))
    df = df.filter(
        pl.col("ld").is_null()
        | ((pl.col("date") - pl.col("ld").str.to_date()).dt.total_days() >= 90)
    )
    # 日线无 raw_open 列：raw_open = open * (raw_close / close)
    df = df.with_columns(
        (pl.col("open") * pl.col("raw_close") / pl.col("close")).alias("raw_open")
    )
    df = df.with_columns(pl.col("raw_close").shift(1).over("symbol").alias("prev_raw"))
    df = df.with_columns(
        polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.lit(False)).alias("lim_pct")
    )
    df = df.with_columns(
        polars_limit_price(pl.col("prev_raw"), pl.col("lim_pct"), up=True).alias("lim")
    )
    for h in HORIZONS:
        df = df.with_columns(
            (pl.col("close").shift(-h).over("symbol") / pl.col("close") - 1).alias(f"t{h}")
        )
    df = df.filter(pl.col("prev_raw").is_not_null() & (pl.col("prev_raw") > 0))
    df = df.with_columns(
        [
            (pl.col("raw_high") >= pl.col("lim") - 0.005).alias("touched"),
            (pl.col("raw_close") >= pl.col("lim") - 0.005).alias("lu"),
            (pl.col("raw_open") / pl.col("prev_raw") - 1).alias("gap_open"),
            (pl.col("amount") / pl.col("amount").rolling_mean(20).over("symbol").shift(1).over("symbol")).alias("amt_ratio"),
        ]
    )
    df = df.with_columns(
        [
            (pl.col("touched") & ~pl.col("lu")).alias("broke"),
            (pl.col("lu") & (pl.col("raw_open") >= pl.col("lim") - 0.005)).alias("yizi"),
        ]
    )
    return df


def q(s: pl.Series, p: float) -> float | None:
    v = s.drop_nulls()
    return round(float(v.quantile(p)), 6) if v.len() else None


def mean(s: pl.Series) -> float | None:
    v = s.drop_nulls()
    return round(float(v.mean()), 6) if v.len() else None


def block(sub: pl.DataFrame, label: str) -> dict:
    if sub.height == 0:
        return {"label": label, "n": 0}
    d = {"label": label, "n": sub.height}
    for h in HORIZONS:
        d[f"ex{h}"] = mean(sub[f"ex{h}"]) if f"ex{h}" in sub.columns else None
    d["t1"] = mean(sub["t1"]) if "t1" in sub.columns else None
    d["win1"] = round(float((sub["t1"].drop_nulls() > 0).mean()), 4) if sub["t1"].drop_nulls().len() else None
    d["p10_t1"] = q(sub["t1"], 0.10)
    return d


def main() -> None:
    ds_all = sorted(d[5:] for d in os.listdir(DAILY) if d.startswith("date="))
    by_year: dict[str, list[str]] = {}
    for d in ds_all:
        by_year.setdefault(d[:4], []).append(d)

    inst = pl.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")
    names = dict(inst.select(["symbol", "name"]).iter_rows())
    listing = dict(inst.select(["symbol", "listing_date"]).iter_rows())

    rows_lu: list[dict] = []
    rows_br: list[dict] = []
    board: dict[str, dict] = {}

    for y in YEARS:
        ds = by_year.get(str(y), [])
        if not ds:
            continue
        nxt = by_year.get(str(y + 1), [])[:PAD]
        df = prep(load_year(ds + nxt), names, listing)
        mkt = df.group_by("date").agg(
            [pl.col(f"t{h}").mean().alias(f"m{h}") for h in HORIZONS]
        )
        df = df.join(mkt, on="date", how="left")
        df = df.with_columns(
            [(pl.col(f"t{h}") - pl.col(f"m{h}")).alias(f"ex{h}") for h in HORIZONS]
        )
        cur = df.filter(pl.col("date").dt.year() == y)
        cur = cur.with_columns(pl.lit(y).alias("year"))

        # 连板高度分布（按千股日归一）
        nd = cur["date"].n_unique()
        nobs = cur.height
        bd = cur.filter(pl.col("consecutive_limit_ups") > 0).group_by(
            pl.col("consecutive_limit_ups").clip(1, 8).alias("b")
        ).len()
        board[str(y)] = {
            "days": nd,
            "n_obs": nobs,
            "per_kday": {str(r["b"]): round(r["len"] / nobs * 1000, 3) for r in bd.iter_rows(named=True)},
            "max_board": int(cur["consecutive_limit_ups"].max() or 0),
        }

        huan = cur.filter(pl.col("lu") & ~pl.col("yizi")).with_columns(
            pl.lit("换手板").alias("kind")
        )
        brk = cur.filter(pl.col("broke")).with_columns(pl.lit("炸板").alias("kind"))
        yz = cur.filter(pl.col("yizi")).with_columns(pl.lit("一字板").alias("kind"))
        big = cur.filter(
            (pl.col("close") / pl.col("prev_raw") - 1 >= 0.07) & ~pl.col("lu")
        ).with_columns(pl.lit("大阳未停").alias("kind"))

        for kind, sub in (("换手板", huan), ("炸板", brk), ("一字板", yz), ("大阳未停", big)):
            rows_lu.append(block(sub, f"{y}|{kind}"))

        # 换手板按开盘涨幅分箱
        hh = huan.with_columns(
            pl.when(pl.col("gap_open") < -0.01).then(pl.lit("A 低开<-1%"))
            .when(pl.col("gap_open") < 0.01).then(pl.lit("B 平开"))
            .when(pl.col("gap_open") < 0.03).then(pl.lit("C 高开1-3%"))
            .when(pl.col("gap_open") < 0.06).then(pl.lit("D 高开3-6%"))
            .otherwise(pl.lit("E 高开>6%")).alias("gb")
        )
        for g, sub in hh.group_by("gb"):
            rows_br.append(block(sub, f"{y}|换手板|{g[0]}"))
        # 炸板按收盘涨跌分箱
        bb = brk.with_columns(
            pl.when(pl.col("close") / pl.col("prev_raw") - 1 >= 0.05).then(pl.lit("I 收涨>=5%"))
            .when(pl.col("close") / pl.col("prev_raw") - 1 >= 0).then(pl.lit("II 收涨0-5%"))
            .otherwise(pl.lit("III 收跌")).alias("gb")
        )
        for g, sub in bb.group_by("gb"):
            rows_br.append(block(sub, f"{y}|炸板|{g[0]}"))
        # 换手板按量比分箱
        vv = huan.with_columns(
            pl.when(pl.col("amt_ratio") < 1).then(pl.lit("u1 量比<1"))
            .when(pl.col("amt_ratio") < 2).then(pl.lit("u2 1-2"))
            .when(pl.col("amt_ratio") < 4).then(pl.lit("u3 2-4"))
            .otherwise(pl.lit("u4 >4")).alias("vb")
        )
        for g, sub in vv.group_by("vb"):
            rows_br.append(block(sub, f"{y}|换手板量比|{g[0]}"))
        print(y, "done", flush=True)

    out = {"by_year_kind": rows_lu, "conditional": rows_br, "board": board}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
