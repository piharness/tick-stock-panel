"""股东户数因子的形状与因果 —— 三个必须回答的问题。

上一轮结论：户数**变化率**有效（chg_yoy 单调，spread -87bp，t=-4.5），户数**水平**无效。
但留下三个洞，任何一个不填都会让页面上的数字变成误导：

  Q1 形状：chg_q 非单调（G1 +7.4bp 弱于 G2 +23.8bp）。是倒 U、阈值，还是
     "暴降 = 崩盘后割肉"这个反向故事把左尾拖下去了？→ 10 分组 + 分段统计。
  Q2 因果：户数变化是不是"股价跌了"的镜像？→ 横截面秩残差化（chg_yoy ~ rev60），
     残差分 5 组。残差若无 alpha，则本因子只是反转效应的换皮。
  Q3 稳定：这个效应是 2016-2026 全程都有，还是只在某几年（如 2021 小票行情）？

全部用与上一轮相同的观测日与收益口径，否则数字不可比。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_pressure import FWD, SAMPLE_STEP, build_panel  # noqa: E402
from _diag_shareholder_alpha import attach, load_holder  # noqa: E402


def prep() -> pl.DataFrame:
    panel = build_panel()
    panel = panel.with_columns(
        [
            (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("rev60"),
            pl.col("amt20").log().alias("log_amt20"),
            pl.col("mcap").log().alias("log_mcap"),
        ]
    )
    dates = sorted(panel["date"].unique().to_list())
    obs = [d for i, d in enumerate(dates) if i % SAMPLE_STEP == 0 and i < len(dates) - FWD - 1][-120:]
    panel = panel.filter(pl.col("date").is_in(obs))
    df = attach(panel, load_holder())
    return df.filter(
        pl.col("fwd20").is_finite() & (pl.col("fwd20").abs() < 0.6)
        & (pl.col("mcap") > 0) & pl.col("chg_yoy").is_finite()
        & pl.col("rev60").is_not_null() & pl.col("chg_q").is_finite()
    )


def q_groups(df: pl.DataFrame, col: str, n: int, label: str) -> pl.DataFrame:
    d = df.with_columns(
        (pl.col(col).rank("average") / pl.col(col).count() * n).ceil().clip(1, n).cast(pl.Int32).alias("g")
    )
    rows = []
    for dt, gg in d.group_by("date", maintain_order=True):
        mkt = gg["fwd20"].mean()
        a = gg.group_by("g").agg((pl.col("fwd20").mean() - mkt).alias("ex"))
        for r in a.to_dicts():
            rows.append({"date": dt, "g": r["g"], "ex": r["ex"]})
    res = pl.DataFrame(rows)
    stat = res.group_by("g").agg(
        pl.col("ex").mean().alias("m"), pl.col("ex").std().alias("s"),
        pl.col("ex").count().alias("k"), (pl.col("ex") > 0).mean().alias("w")
    ).sort("g").with_columns((pl.col("m") / (pl.col("s") / pl.col("k").sqrt())).alias("t"))
    # 每组的因子取值区间（让人看到"G1 到底是什么样的公司"）
    rng = d.group_by("g").agg(
        pl.col(col).quantile(0.05).alias("q05"), pl.col(col).median().alias("med"),
        pl.col(col).quantile(0.95).alias("q95")
    ).sort("g")
    stat = stat.join(rng, on="g")
    print(f"\n=== {label} ({col}, {n} 组) ===")
    for r in stat.to_dicts():
        print(f"  G{r['g']:<2} ex={r['m']*1e4:+8.1f}bp t={r['t']:+5.2f} win={r['w']:.2f} | "
              f"因子 q05={r['q05']:+.3f} 中位={r['med']:+.3f} q95={r['q95']:+.3f}")
    return stat


def residual_factor(df: pl.DataFrame, y: str, x: str) -> pl.DataFrame:
    """逐日横截面秩残差：把 y 中与 x 单调相关的部分剥掉。

    不用线性回归而用秩：收益类变量尾部极重，OLS 会被几只票拽走。
    """
    out = []
    for dt, g in df.group_by("date", maintain_order=True):
        g = g.drop_nulls([y, x])
        if g.height < 200:
            continue
        ry = g[y].rank("average").to_numpy().astype(float)
        rx = g[x].rank("average").to_numpy().astype(float)
        # 秩标准化后 OLS：b = cov(rx,ry)/var(rx)
        rx_c = rx - rx.mean()
        ry_c = ry - ry.mean()
        b = float((rx_c * ry_c).sum() / ((rx_c ** 2).sum() + 1e-12))
        res = ry_c - b * rx_c
        out.append(
            g.select(["symbol", "date", "fwd20"]).with_columns(pl.Series("resid", res))
        )
    return pl.concat(out)


def by_year(df: pl.DataFrame, col: str, n: int = 5) -> None:
    d = df.with_columns(
        (pl.col(col).rank("average").over("date") / pl.col(col).count().over("date") * n)
        .ceil().clip(1, n).cast(pl.Int32).alias("g"),
        pl.col("date").dt.year().alias("y"),
    )
    rows = []
    for (dt,), gg in d.group_by(["date"], maintain_order=True):
        mkt = gg["fwd20"].mean()
        a = gg.group_by("g").agg((pl.col("fwd20").mean() - mkt).alias("ex"))
        m = {r["g"]: r["ex"] for r in a.to_dicts()}
        if 1 in m and n in m:
            rows.append({"date": dt, "y": gg["y"][0], "spread": m[n] - m[1]})
    res = pl.DataFrame(rows)
    print(f"\n=== {col} 逐年 G{n}-G1 价差（bp，20日） ===")
    yr = res.group_by("y").agg(
        pl.col("spread").mean().alias("m"), pl.col("spread").count().alias("n")
    ).sort("y")
    for r in yr.to_dicts():
        bar = "#" * min(40, max(0, int(abs(r["m"]) * 1e4 / 20)))
        sign = "+" if r["m"] >= 0 else "-"
        print(f"  {r['y']}  {r['m']*1e4:+8.1f}bp ({r['n']:>2} 期) {sign}{bar}")


def main() -> None:
    df = prep()
    print(f"sample rows={df.height:,} dates={df['date'].n_unique()}")

    q_groups(df, "chg_yoy", 5, "户数同比 五分位")
    q_groups(df, "chg_yoy", 10, "户数同比 十分位（看左尾）")
    q_groups(df, "chg_q", 10, "户数环比 十分位（看左尾）")

    print("\n########## Q2 因果：剥掉过去 60 日收益后的残差 ##########")
    res = residual_factor(df, "chg_yoy", "rev60")
    print(f"  residual rows={res.height:,}")
    q_groups(res, "__none__", 1, "占位") if False else None
    d = res.with_columns(
        (pl.col("resid").rank("average") / pl.col("resid").count() * 5).ceil().clip(1, 5).cast(pl.Int32).alias("g")
    )
    rows = []
    for dt, gg in d.group_by("date", maintain_order=True):
        mkt = gg["fwd20"].mean()
        a = gg.group_by("g").agg((pl.col("fwd20").mean() - mkt).alias("ex"))
        for r in a.to_dicts():
            rows.append({"date": dt, "g": r["g"], "ex": r["ex"]})
    st = pl.DataFrame(rows).group_by("g").agg(
        pl.col("ex").mean().alias("m"), pl.col("ex").std().alias("s"),
        pl.col("ex").count().alias("k"), (pl.col("ex") > 0).mean().alias("w")
    ).sort("g").with_columns((pl.col("m") / (pl.col("s") / pl.col("k").sqrt())).alias("t"))
    print("  --- 残差(户数同比 ⊥ 过去60日收益) 五分位 ---")
    for r in st.to_dicts():
        print(f"    G{r['g']} ex={r['m']*1e4:+8.1f}bp t={r['t']:+5.2f} win={r['w']:.2f}")
    p = pl.DataFrame(rows).pivot(on="g", index="date", values="ex")
    sp = p["5"] - p["1"]
    print(f"    G5-G1 = {float(sp.mean())*1e4:+.1f}bp  t="
          f"{float(sp.mean()/(sp.std()/len(sp)**0.5)):+.2f}")

    print("\n########## Q3 稳定性：分年 ##########")
    by_year(df, "chg_yoy")
    by_year(df, "chg_q")


if __name__ == "__main__":
    main()
