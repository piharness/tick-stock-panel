"""三道逻辑否定的历史回放 —— 验证「被杀掉的票」启动率是否真的低于「存活的票」。

不是证明「杀手说得通」，而是测「杀手有没有真的把启动率更低的那批票拦掉」。
若某道杀手拦掉的票启动率反而更高，那它不是逻辑，是我包装出来的偏好，要改。

方法（walk-forward 逐日横截面）：
  候选池 = 结构达标(heat≥0.75) 且 20 日无涨停 且 成交额≥3000万 且 非ST 且 非北交所。
  在候选池内按优先级互斥分组：
    young   = 上市 < 250 日                       （次新杀手）
    falling = 60 日涨幅 < -10%                    （下跌中继杀手）
    fading  = 60 日涨幅 ≥ -10% 但 20 日涨幅 < -15% （短期急跌杀手，先涨后跌）
    survive = 其余                                （存活）
  标签 = 未来 5 日 / 20 日是否出现涨停，未来 20 日是否 2 连板。

标签用涨停（consecutive_limit_ups），不依赖龙虎榜披露门槛，无欠采样问题。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
OUT = ROOT / "data" / ".scratch"

from _diag_youzi_features import add_features, attach_ownership  # noqa: E402

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)


def main() -> None:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2024-12"]
    print(f"加载 {len(files)} 个分区…", flush=True)
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close", "high", "low", "amount",
                 "turnover_rate", "consecutive_limit_ups"])
        .collect().sort(["symbol", "date"])
    )
    print(f"面板 {df.height:,} 行", flush=True)

    # 结构特征（与 citizenship 同口径）
    df = df.with_columns([
        ((pl.col("high") - pl.col("low")) / pl.col("close")).rolling_mean(20).over("symbol").alias("amp20"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turn_med"),
        pl.col("amount").rolling_mean(20).over("symbol").alias("amt20"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int32).rolling_sum(20).over("symbol").alias("lu20"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("ret60"),
        # 未来标签
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(5).over("symbol") >= 1).alias("f_lu5"),
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(20).over("symbol") >= 1).alias("f_lu20"),
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(20).over("symbol") >= 2).alias("f_2b20"),
    ])

    # 上市天数 + 市值（含 float_shares）
    inst = pl.read_parquet(ROOT / "data/instruments/instruments.parquet",
                           columns=["symbol", "name", "float_shares", "listing_date"])
    df = df.join(inst.select(["symbol", "name", "float_shares", "listing_date"]), on="symbol", how="left") \
           .with_columns([
               (pl.col("close") * pl.col("float_shares")).alias("mcap"),
               pl.when(pl.col("listing_date").is_not_null())
               .then((pl.col("date") - pl.col("listing_date").str.to_date()).dt.total_days())
               .otherwise(None).cast(pl.Int64).alias("listing_days"),
               (pl.col("name").fill_null("").str.contains("ST")).alias("is_st"),
               (pl.col("symbol").str.ends_with(".BJ") | pl.col("symbol").str.starts_with("920")).alias("is_bse"),
           ])

    # 样本期：2025-11-12 起（留足 60 日 lookback），只保留特征完整
    panel = df.filter(
        (pl.col("date") >= pl.date(2025, 11, 12))
        & pl.col("amp20").is_not_null() & pl.col("ret60").is_not_null()
        & pl.col("float_shares").is_not_null() & pl.col("f_lu20").is_not_null()
    )

    # 当日横截面分位（heat 的四个分量）
    panel = panel.with_columns([
        (pl.col("amp20").rank("ordinal").over("date") / pl.col("amp20").count().over("date")).alias("p_amp"),
        (pl.col("turn_med").rank("ordinal").over("date") / pl.col("turn_med").count().over("date")).alias("p_turn"),
        (pl.col("amount").rank("ordinal").over("date") / pl.col("amount").count().over("date")).alias("p_amt"),
        (pl.col("mcap").rank("ordinal").over("date") / pl.col("mcap").count().over("date")).alias("p_scale"),
    ]).with_columns([
        ((pl.col("p_amp") + pl.col("p_turn") + (1.0 - pl.col("p_scale")) + pl.col("p_amt")) / 4.0).alias("heat"),
    ])

    # 候选池（不套用三道待验证杀手，只排除 ST/北交所/成交额不足/数据缺失）
    cand = panel.filter(
        (pl.col("heat") >= 0.75)
        & (pl.col("lu20") == 0)
        & (pl.col("amount") >= 3e7)
        & ~pl.col("is_st")
        & ~pl.col("is_bse")
    )
    print(f"\n候选池（heat≥0.75 & 20日无涨停 & 成交额≥3000万 & 非ST/北交所）：{cand.height:,} 股票日", flush=True)

    # 互斥分组（优先级：young > falling > fading > survive）
    g = cand.with_columns(
        pl.when(pl.col("listing_days").is_not_null() & (pl.col("listing_days") < 250)).then(pl.lit("young"))
        .when(pl.col("ret60") < -0.10).then(pl.lit("falling"))
        .when(pl.col("ret20") < -0.20).then(pl.lit("fading"))
        .otherwise(pl.lit("survive")).alias("grp")
    )

    base = {k: float(g[k].mean() * 1e4) for k in ("f_lu5", "f_lu20", "f_2b20")}
    print(f"候选池基准：f_lu5={base['f_lu5']:.1f}bp  f_lu20={base['f_lu20']:.1f}bp  f_2b20={base['f_2b20']:.1f}bp", flush=True)

    res = g.group_by("grp").agg([
        pl.len().alias("n"),
        (pl.col("f_lu5").mean() * 1e4).alias("f_lu5_bp"),
        (pl.col("f_lu20").mean() * 1e4).alias("f_lu20_bp"),
        (pl.col("f_2b20").mean() * 1e4).alias("f_2b20_bp"),
    ]).with_columns([
        (pl.col("f_lu5_bp") / base["f_lu5"]).alias("lift5"),
        (pl.col("f_lu20_bp") / base["f_lu20"]).alias("lift20"),
        (pl.col("f_2b20_bp") / base["f_2b20"]).alias("lift_2b"),
    ]).sort("f_lu5_bp", descending=True)

    print("\n=== 分组未来启动率（bp）+ lift ===")
    print(res)
    res.write_json(str(OUT / "youzi_kill_backtest.json"))

    # 逐年稳健性（避免单一年份的偶然）
    print("\n=== 分年 lift（f_lu20）===")
    yr = g.with_columns(pl.col("date").dt.year().alias("y")).group_by(["y", "grp"]).agg([
        pl.len().alias("n"),
        (pl.col("f_lu20").mean() * 1e4).alias("bp"),
    ]).sort(["y", "grp"])
    print(yr)

    # 关键断言：存活组的 f_lu20 是否显著高于三个被杀组
    print("\n=== 显著性（存活 vs 各被杀组，f_lu20）===")
    import numpy as np
    surv = g.filter(pl.col("grp") == "survive")
    s_n, s_p = surv.height, float(surv["f_lu20"].mean())
    for grp in ("young", "falling", "fading"):
        sub = g.filter(pl.col("grp") == grp)
        if sub.height == 0:
            continue
        p1 = float(sub["f_lu20"].mean())
        # 两样本比例 z 检验
        n1 = sub.height
        p_hat = (s_n * s_p + n1 * p1) / (s_n + n1)
        se = (p_hat * (1 - p_hat) * (1 / s_n + 1 / n1)) ** 0.5
        z = (s_p - p1) / se if se > 0 else float("nan")
        print(f"  {grp:8s} n={n1:>7,} bp={p1*1e4:6.1f} vs 存活 {s_p*1e4:.1f}  z={z:+.2f}")

    # 定位「启动率断崖」：在 falling 之外（ret60≥-10%），按 ret20 分档看启动率。
    # 这是对 fading 阈值的直接校准——断崖在哪里，阈值就该划在哪里。
    print("\n=== ret20 分档 → 未来启动率（仅 ret60≥-10% 的非次新）===")
    sub = g.filter((pl.col("grp") != "falling") & (pl.col("grp") != "young"))
    edges = [-0.30, -0.20, -0.15, -0.10, -0.05, 0.0, 0.05, 0.10]
    labels = ["<-30%", "-30~-20", "-20~-15", "-15~-10", "-10~-5", "-5~0", "0~5", "5~10", "10~15"]
    b = sub.with_columns(
        pl.when(pl.col("ret20") < -0.30).then(pl.lit(labels[0]))
        .when(pl.col("ret20") < -0.20).then(pl.lit(labels[1]))
        .when(pl.col("ret20") < -0.15).then(pl.lit(labels[2]))
        .when(pl.col("ret20") < -0.10).then(pl.lit(labels[3]))
        .when(pl.col("ret20") < -0.05).then(pl.lit(labels[4]))
        .when(pl.col("ret20") < 0.0).then(pl.lit(labels[5]))
        .when(pl.col("ret20") < 0.05).then(pl.lit(labels[6]))
        .when(pl.col("ret20") < 0.10).then(pl.lit(labels[7]))
        .otherwise(pl.lit(labels[8])).alias("rb")
    ).group_by("rb").agg([
        pl.len().alias("n"),
        (pl.col("f_lu5").mean() * 1e4).alias("f5_bp"),
        (pl.col("f_lu20").mean() * 1e4).alias("f20_bp"),
    ]).with_columns(
        pl.struct(["rb"]).map_elements(lambda s: labels.index(s["rb"]), return_dtype=pl.Int32).alias("_o")
    ).sort("_o").drop("_o")
    print(b)

    print(f"\n写出 {OUT / 'youzi_kill_backtest.json'}")


if __name__ == "__main__":
    main()
