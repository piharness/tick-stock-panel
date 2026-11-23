"""游资在涨停的哪个位置买进去 —— 盘中 8-10% 追涨 vs 涨停价排单(VIP 通道)。

方法：高声誉游资席位买入的涨停票，用 5min 线找「封板时间」（第一根触及
当日最高价=涨停价的 5min），算封板前的累计成交额（盘中追涨可买到的量）。
  游资买入金额 <= 封板前成交额 ⇒ 主要在盘中追涨（8~10%）
  游资买入金额  > 封板前成交额 ⇒ 超出部分只能来自涨停价排单（VIP 通道/一字板抢单）

同时输出封板时间分布 + 机构家数（对手盘里机构占多少）。
量化无直接字段，另用「机构专用席位 vs 营业部席位」的成交占比间接看。
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
    seats = pl.read_parquet(OUT / "youzi_seats_all.parquet")
    # 高声誉席位买入（每行 = 某席位某日某票的买入额）
    hi_buy = seats.filter((pl.col("side") == "buy") & (pl.col("buy") > 0)) \
        .join(hi.select(["dept_code"]), on="dept_code", how="semi") \
        .select(["symbol", "date", "dept_code", "buy"]).with_columns(pl.col("date").str.to_date())
    print(f"高声誉席位买入 {hi_buy.height:,} 条", flush=True)

    # 每 (symbol,date) 游资买入合计
    by_stock = hi_buy.group_by(["symbol", "date"]).agg(pl.col("buy").sum().alias("hm_buy"))

    # join 日线，只取当日涨停的
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2025-11"]
    kl = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "consecutive_limit_ups"])
        .collect()
    )
    by_stock = by_stock.join(kl, on=["symbol", "date"], how="left")
    lim = by_stock.filter(pl.col("consecutive_limit_ups") >= 1)
    print(f"其中当日涨停 {lim.height:,} 条（占 {lim.height/max(by_stock.height,1)*100:.0f}%）", flush=True)

    # 读 5min，算封板时间 + 封板前成交额
    m5_files = sorted(glob.glob(str(ROOT / "data/kline_5min/date=*/part.parquet")))
    m5_files = [f for f in m5_files if f.split("date=")[1][:7] >= "2025-11"]
    m5 = pl.scan_parquet(m5_files, extra_columns="ignore") \
        .select(["symbol", "datetime", "high", "amount"]) \
        .with_columns(pl.col("datetime").dt.date().cast(pl.Date).alias("date")) \
        .collect().sort(["symbol", "datetime"])
    print(f"5min 面板 {m5.height:,} 行", flush=True)

    # 每票每日：当日最高价 + 封板时间（第一根 high 达到当日 max high 的 5min）
    m5 = m5.with_columns(pl.col("high").max().over(["symbol", "date"]).alias("day_high"))
    m5 = m5.with_columns((pl.col("high") >= pl.col("day_high")).alias("is_seal"))
    m5 = m5.with_columns(
        pl.col("datetime").dt.strftime("%H:%M").alias("hhmm")
    )
    # 封板时间 = 第一个 is_seal 的 5min 时间
    seal = m5.filter(pl.col("is_seal")).group_by(["symbol", "date"], maintain_order=True).first() \
        .select(["symbol", "date", pl.col("hhmm").alias("seal_time")])
    # 封板前成交额 = 封板根之前的 amount 累计（用每票每日的累计 amount，减去封板根及之后）
    m5 = m5.with_columns(pl.col("amount").cum_sum().over(["symbol", "date"]).alias("cum_amt"))
    seal_amt = m5.filter(pl.col("is_seal")).group_by(["symbol", "date"], maintain_order=True).first() \
        .select(["symbol", "date", pl.col("cum_amt").alias("seal_cum")])
    total_amt = m5.group_by(["symbol", "date"]).agg(pl.col("amount").sum().alias("day_amt"))
    seal = seal.join(seal_amt, on=["symbol", "date"], how="left") \
        .join(total_amt, on=["symbol", "date"], how="left")

    # 封板前成交额 = 封板根起始的累计 - 封板根本身（近似：seal_cum - 封板根 amount）
    seal_first_amt = m5.filter(pl.col("is_seal")).group_by(["symbol", "date"], maintain_order=True).first() \
        .select(["symbol", "date", pl.col("amount").alias("seal_bar_amt")])
    seal = seal.join(seal_first_amt, on=["symbol", "date"], how="left")
    seal = seal.with_columns(
        (pl.col("seal_cum") - pl.col("seal_bar_amt")).clip(lower_bound=0).alias("pre_seal_amt")
    )

    # join 游资买入
    r = lim.join(seal, on=["symbol", "date"], how="inner")
    r = r.with_columns(
        # 涨停价排单部分 = 游资买入超过封板前成交额的部分
        ((pl.col("hm_buy") - pl.col("pre_seal_amt")).clip(lower_bound=0) / pl.col("hm_buy")).alias("queue_share"),
    )
    print(f"\n匹配到 5min 的涨停买入 {r.height:,} 条", flush=True)

    print("\n=== 封板时间分布（游资玩的涨停票）===")
    def bucket(t: str) -> str:
        if t <= "09:35": return "09:35前(一字/秒板)"
        if t <= "10:00": return "09:35-10:00"
        if t <= "10:30": return "10:00-10:30"
        if t <= "11:30": return "10:30-11:30"
        if t <= "14:00": return "13:00-14:00"
        return "14:00后(尾盘板)"
    r = r.with_columns(pl.col("seal_time").map_elements(bucket, return_dtype=pl.Utf8).alias("t_bucket"))
    tb = r.group_by("t_bucket").agg([pl.len().alias("n"),
                                     (pl.col("queue_share").median() * 100).alias("排单占比中位%"),
                                     (pl.col("hm_buy").median() / 1e4).alias("游资买入中位万")])
    print(tb)

    print("\n=== 涨停价排单占比（queue_share）分布 ===")
    qs = r.select([
        pl.col("queue_share").mean().alias("mean"),
        pl.col("queue_share").median().alias("med"),
        pl.col("queue_share").quantile(0.75).alias("p75"),
        pl.col("queue_share").quantile(0.9).alias("p90"),
    ])
    print(qs)
    # 分类：追涨为主 vs 排单为主
    r = r.with_columns(pl.when(pl.col("queue_share") < 0.3).then(pl.lit("盘中追涨为主"))
                        .when(pl.col("queue_share") < 0.7).then(pl.lit("混合"))
                        .otherwise(pl.lit("涨停排单为主")).alias("mode"))
    mode_cnt = r.group_by("mode").agg(pl.len().alias("cnt"))
    total = r.height
    print("\n=== 买入方式分类 ===")
    for row in mode_cnt.to_dicts():
        print(f"  {row['mode']:14s} {row['cnt']:>5,} 条  {row['cnt']/total*100:.0f}%")

    # 验证 2：机构家数（对手盘里的机构占比），join 龙虎榜 org 字段
    import json
    org_rows = []
    for f in sorted(glob.glob(str(ROOT / "data/dragon_tiger/date=*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        for it in (d.get("org") or {}).get("stock_items") or []:
            org_rows.append((it.get("thscode"), d.get("trade_date"),
                             it.get("org_buy_num"), it.get("org_sell_num"), it.get("org_net_value")))
    org = pl.DataFrame(org_rows, schema=["symbol", "date", "org_buy_num", "org_sell_num", "org_net_value"],
                       orient="row").with_columns(pl.col("date").str.to_date())
    r2 = lim.join(org, on=["symbol", "date"], how="left")
    print("\n=== 游资涨停票的机构家数（对手盘机构占比）===")
    print(r2.select([
        pl.col("org_buy_num").mean().alias("机构买入家数均值"),
        pl.col("org_buy_num").median().alias("中位"),
        pl.col("org_sell_num").mean().alias("机构卖出家数均值"),
    ]))
    print(r2.group_by((pl.col("org_buy_num").fill_null(0) > 0).alias("有机构上榜")).agg(pl.len().alias("n")))

    r.write_parquet(OUT / "youzi_fill.parquet")
    print(f"\n写出 {OUT / 'youzi_fill.parquet'}")


if __name__ == "__main__":
    main()
