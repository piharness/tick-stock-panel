"""单板块重算：同涨幅箱对照必须同板块，否则「主板涨停」是在跟「20cm 涨 10%」比。

20cm 板涨 10% 没有触板、次日还有空间；主板涨 10% 已经封死。把两者放进同一个涨幅箱，
会让「涨停」看起来比同涨幅股票强 —— 这是分箱口径的污染，不是信号。
所以这里把样本限制到主板 10cm 单一板块（000/001/002/003/600/601/603/605），
在板块内部重新做同涨幅箱匹配，再算一遍。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import polars as pl

OUT = Path(__file__).with_name("_diag_mainboard_only_result.json")
import importlib.util

spec = importlib.util.spec_from_file_location(
    "s2p", Path(__file__).with_name("_diag_signal_to_product.py")
)
s2p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2p)


def add_bucket_excess(df: pl.DataFrame) -> pl.DataFrame:
    out = df
    for h in (2, 5):
        mkt = df.group_by("date").agg(pl.col(f"r{h}").mean().alias(f"m{h}"))
        out = out.join(mkt, on="date", how="left").with_columns(
            (pl.col(f"r{h}") - pl.col(f"m{h}")).alias(f"x{h}")
        )
        bm = out.group_by(["date", "bucket"]).agg(
            pl.col(f"r{h}").mean().alias(f"bm{h}"), pl.len().alias(f"bn{h}")
        )
        out = out.join(bm, on=["date", "bucket"], how="left").with_columns(
            pl.when(pl.col(f"bn{h}") >= 5)
            .then(pl.col(f"r{h}") - pl.col(f"bm{h}"))
            .otherwise(None)
            .alias(f"c{h}")
        )
    return out


def tt(df: pl.DataFrame, cond: str, col: str, h: int) -> dict:
    d = df.filter(pl.col(cond)).select(["date", col]).drop_nulls()
    if d.height == 0:
        return {"n": 0}
    s = d.group_by("date").agg(pl.col(col).mean().alias("v"), pl.len().alias("k"))
    x = s["v"]
    neff = max(len(x) / h, 1.0)
    se = x.std() / math.sqrt(neff)
    return {
        "n": int(d.height),
        "days": len(x),
        "mean_bp": round(x.mean() * 1e4, 1),
        "t": round(x.mean() / se, 2) if se > 0 else None,
        "mde_bp": round(2 * se * 1e4, 1),
    }


def main() -> None:
    df = s2p.build(s2p.load_daily())
    df = df.with_columns(
        pl.col("symbol").str.slice(0, 3).alias("pfx"),
        (pl.col("pct") / 0.005).round().cast(pl.Int32).alias("bucket"),
    )
    main_board = ("000", "001", "002", "003", "600", "601", "603", "605")
    df = df.filter(pl.col("pfx").is_in(main_board) & pl.col("buyable"))
    df = add_bucket_excess(df)
    df = df.with_columns(
        (pl.col("consecutive_limit_ups") >= 1).alias("t_limit"),
        (pl.col("consecutive_limit_ups") == 1).alias("t_first"),
        ((pl.col("high_pct") >= pl.col("lp") - 0.003) & (pl.col("consecutive_limit_ups") == 0)).alias("t_zhaban"),
        (pl.col("turnover_rate") >= 25).alias("t_tr25"),
    )
    res = {"sample": {"rows": df.height, "days": df["date"].n_unique(), "symbols": df["symbol"].n_unique()},
           "cells": {}}
    for name, cond in (("涨停(含连板)", "t_limit"), ("首板", "t_first"),
                       ("炸板", "t_zhaban"), ("换手≥25%", "t_tr25")):
        cell = {}
        for h in (2, 5):
            cell[f"mkt_H{h}"] = tt(df, cond, f"x{h}", h)
            cell[f"bucket_H{h}"] = tt(df, cond, f"c{h}", h)
        cell["rate"] = round(df.filter(pl.col(cond)).height / df.height, 5)
        res["cells"][name] = cell

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"主板单板块样本 {res['sample']}")
    for name, c in res["cells"].items():
        print(f"\n{name}  触发率{c['rate']*100:.2f}%")
        for h in (2, 5):
            for b in ("mkt", "bucket"):
                d = c[f"{b}_H{h}"]
                print(f"  {b:6}H{h} {d['mean_bp']:>8.1f}bp t={d['t']:>6} MDE={d['mde_bp']:>7} n={d['n']:>6}")


if __name__ == "__main__":
    main()
