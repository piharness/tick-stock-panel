"""拉取通达信日线全历史 (1991-04-03 起), 存为**前复权**归档, 并输出与原始价的换算比。

## 为什么不能直接写进 kline_daily

实测 (2026-11-16, 000001.SZ / 600519.SH):

| | 2005-01-04 close | 2026-11-16 close |
|---|---|---|
| 本地 kline_daily (原始价) | 6.52 / 36.45 | 12.30 / 1453.90 |
| 通达信 category=9 | 0.883 / 0.322 | 12.30 / 1453.90 |

⇒ 通达信日线是**前复权**, 基准是今天; 本仓 kline_daily 契约是**原始价**。
两者只在最后一个除权日之后重合, 1991-2004 段差一个逐票不同的常数因子。

## 于是本归档的口径

存**完整前复权序列** (1991 → 今), 一份自洽口径, 而不是把 1991-2004
除以常数硬塞进原始价表 —— 后者在 1991-2000 那段会失真: 早期送股拆股极频繁,
前复权序列在除权日已被平滑, 除以单点因子**不会**把跳变还原回来。

同时输出 `_ratio.parquet` (symbol, anchor_date, ratio):
    ratio = 前复权close(anchor) / 原始close(anchor)
用于把本归档换算回原始价 (`原始 = 前复权 / ratio`), 或在重叠期做交叉校验。
anchor 取该票在本地 kline_daily 里的第一个交易日 (2005 起)。

## 什么时候重跑

日线是**可全量重拉**的 (8492 根/票, 5892 只, 6 分片约 8 分钟), 不存在分钟表那种
"滚出窗口就没了"的问题。重跑会整表重写 `month=YYYY-MM/part.parquet` 并重算
`_calib/qfq_ratio.parquet`。

## 跑在哪

容器内 (依赖 tdxrs, 仅 cp311)。分片与合并:
    docker exec ... --shard 0 --of 6 --chunk 300        # 六个分片并行, 约 8 分钟
    docker exec ... --merge

量纲已核对 (000001.SZ 2026-11-13): OHLC 逐位相等, vol 与本地 volume 同为
"手" (差 1 为舍入), amount 同为元 ⇒ 不需要 `_hand()` 转换。
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import date
from pathlib import Path

import polars as pl

DATA = Path(os.environ.get("TICK_DATA_DIR", "/app/data"))
OUT = DATA / "archive" / "kline_daily_qfq"
TMP = DATA / "archive" / "_tmpdaily"
CALIB = DATA / "archive" / "_calib"
CATEGORY_DAILY = 9
PAGE_ROWS = 800
MAX_PAGES = 12

SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}


def _symbols() -> list[str]:
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet", columns=["symbol"])
    return sorted(set(inst["symbol"].to_list()))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(sym: str, raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in raw:
        ds = str(r.get("datetime") or "")[:10]
        if len(ds) != 10:
            continue
        o, h, lo, c = (_f(r.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, lo, c):
            continue
        out.append({
            "symbol": sym,
            "date": date.fromisoformat(ds),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": _f(r.get("vol")),
            "amount": _f(r.get("amount")),
        })
    return out


def run_shard(shard: int, of: int, chunk: int) -> int:
    from tdxrs import TdxHqClient
    from app.plugins.tdx.provider import to_tdx

    syms = _symbols()
    if of > 1:
        syms = syms[shard::of]
    print(f"[shard {shard}/{of}] symbols={len(syms)}", flush=True)
    TMP.mkdir(parents=True, exist_ok=True)
    target = TMP / f"shard{shard}.parquet"
    if target.exists():
        print(f"[shard {shard}] 已存在, 跳过", flush=True)
        return 0

    client = TdxHqClient()
    client.connect_to_any()
    frames: list[pl.DataFrame] = []
    t0 = time.time()
    done = 0
    empty = 0
    for sym in syms:
        mapped = to_tdx(sym)
        got: list[dict] = []
        if mapped is not None:
            market, code = mapped
            for page in range(MAX_PAGES):
                try:
                    r = client.get_security_bars(CATEGORY_DAILY, market, code, page * PAGE_ROWS, PAGE_ROWS)
                except BaseException:  # noqa: BLE001
                    break
                if not r:
                    break
                got.extend(r)
                if len(r) < PAGE_ROWS:
                    break
        rows = _rows(sym, got) if got else []
        done += 1
        if not rows:
            empty += 1
        else:
            frames.append(pl.DataFrame(rows, schema=SCHEMA))
        if done % chunk == 0 or done == len(syms):
            el = time.time() - t0
            print(
                f"[shard {shard}] {done}/{len(syms)} empty={empty} rows={sum(f.height for f in frames):,} "
                f"elapsed={el/60:.1f}min eta={(el/max(done,1)*(len(syms)-done))/60:.1f}min",
                flush=True,
            )
            if frames:
                pl.concat(frames, how="vertical_relaxed").write_parquet(target)
    if frames:
        pl.concat(frames, how="vertical_relaxed").write_parquet(target)
    print(f"[shard {shard}] done -> {target}", flush=True)
    return 0


def run_merge() -> int:
    parts = sorted(TMP.glob("shard*.parquet"))
    print(f"[merge] shards={len(parts)}")
    if not parts:
        return 1
    df = pl.concat([pl.read_parquet(p) for p in parts], how="vertical_relaxed")
    df = df.unique(subset=["symbol", "date"], keep="last").sort("date", "symbol")
    print(f"[merge] rows={df.height:,} syms={df['symbol'].n_unique()} dates={df['date'].n_unique()} "
          f"{df['date'].min()} .. {df['date'].max()}")

    # 原始价锚点: 本地 kline_daily 里每只票的第一个交易日 close
    kd = pl.scan_parquet(
        str(DATA / "kline_daily" / "**" / "*.parquet"), extra_columns="ignore"
    ).select(["symbol", "date", "close"])
    anchor = kd.group_by("symbol").agg(pl.col("date").min().alias("anchor_date")).collect()
    raw = (
        kd.collect()
        .join(anchor, on="symbol")
        .filter(pl.col("date") == pl.col("anchor_date"))
        .select(["symbol", "date", pl.col("close").alias("raw_close")])
    )
    qfq = df.join(raw, on=["symbol", "date"], how="inner").select(
        ["symbol", "date", "raw_close", pl.col("close").alias("qfq_close")]
    )
    ratio = qfq.with_columns((pl.col("qfq_close") / pl.col("raw_close")).alias("ratio"))
    CALIB.mkdir(parents=True, exist_ok=True)
    ratio.write_parquet(CALIB / "qfq_ratio.parquet")
    print(f"[ratio] {ratio.height} 只, ratio 中位 {ratio['ratio'].median():.6f}")
    dev = ratio.filter((pl.col("ratio") - 1.0).abs() > 0.02)
    print(f"[ratio] 偏离 1 超过 2% 的 {dev.height} 只 (说明确实存在历史除权)")

    OUT.mkdir(parents=True, exist_ok=True)
    df = df.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("_m")).sort("_m", "symbol")
    total = 0
    for key, g in df.partition_by("_m", as_dict=True).items():
        ym = str(key[0]) if isinstance(key, tuple) else str(key)
        out = OUT / f"month={ym}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        g.drop("_m").write_parquet(out)
        total += g.height
    print(f"[merge] done rows={total:,} months={df['_m'].n_unique()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=300)
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()
    if args.merge:
        return run_merge()
    return run_shard(args.shard, args.of, args.chunk)


if __name__ == "__main__":
    raise SystemExit(main())
