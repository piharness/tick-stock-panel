"""口径的「区分度在哪根轴上」—— 决定 p_win 该按日变还是按标的变。

## 要回答的问题

文档 9.7 留了一个未决项：`abs` 口径要不要给 p_win 加**标的级**区分度。
这个问法默认了区分度在标的轴上。但 `abs` 的因变量是「这 20 天涨没涨」，
而同一天所有股票同涨同跌——如果组内相关很高，那么标的轴的区分度天花板很低，
真正的方差在**日**轴上（制度），不在标的轴上。

本脚本用真实行情做一次方差分解，直接给出：

    ICC = (MSB - MSW) / (MSB + (k-1)·MSW)

- ICC 高 ⇒ 同一天的结果高度同质 ⇒ 按标的给不同 p 几乎无用，必须按日给；
- ICC 低 ⇒ 同一天内部差异大 ⇒ 标的轴才有区分度。

同时给出「日级神谕」的 Brier 上限：假设每天都知道当天的真实胜率，
Brier 能降到多少。这个上限减去常数基线，就是**日轴上还剩多少可赚的方差**。

## 口径

与 `verify_pwin_e2e.py` 一致：决策日取换手率前 10，次日收盘入场，持有 HORIZON 日。
收益用复权收盘（`kline_daily_enriched.close`），不涉及撮合，只做方差分解。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.decision import bars  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "data"
TABLE = bars.STOCK_ENRICHED
HORIZON = 20
TOP_N = 10


def _day(day: str) -> pl.DataFrame:
    part = SRC / TABLE / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files],
                           missing_columns="insert").select(
        ["symbol", "close", "turnover_rate"]).collect()


def build(decision_days: list[str]) -> pl.DataFrame:
    need = sorted({d for i in decision_days for d in (i[0], i[1], i[2])})
    frames = {d: _day(d) for d in need}
    frames = {d: f for d, f in frames.items() if not f.is_empty()}
    rows: list[dict] = []
    for as_of, entry, exit_ in decision_days:
        if not all(d in frames for d in (as_of, entry, exit_)):
            continue
        base = (
            frames[as_of]
            .select(["symbol", "close", "turnover_rate"])
            .join(frames[entry].select(["symbol", pl.col("close").alias("ce")]), on="symbol")
            .join(frames[exit_].select(["symbol", pl.col("close").alias("cx")]), on="symbol")
            .filter((pl.col("close") > 0) & (pl.col("ce") > 0) & (pl.col("cx") > 0))
        )
        if base.height < 200:
            continue
        base = base.with_columns(((pl.col("cx") / pl.col("ce")) - 1.0).alias("ret"))
        med = float(base["ret"].median())
        top = base.drop_nulls("turnover_rate").sort(
            "turnover_rate", descending=True).head(TOP_N)
        if top.height < TOP_N:
            continue
        for r in top.iter_rows(named=True):
            rows.append({"as_of": as_of, "symbol": r["symbol"], "ret": float(r["ret"]),
                         "median": med,
                         "outcome": int(r["ret"] > 0),
                         "outcome_med": int(r["ret"] > med)})
    return pl.DataFrame(rows)


def decompose(df: pl.DataFrame, field: str) -> dict:
    k = TOP_N
    groups = df.group_by("as_of").agg(
        pl.col(field).mean().alias("h"),
        pl.len().alias("n"),
    ).sort("as_of")
    h = df[field].mean()
    n_days = groups.height
    # 组间 / 组内均方（一元随机效应）
    msb = float((((groups["h"] - h) ** 2) * groups["n"]).sum() / (n_days - 1))
    within = df.join(groups.select("as_of", "h"), on="as_of").with_columns(
        ((pl.col(field) - pl.col("h")) ** 2).alias("sq"))
    msw = float(within["sq"].sum() / (df.height - n_days))
    icc = (msb - msw) / (msb + (k - 1) * msw) if (msb + (k - 1) * msw) else 0.0
    # 二项噪声下「日胜率」本应有多少方差
    exp_var = float((groups["h"] * (1 - groups["h"]) / groups["n"]).mean())
    obs_var = float(groups["h"].var())
    # 日级神谕：每天都知道当天真实胜率
    brier_const = h * (1 - h)
    brier_oracle = float((groups["h"] * (1 - groups["h"])).mean())
    return {
        "n": df.height, "days": n_days, "hit": round(float(h), 4),
        "day_hit_min": round(float(groups["h"].min()), 3),
        "day_hit_p10": round(float(groups["h"].quantile(0.1)), 3),
        "day_hit_med": round(float(groups["h"].median()), 3),
        "day_hit_p90": round(float(groups["h"].quantile(0.9)), 3),
        "day_hit_max": round(float(groups["h"].max()), 3),
        "obs_var_of_day_hit": round(obs_var, 5),
        "binom_var_expected": round(exp_var, 5),
        "icc": round(icc, 4),
        "brier_const": round(brier_const, 4),
        "brier_day_oracle": round(brier_oracle, 4),
        "day_axis_headroom": round(brier_const - brier_oracle, 4),
    }


def main() -> None:
    cal = bars.sessions(SRC)
    print(f"交易日 {len(cal)}（{cal[0]} .. {cal[-1]}）")
    picks = [(cal[i], cal[i + 1], cal[i + 1 + HORIZON])
             for i in range(1, len(cal) - HORIZON - 1)]
    print(f"可用决策日 {len(picks)}（重叠 {HORIZON} 日窗口）")
    df = build(picks)
    print(f"观测 {df.height}，决策日 {df['as_of'].n_unique()}\n")

    for field, name in (("outcome", "abs   (ret > 0)"),
                        ("outcome_med", "median(ret > 同日中位数)")):
        d = decompose(df, field)
        print(f"[{name}]")
        for k, v in d.items():
            print(f"  {k:<22} {v}")
        print()

    # 尾部：e2e 用的那 40 个决策日（连续），确认结论不是全样本摊平出来的
    tail = df.filter(pl.col("as_of") >= df["as_of"].unique().sort()[-40])
    print(f"--- 最近 40 个决策日切片（{tail.height} 条）---")
    for field, name in (("outcome", "abs"), ("outcome_med", "median")):
        d = decompose(tail, field)
        print(f"[{name}] hit={d['hit']} day_hit={d['day_hit_min']}..{d['day_hit_max']} "
              f"icc={d['icc']} headroom={d['day_axis_headroom']}")


if __name__ == "__main__":
    main()
