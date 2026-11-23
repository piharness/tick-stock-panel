"""席位共现做不了（无营业部明细），退而验证「龙虎榜资金结构」的增量。

数据事实：data/dragon_tiger 的 hot_money 榜（席位表）247 天全空，只有聚合字段
  hot_money_net_value / org_net_value / org_buy_num / org_sell_num。
所以共现网络缺维度，这里验证「谁在买」这个降级信号有没有超出 heat 的增量。

核心问题（防「关注度归零」式自欺）：
  游资净买入发生在「异动日」，而异动已经反映进 heat（振幅/换手/成交额分位）。
  若游资净买组的启动率高，只是因为它们 heat 更高，那就是零增量。
  必须同时报两组的 heat 均值 + 控制 heat 后的对比。

口径：候选池 = heat≥0.75 & 20日无涨停 & 成交额≥3000万 & 非ST/北交所 & 上市≥250日。
      T 日龙虎榜 = T 日盘后可知，用于 T+1 决策，无前视。
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

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_width_chars(250)
pl.Config.set_tbl_cols(40)


def main() -> None:
    files = sorted(glob.glob(str(ROOT / "data/kline_daily_enriched/date=*/part.parquet")))
    files = [f for f in files if f.split("date=")[1][:7] >= "2024-12"]
    print(f"加载 {len(files)} 分区…", flush=True)
    df = (
        pl.scan_parquet(files, extra_columns="ignore")
        .select(["symbol", "date", "close", "high", "low", "amount",
                 "turnover_rate", "consecutive_limit_ups"])
        .collect().sort(["symbol", "date"])
    )
    df = df.with_columns([
        ((pl.col("high") - pl.col("low")) / pl.col("close")).rolling_mean(20).over("symbol").alias("amp20"),
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turn_med"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int32).rolling_sum(20).over("symbol").alias("lu20"),
        (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("ret60"),
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(5).over("symbol") >= 1).alias("f_lu5"),
        (pl.col("consecutive_limit_ups").shift(-1).rolling_max(20).over("symbol") >= 1).alias("f_lu20"),
    ])
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

    panel = df.filter(
        (pl.col("date") >= pl.date(2025, 11, 12))
        & pl.col("amp20").is_not_null() & pl.col("ret60").is_not_null()
        & pl.col("float_shares").is_not_null() & pl.col("f_lu20").is_not_null()
    )
    panel = panel.with_columns([
        (pl.col("amp20").rank("ordinal").over("date") / pl.col("amp20").count().over("date")).alias("p_amp"),
        (pl.col("turn_med").rank("ordinal").over("date") / pl.col("turn_med").count().over("date")).alias("p_turn"),
        (pl.col("amount").rank("ordinal").over("date") / pl.col("amount").count().over("date")).alias("p_amt"),
        (pl.col("mcap").rank("ordinal").over("date") / pl.col("mcap").count().over("date")).alias("p_scale"),
    ]).with_columns([
        ((pl.col("p_amp") + pl.col("p_turn") + (1.0 - pl.col("p_scale")) + pl.col("p_amt")) / 4.0).alias("heat"),
    ])

    # 龙虎榜聚合（每 symbol/date 一行，取 range_days 最小）
    rows = []
    for f in sorted(glob.glob(str(ROOT / "data/dragon_tiger/date=*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        items = (d.get("org") or {}).get("stock_items") or []
        for it in items:
            rows.append({
                "symbol": it.get("thscode"),
                "date": d.get("trade_date"),
                "hm_net": it.get("hot_money_net_value"),
                "org_net": it.get("org_net_value"),
                "org_buy_num": it.get("org_buy_num"),
                "org_sell_num": it.get("org_sell_num"),
            })
    lhb = pl.DataFrame(rows, orient="row").with_columns(pl.col("date").str.to_date())
    lhb = lhb.sort(["symbol", "date"]).group_by(["symbol", "date"], maintain_order=True).first()

    # 候选池 + 龙虎榜 join
    cand = panel.filter(
        (pl.col("heat") >= 0.75) & (pl.col("lu20") == 0) & (pl.col("amount") >= 3e7)
        & ~pl.col("is_st") & ~pl.col("is_bse")
        & (pl.col("listing_days").is_null() | (pl.col("listing_days") >= 250))
        & (pl.col("ret60") >= -0.10)
    )
    cand = cand.join(lhb, on=["symbol", "date"], how="left")
    cand = cand.with_columns([
        pl.col("hm_net").is_not_null().alias("on_lhb"),
        (pl.col("hm_net").fill_null(0) > 0).alias("hm_buy"),
        (pl.col("org_net").fill_null(0) > 0).alias("org_buy"),
    ])
    print(f"\n候选池 {cand.height:,} 股票日；其中 T 日上龙虎榜 {int(cand['on_lhb'].sum()):,}", flush=True)

    base = float(cand["f_lu20"].mean() * 1e4)
    print(f"候选池基准 f_lu20={base:.1f}bp  f_lu5={cand['f_lu5'].mean()*1e4:.1f}bp", flush=True)

    # 分组对比 + heat 均值（关键：看增量是不是只是 heat 更高）
    def report(mask: pl.Series | pl.Expr, label: str):
        sub = cand.filter(mask)
        if sub.height < 100:
            print(f"  {label:20s} n={sub.height:<6,} 样本不足，跳过")
            return
        print(f"  {label:20s} n={sub.height:>7,}  f5={sub['f_lu5'].mean()*1e4:6.1f}bp "
              f"f20={sub['f_lu20'].mean()*1e4:6.1f}bp  lift={sub['f_lu20'].mean()*1e4/base:5.2f}  "
              f"heat均={sub['heat'].mean():.3f}")

    print("\n=== 龙虎榜资金结构 vs 未来启动率 ===")
    report(~pl.col("on_lhb"), "未上榜")
    report(pl.col("on_lhb"), "上榜(任意)")
    report(pl.col("hm_buy"), "游资净买入>0")
    report(pl.col("on_lhb") & ~pl.col("hm_buy"), "上榜但游资净卖/持平")
    report(pl.col("org_buy"), "机构净买入>0")
    report(pl.col("on_lhb") & pl.col("org_buy"), "机构+游资双买")

    # 控制 heat：在 heat 四分位内看游资净买的增量
    print("\n=== 控制 heat 后，游资净买 vs 未上榜（f_lu20 bp）===")
    cand = cand.with_columns(
        pl.col("heat").qcut(3, labels=["H1", "H2", "H3"]).alias("hb")
    )
    for h in ("H1", "H2", "H3"):
        sub = cand.filter(pl.col("hb") == h)
        hm = sub.filter(pl.col("hm_buy"))
        no = sub.filter(~pl.col("on_lhb"))
        if hm.height < 50 or no.height < 100:
            continue
        print(f"  {h}: 游资买 {hm.height:>5,} → {hm['f_lu20'].mean()*1e4:6.1f}bp | "
              f"未上榜 {no.height:>6,} → {no['f_lu20'].mean()*1e4:6.1f}bp | "
              f"差 {(hm['f_lu20'].mean()-no['f_lu20'].mean())*1e4:+5.1f}bp")

    cand.write_parquet(OUT / "youzi_seat_probe.parquet")
    print(f"\n写出 {OUT / 'youzi_seat_probe.parquet'}")


if __name__ == "__main__":
    main()
