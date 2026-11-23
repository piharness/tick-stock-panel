"""检验「毛收益 ∝ 振幅」这个外推假设本身。

表2 用「当年振幅 / 3.39%」线性放大毛收益，这是整个 21 年外推的唯一桥梁。
桥梁若错，表2 就只是算术。这里用 5 分钟样本内部检验它：

  T1 横截面：同一天里，振幅大的票每笔毛收益是否更高（含票级固定效应外的比较）
  T2 时序：振幅大的日子，每笔毛收益是否更高
  T3 弹性：log(毛收益) ~ log(振幅) 的斜率 （毛收益为负的样本无法取 log，改用水平回归 + 隐含弹性）
  T4 分档单调性：按振幅十分位，看 gross 与 gross/ampl 谁更稳

判据 P-1：若 gross 随振幅升、但 gross/ampl 单调降 ⇒ 弹性 < 1 ⇒ 线性外推
            系统性高估历史年份，21 年结论只会更负（结论稳）。
判据 P-2：若 gross/ampl 基本平 ⇒ 线性外推成立。
判据 P-3：若 gross 与振幅无关 ⇒ 外推无效，不得使用表2。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_probe_t0"
COST_BP = 12.0


def main() -> None:
    t = pl.read_parquet(OUT / "trades.parquet")
    s = t.filter((pl.col("d") == 0.01) & pl.col("r1").is_finite()
                 & pl.col("ampl").is_finite() & (pl.col("ampl") > 0))
    s = s.with_columns(((pl.col("r1") + COST_BP / 1e4) * 1e4).alias("g"))
    print(f"[样本] {s.height} 笔 / {s['date'].n_unique()} 日，"
          f"ampl 中位 {s['ampl'].median()*100:.2f}%")

    # ---- T1/T4 横截面分档（日内去均值，剔除当日整体行情）----
    s = s.with_columns([
        (pl.col("g") - pl.col("g").mean().over("date")).alias("g_dm"),
        (pl.col("ampl") - pl.col("ampl").mean().over("date")).alias("a_dm"),
    ])
    s = s.with_columns(
        (pl.col("ampl").rank("ordinal").over("date")
         / pl.col("ampl").count().over("date") * 10).ceil().clip(1, 10).alias("dec"))

    print("\n=== T4 当日振幅十分位（横截面，gross = 毛收益 bp）===")
    print(f"{'十分位':<7}{'ampl均值':>9}{'gross':>9}{'gross/ampl':>12}{'净(扣12)':>10}{'n':>9}")
    g = (s.group_by("dec").agg([
        pl.col("ampl").mean().alias("a"),
        pl.col("g").mean().alias("gb"),
        pl.len().alias("n"),
    ]).sort("dec"))
    for r in g.iter_rows(named=True):
        print(f"Q{int(r['dec']):<6}{r['a']*100:>8.2f}%{r['gb']:>+9.1f}"
              f"{r['gb']/(r['a']*100):>12.3f}{r['gb']-12:>+10.1f}{r['n']:>9}")

    # ---- T1 横截面回归（日内去均值）----
    x = s["a_dm"].to_numpy()
    y = s["g_dm"].to_numpy()
    b = float(np.polyfit(x, y, 1)[0])
    print(f"\n[T1] 横截面：同日振幅高 1.00pct ⇒ 毛收益 {b/100:+.3f}bp "
          f"（斜率 {b:+.2f} bp/pct）")
    # 组内聚类稳健：按日聚合斜率再取中位
    dd = (s.group_by("date").agg([
        pl.col("a_dm").std().alias("sx"),
        pl.cov("a_dm", "g_dm").alias("cxy"),
    ]).filter(pl.col("sx") > 1e-6).with_columns((pl.col("cxy") / pl.col("sx")**2).alias("b_day")))
    bd = dd["b_day"].to_numpy()
    se = bd.std(ddof=1) / np.sqrt(len(bd))
    print(f"     按日分别回归再汇总：{np.median(bd):+.2f} bp/pct  "
          f"均值 {bd.mean():+.2f} ± {se:.2f} (t={bd.mean()/se:+.2f}, {len(bd)} 日)")

    # ---- T2 时序 ----
    day = s.group_by("date").agg([
        pl.col("g").mean().alias("g"),
        pl.col("ampl").mean().alias("a"),
        pl.len().alias("n"),
    ]).filter(pl.col("n") >= 50).sort("date")
    print(f"\n[T2] 日度时序：{day.height} 日，corr(当日振幅, 当日每笔毛收益) = "
          f"{np.corrcoef(day['a'], day['g'])[0,1]:+.3f}")
    b2 = float(np.polyfit(day["a"], day["g"], 1)[0])
    print(f"     斜率 {b2:+.2f} bp/pct")

    # ---- T3 隐含弹性（水平回归 → 在均值处换算）----
    ma, mg = float(s["ampl"].mean()), float(s["g"].mean())
    elas_cs = b * ma / mg
    elas_ts = b2 * float(day["a"].mean()) / float(day["g"].mean())
    print(f"\n[T3] 均值处隐含弹性 横截面 {elas_cs:+.2f} / 时序 {elas_ts:+.2f} "
          f"（=1 才是线性外推）")

    # ---- 直接用实测弹性重做历史外推 ----
    for name, e in (("beta=1.00（表2 原假设）", 1.0),
                    ("beta=横截面实测", elas_cs),
                    ("beta=时序实测", elas_ts)):
        if not np.isfinite(e):
            continue
        hd = pl.read_parquet(OUT / "hist_daily.parquet")
        yr = (hd.filter(pl.col("A").is_finite()).group_by(pl.col("date").str.slice(0, 4))
              .agg(pl.col("A").median().alias("A")).sort("date"))
        best = []
        for r in yr.iter_rows(named=True):
            k = (r["A"] / 0.0339) ** e
            y = int(r["date"])
            stamp = (20 if y <= 2007 and y < 2007 else 10 if y < 2023 else 5)
            if y == 2007:
                stamp = 60
            cost = stamp + 4
            best.append((y, 8.1 * k - cost, 6.6 * k - cost))
        arr = np.array([(b1, b2_) for _, b1, b2_ in best])
        print(f"\n[{name}] 22 年平均 core 净 {arr[:,0].mean():+.1f}bp / "
              f"silent 净 {arr[:,1].mean():+.1f}bp ；"
              f"为正的年份 core {int((arr[:,0]>0).sum())} 个，"
              f"最大 {arr[:,0].max():+.1f}bp")


if __name__ == "__main__":
    main()
