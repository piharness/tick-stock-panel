"""换手≥25% 的 5 日负超额，是不是次新股效应。

主板单板块、同涨幅箱、可买样本已确认 -160bp（t=-2.55）。
剩下最可能的朴素解释：换手 25% 是「新上市 + 无历史筹码」的标志，而次新股在上市后
3~12 个月系统性回落。若效应全部落在上市 <250 天的股票上，那它不是「高换手」的信号，
而是「回避次新股」的常识。
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import duckdb
import polars as pl

OUT = Path(__file__).with_name("_diag_turnover_newlisting_result.json")
spec = importlib.util.spec_from_file_location(
    "s2p", Path(__file__).with_name("_diag_signal_to_product.py")
)
s2p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2p)


def tt(df: pl.DataFrame, cond: str, col: str, h: int = 5) -> dict:
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
    con = duckdb.connect()
    con.execute("create view i as select symbol, listing_date from read_parquet('data/instruments/*.parquet')")
    inst = con.sql("select * from i").pl()
    con.close()

    df = s2p.build(s2p.load_daily())
    df = df.with_columns(
        pl.col("symbol").str.slice(0, 3).alias("pfx"),
        (pl.col("pct") / 0.005).round().cast(pl.Int32).alias("bucket"),
    )
    df = df.filter(
        pl.col("pfx").is_in(("000", "001", "002", "003", "600", "601", "603", "605"))
        & pl.col("buyable")
    )
    df = df.join(inst, on="symbol", how="left").with_columns(
        pl.col("listing_date").str.to_date("%Y-%m-%d", strict=False).alias("ld")
    )
    # 逐日重算同涨幅箱超额（主板内部）
    for h in (5,):
        mkt = df.group_by("date").agg(pl.col(f"r{h}").mean().alias(f"m{h}"))
        df = df.join(mkt, on="date", how="left").with_columns(
            (pl.col(f"r{h}") - pl.col(f"m{h}")).alias(f"x{h}")
        )
        bm = df.group_by(["date", "bucket"]).agg(
            pl.col(f"r{h}").mean().alias(f"bm{h}"), pl.len().alias(f"bn{h}")
        )
        df = df.join(bm, on=["date", "bucket"], how="left").with_columns(
            pl.when(pl.col(f"bn{h}") >= 5)
            .then(pl.col(f"r{h}") - pl.col(f"bm{h}")).otherwise(None).alias(f"c{h}")
        )

    df = df.with_columns(
        (pl.col("date") - pl.col("ld")).dt.total_days().alias("age_days"),
        (pl.col("turnover_rate") >= 25).alias("t_tr25"),
    )
    print("listing_date 缺失:", df["ld"].null_count(), "/", df.height)

    res = {"base": tt(df, "t_tr25", "c5")}
    for lo, hi, name in ((0, 250, "上市<250天"), (250, 500, "250-500天"),
                         (500, 10**6, ">500天")):
        cond = (pl.col("age_days") >= lo) & (pl.col("age_days") < hi) & pl.col("t_tr25")
        res[name] = tt(df.with_columns(cond.alias("cc")), "cc", "c5")
        # 该分组内「高换手」占全部样本的比例
        grp = df.filter((pl.col("age_days") >= lo) & (pl.col("age_days") < hi))
        res[name]["events_in_group"] = int(grp.filter(pl.col("t_tr25")).height)
        res[name]["group_rows"] = grp.height

    # 换手≥25% 事件的上市天数分布
    ev = df.filter(pl.col("t_tr25"))["age_days"]
    res["event_age"] = {
        "median": float(ev.median()),
        "share_lt250": round((ev < 250).mean(), 3),
        "share_lt500": round((ev < 500).mean(), 3),
    }
    # 对照：不限换手，只看「上市<250天」本身的超额
    res["pure_newlisting"] = tt(
        df.with_columns(((pl.col("age_days") < 250)).alias("cc")), "cc", "c5"
    )

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in res.items():
        if isinstance(v, dict) and "mean_bp" in v:
            print(f"  {k:<20}{v['n']:>6}笔 {v['mean_bp']:>8.1f}bp t={v['t']:>6} MDE={v['mde_bp']:>7}")
        elif k == "event_age":
            print("  事件上市天数:", v)
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
