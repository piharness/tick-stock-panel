"""把通达信的多周期分钟 K 归档成本地表 (运维脚本, 不是常驻服务)。

## 什么时候要跑

上游只保留「最近 N 根」, 且**每只票的边界不同** (2026-11-16 实测: 同一时刻
抽样 6-22 / 6-23 / 6-25 / 6-30 / 7-03 都有)。所以这不是一次性任务:

- 首次: 把所有周期拉满 (5m 约 482 个交易日 / 15m·30m·60m 约 500 个);
- 之后: 上游每天向前滚一天, 想保持「最近 N 根」就得定期重跑。不重跑就是一份
  固定在归档当天的快照, 不会自己长大。

表是**只读归档**: 没有盘中落盘、没有自动补齐, 与 kline_minute 的增量链路无关。

## 跑在哪

必须在**容器内**跑 —— 依赖 tdxrs (仅 cp311 wheel, 宿主 venv 是 3.12):

    docker exec TickFlow_Stock_Panel /app/.venv/bin/python /tmp/archive_intraday_periods.py --prepare
    docker exec ... --period 5m --shard 0 --of 6 --chunk 300      # 六个分片并行
    docker exec ... --period 5m --merge

分片是**进程级**的: tdxrs 不释放 GIL, 同一进程里开多线程与单线程等速
(2026-11-16 实测: 4 线程 1.8 分钟 vs 1 线程 1.7 分钟, 处理同样 12 只)。

## 耗时参考 (6 分片, 982 只/片, 2026-11-16 实测)

    5m   ~2.2h      15m  ~45min      30m  ~22min      60m  ~11min

## 口径

原始价 (与 kline_minute 同契约)。复用 `_minute_rows` (含 13:00 错标修正) 与
`_calibrate_by_day` (按当日原始日线收盘逐日反算, 缺锚点 fail-closed 不猜)。
`--prepare` 预导出日线收盘价查表: 不预导出的话 `_daily_close` 的 256 条缓存
装不下 500 天窗口, 每个标的都会重读 500 个分区 (实测 44s/只, 网络只占 8.4s)。
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import date
from pathlib import Path

import polars as pl

DATA = Path(os.environ.get("TICK_DATA_DIR", "/app/data"))
OUT = DATA / "archive" / "kline_5min"
TMP = DATA / "archive" / "_tmp5m"
CATEGORY_5MIN = 0
PAGE_ROWS = 800
MAX_PAGES = 31

#: 周期名 → (协议 category, 输出子目录, 临时目录, 翻页上限)
#: 上游各周期保留约 500 交易日, 但根数上限不同 ⇒ 页数上限也不同:
#: 5min 24000 根=30 页, 15min 8000 根=10 页, 30min 4000=5, 60min 2000=3。
PERIODS = {
    "5m": (0, "kline_5min", "_tmp5m", 31),
    "15m": (1, "kline_15min", "_tmp15m", 11),
    "30m": (2, "kline_30min", "_tmp30m", 6),
    "60m": (3, "kline_60min", "_tmp60m", 4),
}


def _symbols() -> list[str]:
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet", columns=["symbol"])
    return sorted(set(inst["symbol"].to_list()))


def prepare_daily_close() -> int:
    """把 5 分钟窗口内的原始日线收盘价导成单文件, 供各 shard 内存查表。

    为什么必须预导出: `_calibrate_by_day` 逐日查锚点, 而 provider 的
    `_daily_close` 走 `locate_partition_dir` 一次一个分区、只有 256 条缓存。
    5 分钟窗口 ~500 天 > 256 ⇒ 每个标的都把 500 个分区重新读一遍 (9p 上一次
    分区读 ~50ms), 实测 10 只耗时 7.3 分钟, 其中 ~6 分钟是重复读日线。
    """
    base = DATA / "kline_daily"
    frames: list[pl.DataFrame] = []
    dirs = sorted(d for d in base.iterdir() if d.is_dir() and d.name.startswith("date="))
    for d in dirs:
        files = sorted(d.glob("*.parquet"))
        if not files:
            continue
        try:
            df = pl.concat(
                [pl.read_parquet(f, columns=["symbol", "date", "close"]) for f in files],
                how="diagonal_relaxed",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[prepare] 跳过 {d.name}: {e}")
            continue
        df = df.with_columns(
            pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("date")
        ).filter(pl.col("date") >= pl.lit("2024-09-01").str.to_date())
        if not df.is_empty():
            frames.append(df)
    if not frames:
        print("[prepare] 无数据")
        return 1
    out = pl.concat(frames, how="diagonal_relaxed").unique(subset=["symbol", "date"], keep="last")
    out = out.sort("date", "symbol")
    calib = DATA / "archive" / "_calib"
    calib.mkdir(parents=True, exist_ok=True)
    target = calib / "daily_close.parquet"
    out.write_parquet(target)
    print(f"[prepare] rows={out.height:,} dates={out['date'].n_unique()} -> {target}")
    return 0


_DAILY: dict[str, pl.DataFrame] | None = None


def _load_daily() -> dict[str, pl.DataFrame]:
    global _DAILY
    if _DAILY is not None:
        return _DAILY
    src = DATA / "archive" / "_calib" / "daily_close.parquet"
    if not src.exists():
        src = TMP / "daily_close.parquet"
    if not src.exists():
        raise SystemExit("缺少 daily_close.parquet, 先跑 --prepare")
    df = pl.read_parquet(src)
    # group_by 的迭代键是元组 ("2026-11-12",), 不是字符串: 直接 str(k) 会拼成
    # "('2026-11-12',)" ⇒ 查表恒 None ⇒ 全部标的 fail-closed 跳过。
    _DAILY = {
        str(k[0]): v.select(["symbol", "close"])
        for k, v in df.group_by(pl.col("date").cast(pl.String))
    }
    return _DAILY


def _patch_daily_cache() -> None:
    """把 provider 的逐分区读取换成内存查表 (只换数据来源, 判据不动)。"""
    import app.plugins.tdx.provider as prov

    cache = _load_daily()

    def _fast(day: str):
        return cache.get(day)

    prov._daily_close = _fast


def _fetch_one(client, sym: str) -> pl.DataFrame:
    from app.plugins.tdx.provider import _calibrate_by_day, _frame, _minute_rows, to_tdx

    mapped = to_tdx(sym)
    if mapped is None:
        return pl.DataFrame()
    market, code = mapped
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        try:
            got = client.get_security_bars(CATEGORY_5MIN, market, code, page * PAGE_ROWS, PAGE_ROWS)
        except BaseException:  # noqa: BLE001 — tdxrs panic 是 BaseException
            break
        if not got:
            break
        rows.extend(got)
        if len(got) < PAGE_ROWS:
            break
    if not rows:
        return pl.DataFrame()
    try:
        kept = _calibrate_by_day(sym, _minute_rows(sym, rows))
    except BaseException:  # noqa: BLE001
        return pl.DataFrame()
    if not kept:
        return pl.DataFrame()
    return _frame(kept)


def run_shard(shard: int, of: int, chunk: int) -> int:
    from tdxrs import TdxHqClient

    _patch_daily_cache()
    syms = _symbols()
    if of > 1:
        syms = syms[shard::of]
    print(f"[shard {shard}/{of}] symbols={len(syms)}", flush=True)
    TMP.mkdir(parents=True, exist_ok=True)
    target = TMP / f"shard{shard}.parquet"
    if target.exists():
        print(f"[shard {shard}] 已存在 {target}, 跳过", flush=True)
        return 0

    client = TdxHqClient()
    client.connect_to_any()
    frames: list[pl.DataFrame] = []
    t0 = time.time()
    done = 0
    empty = 0
    for sym in syms:
        df = _fetch_one(client, sym)
        done += 1
        if df.is_empty():
            empty += 1
        else:
            frames.append(df)
        if done % chunk == 0 or done == len(syms):
            el = time.time() - t0
            print(
                f"[shard {shard}] {done}/{len(syms)} empty={empty} rows={sum(f.height for f in frames):,} "
                f"elapsed={el/60:.1f}min eta={(el/max(done,1)*(len(syms)-done))/60:.1f}min",
                flush=True,
            )
            # 中途落盘: 2 小时的单进程不能只靠结束时那一次写
            if frames:
                snapshot = pl.concat(frames, how="vertical_relaxed")
                snapshot.write_parquet(target)
    if frames:
        pl.concat(frames, how="vertical_relaxed").write_parquet(target)
    print(f"[shard {shard}] done rows={sum(f.height for f in frames):,} -> {target}", flush=True)
    return 0


def run_merge() -> int:
    parts = sorted(TMP.glob("shard*.parquet"))
    print(f"[merge] shards={len(parts)}")
    if not parts:
        return 1
    dates: set[str] = set()
    for p in parts:
        for r in pl.scan_parquet(p).select(pl.col("datetime").dt.date().alias("d")).unique().collect().iter_rows():
            dates.add(str(r[0]))
    ordered = sorted(dates)
    print(f"[merge] distinct dates={len(ordered)}  {ordered[0]} .. {ordered[-1]}")
    OUT.mkdir(parents=True, exist_ok=True)
    common = None
    for p in parts:
        cols = set(pl.read_parquet_schema(p))
        common = cols if common is None else (common & cols)
    cols = [c for c in ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"] if c in (common or set())]

    # 分批: 逐日 collect 会让 6 个文件各扫 510 遍 (1.2TB IO)。改为每 100 个
    # 交易日 collect 一次再 partition_by 落盘, 读放大降到 6 遍。
    total = 0
    batch = 100
    for i in range(0, len(ordered), batch):
        window = [date.fromisoformat(d) for d in ordered[i : i + batch]]
        lf = pl.concat([pl.scan_parquet(p).select(cols) for p in parts], how="vertical_relaxed")
        df = (
            lf.filter(pl.col("datetime").dt.date().is_in(window))
            .unique(subset=["symbol", "datetime"])
            .collect()
        )
        if df.is_empty():
            continue
        df = df.with_columns(pl.col("datetime").dt.date().alias("_d")).sort("_d", "symbol", "datetime")
        for key, g in df.partition_by("_d", as_dict=True).items():
            day = str(key[0]) if isinstance(key, tuple) else str(key)
            out = OUT / f"date={day}" / "part.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            g.drop("_d").write_parquet(out)
            total += g.height
        print(f"[merge] {min(i+batch, len(ordered))}/{len(ordered)} dates rows={total:,}", flush=True)
    print(f"[merge] done rows={total:,} dates={len(ordered)}")
    return 0


def main() -> int:
    global OUT, TMP, CATEGORY_5MIN, MAX_PAGES
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5m", choices=sorted(PERIODS))
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=200)
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--prepare", action="store_true")
    args = ap.parse_args()
    cat, out_name, tmp_name, pages = PERIODS[args.period]
    CATEGORY_5MIN, MAX_PAGES = cat, pages
    OUT = DATA / "archive" / out_name
    TMP = DATA / "archive" / tmp_name
    if args.prepare:
        return prepare_daily_close()
    if args.merge:
        return run_merge()
    return run_shard(args.shard, args.of, args.chunk)


if __name__ == "__main__":
    raise SystemExit(main())
