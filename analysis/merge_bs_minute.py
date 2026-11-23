"""把 backfill_bs_minute 下载的 baostock 分钟（原始价、volume 股）整理成可拼接的面板。

**口径前提（2026-11-19 实测修正，旧注释是错的）**
本地 `kline_5min` 存的是**原始价**，不是前复权价。证据：601127.SH 2024-11-07
本地 5min 末根 close = 135.87 = 日线 `raw_close`；经 `apply_minute_adjustment`
投影后才等于日线 `close` = 132.959934（精确到 1e-6）。原因是本地按「拉取当日
前复权」落盘，而当日 ratio 恒为 1 ⇒ 等于当天的原始价。

所以本项目**事实层统一存原始价**，两段都存原始价，读取时用 `minute_union.load(adjust=True)`
统一投影。若这里输出前复权价，会在边界处产生股息比例大小的跳空
（601127 是 2.19%，几乎所有分红股都有），而且不报错。

单位换算：
  1. volume 股→手（÷100）。实测与本地日线 volume 比值 median=1.0000。
  2. amount 缺失（下载端因服务端慢查询不取），用 volume × close × 100 近似。
     本地实测 amount/(volume×close) median = 100.0037 ⇒ **必须乘 100**，
     少乘不会报错，只会让成交额/VWAP 静默错 100 倍。

输出仍写独立目录 `f5_merged/`，**不要**塞进 `data/kline_5min/date=*/`（同日读两遍事故）。

用法:
    python analysis/merge_bs_minute.py --list        # 查看已下载清单
    python analysis/merge_bs_minute.py --verify      # 重叠段验证（bs vs 本地日线）
    python analysis/merge_bs_minute.py --all         # 全量整理（原始价）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import polars as pl  # noqa: E402

ROOT = Path(r"D:/DSH/tick-stock-panel")
DATA = ROOT / "data"
SRC = DATA / "backfill_bs_minute" / "f5"
DST = DATA / "backfill_bs_minute" / "f5_merged"
OUT = DATA / "backfill_bs_minute"

# 本地 kline_5min 的 schema（实测）
LOCAL_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


def load_bs(symbol: str) -> pl.DataFrame:
    p = SRC / f"{symbol}.parquet"
    if not p.exists():
        return pl.DataFrame()
    df = pl.read_parquet(p)
    # 整日零成交 = 停牌占位，不是行情。baostock 对停牌日会返回 1 根
    # open=high=low=close、volume=0 的假 bar，它和「一字板」长得一模一样：
    # 全天一个价、无量。直接喂给封板/涨停检测会变成假涨停，且不报错。
    # 实测已下载的 449 只里有 32 个这样的 只-日（全部 1 根、日内振幅 0）。
    # 只删**整日合计为 0** 的日子，不删日内个别零量 bar —— 高价低流动性票
    # （如 300799 日均 700 手）正常有大量零量 bar，删掉会把成交量打低。
    df = df.with_columns(pl.col("datetime").dt.date().alias("_d"))
    df = (df.with_columns(pl.col("volume_raw").sum().over("_d").alias("_dayv"))
            .filter(pl.col("_dayv") > 0)
            .drop(["_d", "_dayv"]))
    # volume 股 → 手
    df = df.with_columns((pl.col("volume_raw") / 100.0).alias("volume"))
    # amount 缺（下载端因服务端慢查询未取），用 volume × close 近似补齐。
    # 单位必须是「手 × 元 × 100 = 元」：本地 kline_5min 实测
    # amount/(volume×close) median = 100.0037（2026-11-16 全市场 2000 行抽样）。
    # 少乘 100 不会报错，只会让所有成交额/VWAP 分析静默错 100 倍。
    df = df.with_columns((pl.col("volume") * pl.col("close") * 100.0).alias("amount"))
    return df.select(LOCAL_COLS)


def apply_adjust(df: pl.DataFrame) -> pl.DataFrame:
    """原始价 → 前复权投影。复用项目的复权机制。"""
    from backend.app.services.minute_adjust import apply_minute_adjustment
    return apply_minute_adjustment(df, DATA, asset_type="stock")


def cmd_list() -> None:
    files = sorted(SRC.glob("*.parquet"))
    print(f"已下载 {len(files)} 只")
    total_rows = 0
    for f in files[:10]:
        n = pl.read_parquet(f, columns=["datetime"]).height
        total_rows += n
        print(f"  {f.stem}: {n} 根")
    if len(files) > 10:
        print(f"  ... 共 {len(files)} 只")


def cmd_verify() -> None:
    """重叠段验证：baostock 原始价 vs 本地 5min 原始价 / vs 日线 raw_close。"""
    sym = "601127.SH"
    df = load_bs(sym)
    if df.is_empty():
        print(f"{sym} 未下载（用 --limit 1 先下）")
        return
    d0, d1 = "2024-11-04", "2024-11-06"
    sub = df.filter(pl.col("datetime").dt.date().is_between(
        pl.lit(d0).str.to_date(), pl.lit(d1).str.to_date()))
    days = ["2024-11-04", "2024-11-05", "2024-11-06"]
    dl = pl.concat([
        pl.read_parquet(DATA / "kline_daily_enriched" / f"date={d}" / "part.parquet",
                        columns=["symbol", "date", "close", "raw_close"])
          .filter(pl.col("symbol") == sym) for d in days])
    lb = sub.group_by(pl.col("datetime").dt.date().alias("date")).agg(
        pl.col("close").last().alias("mclose"), pl.col("volume").sum().alias("mv"),
        pl.col("amount").sum().alias("ma"))
    j = lb.join(dl, on="date")
    print(f"{sym} {d0}~{d1}")
    print(f"  |mclose - raw_close| max = "
          f"{(j['mclose'] - j['raw_close']).abs().max():.6f}  (应 0)")
    print(f"  |mclose - close|     max = "
          f"{(j['mclose'] - j['close']).abs().max():.6f}  (应 >0：close 是前复权)")


def cmd_panel_verify() -> None:
    """全批次自证：已合并段的 close 必须等于日线 raw_close（原始口径）。"""
    from pathlib import Path as _P
    files = sorted(DST.glob("*.parquet"))
    g = pl.read_parquet(DATA / "backfill_20261117/stock_gap_daily.parquet",
                        columns=["symbol", "date", "close"]).rename({"close": "raw_close"})
    # 该文件本身就是原始价（backfill_20261117 只取东财 fqt=0），无 raw_close 列
    bad = []
    n = 0
    for f in files[:400]:
        d = pl.read_parquet(f)
        lb = (d.group_by(pl.col("datetime").dt.date().alias("date"))
              .agg(pl.col("close").last().alias("mclose")))
        j = lb.join(g.filter(pl.col("symbol") == f.stem), on="date", how="inner")
        if j.height == 0:
            continue
        n += j.height
        # 基准错的签名是「所有日同比例偏移」（股息比例）；流动性造成的错价
        # 只影响部分日且无系统符号。所以判据用**相对差的中位数**，不用比例。
        rel = ((j["mclose"] / j["raw_close"] - 1).abs())
        if float(rel.median()) > 0.005:
            bad.append((f.stem, j.height, round(float(rel.median()), 4)))
    print(f"抽检 {min(len(files),400)} 只 / {n:,} 只-日：基准疑似不一致的票 {len(bad)} 只"
          f"（判据 median|相对差| > 0.5%）")
    for b in bad[:8]:
        print("  ", b)


def cmd_merge(adjust: bool = False) -> None:
    files = sorted(SRC.glob("*.parquet"))
    DST.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    idx = []
    for i, f in enumerate(files):
        sym = f.stem
        df = load_bs(sym)
        if df.is_empty():
            continue
        out = apply_adjust(df) if adjust else df
        out.write_parquet(DST / f"{sym}.parquet")
        idx.append({"symbol": sym, "first": out["datetime"].min(),
                    "last": out["datetime"].max(), "rows": out.height})
        if i % 200 == 0:
            print(f"  [{i}/{len(files)}] {sym} {out.height} 根 {time.time()-t0:.0f}s", flush=True)
    # 索引落在 OUT 根目录而不是 f5_merged（后者被 *.parquet 通配，多一个文件会被当成标的）
    pl.DataFrame(idx).write_parquet(OUT / "_bs_index.parquet")
    print(f"完成 {len(files)} 只 → {DST}（{'前复权' if adjust else '原始价'}）, "
          f"耗时 {time.time()-t0:.0f}s；索引 → {OUT/'_bs_index.parquet'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--panel-verify", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--adjust", action="store_true", help="输出前复权（默认原始价）")
    args = ap.parse_args()
    if args.list:
        cmd_list()
    elif args.verify:
        cmd_verify()
    elif args.panel_verify:
        cmd_panel_verify()
    elif args.all:
        cmd_merge(args.adjust)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
