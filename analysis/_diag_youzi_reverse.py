"""逆向游资选股 —— 高声誉席位买入的票，买入前一刻（T-1）长什么样。

核心问题：游资选股是「条件筛选」（T-1 已满足冷启动条件，可被 heat 提前识别）
还是「事件驱动」（T-1 平静，T 日因题材/消息突然点火）？

若是前者，heat 方向对、只需优化颗粒度；若是后者，静态条件永远抓不到游资，
必须转向「题材/事件」维度（limit_reason 里的故事）。

方法：
  高声誉席位 = seat_rep 中 rep20>=0.05 且 n>=20（历史胜率高的游资席位）
  取这些席位的买入事件 (symbol, T)，回看 T-1 的：
    heat 分位（振幅/换手/成交额/市值四项 rank over date）
    是否已冷启动（heat>=0.75 且 20 日无涨停）
    ret20 / ret60 / 上市天数
  对比低声誉席位（rep20<0）与全市场，看差异。
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)


def main() -> None:
    rep = pl.read_parquet(ROOT / "data/dragon_tiger_seats/seat_rep.parquet")
    hi = rep.filter((pl.col("rep20") >= 0.05) & (pl.col("n") >= 20))
    lo = rep.filter((pl.col("rep20") < 0.0) & (pl.col("n") >= 20))
    print(f"高声誉席位 {hi.height} 个，低声誉席位 {lo.height} 个", flush=True)

    seats = pl.read_parquet(OUT / "youzi_seats_all.parquet")
    buy = seats.filter((pl.col("side") == "buy") & (pl.col("buy") > 0)).select([
        "symbol", "date", "dept_code", "dept_name",
    ]).with_columns(pl.col("date").str.to_date())

    # 高/低声誉席位的买入事件
    hi_buy = buy.join(hi.select(["dept_code"]), on="dept_code", how="semi") \
        .with_columns(pl.lit("high").alias("rep_grp"))
    lo_buy = buy.join(lo.select(["dept_code"]), on="dept_code", how="semi") \
        .with_columns(pl.lit("low").alias("rep_grp"))
    print(f"高声誉买入事件 {hi_buy.height:,} 条，低声誉 {lo_buy.height:,} 条", flush=True)

    # 日线：算 T-1 特征（用 shift 到前一日）
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2024-12"]
    kl = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close", "high", "low", "amount",
                 "turnover_rate", "consecutive_limit_ups"])
        .collect().sort(["symbol", "date"])
    )
    kl = kl.with_columns([
        ((pl.col("high") - pl.col("low")) / pl.col("close")).rolling_mean(20).over("symbol").alias("amp20"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turn_med"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int32).rolling_sum(20).over("symbol").alias("lu20"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("ret60"),
    ])
    # 当日横截面分位
    kl = kl.with_columns([
        (pl.col("amp20").rank("ordinal").over("date") / pl.col("amp20").count().over("date")).alias("p_amp"),
        (pl.col("turn_med").rank("ordinal").over("date") / pl.col("turn_med").count().over("date")).alias("p_turn"),
        (pl.col("amount").rank("ordinal").over("date") / pl.col("amount").count().over("date")).alias("p_amt"),
    ])
    kl = kl.with_columns(((pl.col("p_amp") + pl.col("p_turn") + pl.col("p_amt")) / 3.0).alias("heat3"))

    # 买入事件 join T-1 的特征（用买入日 date 对应 kl 的同一日，但特征要 T-1）
    # 直接取买入日的特征（amp20/ret20 是「截至买入日」的滚动值，已含当日？）
    # 严格应取 T-1，但先看「买入日」的静态特征（近似），关键看 lu20 和 ret20
    def profile(b: pl.DataFrame, label: str) -> None:
        m = b.join(kl.select(["symbol", "date", "heat3", "lu20", "ret20", "ret60",
                              "amp20", "turn_med", "p_amp", "p_turn", "p_amt"]),
                   on=["symbol", "date"], how="left")
        m = m.drop_nulls(["heat3"])
        cold = (m["heat3"] >= 0.75) & (m["lu20"] == 0)
        print(f"\n=== {label}（n={m.height:,}）买入日画像 ===")
        print(f"  已冷启动(heat3>=0.75 & 20日无涨停) 占比 {cold.mean()*100:.1f}%")
        print(f"  买入日涨停(lu20>=1) 占比 {(m['lu20']>=1).mean()*100:.1f}%")
        print(f"  ret20 中位 {m['ret20'].median()*100:+.1f}%  ret60 中位 {m['ret60'].median()*100:+.1f}%")
        print(f"  amp20 中位 {m['amp20'].median()*100:.2f}%  turn_med 中位 {m['turn_med'].median():.2f}%")
        print(f"  振幅分位中位 {m['p_amp'].median():.2f}  换手分位中位 {m['p_turn'].median():.2f}")

    profile(hi_buy, "高声誉席位买入")
    profile(lo_buy, "低声誉席位买入")

    # 关键对比：高声誉席位买入的票，买入日已涨停 vs 冷启动
    hi_m = hi_buy.join(kl.select(["symbol", "date", "heat3", "lu20", "ret20", "ret60"]),
                       on=["symbol", "date"], how="left").drop_nulls(["heat3"])
    # 分类：买入日涨停 / 买入日冷启动 / 其他
    hi_m = hi_m.with_columns(
        pl.when(pl.col("lu20") >= 1).then(pl.lit("当日涨停"))
        .when(pl.col("heat3") >= 0.75).then(pl.lit("当日冷启动候选"))
        .otherwise(pl.lit("其他")).alias("buy_ctx")
    )
    print("\n=== 高声誉席位买入时的市场状态分布 ===")
    print(hi_m.group_by("buy_ctx").agg([pl.len().alias("n"),
                                        (pl.col("ret20").median() * 100).alias("ret20_med")]))

    hi_m.write_parquet(OUT / "youzi_reverse.parquet")
    print(f"\n写出 {OUT / 'youzi_reverse.parquet'}")


if __name__ == "__main__":
    main()
