"""放量前的两道闸门（对现存股口径，退市股已验证过）：
  A. 成本是否与行数线性 → 决定分块/排序是否有意义
  B. 复权投影后 5min 收盘 vs 本地日线「前复权 close」是否对齐 → 决定 19h 是否白跑

用法: python analysis/_probe_bs_merge_gate.py
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:/DSH/tick-stock-panel")
sys.path.insert(0, r"D:/DSH/tick-stock-panel/data/_probe_t0")
sys.path.insert(0, r"D:/DSH/tick-stock-panel/data/_probe_t0/_extlib")

import polars as pl  # noqa: E402
from _fixed_bs import FixedBaostock  # noqa: E402

DATA = Path(r"D:/DSH/tick-stock-panel/data")


def pull(c, code, start, end):
    t0 = time.time()
    r = c.query_k(code, "date,time,open,high,low,close,volume", start, end,
                  frequency="5", adjustflag="3")
    return r, time.time() - t0


def to_df(r, symbol):
    df = pl.DataFrame(r["rows"],
                      schema=["date", "time", "open", "high", "low", "close", "volume"],
                      orient="row")
    return (df.with_columns(pl.col("time").str.slice(0, 14)
                            .str.to_datetime("%Y%m%d%H%M%S").alias("datetime"))
            .with_columns([pl.col(c).cast(pl.Float64) for c in ["open", "high", "low", "close"]])
            .with_columns(pl.col("volume").cast(pl.Float64).alias("volume_raw"))
            .select(["datetime", "open", "high", "low", "close", "volume_raw"])
            .filter(pl.col("close") > 0)
            .with_columns(pl.lit(symbol).alias("symbol"))
            .sort("datetime"))


def main():
    socket.setdefaulttimeout(60)
    c = FixedBaostock()
    assert c.login(), "login 失败"

    print("=== A. 成本线性度 ===", flush=True)
    for label, s, e in [("1 年 2020", "2020-01-01", "2020-12-31"),
                        ("5 年 2020-2024", "2020-01-02", "2024-11-06")]:
        r, dt = pull(c, "sz.000001", s, e)
        n = len(r["rows"])
        print(f"  {label:<16} {n:>6} 行 {dt:>6.1f}s  {n/dt:>6.0f} 行/s", flush=True)

    print("\n=== B. 复权投影对齐（000001 2020 全年）===", flush=True)
    r, dt = pull(c, "sz.000001", "2020-01-01", "2020-12-31")
    raw = to_df(r, "000001.SZ")
    print(f"  下载 {raw.height} 根 {dt:.0f}s", flush=True)

    from backend.app.services.minute_adjust import apply_minute_adjustment
    adj = apply_minute_adjustment(raw, DATA, asset_type="stock")
    ratio = (adj["close"] / raw["close"]).to_list()
    print(f"  投影 ratio: min={min(ratio):.6f} max={max(ratio):.6f} "
          f"unique={len(set(round(x, 6) for x in ratio))}", flush=True)

    # 本地日线：close 是前复权，raw_close 是原始价
    days = [f"date={d}" for d in sorted({str(x)[:10] for x in raw['datetime'].dt.date().to_list()})]
    dl = pl.concat([
        pl.read_parquet(DATA / "kline_daily_enriched" / d / "part.parquet",
                        columns=["symbol", "date", "close", "raw_close"])
          .filter(pl.col("symbol") == "000001.SZ")
        for d in days if (DATA / "kline_daily_enriched" / d / "part.parquet").exists()
    ])
    lastbar = (adj.sort("datetime").group_by(pl.col("datetime").dt.date().alias("date"))
               .agg(pl.col("close").last().alias("mclose")))
    j = lastbar.join(dl, on="date", how="inner")
    d1 = (j["mclose"] - j["close"]).abs()
    d2 = (raw.sort("datetime").group_by(pl.col("datetime").dt.date().alias("date"))
          .agg(pl.col("close").last().alias("raw_mclose"))
          .join(dl, on="date").with_columns(
              (pl.col("raw_mclose") - pl.col("raw_close")).abs().alias("d")).get_column("d"))
    print(f"  对齐 {j.height} 日")
    print(f"  前复权：max|Δ|={d1.max():.6f}  >0.001 占比={float((d1>0.001).mean()):.4f}")
    print(f"  原始价：max|Δ|={d2.max():.6f}  >0.001 占比={float((d2>0.001).mean()):.4f}")
    bad = j.with_columns((pl.col("mclose")-pl.col("close")).abs().alias("dd")).filter(pl.col("dd")>0.001)
    if bad.height:
        print(bad.head(5).select(["date", "mclose", "close", "raw_close"]))
    c.logout()


if __name__ == "__main__":
    main()
