"""游资 × 量化 博弈结构变迁诊断.

核心检验：`拉涨停就被砸盘` 是否在数据里成立，以及溢价从哪里迁移到了哪里。

逐年口径（2010-2026）：
  1. 涨停股占比、炸板率（触板未封）
  2. 涨停次日：隔夜 gap / 日内 open->close / 全天 close->close / 5 日
  3. 首板 vs 连板（1/2/3/>=4）的次日分解
  4. 大阳线（>=7% 未涨停）之后的 T+1、T+5 —— 「拉涨被砸」的直接度量
  5. 冲高回落：日内最高涨幅 >=5% 的样本，(close/high - 1) 的均值
  6. 涨停封板强度不可得（无历史分钟线），用「炸板率」代理

数据边界：raw_* 用于涨跌停判定；close/open（前复权）用于收益率。
ST 股按当前名称剔除（历史上戴帽/摘帽不可知，混入会污染涨跌停阈值）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.price_limits import polars_limit_price, polars_price_limit_pct  # noqa: E402

DAILY = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "game_shift.json"
COLS = [
    "symbol",
    "date",
    "open",
    "high",
    "close",
    "raw_close",
    "raw_high",
    "consecutive_limit_ups",
]
YEARS = list(range(2010, 2027))
PAD = 12  # 年末向前补的下一交易日元数，用于 T+5


def partitions() -> list[str]:
    ds = sorted(d[5:] for d in os.listdir(DAILY) if d.startswith("date="))
    return ds


def load_year(dates: list[str]) -> pl.DataFrame:
    frames = []
    for d in dates:
        p = DAILY / f"date={d}" / "part.parquet"
        if p.exists():
            frames.append(pl.read_parquet(p, columns=COLS))
    df = pl.concat(frames)
    return df.sort(["symbol", "date"])


def prep(df: pl.DataFrame, names: dict[str, str], listing: dict[str, str]) -> pl.DataFrame:
    df = df.with_columns(
        [
            pl.col("symbol").replace_strict(names, default="").alias("name"),
            pl.col("symbol").replace_strict(listing, default=None).alias("ld"),
        ]
    )
    df = df.filter(
        ~pl.col("name").str.to_uppercase().str.contains("ST", literal=True)
    )
    # 剔除上市未满 90 个自然日的次新：一字板/无涨跌幅窗口会污染涨停溢价
    df = df.filter(
        pl.col("ld").is_null()
        | ((pl.col("date") - pl.col("ld").str.to_date()).dt.total_days() >= 90)
    )
    # 无 raw_open 列：raw_open = open * (raw_close / close)，否则一字板判定会因复权错位
    df = df.with_columns(
        (pl.col("open") * pl.col("raw_close") / pl.col("close")).alias("raw_open")
    )
    df = df.with_columns(pl.col("raw_close").shift(1).over("symbol").alias("prev_raw"))
    df = df.with_columns(
        polars_price_limit_pct(
            pl.col("symbol"), pl.col("date"), pl.lit(False)
        ).alias("lim_pct")
    )
    df = df.with_columns(
        polars_limit_price(pl.col("prev_raw"), pl.col("lim_pct"), up=True).alias("lim")
    )
    # 收益率（前复权）
    df = df.with_columns(
        [
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret1"),
            (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1).alias("t1"),
            (pl.col("open").shift(-1).over("symbol") / pl.col("close") - 1).alias("t1_gap"),
            (
                pl.col("close").shift(-1).over("symbol")
                / pl.col("open").shift(-1).over("symbol")
                - 1
            ).alias("t1_intra"),
            (pl.col("close").shift(-5).over("symbol") / pl.col("close") - 1).alias("t5"),
            (
                pl.col("raw_high") / pl.col("prev_raw") - 1
            ).alias("hi_gain"),
            (pl.col("raw_close") / pl.col("raw_high") - 1).alias("fade"),
            # 次日盘中路径：开盘后是否还有上冲
            (
                pl.col("high").shift(-1).over("symbol")
                / pl.col("open").shift(-1).over("symbol")
                - 1
            ).alias("t1_hi_open"),
        ]
    )
    df = df.filter(pl.col("prev_raw").is_not_null() & (pl.col("prev_raw") > 0))
    df = df.with_columns(
        [
            (pl.col("raw_high") >= pl.col("lim") - 0.005).alias("touched"),
            (pl.col("raw_close") >= pl.col("lim") - 0.005).alias("lu"),
        ]
    )
    df = df.with_columns(
        [
            (pl.col("touched") & ~pl.col("lu")).alias("broke"),
            # 一字板：开盘即在涨停价，全天无换手（open 贴住 lim）
            (pl.col("lu") & (pl.col("raw_open") >= pl.col("lim") - 0.005)).alias("yizi"),
        ]
    )
    return df


def _m(s: pl.Series, q: float | None = None) -> float | None:
    v = s.drop_nulls()
    if v.len() == 0:
        return None
    if q is not None:
        return round(float(v.quantile(q)), 6)
    return round(float(v.mean()), 6)


def stat_block(sub: pl.DataFrame) -> dict:
    if sub.height == 0:
        return {"n": 0}
    return {
        "n": sub.height,
        "t1": _m(sub["t1"]),
        "t1_gap": _m(sub["t1_gap"]),
        "t1_intra": _m(sub["t1_intra"]),
        "t1_hi_open": _m(sub["t1_hi_open"]),
        "t5": _m(sub["t5"]),
        "ex1": _m(sub["ex1"]) if "ex1" in sub.columns else None,
        "ex5": _m(sub["ex5"]) if "ex5" in sub.columns else None,
        "win1": round(float((sub["t1"].drop_nulls() > 0).mean()), 4)
        if sub["t1"].drop_nulls().len()
        else None,
        "p90_t1": _m(sub["t1"], 0.90),
        "p10_t1": _m(sub["t1"], 0.10),
    }


def main() -> None:
    dates = partitions()
    by_year: dict[str, list[str]] = {}
    for d in dates:
        by_year.setdefault(d[:4], []).append(d)

    inst = pl.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")
    names = dict(inst.select(["symbol", "name"]).iter_rows())
    listing = dict(inst.select(["symbol", "listing_date"]).iter_rows())

    res: dict[str, dict] = {}
    for y in YEARS:
        ys = str(y)
        ds = by_year.get(ys, [])
        if not ds:
            continue
        nxt = by_year.get(str(y + 1), [])[:PAD]
        df = prep(load_year(ds + nxt), names, listing)
        # 全市场等权基准（同日），用于把「涨停溢价」拆成 alpha 与市场 beta
        mkt = (
            df.group_by("date")
            .agg(
                pl.col("t1").mean().alias("m1"),
                pl.col("t5").mean().alias("m5"),
            )
        )
        df = df.join(mkt, on="date", how="left").with_columns(
            [(pl.col("t1") - pl.col("m1")).alias("ex1"), (pl.col("t5") - pl.col("m5")).alias("ex5")]
        )
        # 只统计本年
        cur = df.filter(pl.col("date").dt.year() == y)
        ndays = cur["date"].n_unique()
        nobs = cur.height

        lu = cur.filter(pl.col("lu"))
        broke = cur.filter(pl.col("broke"))
        touched = cur.filter(pl.col("touched"))
        big = cur.filter((pl.col("ret1") >= 0.07) & ~pl.col("lu"))
        high5 = cur.filter(pl.col("hi_gain") >= 0.05)

        b = {
            "trading_days": ndays,
            "n_obs": nobs,
            "lu_n": lu.height,
            "lu_share": round(lu.height / nobs, 6),
            "lu_per_day": round(lu.height / ndays, 2),
            "touch_n": touched.height,
            "break_rate": round(broke.height / touched.height, 4) if touched.height else None,
            "max_board": int(cur["consecutive_limit_ups"].max() or 0),
            "n_board_ge3": cur.filter(pl.col("consecutive_limit_ups") >= 3).height,
            "n_board_ge5": cur.filter(pl.col("consecutive_limit_ups") >= 5).height,
            "lu_all": stat_block(lu),
            "lu_huan": stat_block(lu.filter(~pl.col("yizi"))),  # 换手板（非一字）
            "lu_yizi": stat_block(lu.filter(pl.col("yizi"))),
            "big_up": stat_block(big),
            "fade_hi5": {
                "n": high5.height,
                "fade": _m(high5["fade"]),
                "fade_p25": _m(high5["fade"], 0.25),
            },
        }
        # 分板高度
        for lvl, cond in (
            ("b1", pl.col("consecutive_limit_ups") == 1),
            ("b2", pl.col("consecutive_limit_ups") == 2),
            ("b3", pl.col("consecutive_limit_ups") == 3),
            ("b4p", pl.col("consecutive_limit_ups") >= 4),
        ):
            b[f"lu_{lvl}"] = stat_block(lu.filter(cond))
        res[ys] = b
        print(f"{ys} days={ndays} obs={nobs} lu={lu.height} broke={b['break_rate']}", flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
