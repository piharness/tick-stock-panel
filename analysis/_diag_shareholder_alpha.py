"""股东结构因子的增量检验 —— 它是不是又一个量价影子？

用户主张：「散户多的个股不需要太多注意力，抛压低的个股超额收益可能性大」。
上一轮已判决：换手率/成交额/振幅这一族代理彼此高度共线，且与规模交互。
本轮唯一要回答的问题：**股东户数（真实持仓集中度）在控制量价之后还剩多少。**

三条纪律（否则结论作废）：
  1. 可得性对齐用**公告日**（notice_date / period_end+45d 保守兜底），不用报告期。
     用报告期做观测日 = 未来函数，季报公布滞后 1~3 个月。
  2. 控制变量必须包含**过去收益**（rev60）。户数变化与股价互为因果：
     跌了散户抄底或割肉，户数就变；不控过去收益，测出来的"户数效应"
     可能只是反转效应换了个名字。
  3. 判决用 double sort，不用回归系数（抑制变量假象，见 MEMORY 母题 5 附注）。
"""
from __future__ import annotations

import sys

import numpy as np
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_pressure import DATA, FWD, SAMPLE_STEP, build_panel  # noqa: E402
from _diag_chip_pressure_doublesort import _qcut, double_sort  # noqa: E402

NG = 3


def load_holder() -> pl.DataFrame:
    df = pl.read_parquet(DATA / "shareholder" / "holder_num" / "part.parquet")
    df = df.with_columns(
        [
            pl.col("period_end").str.to_date("%Y-%m-%d"),
            # 保守可得日：公告日缺失时给报告期 + 45 天（季报法定 1 个月内、年报 4 个月内，
            # 45 天对季报是保守上界；对年报偏乐观，但只影响 3 月前后的少数观测）
            pl.when(pl.col("notice_date").is_not_null() & (pl.col("notice_date") != ""))
            .then(pl.col("notice_date").str.to_date("%Y-%m-%d", strict=False))
            .otherwise(pl.col("period_end").str.to_date("%Y-%m-%d", strict=False) + pl.duration(days=45))
            .alias("avail_date"),
        ]
    )
    # 编号：code → symbol（补后缀）
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet").select(
        ["symbol", "code", "exchange"]
    )
    df = df.join(inst, left_on="code", right_on="code", how="inner")
    df = df.sort(["symbol", "period_end"])
    # 环比变化率（东财 HOLDER_NUM_RATIO 是百分数；自己算一遍以便校验）
    df = df.with_columns(
        [
            (pl.col("HOLDER_NUM") / pl.col("HOLDER_NUM").shift(1).over("symbol") - 1).alias("chg_q"),
            (pl.col("HOLDER_NUM") / pl.col("HOLDER_NUM").shift(4).over("symbol") - 1).alias("chg_yoy"),
            pl.col("HOLDER_NUM").log().alias("log_holders"),
            pl.col("AVG_MARKET_CAP").log().alias("log_avg_mcap"),
        ]
    )
    return df


def attach(panel: pl.DataFrame, holder: pl.DataFrame) -> pl.DataFrame:
    """按 asof（可得日 <= 观测日）把最近一期股东数据贴到面板上。"""
    obs = panel.select(["symbol", "date"]).unique().sort(["symbol", "date"])
    h = holder.select(
        ["symbol", "avail_date", "period_end", "chg_q", "chg_yoy", "log_holders",
         "log_avg_mcap", "HOLDER_NUM", "AVG_MARKET_CAP"]
    ).sort(["symbol", "avail_date"])
    joined = obs.join_asof(
        h, left_on="date", right_on="avail_date", by="symbol", strategy="backward"
    )
    # 数据太旧的不要（>200 天 = 一年没披露，可能是退市/停牌）
    joined = joined.with_columns(
        (pl.col("date") - pl.col("period_end")).dt.total_days().alias("staleness_d")
    ).filter(pl.col("staleness_d") <= 200)
    return panel.join(joined, on=["symbol", "date"], how="left")


def main() -> None:
    panel = build_panel()
    panel = panel.with_columns(
        [
            (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("rev60"),
            pl.col("amt20").log().alias("log_amt20"),
            pl.col("mcap").log().alias("log_mcap"),
        ]
    )
    holder = load_holder()
    print(f"holder rows={holder.height} avail range="
          f"{holder['avail_date'].min()}..{holder['avail_date'].max()}")

    dates = sorted(panel["date"].unique().to_list())
    obs = [d for i, d in enumerate(dates) if i % SAMPLE_STEP == 0 and i < len(dates) - FWD - 1][-120:]
    panel = panel.filter(pl.col("date").is_in(obs))

    df = attach(panel, holder)
    cov = df.filter(pl.col("chg_q").is_not_null()).height / max(df.height, 1)
    print(f"panel rows={df.height:,} holder coverage={cov:.1%}")

    base = df.filter(
        pl.col("fwd20").is_finite()
        & (pl.col("fwd20").abs() < 0.6)
        & pl.col("mcap").is_not_null()
        & (pl.col("mcap") > 0)
        & pl.col("rev60").is_not_null()
    )

    print("\n########## 1. 相关性：股东因子 vs 量价因子 ##########")
    sub = base.filter(pl.col("chg_q").is_not_null() & pl.col("chg_q").is_finite())
    for a in ["chg_q", "chg_yoy", "log_holders", "log_avg_mcap"]:
        row = []
        for b in ["to20", "log_amt20", "log_mcap", "rev60"]:
            g = sub.drop_nulls([a, b])
            # 秩相关（venv 无 scipy，用秩的皮尔逊手算）
            ra = g[a].rank("ordinal").to_numpy().astype(float)
            rb = g[b].rank("ordinal").to_numpy().astype(float)
            ra = (ra - ra.mean()) / (ra.std() + 1e-12)
            rb = (rb - rb.mean()) / (rb.std() + 1e-12)
            row.append(f"{b}={float(np.mean(ra*rb)):+.2f}")
        print(f"  {a:<12} " + "  ".join(row))

    print("\n########## 2. 单因子五分位（未来20日超额） ##########")
    factors = {
        "chg_q": "户数环比(筹码集中度变化)",
        "chg_yoy": "户数同比",
        "log_holders": "股东户数(散户绝对量)",
        "log_avg_mcap": "户均持股市值(散户度倒数)",
    }
    for f in factors:
        g = base.filter(pl.col(f).is_not_null() & pl.col(f).is_finite())
        d = g.with_columns(
            (pl.col(f).rank("average") / pl.col(f).count() * 5).ceil().clip(1, 5).cast(pl.Int32).alias("grp")
        )
        rows = []
        for dt, gg in d.group_by("date", maintain_order=True):
            mkt = gg["fwd20"].mean()
            a = gg.group_by("grp").agg((pl.col("fwd20").mean() - mkt).alias("ex"),
                                        pl.col("symbol").count().alias("n"))
            for r in a.to_dicts():
                rows.append({"date": dt, **r})
        res = pl.DataFrame(rows)
        stat = res.group_by("grp").agg(
            pl.col("ex").mean().alias("m"), pl.col("ex").std().alias("s"),
            pl.col("ex").count().alias("n"), (pl.col("ex") > 0).mean().alias("w")
        ).sort("grp").with_columns((pl.col("m") / (pl.col("s") / pl.col("n").sqrt())).alias("t"))
        print(f"\n  --- {f} ({factors[f]}) ---  n_dates={res['date'].n_unique()} "
              f"avg_stocks={int(res['n'].mean())}")
        for r in stat.to_dicts():
            print(f"      G{r['grp']}  {r['m']*1e4:+8.1f}bp  t={r['t']:+5.2f}  win={r['w']:.2f}")
        p = res.pivot(on="grp", index="date", values="ex")
        sp = p["5"] - p["1"]
        print(f"      G5-G1 = {float(sp.mean())*1e4:+.1f}bp  t="
              f"{float(sp.mean()/(sp.std()/len(sp)**0.5)):+.2f}")

    print("\n########## 3. 控制量价后还剩多少（double sort） ##########")
    sub3 = base.filter(pl.col("chg_q").is_not_null() & pl.col("chg_q").is_finite()
                       & pl.col("log_avg_mcap").is_finite())
    for target in ["chg_q", "log_avg_mcap", "log_holders"]:
        for ctrl in ["log_amt20", "rev60"]:
            double_sort(sub3, target, ctrl, want_low=True)


if __name__ == "__main__":
    main()
