"""抛压代理 double sort —— 控制规模后，换手类口径还剩多少。

第一轮已看到：to20（换手率）G5-G1 = -208bp，log_amt20（成交额）G5-G1 = -192bp。
两个因子价差几乎等大 ⇒ 高度怀疑是同一根变量（规模/流动性）的两个投影。

本脚本的判决方式（不是再跑一次回归）：
  1. 每日先按规模分 3 层（小/中/大），层内再按目标因子分 3 组。
  2. 若层内价差塌到 ~0 且 t 不显著 ⇒ 目标因子只是规模的影子，判决「归零」。
  3. 若层内仍单调且 |t|>2 ⇒ 有独立信息，值得进页面。

规模口径用 log(mcap_float) 与 log_amt20 各跑一遍（两个都试，防止口径选择制造结论）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_pressure import FWD, SAMPLE_STEP, build_panel  # noqa: E402

NG = 3  # 每组层数 / 组数


def _qcut(col: str, n: int) -> pl.Expr:
    return (
        ((pl.col(col).rank("average") / pl.col(col).count()) * n)
        .ceil()
        .clip(1, n)
        .cast(pl.Int32)
    )


def double_sort(sub: pl.DataFrame, target: str, control: str, want_low: bool) -> None:
    """按 control 分 3 层，层内按 target 分 3 组，报告每层的 target 价差。"""
    per_date = []
    for d, g in sub.group_by("date", maintain_order=True):
        g = g.drop_nulls([target, control, "fwd20"])
        if g.height < 300:
            continue
        mkt = g["fwd20"].mean()
        g = g.with_columns(
            [_qcut(control, NG).alias("c"), _qcut(target, NG).alias("t")]
        )
        cell = g.group_by(["c", "t"]).agg(pl.col("fwd20").mean().alias("r"))
        rec = {"date": d, "mkt": mkt}
        for row in cell.to_dicts():
            rec[f"c{row['c']}_t{row['t']}"] = row["r"] - mkt
        per_date.append(rec)

    if not per_date:
        return
    df = pl.DataFrame(per_date)
    print(f"\n=== target={target} | control={control} ===")
    print(f"    {'层':<8}{'T1低':>10}{'T2':>10}{'T3高':>10}{'T3-T1':>10}{'t':>7}")
    spreads = []
    for c in range(1, NG + 1):
        cols = [f"c{c}_t{t}" for t in range(1, NG + 1)]
        if not all(x in df.columns for x in cols):
            continue
        s = df[cols[2]] - df[cols[0]]
        spreads.append(s)
        m = [float(df[x].mean()) * 1e4 for x in cols]
        t = float(s.mean() / (s.std() / len(s) ** 0.5))
        label = {1: "小规模", 2: "中规模", 3: "大规模"}[c] if not want_low else {1: "低", 2: "中", 3: "高"}[c]
        print(f"    {label:<8}{m[0]:>+10.1f}{m[1]:>+10.1f}{m[2]:>+10.1f}{m[2]-m[0]:>+10.1f}{t:>+7.2f}")
    if spreads:
        pool = pl.concat([s.alias("d") for s in spreads])
        print(f"    层内平均价差 = {float(pool.mean())*1e4:+.1f}bp  "
              f"t = {float(pool.mean()/(pool.std()/len(pool)**0.5)):+.2f}")


def main() -> None:
    df = build_panel()
    df = df.with_columns(
        [
            (pl.col("to20") / pl.col("to120")).alias("to_ratio"),
            (pl.col("to_std20") / pl.col("to20")).alias("to_cv"),
            pl.col("amt20").log().alias("log_amt20"),
            pl.col("mcap").log().alias("log_mcap"),
        ]
    )
    dates = sorted(df["date"].unique().to_list())
    obs = [d for i, d in enumerate(dates) if i % SAMPLE_STEP == 0 and i < len(dates) - FWD - 1][-120:]
    sub = df.filter(
        pl.col("date").is_in(obs)
        & pl.col("fwd20").is_finite()
        & (pl.col("fwd20").abs() < 0.6)
        & pl.col("mcap").is_not_null()
        & (pl.col("mcap") > 0)
    )
    print(f"obs rows={sub.height:,}")

    for target in ["to20", "to_ratio", "to_cv", "illiq20", "amp20"]:
        double_sort(sub, target, "log_amt20", want_low=True)
    # 反向对照：控制换手率后，规模还剩多少（确认谁是根变量）
    for target in ["log_amt20", "log_mcap"]:
        double_sort(sub, target, "to20", want_low=True)


if __name__ == "__main__":
    sys.exit(main())
