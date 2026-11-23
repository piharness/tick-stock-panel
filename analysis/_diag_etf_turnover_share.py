"""量级检验：ETF 是不是抛压源？——用成交额占比看 ETF 在下跌期有没有异常放量

二手叙事的流行版本：「ETF 被机构抛售，所以 ETF 跌」。
若成立，ETF 的成交额占全市场比例应在下跌月份**显著抬升**（抛售=换手放大）。

口径：
  ETF 成交额 = data/backfill_20261117/etf_daily_full.parquet + data/kline_etf_daily 增量
  全市场成交额 = data/kline_daily_enriched 按日求和（个股，不含 ETF，避免自我叠加）
  输出逐月：ETF 占比、ETF 成交额、全市场成交额、ETF 等权月收益

注意（写进结论里）：ETF 成交额含做市商对倒与套利盘，会系统性高于「真实方向性交易」，
所以这个比例是**抛压的上界**，不是抛压下界。若连上界都不高，叙事就不成立。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
BASE_ETF = ROOT / "data" / "backfill_20261117" / "etf_daily_full.parquet"
PART_ETF = ROOT / "data" / "kline_etf_daily"
STOCK = ROOT / "data" / "kline_daily_enriched"
OUT = ROOT / "data" / ".scratch" / "etf_turnover_share.json"


def load_etf() -> pl.DataFrame:
    base = pl.read_parquet(BASE_ETF).select(["symbol", "date", "amount", "close"])
    parts = sorted(glob.glob(str(PART_ETF / "date=*" / "part.parquet")))
    frames = [base]
    if parts:
        frames.append(
            pl.scan_parquet(parts, extra_columns="ignore")
            .select(["symbol", "date", "amount", "close"]).collect()
        )
    df = pl.concat(frames).with_columns(pl.col("date").cast(pl.Date))
    return df.unique(subset=["symbol", "date"], keep="last")


def load_stock_amount() -> pl.DataFrame:
    parts = sorted(glob.glob(str(STOCK / "date=*" / "part.parquet")))
    return (
        pl.scan_parquet(parts, extra_columns="ignore")
        .select(["symbol", "date", "amount", "close"])
        .collect()
        .with_columns(pl.col("date").cast(pl.Date))
    )


def main() -> None:
    print("读 ETF 日线…", flush=True)
    etf = load_etf()
    etf = etf.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("ym"))
    # ETF 等权月收益：用每只票当月首末 close 算，避免复权问题先只看方向
    etf_m = (
        etf.group_by("ym")
        .agg([
            (pl.col("amount").sum() / 1e8).alias("etf_amt_yi"),
            pl.col("symbol").n_unique().alias("n_etf"),
            pl.len().alias("rows"),
        ])
        .sort("ym")
    )

    print("读个股日线（聚合全市场成交额）…", flush=True)
    stk = load_stock_amount()
    stk = stk.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("ym"))
    stk_m = (
        stk.group_by("ym")
        .agg([
            (pl.col("amount").sum() / 1e8).alias("stk_amt_yi"),
            pl.col("symbol").n_unique().alias("n_stk"),
        ])
        .sort("ym")
    )

    m = etf_m.join(stk_m, on="ym", how="inner")
    m = m.with_columns(
        (pl.col("etf_amt_yi") / (pl.col("etf_amt_yi") + pl.col("stk_amt_yi")) * 100).alias("share_pct")
    )

    print("\n=== 逐月：ETF 成交额占全市场（个股+ETF）比例 ===")
    print(f"{'月份':>8} {'ETF亿':>10} {'个股亿':>12} {'占比%':>7}")
    for r in m.iter_rows(named=True):
        if r["ym"] >= "2025-06":
            print(f"{r['ym']:>8} {r['etf_amt_yi']:>10,.0f} {r['stk_amt_yi']:>12,.0f} {r['share_pct']:>7.2f}")

    y26 = m.filter(pl.col("ym") >= "2026-01")
    hist = m.filter((pl.col("ym") >= "2024-01") & (pl.col("ym") < "2026-01"))

    # 用中证全指/沪深300 的月收益把 2026 各月分成「跌月/涨月」，看占比是否在跌月抬升
    idx_file = ROOT / "data" / "backfill_20261117" / "index_daily_full.parquet"
    idx = pl.read_parquet(idx_file, columns=["symbol", "date", "close"]).filter(
        pl.col("symbol") == "000300.SH"
    ).with_columns(pl.col("date").cast(pl.Date).dt.strftime("%Y-%m").alias("ym"))
    idx_m = (
        idx.sort("date").group_by("ym")
        .agg([pl.col("close").first().alias("o"), pl.col("close").last().alias("c")])
        .with_columns(((pl.col("c") / pl.col("o") - 1) * 100).alias("hs300_ret"))
        .select(["ym", "hs300_ret"])
        .sort("ym")
    )
    j = y26.join(idx_m, on="ym", how="left").sort("ym")

    print("\n=== 2026 逐月：ETF 占比 vs 沪深300 涨跌 ===")
    print(f"{'月份':>8} {'占比%':>7} {'HS300%':>8}")
    for r in j.iter_rows(named=True):
        print(f"{r['ym']:>8} {r['share_pct']:>7.2f} {r['hs300_ret']:>8.2f}")

    down = j.filter(pl.col("hs300_ret") < 0)
    up = j.filter(pl.col("hs300_ret") >= 0)
    res = {
        "meta": {
            "note": "ETF 成交额占比 = ETF amount / (ETF + 个股 amount)。含做市商对倒 ⇒ 抛压上界。",
            "etf_source": "backfill etf_daily_full + kline_etf_daily 增量",
            "stock_source": "kline_daily_enriched 全量 amount 按日聚合",
        },
        "share_2026": {
            "mean": round(float(y26["share_pct"].mean()), 2),
            "min": round(float(y26["share_pct"].min()), 2),
            "max": round(float(y26["share_pct"].max()), 2),
        },
        "share_hist_2024_2025": {
            "mean": round(float(hist["share_pct"].mean()), 2),
            "min": round(float(hist["share_pct"].min()), 2),
            "max": round(float(hist["share_pct"].max()), 2),
        },
        "down_months": [
            {"ym": r["ym"], "share": round(r["share_pct"], 2), "hs300": round(r["hs300_ret"], 2)}
            for r in down.iter_rows(named=True) if r["hs300_ret"] is not None
        ],
        "up_months": [
            {"ym": r["ym"], "share": round(r["share_pct"], 2), "hs300": round(r["hs300_ret"], 2)}
            for r in up.iter_rows(named=True) if r["hs300_ret"] is not None
        ],
        "monthly": [
            {"ym": r["ym"], "share": round(r["share_pct"], 2),
             "etf_yi": round(r["etf_amt_yi"], 0), "stk_yi": round(r["stk_amt_yi"], 0),
             "hs300": None if r["hs300_ret"] is None else round(r["hs300_ret"], 2)}
            for r in j.iter_rows(named=True)
        ],
    }
    res["down_mean"] = round(float(down["share_pct"].mean()), 2) if down.height else None
    res["up_mean"] = round(float(up["share_pct"].mean()), 2) if up.height else None

    print("\n=== 汇总 ===")
    print("2026 均值", res["share_2026"]["mean"], "% | 2024-2025 均值", res["share_hist_2024_2025"]["mean"], "%")
    print("2026 跌月均值", res["down_mean"], "% | 涨月均值", res["up_mean"], "%")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("写入", OUT)


if __name__ == "__main__":
    main()
