"""长区间回看：本地不足的一段，用上游**前复权**日线补齐（只读，不落盘）。

问题：个股分析页的日K来自本地 enriched 表，而本地只覆盖某个起点之后的日期
（实测 2025-05-07 起）。用户要看两年形态时，起点之前那一段不存在。

两条路，本模块只允许第二条：

  A. 把上游历史写进 `kline_daily`（产品已有「向前扩展历史」走这条）。
     但写进去的是 `adjust="none"` 的原始价，而除权因子需要 Pro 权限才能取
     （实测 `ex_factors` → PermissionError）。缺因子时 enriched 不做前复权，
     图表上每一次送转 / 分红都会变成一根**假跳空** —— 2024-08~2025-05 这
     9 个月里，抽样 10 只票有 8 只存在 4.7%~67% 的复权缺口（002594 最高
     67%，000651 18%）。那不是数据缺口，是会引导错误结论的图形。

  B. 只读穿透：直接向上游要 `adjust="forward"` 的同一条日线，与本地 8 段
     拼接渲染，不写任何表。这一条成立的前提只有一件事 —— 上游 forward 与
     本地前复权是**同一个基**。已实测（600519.SH 2025-05-07）：上游
     close=1464.838615，本地 enriched close=1464.835838，相对差 1.9e-6；
     volume 27462 对 27462、amount 4.2751e9 对 4.2751e9。⇒ 拼接处无接缝。

缓存：同一 (symbol, start, end) 10 分钟。免费档首次调用要付 SDK 冷启动
（35~50s），热后 1.5s/次；一次要两年会 ReadTimeout，所以按 ≤180 天分段。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

import polars as pl

logger = logging.getLogger(__name__)

# 分段上限：免费档一次要 539 根（两年）会超时（实测 52s 后 ReadTimeout）；
# 300 天≈200 根实测可过，且能把常见的 9~12 个月缺口压成 1 次请求
# （容器内 2 段=12.5s，1 段=~5s）。
CHUNK_DAYS = 300
# 回看上限：4 年 ≈ 8 段；再长就是"全历史"需求，应该走入库路径而不是每次现拉。
MAX_CHUNKS = 8
# 缓存 6 小时：补齐窗口的右端恒在「本地覆盖起点」之前，那一段历史不会变
# （不随今天滚动），所以长 TTL 不会拿到过期数据；代价只是进程内存。
# 实测容器内一次两年补齐 14s、命中缓存 1.7s（后者主要是本地 9p 读）。
CACHE_TTL_S = 6 * 3600
_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]

_CACHE: dict[tuple[str, str, str], tuple[float, pl.DataFrame]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple[str, str, str]) -> pl.DataFrame | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if not hit:
        return None
    if hit[0] < time.time():
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        return None
    return hit[1]


def _cache_put(key: tuple[str, str, str], df: pl.DataFrame) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) > 64:  # 单票几百行, 64 个窗口≈几 MB, 超了先清过期
            now = time.time()
            for k in [k for k, v in _CACHE.items() if v[0] < now]:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.time() + CACHE_TTL_S, df)


def _fetch_chunk(symbol: str, start: date, end: date) -> pl.DataFrame:
    from app.services.kline_sync import (
        _compact_klines_to_df, _datetime_to_ms, _timestamp_to_beijing_datetime, get_client,
    )

    tf = get_client()
    raw = tf.klines.batch(
        [symbol], period="1d", adjust="forward",
        start_time=_datetime_to_ms(datetime.combine(start, datetime.min.time())),
        end_time=_datetime_to_ms(datetime.combine(end, datetime.max.time())),
        count=10000, as_dataframe=False, show_progress=False,
    )
    seg = _compact_klines_to_df(raw)
    if seg.is_empty():
        return pl.DataFrame()
    seg = seg.with_columns(_timestamp_to_beijing_datetime(pl.col("timestamp")).alias("_dt"))
    out = seg.select([
        pl.col("_dt").cast(pl.Date).alias("date"),
        *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "amount")
          if c in seg.columns],
    ])
    return out.unique(subset=["date"], keep="last").sort("date")


def fetch_forward(symbol: str, start: date, end: date, *, chunk_days: int = CHUNK_DAYS) -> pl.DataFrame:
    """上游前复权日线 [start, end]，分段取再拼。非交易区间可能返回空。"""
    if start > end:
        return pl.DataFrame()
    frames: list[pl.DataFrame] = []
    cur = start
    for _ in range(MAX_CHUNKS + 1):
        if cur > end:
            break
        nxt = min(cur + timedelta(days=chunk_days), end)
        try:
            part = _fetch_chunk(symbol, cur, nxt)
        except Exception as e:  # noqa: BLE001 - 上游超时/断网不该让整页失败
            logger.warning("history fill: %s %s~%s 拉取失败: %s", symbol, cur, nxt, e)
            raise
        if not part.is_empty():
            frames.append(part)
        cur = nxt + timedelta(days=1)
    if not frames:
        return pl.DataFrame()
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .unique(subset=["date"], keep="last")
        .sort("date")
        .select([c for c in _COLS if c in pl.concat(frames, how="diagonal_relaxed").columns])
    )


def fill_head(
    symbol: str,
    start: date,
    end: date,
    local_first: date | None,
    *,
    cap_days: int = MAX_CHUNKS * CHUNK_DAYS,
) -> tuple[pl.DataFrame, dict]:
    """补齐 [start, local_first) 这一段。返回 (df, meta)。

    meta: {attempted, fetched_rows, first, last, error, clamped, elapsed_ms}
    只有真的缺一段才会发起请求；本地已覆盖的部分一律不碰（不重复、不覆盖）。
    """
    meta: dict = {
        "attempted": False, "fetched_rows": 0, "first": None, "last": None,
        "error": None, "clamped": False, "elapsed_ms": 0,
    }
    if start > end:
        return pl.DataFrame(), meta
    fill_end = (local_first - timedelta(days=1)) if local_first else end
    if fill_end < start:
        return pl.DataFrame(), meta
    if (fill_end - start).days > cap_days:
        start = fill_end - timedelta(days=cap_days)
        meta["clamped"] = True

    key = (symbol, start.isoformat(), fill_end.isoformat())
    cached = _cache_get(key)
    if cached is not None:
        meta.update({
            "attempted": True, "fetched_rows": cached.height,
            "first": str(cached["date"].min()) if cached.height else None,
            "last": str(cached["date"].max()) if cached.height else None,
            "cached": True,
        })
        return cached, meta

    t0 = time.perf_counter()
    meta["attempted"] = True
    try:
        df = fetch_forward(symbol, start, fill_end)
    except Exception as e:  # noqa: BLE001
        meta["error"] = f"{type(e).__name__}: {e}"
        meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return pl.DataFrame(), meta
    meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    if df.is_empty():
        return df, meta
    _cache_put(key, df)
    meta.update({
        "fetched_rows": df.height,
        "first": str(df["date"].min()), "last": str(df["date"].max()),
    })
    return df, meta
