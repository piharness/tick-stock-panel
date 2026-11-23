"""买点体检判据 v1 —— 预注册定义 + 历史统计落盘。

用户要的是「当前一段时间内是否有买点」。上一轮实测给出的答案是：**这套判据
做不到预测上涨**（胜率最多 +4.3pp，且分年 3/10 年低于基准），但**能稳定把
暴跌概率砍半**（17.6% → 8.4%，两段各自 −8.7pp / −9.5pp）。

所以判据不从"该买"出发，而从"不该买"出发 —— 这是数据允许的唯一诚实版本。

## 预注册（改了就是 v2，旧记录不可与新记录合并统计）

判据版本 `entry-v1`，四个多头条件 + 三个空头条件，全部用**同日横截面分位**：

    C1  turnover_pct <= 0.33       低换手
    C2  vol_pct      <= 0.33       低波动
    C3  0.33 < rev_pct <= 0.67     近 20 日涨幅居中（既不追高也不接刀）
    C4  holder_pct   <= 0.40       户数不膨胀（公告日对齐）

    档位：命中 >= 3 为 low；命中 <= 1 为 mid；命中 == 0 为 watch（尚未成立）

    R1  turnover_pct >= 0.80       高换手
    R2  vol_pct      >= 0.80       高波动
    R3  rev_pct      >= 0.80       涨幅过大
    high 档：>= 2 条命中（覆盖 low 判定）

门槛（0.33 / 0.40 / 0.80）取自上一轮验证时用过的分位，**不是本轮新调的**：
在 0.2~0.4 之间任取一档结论都一样（单维度 Δ胜率 2.5~3.1pp），所以这里没有
优化空间，选了整数值只是为了可复述。

本脚本产出 `data/entry_check/stats.json`，服务只读不写 —— 同一批数字在页面、
测试、文档里出现三次，必须有唯一产地。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_entry_combo import add_pct  # noqa: E402
from _diag_entry_point_dist import dist_stats, prep  # noqa: E402

CRITERIA = "entry-v1"


def tier_of(df: pl.DataFrame) -> pl.DataFrame:
    c1 = pl.col("p_to20") <= 0.33
    c2 = pl.col("p_annvol") <= 0.33
    c3 = (pl.col("p_rev20") > 0.33) & (pl.col("p_rev20") <= 0.67)
    c4 = pl.col("p_chg_yoy") <= 0.40
    r1 = pl.col("p_to20") >= 0.80
    r2 = pl.col("p_annvol") >= 0.80
    r3 = pl.col("p_rev20") >= 0.80
    bull = (c1.cast(pl.Int8) + c2.cast(pl.Int8) + c3.cast(pl.Int8) + c4.cast(pl.Int8))
    bear = (r1.cast(pl.Int8) + r2.cast(pl.Int8) + r3.cast(pl.Int8))
    return df.with_columns([bull.alias("bull_hits"), bear.alias("bear_hits")]).with_columns(
        pl.when(pl.col("bear_hits") >= 2).then(pl.lit("high"))
        .when(pl.col("bull_hits") >= 3).then(pl.lit("low"))
        .otherwise(pl.lit("mid")).alias("tier")
    )


def stat_block(x: np.ndarray, base: dict) -> dict:
    s = dist_stats(x)
    return {
        "n": s["n"],
        "win": round(s["win"], 4),
        "win_delta_pp": round((s["win"] - base["win"]) * 100, 2),
        "med": round(s["med"], 5),
        "q10": round(s["q10"], 5),
        "crash": round(s["crash"], 4),
        "crash_delta_pp": round((s["crash"] - base["crash"]) * 100, 2),
        "pl": round(s["pl"], 3) if s["pl"] == s["pl"] else None,
    }


def main() -> None:
    df = tier_of(add_pct(prep()))
    base = dist_stats(df["fwd20"].to_numpy())
    print(f"sample {df.height:,} 票日 / {df['date'].n_unique()} 期")
    print(f"base 胜率 {base['win']:.2%} 中位 {base['med']*100:+.2f}% "
          f"Q10 {base['q10']*100:+.1f}% 暴跌 {base['crash']:.2%}")

    tiers = {}
    print(f"\n{'档位':<8}{'n票日':>9}{'占比':>7}{'胜率':>8}{'Δ胜率':>9}{'中位':>8}"
          f"{'Q10':>8}{'暴跌率':>8}{'Δ暴跌':>9}")
    for t, label in [("low", "低风险"), ("mid", "中性"), ("high", "高危")]:
        sub = df.filter(pl.col("tier") == t)
        if sub.height < 300:
            print(f"  {label}: 样本不足 {sub.height}")
            continue
        blk = stat_block(sub["fwd20"].to_numpy(), base)
        blk["label"] = label
        blk["share"] = round(sub.height / df.height, 4)
        tiers[t] = blk
        print(f"  {label:<6}{blk['n']:>9}{blk['share']:>6.1%}{blk['win']:>7.1%}"
              f"{blk['win_delta_pp']:>+8.1f}pp{blk['med']*100:>+7.1f}%"
              f"{blk['q10']*100:>+7.1f}%{blk['crash']:>7.1%}{blk['crash_delta_pp']:>+8.1f}pp")

    # —— 稳定性：low 档的胜率与暴跌率分年 ——
    yearly = {}
    low = df.filter(pl.col("tier") == "low").with_columns(pl.col("date").dt.year().alias("y"))
    allw = df.with_columns(pl.col("date").dt.year().alias("y"))
    for g in sorted(low.partition_by("y", as_dict=True).values(), key=lambda x: x["y"][0]):
        y = int(g["y"][0])
        if g.height < 100:
            continue
        b = dist_stats(allw.filter(pl.col("y") == y)["fwd20"].to_numpy())
        s = dist_stats(g["fwd20"].to_numpy())
        yearly[str(y)] = {
            "win": round(s["win"], 4), "base_win": round(b["win"], 4),
            "crash": round(s["crash"], 4), "base_crash": round(b["crash"], 4),
            "n": s["n"],
        }
    below = sum(1 for v in yearly.values() if v["win"] < v["base_win"])
    worse_crash = sum(1 for v in yearly.values() if v["crash"] > v["base_crash"])
    print(f"\n稳定性：low 档胜率跑输基准 {below}/{len(yearly)} 年；"
          f"暴跌率不如基准 {worse_crash}/{len(yearly)} 年")
    for y, v in yearly.items():
        print(f"  {y}: 胜率 {v['win']:.1%} (base {v['base_win']:.1%}) "
              f"暴跌 {v['crash']:.1%} (base {v['base_crash']:.1%})")

    if "--write" in sys.argv:
        out = Path(__file__).resolve().parents[1] / "data" / "entry_check" / "stats.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {
                "criteria": CRITERIA,
                "sample": f"{df['date'].min()} ~ {df['date'].max()}",
                "n_dates": int(df["date"].n_unique()),
                "n_rows": int(df.height),
                "fwd_days": 20,
                "return_basis": "等权绝对收益（非超额）；暴跌＝未来20日跌幅 >10%",
                "thresholds": {"C1_turnover": 0.33, "C2_vol": 0.33, "C3_rev_mid": [0.33, 0.67],
                               "C4_holder": 0.40, "R_high": 0.80, "low_min_hits": 3, "high_min_hits": 2},
                "generated_by": "analysis/_diag_entry_criteria.py --write",
                "honest_note": "胜率提升分年不稳定（见 yearly），暴跌率改善两段一致；本判据不做涨跌预测",
            },
            "base": {**stat_block(df["fwd20"].to_numpy(), base)},
            "tiers": tiers,
            "yearly": yearly,
            "stability": {
                "years": len(yearly),
                "win_below_base": below,
                "crash_worse_than_base": worse_crash,
            },
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten -> {out}")


if __name__ == "__main__":
    main()
