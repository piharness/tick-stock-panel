"""买点状态的组合与稳定性 —— 以及它到底能兑现什么。

单维度结论（已测）：胜率最多 +3.1pp（低波动），但**暴跌率能从 17.6% 砍到 9.1%**。
所以本轮的真正问题不是「能不能挑出会涨的票」，而是：

    组合之后，胜率能推到多少？尾部会不会反而变差？样本还剩多少？分年稳不稳？

四条纪律：
  1. 组合条件全部取「事先可解释的分位」（低 1/3 / 中 1/3），不网格搜索阈值；
  2. 每次组合都同时报**留存率**与**均票数** —— 只剩 3% 的票再好也没法用；
  3. 必须报独立票数（同一票在多期重复出现，票日数高估有效样本）；
  4. 分年 + 分两段（2016-2021 / 2022-2026）双向验证，任一段符号翻转即判不稳。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_entry_point_dist import dist_stats, prep  # noqa: E402


def add_pct(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        (pl.col(c).rank("average").over("date") / pl.col(c).count().over("date")).alias(f"p_{c}")
        for c in ("to20", "annvol", "rev20", "chg_yoy", "dd250", "ma60dev")
        if c in df.columns
    ])


def ev(df: pl.DataFrame, name: str, cond: pl.Expr, base: dict) -> dict | None:
    hit = df.filter(cond)
    if hit.height < 300:
        print(f"  {name:<34} 样本不足 ({hit.height})")
        return None
    s = dist_stats(hit["fwd20"].to_numpy())
    keep = hit.height / df.height
    syms = hit["symbol"].n_unique()
    print(f"  {name:<34}{s['n']:>9}{s['win']:>7.1%}{(s['win']-base['win'])*100:>+7.1f}pp"
          f"{s['q10']*100:>+8.1f}%{s['crash']:>8.1%}{keep:>7.1%}{syms:>7d}")
    return {"name": name, **s, "keep": keep, "syms": syms}


def main() -> None:
    df = add_pct(prep())
    base = dist_stats(df["fwd20"].to_numpy())
    print(f"sample {df.height:,} 票日 / {df['date'].n_unique()} 期 / "
          f"{df['symbol'].n_unique()} 只")
    print(f"base: 胜率 {base['win']:.2%}  中位 {base['med']*100:+.2f}%  "
          f"Q10 {base['q10']*100:+.1f}%  暴跌 {base['crash']:.2%}")

    print(f"\n{'组合':<34}{'n票日':>9}{'胜率':>7}{'Δ胜率':>9}{'Q10':>8}{'暴跌率':>8}"
          f"{'留存':>7}{'票数':>7}")

    low_to = pl.col("p_to20") <= 1 / 3
    low_vol = pl.col("p_annvol") <= 1 / 3
    mid_rev = (pl.col("p_rev20") > 1 / 3) & (pl.col("p_rev20") <= 2 / 3)
    low_hy = pl.col("p_chg_yoy") <= 0.4
    near_ma = (pl.col("p_ma60dev") > 0.2) & (pl.col("p_ma60dev") <= 0.6)

    cases = [
        ("① 低换手 (≤33%)", low_to),
        ("② 低波动 (≤33%)", low_vol),
        ("③ 短期涨幅居中 (33~67%)", mid_rev),
        ("④ 户数不膨胀 (≤40%)", low_hy),
        ("①∩②", low_to & low_vol),
        ("①∩②∩④", low_to & low_vol & low_hy),
        ("①∩②∩③", low_to & low_vol & mid_rev),
        ("①∩②∩③∩④", low_to & low_vol & mid_rev & low_hy),
        ("①∩②∩③∩④∩MA60居中", low_to & low_vol & mid_rev & low_hy & near_ma),
        ("反例：高换手∩高波动∩涨最多", (pl.col("p_to20") >= 0.8) & (pl.col("p_annvol") >= 0.8)
                                          & (pl.col("p_rev20") >= 0.8)),
    ]
    for name, cond in cases:
        ev(df, name, cond, base)

    print("\n########## 分年稳定性：①∩②∩③∩④ ##########")
    d = df.filter(low_to & low_vol & mid_rev & low_hy).with_columns(
        pl.col("date").dt.year().alias("y")
    )
    print(f"  {'年':<6}{'n票日':>8}{'胜率':>8}{'中位':>8}{'Q10':>8}{'暴跌率':>8}")
    for g in sorted(d.partition_by("y", as_dict=True).values(), key=lambda x: x["y"][0]):
        y = g["y"][0]
        s = dist_stats(g["fwd20"].to_numpy())
        if s["n"] < 100:
            continue
        print(f"  {y:<6}{s['n']:>8}{s['win']:>7.1%}{s['med']*100:>+7.1f}%"
              f"{s['q10']*100:>+7.1f}%{s['crash']:>7.1%}")

    print("\n########## 两段对照（防单段拟合） ##########")
    for lo, hi, tag in [("2016-01-01", "2021-12-31", "2016-2021"),
                        ("2022-01-01", "2026-12-31", "2022-2026")]:
        seg = df.filter((pl.col("date") >= pl.lit(lo).str.to_date())
                        & (pl.col("date") <= pl.lit(hi).str.to_date()))
        b = dist_stats(seg["fwd20"].to_numpy())
        h = seg.filter(low_to & low_vol & mid_rev & low_hy)
        s = dist_stats(h["fwd20"].to_numpy())
        if not s:
            continue
        print(f"  {tag}: base 胜率 {b['win']:.1%} 暴跌 {b['crash']:.1%} | "
              f"组合 胜率 {s['win']:.1%} ({(s['win']-b['win'])*100:+.1f}pp) "
              f"暴跌 {s['crash']:.1%} ({s['crash']-b['crash']:+.1%}) n={s['n']}")


if __name__ == "__main__":
    main()
