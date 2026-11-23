"""主板 20 日超额收益：静态/动态指标的前视统计显著性（含日内拆单代理）。

口径（与 cross_section_alpha / market_percentiles 对齐）：
  - 信号在 T 日收盘可知，T+1 开盘买入，T+20 收盘卖出（交易日历，不是 shift 行数）
  - 超额 = 个股 h=20 收益 − 同日全市场等权 h=20 收益
  - 指标全部取 T 日及之前的数据（pre-window），不使用窗口内信息

阶段：
  build     全量日线 → 指标面板（只保留每 5 个交易日一个锚点）
  intraday  5min 分区 → 票-日日内拆单代理（2024-09 起）
  stats     横截面 IC / 分层 / Fama-MacBeth 残差 IC / FDR / 子区间
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / ".scratch" / "footprint20d"
OUT.mkdir(parents=True, exist_ok=True)

ENRICHED = DATA / "kline_daily_enriched"
SHARES = DATA / "backfill_20261117" / "fin_shares.parquet"
INSTR = DATA / "instruments" / "instruments.parquet"
INDUSTRY = DATA / "ext_data" / "ext_hy_ths" / "part.parquet"
MIN5 = DATA / "kline_5min"

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
START = pl.date(2009, 1, 1)
SIGNAL_START = pl.date(2010, 1, 1)


def _main_board(sym: str) -> bool:
    code = sym.split(".")[0]
    return code[:3] in MAIN_PREFIX


def build_panel() -> None:
    lf = pl.scan_parquet(str(ENRICHED / "*" / "part.parquet"))
    df = (
        lf.filter(pl.col("date") >= START)
        .select(
            "symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "raw_close", "raw_high", "raw_low", "turnover_rate",
            "consecutive_limit_ups", "consecutive_limit_downs",
        )
        .collect()
    )
    # 主板 + 非北交所
    df = df.filter(pl.col("symbol").str.slice(0, 3).is_in(MAIN_PREFIX))
    df = df.filter(pl.col("amount").is_not_null() & (pl.col("amount") > 0))
    df = df.sort(["symbol", "date"])

    # ---- 历史流通股本（as-of） ----
    sh = (
        pl.read_parquet(SHARES)
        .with_columns(pl.col("end_date").cast(pl.Date))
        .with_columns(
            pl.coalesce(
                pl.col("listed_a_shares"), pl.col("unlimited_shares"), pl.col("total_shares")
            ).alias("float_shares")
        )
        .filter(pl.col("float_shares") > 0)
        .select("symbol", "end_date", "float_shares")
        .sort(["symbol", "end_date"])
    )
    df = df.sort(["symbol", "date"]).join_asof(
        sh, left_on="date", right_on="end_date", by="symbol", strategy="backward"
    )

    # ---- 基础收益 ----
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("ret"),
        (pl.col("open") / pl.col("close").shift(1).over("symbol") - 1).alias("gap"),
        (pl.col("close") / pl.col("open") - 1).alias("intraday"),
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("range"),
        (pl.col("consecutive_limit_ups") > 0).cast(pl.Int8).alias("is_limit_up"),
        pl.int_range(1, pl.len() + 1).over("symbol").alias("k"),
    )
    df = df.with_columns(
        pl.col("ret").abs().alias("absret"),
        (pl.col("ret").abs() < 0.01).cast(pl.Int8).alias("is_quiet"),
    )
    # 异常跳变（除权/重组）：污染指标但不删样本，置空
    df = df.with_columns(
        pl.when(pl.col("ret").abs() > 0.25).then(None).otherwise(pl.col("ret")).alias("ret_clean"),
        pl.when(pl.col("gap").abs() > 0.25).then(None).otherwise(pl.col("gap")).alias("gap_clean"),
    )

    # 市场等权日收益（用于 beta / 残差波动）
    mkt = df.group_by("date").agg(pl.col("ret_clean").mean().alias("mkt_ret"))
    df = df.join(mkt, on="date", how="left").sort(["symbol", "date"])

    w = 20
    df = df.with_columns(
        # 收益/动量
        (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("ret20"),
        (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1).alias("ret5"),
        pl.col("ret_clean").rolling_std(w, min_samples=15).over("symbol").alias("vol20"),
        pl.col("ret_clean").rolling_skew(w, min_samples=15).over("symbol").alias("skew20"),
        pl.col("ret_clean").rolling_max(w, min_samples=15).over("symbol").alias("max_ret20"),
        pl.rolling_corr(pl.col("ret_clean"), pl.col("ret_clean").shift(1), window_size=w, min_samples=15)
        .over("symbol").alias("ar1_20"),
        # 换手/流动性
        pl.col("turnover_rate").rolling_mean(w, min_samples=15).over("symbol").alias("turn20"),
        (pl.col("turnover_rate").rolling_mean(5, min_samples=4).over("symbol")
         / pl.col("turnover_rate").rolling_mean(20, min_samples=15).over("symbol") - 1).alias("turn_chg"),
        pl.col("turnover_rate").rolling_std(w, min_samples=15).over("symbol").alias("turn_std20"),
        pl.col("amount").rolling_mean(w, min_samples=15).over("symbol").alias("amt20"),
        (pl.col("amount").rolling_mean(5, min_samples=4).over("symbol")
         / pl.col("amount").rolling_mean(20, min_samples=15).over("symbol") - 1).alias("amt_chg"),
        (pl.col("absret") / (pl.col("amount") / 1e8)).rolling_mean(w, min_samples=15)
        .over("symbol").alias("amihud"),
        # 量价结构
        (pl.when(pl.col("ret_clean") > 0).then(pl.col("volume")).otherwise(0))
        .rolling_sum(w, min_samples=15).over("symbol").alias("up_vol"),
        (pl.when(pl.col("ret_clean") < 0).then(pl.col("volume")).otherwise(0))
        .rolling_sum(w, min_samples=15).over("symbol").alias("dn_vol"),
        pl.rolling_corr(pl.col("ret_clean"), pl.col("amount").pct_change(), window_size=w, min_samples=15)
        .over("symbol").alias("corr_pv"),
        pl.col("range").rolling_mean(w, min_samples=15).over("symbol").alias("range20"),
        pl.col("is_limit_up").rolling_sum(w, min_samples=15).over("symbol").alias("limit_up_cnt20"),
        pl.col("is_quiet").rolling_sum(w, min_samples=15).over("symbol").alias("quiet_cnt20"),
        # 日内/隔夜
        pl.col("gap_clean").rolling_mean(w, min_samples=15).over("symbol").alias("overnight20"),
        pl.col("intraday").rolling_mean(w, min_samples=15).over("symbol").alias("intraday20"),
        # 位置
        (pl.col("close") / pl.col("high").rolling_max(250, min_samples=200).over("symbol") - 1)
        .alias("dist_high250"),
        # beta（60 日）
        (pl.rolling_corr(pl.col("ret_clean"), pl.col("mkt_ret"), window_size=60, min_samples=40).over("symbol")
         * pl.col("ret_clean").rolling_std(60, min_samples=40).over("symbol")
         / pl.col("mkt_ret").rolling_std(60, min_samples=40).over("symbol")).alias("beta60"),
    )
    df = df.with_columns(
        pl.col("ret_clean").rolling_std(60, min_samples=40).over("symbol").alias("vol60"),
    )
    df = df.with_columns(
        (pl.col("vol60") ** 2 - (pl.col("beta60") ** 2) * (pl.col("mkt_ret").rolling_var(60, min_samples=40).over("symbol")))
        .clip(lower_bound=0).sqrt().alias("idio_vol60"),
        (pl.col("up_vol") / pl.col("dn_vol").clip(lower_bound=1)).log().alias("upvol_ratio"),
        pl.col("amihud").log1p().alias("log_amihud"),
        pl.col("amt20").log().alias("log_amt20"),
        (pl.col("raw_close") * pl.col("float_shares")).log().alias("log_mcap"),
        pl.col("raw_close").log().alias("log_price"),
        (pl.col("turn_std20") / pl.col("turn20")).alias("turn_cv20"),
        (pl.col("range20") / (pl.col("vol20") + 1e-9)).alias("range_vol_ratio"),
    )

    # 窗口内（t+1..t+20）的换手/波动，仅用于「窗口内 vs 窗口前」对照
    df = df.with_columns(
        pl.col("turnover_rate").rolling_mean(w, min_samples=15).shift(-w).over("symbol").alias("turn20_fwd"),
        pl.col("ret_clean").rolling_std(w, min_samples=15).shift(-w).over("symbol").alias("vol20_fwd"),
    )

    # ---- 静态：上市年龄 / 交易所 ----
    ins = (
        pl.read_parquet(INSTR)
        .select("symbol", "listing_date", "name")
        .with_columns(pl.col("listing_date").cast(pl.Date))
    )
    df = df.join(ins, on="symbol", how="left")
    df = df.with_columns(
        ((pl.col("date") - pl.col("listing_date")).dt.total_days() / 365.25).alias("age_years"),
        pl.col("symbol").str.ends_with(".SH").cast(pl.Int8).alias("is_sh"),
        pl.col("name").str.contains("ST").cast(pl.Int8).alias("is_st"),
    )

    # ---- 行业 ----
    hy = (
        pl.read_parquet(INDUSTRY)
        .select(
            "symbol",
            pl.col("所属同花顺行业").str.split("-").list.first().alias("industry"),
        )
        .unique(subset=["symbol"])
    )
    df = df.join(hy, on="symbol", how="left").with_columns(
        pl.col("industry").fill_null("未分类")
    )

    # ---- 交易日历：T+1 开盘、T+20 收盘 ----
    cal = df.select("date").unique().sort("date").with_row_index("di")
    cal = cal.with_columns(
        (pl.col("di") + 1).alias("di_entry"), (pl.col("di") + 20).alias("di_exit")
    )
    entry = cal.select(pl.col("di").alias("key"), pl.col("date").alias("entry_date"))
    exit_ = cal.select(pl.col("di").alias("key"), pl.col("date").alias("exit_date"))
    df = df.join(cal.select("date", "di", "di_entry", "di_exit"), on="date").join(
        entry, left_on="di_entry", right_on="key"
    ).join(exit_, left_on="di_exit", right_on="key")

    px = df.select(
        "symbol", "date", "open", "close", "raw_close", "raw_high", "raw_low"
    )
    df = df.join(
        px.rename({
            "date": "entry_date", "open": "entry_open", "close": "entry_close",
            "raw_close": "entry_raw_close", "raw_high": "entry_raw_high", "raw_low": "entry_raw_low",
        }),
        on=["symbol", "entry_date"], how="left",
    ).join(
        px.rename({"date": "exit_date", "close": "exit_close"}),
        on=["symbol", "exit_date"], how="left",
    )
    df = df.with_columns(
        (pl.col("exit_close") / pl.col("entry_open") - 1).alias("fwd20"),
        (pl.col("entry_open") / pl.col("close") - 1).alias("entry_gap"),
    )
    # 全市场等权基准（用全部 A 股，不只主板）
    bench = (
        pl.scan_parquet(str(ENRICHED / "*" / "part.parquet"))
        .filter(pl.col("date") >= START)
        .select("symbol", "date", "open", "close")
        .collect()
        .sort(["symbol", "date"])
        .join(cal.select("date", "di", "di_entry", "di_exit"), on="date")
    )
    bench = bench.join(entry, left_on="di_entry", right_on="key", how="left").join(
        exit_, left_on="di_exit", right_on="key", how="left"
    )
    bpx = bench.select("symbol", "date", "open", "close")
    bench = bench.join(
        bpx.rename({"date": "entry_date", "open": "entry_open"}), on=["symbol", "entry_date"], how="left"
    ).join(
        bpx.rename({"date": "exit_date", "close": "exit_close"}), on=["symbol", "exit_date"], how="left"
    ).with_columns((pl.col("exit_close") / pl.col("entry_open") - 1).alias("fwd20"))
    bm = bench.group_by("date").agg(pl.col("fwd20").mean().alias("bench20"))
    df = df.join(bm, on="date", how="left").with_columns(
        (pl.col("fwd20") - pl.col("bench20")).alias("ex20")
    )

    # ---- 宇宙与可交易过滤 ----
    df = df.with_columns(
        pl.when(
            (pl.col("k") >= 250)
            & (pl.col("amount") >= 2e7)
            & (pl.col("raw_close") >= 1.0)
            & pl.col("turnover_rate").is_not_null()
            & (pl.col("turnover_rate") > 0.05)
            & pl.col("fwd20").is_not_null()
            & pl.col("bench20").is_not_null()
            & (pl.col("is_st") == 0)
            # 次日一字涨停 / 大幅跳空：买不进
            & ~((pl.col("entry_gap") > 0.095) & (pl.col("entry_raw_high") == pl.col("entry_raw_low")))
            & (pl.col("entry_gap") < 0.095)
        ).then(1).otherwise(0).alias("ok"),
        (pl.col("di") % 5 == 0).alias("anchor"),
    )
    # 主板内部等权基准（主口径；ex20 为全 A 等权，留作稳健性）
    mbm = (
        df.filter(pl.col("ok") == 1)
        .group_by("date")
        .agg(pl.col("fwd20").mean().alias("bench_mb"))
    )
    df = df.join(mbm, on="date", how="left").with_columns(
        (pl.col("fwd20") - pl.col("bench_mb")).alias("ex20_mb")
    )

    keep = [
        "symbol", "date", "di", "industry", "ok", "anchor",
        "fwd20", "ex20", "ex20_mb", "bench20", "entry_gap",
        # static
        "log_mcap", "log_price", "age_years", "is_sh",
        # dynamic
        "ret20", "ret5", "vol20", "skew20", "max_ret20", "ar1_20",
        "turn20", "turn_chg", "turn_cv20", "log_amt20", "log_amihud", "amt_chg",
        "upvol_ratio", "corr_pv", "range20", "range_vol_ratio", "limit_up_cnt20",
        "quiet_cnt20",         "overnight20", "intraday20", "dist_high250", "beta60", "idio_vol60",
        "turn20_fwd", "vol20_fwd",
    ]
    out = df.filter(
        pl.col("anchor") & (pl.col("di") >= 250) & (pl.col("date") >= SIGNAL_START)
    ).select(keep)
    out.write_parquet(OUT / "panel.parquet")
    print(
        "panel", out.shape, "dates", out["date"].n_unique(),
        "symbols", out["symbol"].n_unique(), "ok", int(out["ok"].sum()),
    )


def build_intraday() -> None:
    """逐日算 5min 拆单代理，输出票-日一行的表。"""
    files = sorted(MIN5.glob("date=*/part.parquet"))
    rows = []
    for i, f in enumerate(files):
        d = pl.read_parquet(f)
        if d.height == 0:
            continue
        d = d.sort(["symbol", "datetime"]).with_columns(
            (pl.col("close") / pl.col("open") - 1).alias("r"),
            (pl.col("amount") / pl.col("amount").sum().over("symbol")).alias("p"),
            pl.int_range(1, pl.len() + 1).over("symbol").alias("bar"),
            pl.col("amount").median().over("symbol").alias("a_med"),
        )
        d = d.with_columns(
            pl.col("volume").shift(1).over("symbol").alias("prev_v"),
            pl.col("r").shift(1).over("symbol").alias("prev_r"),
        )
        g = d.group_by("symbol").agg(
            pl.len().alias("n_bars"),
            (pl.col("amount") * pl.col("r")).sum().alias("sf_num"),
            pl.col("amount").sum().alias("amt"),
            pl.col("volume").sum().alias("vol"),
            pl.col("close").last().alias("last_close"),
            pl.col("open").first().alias("first_open"),
            pl.col("high").max().alias("day_high"),
            pl.col("low").min().alias("day_low"),
            pl.col("r").std().alias("r_std"),
            (pl.col("p").pow(2)).sum().alias("hhi"),
            pl.col("p").max().alias("top_share"),
            (pl.when(pl.col("r") > 0).then(pl.col("volume")).otherwise(0)).sum().alias("up_v"),
            (pl.col("volume").std() / pl.col("volume").mean()).alias("vol_cv"),
            pl.corr("r", "prev_r").alias("ret_ac1"),
            pl.corr("volume", "prev_v").alias("vol_ac1"),
            pl.corr("r", "volume").alias("corr_rv"),
            pl.corr((pl.col("r").sign() * pl.col("volume")),
                    (pl.col("prev_r").sign() * pl.col("prev_v"))).alias("signed_vol_ac1"),
            (pl.col("amount").filter(pl.col("bar") <= 6).sum()
             / pl.col("amount").sum()).alias("open_share"),
            (pl.col("amount").filter(pl.col("bar") > 42).sum()
             / pl.col("amount").sum()).alias("tail_share"),
            (pl.col("close").filter(pl.col("bar") == 6).last()
             / pl.col("open").first() - 1).alias("first30_ret"),
            (pl.col("close").last()
             / pl.col("close").filter(pl.col("bar") == 42).last() - 1).alias("last30_ret"),
            (pl.col("amount").filter(pl.col("amount") > 3 * pl.col("a_med"))
             .sum() / pl.col("amount").sum()).alias("big_share"),
            (-(pl.col("p") * pl.col("p").log()).sum() / math.log(48)).alias("entropy"),
            (pl.col("r").abs() / (pl.col("amount") / 1e8)).mean().alias("illiq"),
        )
        dd = f.parent.name.split("=")[1]
        g = g.with_columns(
            (pl.col("sf_num") / pl.col("amt")).alias("sf"),
            (pl.col("last_close") / (pl.col("amt") / (pl.col("vol") * 100)) - 1).alias("vwap_dev"),
            ((pl.col("last_close") - pl.col("day_low"))
             / (pl.col("day_high") - pl.col("day_low")).clip(lower_bound=1e-9)).alias("clv"),
            (pl.col("last_close") / pl.col("first_open") - 1).alias("day_ret"),
            ((pl.col("day_high") - pl.col("day_low"))
             / (pl.col("amt") / (pl.col("vol") * 100))).alias("day_range"),
            (pl.col("up_v") / pl.col("vol")).alias("up_vol_share"),
            pl.lit(dd).str.to_date().alias("date"),
        ).select(
            "symbol", "date", "n_bars", "sf", "open_share", "tail_share", "vwap_dev", "clv",
            "hhi", "top_share", "up_vol_share", "ret_ac1", "vol_ac1", "corr_rv",
            "signed_vol_ac1", "vol_cv", "entropy", "first30_ret", "last30_ret",
            "big_share", "day_range", "illiq", "day_ret", "r_std",
        )
        rows.append(g)
        if (i + 1) % 100 == 0:
            print("intraday", i + 1, "/", len(files), flush=True)
    out = pl.concat(rows).sort(["symbol", "date"])
    out.write_parquet(OUT / "intraday_daily.parquet")
    print("intraday", out.shape, out["date"].n_unique())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["build", "intraday", "both"], default="build")
    a = ap.parse_args()
    if a.stage in ("build", "both"):
        build_panel()
    if a.stage in ("intraday", "both"):
        build_intraday()
