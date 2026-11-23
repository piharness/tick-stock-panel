"""把 `kline_5min` 面板表向前滚到最新交易日（增量补尾，tdx 裸协议）。

## 为什么需要这个脚本

`scripts/archive_intraday_periods.py` 是「首建/全量快照」：482 个交易日 × 全池，
5m 一次约 2.2 小时。而面板表平时只差最后几天时，全量重拉是浪费；更要紧的是
它写 `data/archive/kline_5min`，不是 `minute_union` 读的 `data/kline_5min`。

上游 5m 深度 24000 根（≈482 交易日），page0 的 800 根就覆盖 16 个交易日
⇒ 每只一次请求就能补完尾巴。

## 判据（都是实测过的，不要改成"差不多"）

1. 写入必须走 `kline_sync._write_minute_partition` —— 唯一分钟落盘入口：
   按日分区、unique(symbol,datetime) 幂等合并、越界副本逐行隔离。手写
   `to_parquet` 会绕开这三条，且与 API 侧并发写同一分区时互相覆盖。
2. 取数/口径复用 provider 的 `_minute_rows`（含 13:00 错标修正）与
   `_calibrate_by_day`（用当日**原始**日线收盘逐日反算，缺锚点 fail-closed）。
   tdx 返回的是前复权（基准是下一个除权日），直接落盘会与日线复权叠加。
3. `--verify` 是写入的前置关卡：拉一个**本地已有**的交易日，与 `kline_5min`
   逐 bar 比。max|Δ| > 1e-9 就拒绝写入 —— 那意味着 tdx 服务端的构造变了
   （或校准失效），续写会让同一天的两个来源混在一个分区里，且不报错。

## 用法

    python analysis/_fill_5min_tail.py --verify                 # 先验口径
    python analysis/_fill_5min_tail.py --shard 0 --of 4        # 四分片并行
    python analysis/_fill_5min_tail.py --merge                 # 合并落盘

分片是**进程级**的（tdxrs 不释放 GIL，同进程多线程与单线程等速）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("TICK_DATA_DIR", str(ROOT / "data"))

from app.config import settings  # noqa: E402

try:
    from tdxrs import TdxHqClient
except ModuleNotFoundError as exc:  # pragma: no cover - 环境问题，不是逻辑分支
    raise SystemExit(
        "缺 tdxrs（项目 pyproject 把它限在 <3.12，但 0.6.10 能在 3.12 源码编译）。\n"
        "隔离安装，不动 venv：\n"
        "  uv pip install tdxrs --python backend/.venv/Scripts/python.exe --target /tmp/tdxenv\n"
        "  PYTHONPATH=/tmp/tdxenv backend/.venv/Scripts/python.exe analysis/_fill_5min_tail.py --verify"
    ) from exc

DATA = Path(os.environ["TICK_DATA_DIR"])
OUT = DATA / "kline_5min"
TMP = DATA / ".staging" / "_tail5m"
CATEGORY_5MIN = 0
PAGE_ROWS = 800
LOOKBACK_DAYS = 10          # 日历日：覆盖 4 个交易日 + 周末冗余


def _symbols() -> list[str]:
    """以面板表最近一个分区为准：不引入第二份标的池定义。"""
    days = sorted(p.name[5:] for p in OUT.iterdir() if p.name.startswith("date="))
    if not days:
        raise SystemExit(f"{OUT} 没有 date= 分区")
    last = OUT / f"date={days[-1]}" / "part.parquet"
    return sorted(pl.read_parquet(last, columns=["symbol"])["symbol"].unique().to_list())


def _fetch_tail(client, sym: str, cutoff: date) -> pl.DataFrame:
    from app.plugins.tdx.provider import (
        _calibrate_by_day,
        _calibrate_today,
        _daily_close,
        _frame,
        _minute_rows,
        _scale_from_amount,
        to_tdx,
    )

    mapped = to_tdx(sym)
    if mapped is None:
        return pl.DataFrame()
    market, code = mapped
    try:
        got = client.get_security_bars(CATEGORY_5MIN, market, code, 0, PAGE_ROWS)
    except BaseException:  # noqa: BLE001 — tdxrs panic 是 BaseException
        return pl.DataFrame()
    if not got:
        return pl.DataFrame()
    rows = [r for r in _minute_rows(sym, got) if r["datetime"].date() >= cutoff]
    if not rows:
        return pl.DataFrame()

    # 逐日选校准路径（当日收盘后日线还没落盘时，不能整只 fail-closed）：
    #   有当日原始日线 close  → `_calibrate_by_day`，精确反算 ratio（权威）
    #   无（当日/当日延迟）   → `_calibrate_today`，用 bar 内 amount/volume 反读系数
    # 反读不可用（bar < 20 根，薄票/半日停牌）时**跳过该日**，不用未校准的前复权价充数。
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(str(r["datetime"].date()), []).append(r)
    kept: list[dict] = []
    for day in sorted(by_day):
        anchors = _daily_close(day)
        has_anchor = anchors is not None and anchors.filter(pl.col("symbol") == sym).height > 0
        if has_anchor:
            kept.extend(_calibrate_by_day(sym, by_day[day]))
        elif _scale_from_amount(by_day[day]) is not None:
            kept.extend(_calibrate_today(by_day[day]))
    if not kept:
        return pl.DataFrame()
    return _frame(kept)


def cmd_verify() -> int:
    """本地已有的最近交易日 × 两只票，逐 bar 对账。"""
    from tdxrs import TdxHqClient

    days = sorted(p.name[5:] for p in OUT.iterdir() if p.name.startswith("date="))
    probe_days = days[-3:]
    cutoff = date.fromisoformat(probe_days[0])
    client = TdxHqClient()
    client.connect_to_any()
    worst = 0.0
    for sym in ["000001.SZ", "600519.SH", "300750.SZ"]:
        fresh = _fetch_tail(client, sym, cutoff)
        if fresh.is_empty():
            print(f"[{sym}] tdx 无数据，跳过")
            continue
        local = pl.concat([
            pl.read_parquet(OUT / f"date={d}" / "part.parquet").filter(pl.col("symbol") == sym)
            for d in probe_days
        ]).sort("datetime")
        j = local.join(fresh, on="datetime", suffix="_tdx")
        if j.height == 0:
            print(f"[{sym}] !! 时间轴对不上（本地 {local.height} / 新拉 {fresh.height}）")
            worst = 1.0
            continue
        for c in ["open", "high", "low", "close"]:
            d = float((j[c] - j[c + "_tdx"]).abs().max())
            worst = max(worst, d)
            print(f"[{sym}] {c:<6} n={j.height} max|Δ|={d:.3e}")
        rv = float((j["volume"] / j["volume_tdx"]).median())
        print(f"[{sym}] volume 比 median={rv:.6f}（应 1.0）")
    client.disconnect() if hasattr(client, "disconnect") else None
    print(f"\nworst max|Δ| = {worst:.3e}")
    if worst > 1e-9:
        print("!! 口径不一致：拒绝写入。先查 tdx 服务端构造/校准，不要续写面板表")
        return 1
    print("⇒ 同源同构造，可以续写")
    return 0


def cmd_shard(shard: int, of: int, lookback: int) -> int:
    from tdxrs import TdxHqClient

    syms = _symbols()
    if of > 1:
        syms = syms[shard::of]
    cutoff = date.today() - timedelta(days=lookback)
    TMP.mkdir(parents=True, exist_ok=True)
    target = TMP / f"shard{shard}.parquet"
    # 不因 target 存在而跳过：这是「补尾」脚本，重跑就是重拉最新（上游每天往前滚）。
    # 上一版继承了全量归档的「存在即跳过」，第二次运行时会把旧分片当新数据合并，
    # 而且不报错 —— 与「停了 vs 被封」同类的静默失效。

    client = TdxHqClient()
    client.connect_to_any()
    frames: list[pl.DataFrame] = []
    empty = 0
    t0 = time.time()
    for i, sym in enumerate(syms, 1):
        df = _fetch_tail(client, sym, cutoff)
        if df.is_empty():
            empty += 1
        else:
            frames.append(df)
        if i % 200 == 0:
            el = time.time() - t0
            print(f"[shard {shard}] {i}/{len(syms)} empty={empty} "
                  f"rows={sum(f.height for f in frames):,} "
                  f"elapsed={el/60:.1f}min eta={el/max(i,1)*(len(syms)-i)/60:.1f}min", flush=True)
            # 中途快照：一次连接跑 1,400 只约 6 分钟，进程被中断时不能全靠结尾那一次写
            if frames and i % 500 == 0:
                pl.concat(frames, how="vertical_relaxed").write_parquet(target)
    if frames:
        pl.concat(frames, how="vertical_relaxed").write_parquet(target)
    print(f"[shard {shard}] done rows={sum(f.height for f in frames):,} -> {target}", flush=True)
    return 0


def cmd_merge() -> int:
    from app.services.kline_sync import _write_minute_partition

    parts = sorted(TMP.glob("shard*.parquet"))
    if not parts:
        print("[merge] 没有分片")
        return 1
    df = pl.concat([pl.read_parquet(p) for p in parts], how="vertical_relaxed")
    df = df.unique(subset=["symbol", "datetime"], keep="last").sort(["datetime", "symbol"])
    print(f"[merge] 输入 {df.height:,} 行 / {df['symbol'].n_unique()} 只 / "
          f"{df['datetime'].dt.date().n_unique()} 个交易日 "
          f"({df['datetime'].min()} ~ {df['datetime'].max()})")
    written = _write_minute_partition(df, OUT, origin="tdx_tail")
    print(f"[merge] 落盘 {written:,} 行 → {OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--merge", action="store_true")
    a = ap.parse_args()
    if a.verify:
        return cmd_verify()
    if a.merge:
        return cmd_merge()
    return cmd_shard(a.shard, a.of, a.lookback)


if __name__ == "__main__":
    raise SystemExit(main())
