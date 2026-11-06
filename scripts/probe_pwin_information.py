"""p_win 到底有没有**信息**？—— 把「基线错了」和「预测有用」分开。

## 为什么需要这个脚本

`walk_forward` 的 skill 定义为 `1 - brier / brier_const`，而 `median` 口径的
`brier_const` 恒为 0.25（常数 0.5 的 Brier）。问题是：

    一个命中率 20% 的策略，其「常数 p = 0.2」的 Brier 是 0.16，
    skill = 1 - 0.16/0.25 = +36%。

这个 +36% 里**没有一比特关于未来的信息**——它只说明"这个策略的胜率不是 50%"。
换句话说，`brier_const = 0.5` 检验的是「你的胜率离 50% 有多远」，
不是「你的历史胜率能不能预测未来」。

`abs` 口径反过来：基线取同样本命中率（**用了未来**），任何决策时可知的常数
都必然打不赢它，skill ≤ 0 是恒等式，不是发现。

两个口径都答不了「p_win 有没有信息」。真正能答这个问题的是**两个都诚实的
预测器之间的比较**：

    A  近端胜率（trailing W 天，收缩）      —— 当前的 p_win
    B  远端胜率（expanding 全历史，收缩）   —— 「最近这段没有额外信息」的对照
    C  常数 0.5                              —— 现行 median 基线
    D  全样本命中率                          —— 神谕常数（下界，不可实现）

A 打赢 C 是必然的（C 是错的）。**只有 A 打赢 B 才说明近端胜率含有信息。**

## 口径

决策日与 `probe_basis_axis.py` 相同；可见性用 `exit.date < t`（与 pwin.py 一致）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import polars as pl

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "backend"))

from probe_basis_axis import HORIZON, build  # noqa: E402

from app.decision import bars  # noqa: E402

WINDOW = 60
KAPPA = 20.0
PRIOR = 0.5
Z90 = 1.645


def _shrink(hits: float, n: float) -> float:
    return (hits + KAPPA * PRIOR) / (n + KAPPA)


def _lcb(daily: list[float]) -> float:
    m = sum(daily) / len(daily)
    v = sum((x - m) ** 2 for x in daily) / (len(daily) - 1)
    return m - Z90 * math.sqrt(v / len(daily))


def run(df: pl.DataFrame, field: str, cal: list[str]) -> dict:
    # 每个决策日的出场日：as_of 在 cal 中的位置 + 1 + HORIZON
    pos = {d: i for i, d in enumerate(cal)}
    days = sorted(df["as_of"].unique().to_list())
    per_day = {k[0]: g[field].to_list() for k, g in df.group_by("as_of")}
    exit_of = {d: cal[pos[d] + 1 + HORIZON] for d in days if pos[d] + 1 + HORIZON < len(cal)}

    # 神谕常数：全样本命中率
    all_y = df[field].to_list()
    oracle = sum(all_y) / len(all_y)

    acc: dict[str, list[float]] = {k: [] for k in ("A", "B", "C", "D")}
    pA_hist: list[float] = []
    for d in days:
        if d not in exit_of:
            continue
        today = d
        vis = [x for x in days if x < d and exit_of.get(x, "9999") < today]
        if len(vis) < 2:
            continue
        ys = [y for x in vis for y in per_day[x]]
        if not ys:
            continue
        tail = [y for x in vis[-WINDOW:] for y in per_day[x]]
        pA, pB = _shrink(sum(tail), len(tail)), _shrink(sum(ys), len(ys))
        outs = per_day[d]
        # 当天全部预测器在**同一批**样本上打分，避免样本不同导致的不可比
        for key, p in (("A", pA), ("B", pB), ("C", PRIOR), ("D", oracle)):
            acc[key].append(sum((p - y) ** 2 for y in outs) / len(outs))
        pA_hist.append(round(pA, 4))

    if len(acc["A"]) < 3:
        return {"oos_days": len(acc["A"]), "note": "样本不足"}
    out = {"oos_days": len(acc["A"]), "oracle_const": round(oracle, 4),
           "pA_range": [min(pA_hist), max(pA_hist)],
           "pA_mean": round(sum(pA_hist) / len(pA_hist), 4)}
    for k in ("A", "B", "C", "D"):
        out[f"brier_{k}"] = round(sum(acc[k]) / len(acc[k]), 4)
    for a, b in (("A", "C"), ("A", "B"), ("A", "D")):
        diff = [y - x for x, y in zip(acc[a], acc[b])]
        out[f"{a}_minus_{b}"] = round(sum(diff) / len(diff), 5)
        out[f"{a}_minus_{b}_lcb"] = round(_lcb(diff), 5)
    return out


def main() -> None:
    cal = bars.sessions(Path(__file__).resolve().parents[1] / "data")
    picks = [(cal[i], cal[i + 1], cal[i + 1 + HORIZON])
             for i in range(1, len(cal) - HORIZON - 1)]
    df = build(picks)
    agree = float((df["outcome"] == df["outcome_med"]).mean())
    med_level = df.group_by("as_of").agg(pl.col("median").first())["median"].mean()
    print(f"观测 {df.height}，决策日 {df['as_of'].n_unique()}")
    print(f"abs 与 median 两个因变量的一致率：{agree:.2%}")
    print(f"20 日全市场收益中位数的均值：{med_level:+.2%}")
    print(f"（中位数水平≈0 时，ret>0 与 ret>中位数 是同一个事件）\n")

    for field, name in (("outcome", "abs"), ("outcome_med", "median")):
        r = run(df, field, cal)
        print(f"[{name}]")
        for k, v in r.items():
            print(f"  {k:<18} {v}")
        print()

    # 分区间：命中率水平是否在漂移（A 与 B 的差只能来自这里）
    print("日胜率按季度：")
    q = (df.with_columns(pl.col("as_of").str.slice(0, 7).alias("m"))
           .group_by("m").agg(pl.col("outcome").mean().alias("abs"),
                              pl.col("outcome_med").mean().alias("med"),
                              pl.len().alias("n")).sort("m"))
    for r in q.iter_rows(named=True):
        print(f"  {r['m']}  abs={r['abs']:.3f}  med={r['med']:.3f}  n={r['n']}")


if __name__ == "__main__":
    main()
