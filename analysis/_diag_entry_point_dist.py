"""买点的条件分布 —— 从「平均超额」升级到「胜率 / 尾部 / 赔率」。

上一轮证明了若干状态能提高**平均**超额，但「平均 +70bp」对买点决策几乎没有用：
它不告诉你赢的概率、输的时候有多惨。买点是一个**下注决策**，需要的是分布：

    P(fwd20 > 0)   胜率，必须与无条件基准（base rate）并列，否则毫无意义
    中位数          典型情形（均值会被右尾拉走）
    Q10            最坏 10% 情形 —— 买点最该看的一格
    P(fwd20 < -10%) 暴跌概率
    盈亏比          平均盈利 / 平均亏损

判据（四条同时满足才算「买点状态」）：
  1. 胜率比 base rate 高 >= 5pp；
  2. Q10 不比 base rate 差（不能"高胜率 + 尾部崩"）；
  3. 样本 >= 500 票-日；
  4. 分年 7/10 年以上同向（方向稳定性）。

全部状态特征都是观测日收盘后可知，无未来信息。fwd20 严格日历对齐（rn+20）。
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
    panel = panel.sort(["symbol", "rn"])
    panel = panel.with_columns([
        (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("rev60"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("rev20"),
        pl.col("amt20").log().alias("log_amt20"),
        pl.col("mcap").log().alias("log_mcap"),
        pl.col("close").rolling_max(250).over("symbol").alias("hi250"),
        pl.col("close").rolling_mean(60).over("symbol").alias("ma60v"),
        (pl.col("close") / pl.col("close").shift(1) - 1).rolling_std(20).over("symbol")
        .alias("vol20"),
    ]).with_columns([
        (pl.col("close") / pl.col("hi250") - 1).alias("dd250"),
        (pl.col("close") / pl.col("ma60v") - 1).alias("ma60dev"),
        (pl.col("vol20") * np.sqrt(252)).alias("annvol"),
    ])
    dates = sorted(panel["date"].unique().to_list())
    obs = [d for i, d in enumerate(dates) if i % SAMPLE_STEP == 0 and i < len(dates) - FWD - 1][-120:]
    panel = panel.filter(pl.col("date").is_in(obs))
    df = attach(panel, load_holder())
    return df.filter(
        pl.col("fwd20").is_finite() & (pl.col("fwd20").abs() < 0.6)
        & (pl.col("mcap") > 0) & pl.col("rev60").is_not_null()
        & pl.col("dd250").is_finite() & pl.col("ma60dev").is_finite()
        & pl.col("annvol").is_finite()
    )


def dist_stats(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {}
    pos = x > 0
    gain = x[pos].mean() if pos.any() else 0.0
    loss = -x[~pos].mean() if (~pos).any() else 0.0
    return {
        "n": int(x.size),
        "win": float(pos.mean()),
        "med": float(np.median(x)),
        "q10": float(np.quantile(x, 0.10)),
        "crash": float((x < -0.10).mean()),
        "pl": float(gain / loss) if loss > 1e-9 else float("nan"),
    }


def by_group(df: pl.DataFrame, col: str, n: int, label: str, base: dict,
             higher_is_better: bool = True) -> None:
    d = df.filter(pl.col(col).is_finite())
    if d.is_empty():
        print(f"\n--- {label}: 无有效样本")
        return
    d = d.with_columns(
        (pl.col(col).rank("average").over("date") / pl.col(col).count().over("date") * n)
        .ceil().clip(1, n).cast(pl.Int32).alias("g")
    )
    print(f"\n=== {label} ({col}, {n} 组) ===  base: 胜率 {base['win']:.1%} "
          f"中位 {base['med']*100:+.1f}%")
    print(f"  {'G':<4}{'n票日':>9}{'胜率':>8}{'Δ胜率':>8}{'中位':>8}{'Q10':>8}"
          f"{'暴跌率':>8}{'盈亏比':>8}")
    for g in range(1, n + 1):
        sub = d.filter(pl.col("g") == g)
        s = dist_stats(sub["fwd20"].to_numpy())
        if not s:
            continue
        print(f"  {g:<4}{s['n']:>9}{s['win']:>7.1%}{(s['win']-base['win'])*100:>+7.1f}pp"
              f"{s['med']*100:>+7.1f}%{s['q10']*100:>+7.1f}%{s['crash']:>7.1%}{s['pl']:>8.2f}")


def main() -> None:
    df = prep()
    print(f"sample {df.height:,} 票日  {df['date'].n_unique()} 期  "
          f"约 {int(df.height/df['date'].n_unique())} 只/期")
    base = dist_stats(df["fwd20"].to_numpy())
    print(f"\n########## 无条件基准（base rate） ##########")
    print(f"  n={base['n']:,}  胜率={base['win']:.2%}  中位={base['med']*100:+.2f}%  "
          f"Q10={base['q10']*100:+.1f}%  暴跌率(<−10%)={base['crash']:.2%}  "
          f"盈亏比={base['pl']:.2f}")

    by_group(df, "to20", 5, "20日换手率（低→高）", base)
    by_group(df, "chg_yoy", 5, "户数同比（降→增）", base)
    by_group(df, "dd250", 5, "距250日高点回撤（深→浅）", base)
    by_group(df, "ma60dev", 5, "距MA60偏离（低→高）", base)
    by_group(df, "rev20", 5, "过去20日收益（跌→涨）", base)
    by_group(df, "annvol", 5, "年化波动率（低→高）", base)


if __name__ == "__main__":
    main()
