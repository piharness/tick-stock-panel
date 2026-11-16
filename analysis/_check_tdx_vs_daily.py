"""端到端对账: 通达信源 (含反前复权校准) vs 本地原始日线 (fuyao)。

这是新数据源的验收线, 不是可选项 —— 通达信分钟默认是**前复权**, 且基准是
"下一个除权日": 实测 600519 在 2026-06-25 的分钟收盘 1184.076 比原始价
1212.10 低 28.024 (正是次日每股分红); 000426 在 2026-11-12 分钟 23.68 比
原始价 23.78 低 0.10 (次日除权)。不做校准直接落盘, 除权股历史分钟会整体
错一个分红幅度, 并与日线复权叠加成二次复权。

本脚本逐只比对:
  open/high/low/close 与日线逐位相等 (容差 0.011 元, 覆盖最小价位 0.01)
  volume (手) 相对差 ≤0.5% (逐根 floor 的舍入)
  amount 相对差 ≤0.5%

运行 (需 Python 3.11 + tdxrs, 即 Docker 内环境):
    DATA_DIR=<repo>/data python analysis/_check_tdx_vs_daily.py [N]
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import settings  # noqa: E402
from app.plugins.tdx.provider import TdxProvider  # noqa: E402

MAX_N = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def main() -> int:
    root = settings.data_dir / "kline_daily"
    day = max(p.name.split("=")[1] for p in root.glob("date=*"))
    stored = pl.read_parquet(root / f"date={day}" / "part.parquet")
    stored = stored.filter(pl.col("symbol").str.contains(r"\.(SH|SZ)$"))
    syms = stored["symbol"].to_list()[:MAX_N]

    p = TdxProvider()
    df = p.get_minute(syms, datetime.strptime(day, "%Y-%m-%d"), None)
    if df.is_empty():
        print("无分钟数据 (检查日线分区或行情主站)")
        return 2

    agg = df.group_by("symbol").agg([
        pl.col("open").first().alias("open_t"),
        pl.col("high").max().alias("high_t"),
        pl.col("low").min().alias("low_t"),
        pl.col("close").last().alias("close_t"),
        pl.col("volume").sum().alias("vol_t"),
        pl.col("amount").sum().alias("amt_t"),
    ])
    j = stored.join(agg, on="symbol")
    j = j.with_columns(
        (pl.col("close_t") - pl.col("close")).abs().alias("d_close"),
        (pl.col("high_t") - pl.col("high")).abs().alias("d_high"),
        (pl.col("low_t") - pl.col("low")).abs().alias("d_low"),
        (pl.col("open_t") - pl.col("open")).abs().alias("d_open"),
        ((pl.col("vol_t") - pl.col("volume")).abs()
         / pl.max_horizontal(pl.col("volume").abs(), pl.lit(1.0))).alias("d_vol"),
        ((pl.col("amt_t") - pl.col("amount")).abs()
         / pl.max_horizontal(pl.col("amount").abs(), pl.lit(1.0))).alias("d_amt"),
    )
    n = j.height
    bad_price = j.filter(
        (pl.col("d_close") > 0.011) | (pl.col("d_high") > 0.011)
        | (pl.col("d_low") > 0.011) | (pl.col("d_open") > 0.011)
    )
    bad_vol = j.filter(pl.col("d_vol") > 0.005)
    bad_amt = j.filter(pl.col("d_amt") > 0.005)
    print(f"{day}: 对账 {n} 只 (请求 {len(syms)} 只, 缺 {len(syms) - n} 只因无日线/停牌)")
    print(f"  OHLC 不一致: {bad_price.height}  (max d_close {j['d_close'].max():.4f})")
    print(f"  volume 相对差 >0.5%: {bad_vol.height}  (max {j['d_vol'].max():.5f})")
    print(f"  amount 相对差 >0.5%: {bad_amt.height}  (max {j['d_amt'].max():.5f})")
    if bad_price.height:
        print(bad_price.select("symbol", "close", "close_t", "high", "high_t", "volume", "vol_t").head(10))
    ok = bad_price.height == 0 and bad_vol.height <= n * 0.02 and bad_amt.height <= n * 0.02
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
