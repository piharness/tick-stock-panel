"""抛压代理 × 未来超额收益 —— 横截面分组回测（第一轮：只用本地量价）。

目的：用户主张「散户多 → 抛压大 → 不值得看；抛压低 → 超额收益高」。
      在把任何指标搬上页面之前，先判定哪个口径真的能预测未来 20 日超额收益。

口径约定（报告必带）：
  - 收益：市场等权日收益，fwd20 = close[rn+20]/close[rn]-1，rn 是**全局交易日索引**
    （严格日历对齐；停牌导致的缺格记 NaN，不用下一行顶替）。
  - 超额：当日全样本等权 fwd20 减去该组等权 fwd20 的相反数（组 - 市场）。
  - 因子：观测日收盘后可知的量价统计，无未来信息。
  - 抽样：每 20 个交易日一个观测日（不重叠窗口），时间上做 Newey-West 不调，
    直接对观测日序列求 t（20 日重叠已通过不重叠采样消除）。

输出：每组每日超额序列 → 均值(bp) / t / 胜率 / 单调性。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
START = "2016-01-01"
FWD = 20
SAMPLE_STEP = 20
NQ = 5


def load_daily() -> pl.DataFrame:
    lf = pl.scan_parquet(
        str(DATA / "kline_daily_enriched" / "date=*" / "*.parquet"),
        extra_columns="ignore",
    )
    df = lf.select(
        ["symbol", "date", "close", "high", "low", "volume", "amount", "turnover_rate"]
    ).collect()
    df = df.filter(pl.col("date") >= pl.lit(START).str.to_date())
    return df.sort(["date", "symbol"])


def build_panel() -> pl.DataFrame:
    df = load_daily()
    print(f"raw rows={df.height:,} symbols={df['symbol'].n_unique()} "
          f"dates={df['date'].n_unique()}")

    # 全局交易日索引：日历对齐的唯一真相
    cal = (
        df.select("date").unique().sort("date").with_row_index("rn")
    )
    df = df.join(cal, on="date", how="left")

    # 市值代理：close × 总股本（当前股本，只用于规模分层，不用于收益）
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet").select(
        ["symbol", "total_shares", "float_shares"]
    )
    df = df.join(inst, on="symbol", how="left")
    df = df.with_columns(
        (pl.col("close") * pl.col("float_shares")).alias("mcap_float")
    )

    # 20 日滚动统计（按 symbol 分组；这里允许数行数天，因为窗口内停牌日会自然缺行，
    # 影响的是因子平滑度而不是收益归属）
    df = df.sort(["symbol", "rn"])
    roll = [
        pl.col("turnover_rate").rolling_mean(20).alias("to20"),
        pl.col("turnover_rate").rolling_mean(120).alias("to120"),
        pl.col("turnover_rate").rolling_std(20).alias("to_std20"),
        pl.col("amount").rolling_mean(20).alias("amt20"),
        pl.col("mcap_float").rolling_mean(5).alias("mcap"),
        ((pl.col("high") - pl.col("low")) / pl.col("close")).rolling_mean(20).alias("amp20"),
    ]
    df = df.with_columns(roll).with_columns(
        [
            # Amihud 非流动性：|日收益| / 成交额（元），放大到可读量级
            (pl.col("close") / pl.col("close").shift(1) - 1).abs().alias("ret1"),
        ]
    ).with_columns(
        [
            (pl.col("ret1") / (pl.col("amount") / 1e8)).rolling_mean(20).alias("illiq20"),
        ]
    )

    # —— 前向收益：日历对齐（rn + 20），不用 shift 数行 ——
    fwd = df.select(
        ["symbol", "rn", "close", pl.col("rn").alias("rn_key")]
    ).with_columns((pl.col("rn") - FWD).alias("rn_key")).select(
        ["symbol", "rn_key", pl.col("close").alias("close_fwd")]
    )
    df = df.join(fwd, left_on=["symbol", "rn"], right_on=["symbol", "rn_key"], how="left")
    df = df.with_columns(
        (pl.col("close_fwd") / pl.col("close") - 1).alias("fwd20")
    )
    return df


def cross_section(frame: pl.DataFrame, obs_dates: list) -> None:
    """逐观测日做横截面分组，输出每组超额序列。"""
    # 因子定义：全部是"抛压 / 散户结构"的候选代理
    factors = {
        "to20": "20日换手率(水平)",
        "to_ratio": "换手异动 to20/to120",
        "to_cv": "换手波动 to_std20/to20",
        "illiq20": "Amihud非流动性",
        "amp20": "20日振幅",
        "log_amt20": "成交额(规模代理)",
    }
    frame = frame.with_columns(
        [
            (pl.col("to20") / pl.col("to120")).alias("to_ratio"),
            (pl.col("to_std20") / pl.col("to20")).alias("to_cv"),
            pl.col("amt20").log().alias("log_amt20"),
        ]
    )

    sub = frame.filter(
        pl.col("date").is_in(obs_dates)
        & pl.col("fwd20").is_finite()
        & pl.col("close_fwd").is_not_null()
        # 剔除极端收益（数据错误 / 复牌跳空），保留 ±60%/20日
        & (pl.col("fwd20").abs() < 0.6)
    )
    print(f"obs rows={sub.height:,} dates={sub['date'].n_unique()}")

    for fcol, flabel in factors.items():
        rows = []
        for d, g in sub.group_by("date", maintain_order=True):
            g = g.drop_nulls([fcol, "fwd20", "mcap"])
            if g.height < 200:
                continue
            mkt = g["fwd20"].mean()
            # 按因子分 NQ 组（qcut 用秩，避免重复值）
            g = g.with_columns(
                (pl.col(fcol).rank("average") / pl.col(fcol).count()).alias("pct")
            ).with_columns(
                (pl.col("pct") * NQ).ceil().clip(1, NQ).cast(pl.Int32).alias("grp")
            )
            agg = g.group_by("grp").agg(
                pl.col("fwd20").mean().alias("r"), pl.col("symbol").count().alias("n")
            )
            for r in agg.to_dicts():
                rows.append({"date": d, "grp": r["grp"], "ex": r["r"] - mkt})
        if not rows:
            continue
        res = pl.DataFrame(rows)
        stat = (
            res.group_by("grp")
            .agg(
                pl.col("ex").mean().alias("mean_ex"),
                pl.col("ex").std().alias("sd"),
                pl.col("ex").count().alias("n_obs"),
                (pl.col("ex") > 0).mean().alias("win"),
            )
            .sort("grp")
        )
        stat = stat.with_columns(
            (pl.col("mean_ex") / (pl.col("sd") / pl.col("n_obs").sqrt())).alias("t")
        )
        spread = res.filter(pl.col("grp").is_in([1, NQ])).pivot(
            on="grp", index="date", values="ex"
        )
        c1, c5 = f"1", f"{NQ}"
        if c1 in spread.columns and c5 in spread.columns:
            diff = spread[c5] - spread[c1]
            sp_mean = float(diff.mean()) * 1e4
            sp_t = float(diff.mean() / (diff.std() / len(diff) ** 0.5))
        else:
            sp_mean = sp_t = float("nan")
        print(f"\n=== {fcol}  {flabel} ===")
        for r in stat.to_dicts():
            print(f"  G{r['grp']}  ex={r['mean_ex']*1e4:+7.1f}bp  t={r['t']:+5.2f}  "
                  f"win={r['win']:.2f}  n={r['n_obs']}")
        print(f"  G{NQ}-G1 spread = {sp_mean:+.1f}bp  t={sp_t:+.2f}")


def main() -> None:
    df = build_panel()
    dates = sorted(df["date"].unique().to_list())
    # 抽样：每 SAMPLE_STEP 个交易日一个观测日；要求该日之后还有 FWD 个交易日
    obs = [d for i, d in enumerate(dates) if i % SAMPLE_STEP == 0 and i < len(dates) - FWD - 1]
    obs = obs[-120:]
    print(f"observation dates: {len(obs)}  {obs[0]} .. {obs[-1]}")
    cross_section(df, obs)


if __name__ == "__main__":
    sys.exit(main())
