"""最后一刀：换手≥25% 的 5 日负超额，是不是「短期反转」的另一个说法。

做法：
  A. 只用「过去 5 日涨幅」排序（完全不看换手），看 H5 超额长什么样；
  B. 在「过去 5 日涨幅」十分位内部，再看换手≥25% 的增量；
  C. 反过来，在换手≥25% 内部，看 5 日涨幅是否还有增量。
如果 A 就能复现 B 之前的数，且 B 的增量≈0，则换手没有独立信息。
另附：H1 窗口的合法性检查（同日买卖在 A 股 T+1 下不成立）。
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import polars as pl

OUT = Path(__file__).with_name("_diag_reversal_control_result.json")
spec = importlib.util.spec_from_file_location(
    "s2p", Path(__file__).with_name("_diag_signal_to_product.py")
)
s2p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2p)


def tt(df: pl.DataFrame, col: str, h: int) -> dict:
    d = df.select(["date", col]).drop_nulls()
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
    df = s2p.add_excess(s2p.build(s2p.load_daily()))
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1).alias("mom5"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("mom20"),
        (pl.col("turnover_rate") >= 25).alias("t_tr"),
    )
    df = df.with_columns(
        [
            (pl.col("mom5").rank("ordinal").over("date") / pl.len().over("date") * 10)
            .ceil().cast(pl.Int32).alias("d_mom5"),
            (pl.col("turnover_rate").rank("ordinal").over("date") / pl.len().over("date") * 10)
            .ceil().cast(pl.Int32).alias("d_tr"),
        ]
    )

    res: dict = {"legal_window_check": {}, "A_pure_momentum": {}, "B_within_mom5": {},
                 "C_within_turnover": {}, "D_exact_zero_share": {}}

    # --- A: 纯短期反转，不看换手
    for dec in range(1, 11):
        res["A_pure_momentum"][f"mom5_dec{dec}"] = tt(df.filter(pl.col("d_mom5") == dec), "c5", 5)
    res["A_pure_momentum"]["mkt_H5_all"] = tt(df, "x5", 5)

    # --- B: 在 mom5 十分位内部，换手≥25% 的增量
    for dec in (8, 9, 10):
        sub = df.filter(pl.col("d_mom5") == dec)
        res["B_within_mom5"][f"mom5_dec{dec}_tr>=25"] = tt(sub.filter(pl.col("t_tr")), "c5", 5)
        res["B_within_mom5"][f"mom5_dec{dec}_tr<25"] = tt(sub.filter(~pl.col("t_tr")), "c5", 5)

    # --- C: 换手≥25% 内部按 5 日涨幅分组
    sub = df.filter(pl.col("t_tr"))
    for dec in (1, 5, 7, 9, 10):
        res["C_within_turnover"][f"tr>=25_mom5_dec{dec}"] = tt(
            sub.filter(pl.col("d_mom5") == dec), "c5", 5
        )
    res["C_within_turnover"]["tr>=25_all"] = tt(sub, "c5", 5)

    # --- D: H1 窗口合法性（同日买入同日卖出在 T+1 下不成立）
    sub = df.filter(pl.col("turnover_rate") >= 25)
    zeros = (sub["r1"].abs() < 1e-12).mean()
    res["D_exact_zero_share"] = {
        "share_r1_exact_zero": round(zeros, 3),
        "H1_bin_bp": round(sub["c1"].mean() * 1e4, 1),
        "H1_note": "close[T+1]/open[T+1]：同日买入当日卖出，A股T+1不成立，仅作诊断",
        "H2_bin_bp": round(sub["c2"].mean() * 1e4, 1),
    }

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    def line(k, d):
        if not d.get("n"):
            return f"  {k:<26} 样本不足"
        return f"  {k:<26}{d['n']:>7}笔 {d['mean_bp']:>8.1f}bp t={d['t']:>6} MDE={d['mde_bp']:>7}"

    print("A. 纯 5 日反转（不看换手），同涨幅箱口径 H5")
    for k, v in res["A_pure_momentum"].items():
        print(line(k, v))
    print("\nB. 在 5 日涨幅十分位内部，换手≥25% 的增量")
    for k, v in res["B_within_mom5"].items():
        print(line(k, v))
    print("\nC. 换手≥25% 内部按 5 日涨幅分")
    for k, v in res["C_within_turnover"].items():
        print(line(k, v))
    print("\nD. H1 窗口合法性与精确零收益占比")
    print("  ", res["D_exact_zero_share"])
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
