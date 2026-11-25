"""通达信裸协议数据源 (免费, 无 Key, 无积分)。

为什么存在: TdxQuant 按积分计费, 积分耗尽后 depth5 / minute / full_minute 三项
能力整体失效; TickFlow 免费档只有日K; fuyao 没有五档与分钟。通达信行情主站
(7709 端口) 的裸协议不校验任何凭据, 同一份数据由券商客户端自身使用, 因此可
以零成本覆盖这三项缺口。

实测边界 (2026-11-12, 上海电信主站, 见 analysis/_probe_tdx_source_result.json):
  - 全市场快照 5225 只 = 88 批 × 60 只, 单轮 3.4s, 3 轮 0 失败 0 漏票
  - 单批硬上限 60 只 (61 只起服务端静默截断为 60)
  - 1 分钟 K 可翻页深度 23520 根 ≈ 98 个交易日, 单页 800 根
  - 全市场当日 1 分钟一轮 ≈ 9~10 分钟 (受客户端 10 req/s 分钟通道限流)
  - 历史分时 (按日期) 回溯 ≥3 年, 240 点/日, 只有 close/avg/vol, 无 OHLC
  - 北交所仅 920xxx 段可用; 43x/83x/87x 服务端会回别的票 (已做代码回读校验丢弃)
  - 快照 servertime 字段秒位越界 (实测 '15:15:9174'), 不可作为行情时刻

单位口径 (实测, 与 CONTRIBUTING §3 小数制一致; 2026-11-12 逐项对账):
  - 快照 vol = 手, amount = 元 (茅台 32372 手 × 100 ≈ 日线 3237294 股)
  - K 线 vol = 股 → floor(/100) 转手; amount = 元 (240 根合计与日线差 6 股)
  - 1 分钟 K 每日 240 根, 09:31..15:00, 不含 09:25 集合竞价成交
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.config import settings
from app.minute_labels import fix_morning_close_label
from app.plugins.tdx.client import (
    CATEGORY_1MIN,
    MARKET_BJ,
    MARKET_SH,
    MARKET_SZ,
    TdxClient,
    TdxError,
    module_missing_reason,
)

logger = logging.getLogger(__name__)

_DATASETS = ("realtime", "depth5", "minute", "full_minute")

_BEIJING = ZoneInfo("Asia/Shanghai")
_BARS_PER_DAY = 240          # A 股 1 分钟一交易日根数 (09:31..15:00)
# 单标的翻页硬上限: 30×800 = 24000 根 ≈ 100 个交易日。服务端实测深度
# 23520 根 / 98 个交易日 (2026-11-12 实测 000001.SZ 最早到 2026-06-22),
# 即声明深度就是上限, 再翻不会有新行。实际翻几页由窗口决定 (见
# `_pages_for_window`), 常量只兜底, 不是取值范围。
_MINUTE_MAX_PAGES = 30
_MINUTE_PAGE_ROWS = 800
_UNIVERSE_TTL_S = 300.0
_SNAPSHOT_BATCH = 60
# 每轮全市场扫描的标的数。取 500: 逐只 ≈98ms ⇒ 一轮取数 ≈49s, 4 小时可跑
# 200+ 轮、把 5,571 只覆盖十余遍。调大的唯一好处是减少分区整文件重写的次数
# (落盘是「读旧分区 → 合并本批 → 原子写」, 代价与分区总行数成正比, 与本批
# 行数无关), 调小的代价是同样的写放大被更多轮次平摊。见 `_scan_slice`。
_SCAN_BATCH = 500
# 逐票翻页的并发与速率。旧实现「一只接一只串行翻页」: 5 天窗口 ⇒ 全市场 ~29 分钟,
# 瓶颈是标的数 × 每页 RTT, 与日K同构。并发必须**每线程独占一条 TdxClient 连接**
# (client.py 契约: tdxrs 非线程安全), 共享一把锁只会把串行换个名字。
# 速率钉在 10 req/s = 模块 docstring 实测的通道限流值; 并发只重叠 RTT 等待,
# 不放大请求频率。页级领牌: 一页 = 一次服务端请求。
_MINUTE_CONCURRENCY = 8
_MINUTE_RATE_PER_S = 10.0
# 少于该数的批次不值得开线程池 (每线程建连/关连的固定开销 > 收益)
_MINUTE_PARALLEL_MIN = 16
_CURSOR_REL = "state/tdx_minute_cursor.json"

_MINUTE_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}

_SUFFIX_MARKET = {"SH": MARKET_SH, "SZ": MARKET_SZ, "BJ": MARKET_BJ}

_universe_cache: tuple[float, list[str]] | None = None


# ---------------------------------------------------------------------------
# 代码映射
# ---------------------------------------------------------------------------
def to_tdx(symbol: str) -> tuple[int, str] | None:
    """内部 symbol (600519.SH) → (market, code); 不支持的返回 None。

    北交所只放行 920xxx: 服务端对 43x/83x/87x 会返回 600839 的行情 (实测),
    这种"有返回值但串票"的情形比空值更危险, 一律在映射层拦掉。
    (本仓 instruments 维表 355 只北交所全部是 920xxx, 不影响现有票池。)
    """
    text = str(symbol or "").strip().upper()
    code, _, suffix = text.partition(".")
    market = _SUFFIX_MARKET.get(suffix)
    if market is None or len(code) != 6 or not code.isdigit():
        return None
    if market == MARKET_BJ and not code.startswith("9"):
        return None
    return market, code


def _instruments_path(name: str) -> Path:
    return settings.data_dir / name / f"{name}.parquet"


def _load_symbols(name: str) -> list[str]:
    """读一张维表的 symbol 列并按协议可映射性过滤; 缺失/损坏返回空。"""
    path = _instruments_path(name)
    if not path.exists():
        return []
    try:
        df = pl.read_parquet(path, columns=["symbol"])
    except Exception as e:  # noqa: BLE001 — 维表损坏不应让整轮实时行情失败
        logger.warning("通达信源读取 %s 失败: %s", path, e)
        return []
    return [s for s in df["symbol"].cast(pl.Utf8).to_list() if to_tdx(s)]


def _load_universe() -> list[str]:
    """股票 + ETF 全集 (与 quote_service 的 CN_Equity_A + CN_ETF 同覆盖)。"""
    global _universe_cache
    now = time.monotonic()
    if _universe_cache is not None and now - _universe_cache[0] < _UNIVERSE_TTL_S:
        return _universe_cache[1]
    symbols: list[str] = []
    for name in ("instruments", "instruments_etf"):
        symbols.extend(_load_symbols(name))
    symbols = list(dict.fromkeys(symbols))
    _universe_cache = (now, symbols)
    return symbols


# ---------------------------------------------------------------------------
# 全市场扫描游标
# ---------------------------------------------------------------------------
# 为什么需要: 逐只 ≈98ms 决定了一轮跑不完全市场, 而轮次会被重启/限流/异常/
# 下一轮抢占打断。没有游标时每轮都从 sorted 头部开始, 于是落盘永久钉在同一个
# 前缀上 —— 2026-11-12 全市场只落了 000001..000050 (32 只, 逐字连续), 而
# 000426 当天 42bp 的真实除权偏离排在 100 位, 一次都没被采到。
# 有游标后中断只损失一段, 且覆盖顺序随时间轮转。
# 跨日归零: 开盘从头部重来符合直觉, 且新的一天本来就要整表重扫。


def _cursor_path() -> Path:
    return settings.data_dir / _CURSOR_REL


def _read_cursor(day: str, total: int) -> int:
    """读当日 offset; 跨日 / 文件损坏 / 越界一律从头 (只影响顺序, 不影响正确性)。"""
    try:
        data = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict) or str(data.get("day")) != day:
        return 0
    try:
        offset = int(data.get("offset", 0))
    except (TypeError, ValueError):
        return 0
    return offset if 0 <= offset < total else 0


def _write_cursor(day: str, offset: int) -> None:
    path = _cursor_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": day, "offset": offset}), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:  # 写失败只退化为"每轮从头", 绝不因此中断取数
        logger.debug("通达信分钟游标写入失败: %s", e)


def _scan_slice(symbols: list[str], limit: int) -> tuple[list[str], str, int]:
    """从持久游标处环回切一段, 返回 (本批, 交易日, 下一 offset)。"""
    day = datetime.now(_BEIJING).date().isoformat()
    total = len(symbols)
    if total == 0:
        return [], day, 0
    if limit >= total:
        return list(symbols), day, 0
    offset = _read_cursor(day, total)
    end = offset + limit
    batch = symbols[offset:end] if end <= total else symbols[offset:] + symbols[: end - total]
    return batch, day, end % total


# ---------------------------------------------------------------------------
# 字段归一
# ---------------------------------------------------------------------------
def _f(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _hand(shares: float | None) -> float | None:
    """股 → 手 (floor), 与 tdxquant/sina 同口径。"""
    if shares is None:
        return None
    return float(math.floor(shares / 100.0))


def _bar_datetime(row: dict) -> datetime | None:
    """tdxrs 的 datetime 已是 'YYYY-MM-DD HH:MM' 北京墙钟 (无时区), 直接解析。"""
    text = str(row.get("datetime") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _naive_bj(dt: datetime | None) -> datetime | None:
    """把带时区的调用方窗口折成北京墙钟 naive, 与 bar 的 datetime 同口径再比较。

    为什么必须折: bar 的 datetime 是协议原样的墙钟 (见 `_bar_datetime`), 存储
    schema 也是 `pl.Datetime("us")` 无时区; 而 `kline_sync` 传进来的
    `start_time/end_time` 带 `tzinfo=CN_TZ` (naive 会被 .timestamp() 按宿主时区
    解释, UTC 容器上整窗偏移 8 小时)。两种写法各自都对, 直接 `>=` 会
    `TypeError: can't compare offset-naive and offset-aware datetimes` ——
    2026-11-12 20:11 起 25 次补采全死在这里, 历史分钟缺口一行都没补上。
    统一在比较点折齐, 不改 schema、不改落盘契约。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_BEIJING).replace(tzinfo=None)


def _pages_for_window(start_time: datetime | None, *, now: datetime | None = None) -> int:
    """窗口起点 → 需要向前翻几页才能覆盖它。

    页数必须由窗口决定, 不能是常量。固定 8 页只够 26 个交易日, 而服务端实测
    有 98 个交易日 (23520 根) —— 更早的日期翻不到窗口, 返回空帧, 上游就把
    「翻页不够」误报成「数据源未提供」。反过来, 单日窗口只需 1 页: 固定 8 页
    会让每次点开历史分时都白发 7 个请求, 而整只又被时区比较异常打空。
    """
    if start_time is None:
        return 1
    if start_time.tzinfo is not None:
        start_time = start_time.astimezone(_BEIJING).replace(tzinfo=None)
    anchor = now or datetime.now(_BEIJING).replace(tzinfo=None)
    calendar_days = max(0, (anchor - start_time).days)
    # 自然日 → 交易日 (5/7), +2 覆盖节假日与当日尚未走完的余量
    trading_days = int(calendar_days * 5 / 7) + 2
    pages = -(-trading_days * _BARS_PER_DAY // _MINUTE_PAGE_ROWS)   # 向上取整
    return max(1, min(_MINUTE_MAX_PAGES, pages))


def _quote_record(symbol: str, row: dict, fetch_ms: int) -> dict | None:
    last = _f(row.get("price"))
    prev = _f(row.get("last_close"))
    if last is None or last <= 0 or prev is None or prev <= 0:
        return None  # 停牌/无成交: 不得进入 enriched (会把 change_pct 打成 -100%)
    change = last - prev
    return {
        "name": None,          # 协议快照不带名称, 名称由维表补
        "last_price": last,
        "prev_close": prev,
        "open": _f(row.get("open")),
        "high": _f(row.get("high")),
        "low": _f(row.get("low")),
        "volume": _f(row.get("vol")),        # 已是手
        "amount": _f(row.get("amount")),     # 元
        "change_pct": change / prev if prev else None,
        "change_amount": change,
        "amplitude": None,        # 由 enriched 统一口径计算
        "turnover_rate": None,    # 需股本口径, 交给 enriched
        # servertime 秒位越界 (实测 '15:15:9174'), 用抓取时刻代替交易所时刻
        "timestamp": fetch_ms,
        "session": None,
    }


def _depth_record(row: dict, fetch_ms: int) -> dict | None:
    bids = [(_f(row.get(f"bid{i}")), _f(row.get(f"bid_vol{i}"))) for i in range(1, 6)]
    asks = [(_f(row.get(f"ask{i}")), _f(row.get(f"ask_vol{i}"))) for i in range(1, 6)]
    if all(p is None or p <= 0 for p, _ in bids) or all(p is None or p <= 0 for p, _ in asks):
        return None  # 停牌/指数/无盘口
    return {
        "bid_prices": [p or 0.0 for p, _ in bids],
        "bid_volumes": [int(v or 0) for _, v in bids],
        "ask_prices": [p or 0.0 for p, _ in asks],
        "ask_volumes": [int(v or 0) for _, v in asks],
        "timestamp": fetch_ms,
    }


def _minute_rows(symbol: str, rows: list[dict]) -> list[dict]:
    """原生分钟 bar → 内部分钟行。

    13:00 的错标修正走**与 tdxquant 同一份** `app.minute_labels` —— 这条链路上有
    两个 TDX 生产者，修法写两遍就会只改一份（2026-11-13 复核：tdx 原来没有修标，
    它接管分钟源之后 13:00 会以「每天隔离几行」的形式回来）。
    """
    out: list[dict] = []
    for row in rows:
        dt = _bar_datetime(row)
        if dt is None:
            continue
        close = _f(row.get("close"))
        open_ = _f(row.get("open"))
        high = _f(row.get("high"))
        low = _f(row.get("low"))
        if open_ is None or close is None or high is None or low is None:
            continue
        out.append({
            "symbol": symbol,
            "datetime": dt,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": _hand(_f(row.get("vol"))),
            "amount": _f(row.get("amount")),
        })
    return fix_morning_close_label(symbol, out)


def _calibrate_by_day(symbol: str, rows: list[dict]) -> list[dict]:
    """按交易日分组校准 (复权比例逐日不同: 除权日两侧不是同一个倍数)。"""
    if not rows:
        return rows
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(str(r["datetime"].date()), []).append(r)
    out: list[dict] = []
    for day in sorted(by_day):
        out.extend(_calibrate_day(symbol, by_day[day]))
    return out


def _frame(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=_MINUTE_SCHEMA)
    return pl.DataFrame(rows, schema=_MINUTE_SCHEMA)


# ---------------------------------------------------------------------------
# 复权口径校准
# ---------------------------------------------------------------------------
# 通达信分钟 K 默认返回**前复权**序列 (fq=1), 而且它的"前"基准是**下一个**
# 除权日: 实测 600519 在 2026-06-25 的分钟收盘为 1184.076, 原始价为 1212.10,
# 差值 28.024 恰是 2026-06-26 的每股分红; 000426 在 2026-11-12 收 23.78,
# 分钟却是 23.68, 差 0.10 正是次日 (11-13) 的每股分红。fq=0 (不复权) 在
# tdxrs 0.6.10 上只回 24 根脏数据, 不可用。
#
# 本仓的分钟存储契约是**原始价** (CONTRIBUTING §3.2 / minute_adjust 标记),
# 直接落前复权会与日线复权叠加成二次复权。因此这里逐 (symbol, 交易日) 用
# 当日原始收盘价锚定, 求一个乘法比例把整日 OHLC 换算回原始价:
#     ratio = 原始日线 close / 前复权最后一根 close
# 实测该比例对 open/high/low 同样成立 (600519 全天 open/high/low 逐位复原),
# 因为复权是纯乘法。amount 是真实成交额, 不缩放; volume 不变。
# 缺当日日线的标的宁可不给这段分钟 (fail-closed), 不猜口径。
#
# 盘中当日还没有"当日原始收盘价" (收盘价还在动), 旧实现改用一次性快照价锚定
# 最后一根 bar —— 但快照在整轮开始时取一次, 全市场一轮 9~10 分钟, 排尾标的
# 的分母就是 10 分钟后的价, 整日 OHLC 会被乘上 (1+期间涨跌): 2026-11-12 实测
# 10 分钟 |涨跌| 中位 0.214%, 且随 universe 序号单调 ⇒ 是横截面结构化误差,
# 不是白噪声。当日口径改由 bar 内部的量决定 —— amount 是真实成交额、volume
# 是真实成交量, 两者都不随复权变化:
#     amount / (volume·100·close) = 1/s        (s = close 被乘上的系数)
# 逐 bar 取中位数即得 1/s: 与 close 同一根 bar, 不存在时间错配, 也不需要快照。
# 实测 (32 只 × 2 个交易日, 4,947 根) 中位 1.0000, 逐标的中位极差 ±2.6e-4。
_daily_cache: dict[str, tuple[float, pl.DataFrame]] = {}
# 覆盖 1 分钟可翻页深度 (98 个交易日) 还留余量: 回补时是「按标的循环」,
# 每个标的都要读全部日期分区, 缓存小于窗口长度会在每个标的上全部击穿
# (data/ 可能是 9p 挂载, 每文件一次元数据往返)。
_DAILY_CACHE_MAX = 256
# 并发翻页时多个线程会同时读写这个模块级 dict (pop 淘汰与 next(iter()) 迭代
# 可能交错出 RuntimeError)。读盘也放在锁内: 首次命中最贵也就一个分区, 之后全命中。
_daily_cache_lock = threading.Lock()


def _daily_close(day: str) -> pl.DataFrame | None:
    """读本地原始日线该交易日的 symbol/close, 带 mtime 缓存。

    并发安全: 缓存读写在 _daily_cache_lock 内完成 (含首次读盘) —— 多线程
    同时未命中时最多重复读一次盘, 但 pop 淘汰与迭代不会再交错出 RuntimeError。

    不能用 `kline_daily/date={day}/part.parquet`: 该表自 2026-11 起按月分区,
    目录名只给月份、且一个月里有多天的行 —— 硬编码日分区会让**所有日期**都
    找不到锚点, 表现为「缺 X 的原始日线, 该日分钟已跳过」刷屏 (2026-11-12 实测
    全市场逐日全跳过, 分钟回补一行没落)。统一走 locate_partition_dir + 按日过滤。
    """
    with _daily_cache_lock:
        return _daily_close_uncached(day)


def _daily_close_uncached(day: str) -> pl.DataFrame | None:
    if day in _daily_cache:
        return _daily_cache[day][1]
    from app.tickflow.partitions import locate_partition_dir

    try:
        target = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return None
    part = locate_partition_dir(settings.data_dir / "kline_daily", "kline_daily", target)
    if not part.is_dir():
        return None
    files = sorted(part.glob("*.parquet"))
    if not files:
        return None
    frames: list[pl.DataFrame] = []
    mtime = 0.0
    try:
        for f in files:
            mtime = max(mtime, f.stat().st_mtime)
            # date 列在不同批次里是 date32 / dictionary<string> 两种编码, 统一
            # 走字符串再转日期, 避免 concat 时类型冲突。
            frames.append(
                pl.read_parquet(f, columns=["symbol", "close", "date"]).with_columns(
                    pl.col("date").cast(pl.String).str.slice(0, 10)
                    .str.to_date(strict=False).alias("date")
                )
            )
        df = (
            pl.concat(frames, how="diagonal_relaxed")
            .filter(pl.col("date") == target)
            .select(["symbol", "close"])
        )
    except Exception as e:  # noqa: BLE001 — 单个分区损坏只影响该日
        logger.warning("通达信分钟校准: 读取 %s 失败 (%s)", part, e)
        return None
    if df.is_empty():
        return None
    if len(_daily_cache) >= _DAILY_CACHE_MAX:
        _daily_cache.pop(next(iter(_daily_cache)))
    _daily_cache[day] = (mtime, df)
    return df


def _calibrate_day(symbol: str, rows: list[dict]) -> list[dict]:
    """把一日的前复权分钟行换算回原始价; 无锚点返回空 (不猜)。"""
    if not rows:
        return rows
    day = str(rows[-1]["datetime"].date())
    closes = _daily_close(day)
    if closes is None:
        logger.warning("通达信分钟校准: %s 缺 %s 的原始日线, 该日分钟已跳过", symbol, day)
        return []
    hit = closes.filter(pl.col("symbol") == symbol)
    if hit.height == 0:
        logger.debug("通达信分钟校准: %s 在 %s 无日线 (停牌/未上市), 跳过", symbol, day)
        return []
    raw_close = float(hit["close"][0] or 0.0)
    adj_close = float(rows[-1]["close"] or 0.0)
    if raw_close <= 0 or adj_close <= 0:
        return []
    ratio = raw_close / adj_close
    if abs(ratio - 1.0) < 1e-9:
        return rows
    return [
        {**r, "open": r["open"] * ratio, "high": r["high"] * ratio,
         "low": r["low"] * ratio, "close": r["close"] * ratio}
        for r in rows
    ]


_SCALE_MIN_BARS = 20       # 少于 20 根不足以定中位数 (半日停牌 / 刚上市)
_SCALE_MIN_SHIFT = 5e-4    # 效应量下限: 5bp 以下的"复权"没有经济含义, 落在噪声里
_SCALE_MAX_SHIFT = 0.20    # 超过 20% 只可能是口径错 (量纲混淆), 拒绝缩放
_TICK = 0.01               # A 股最小变动价位 (元): 小于半跳的差异在价格上看不见


def _scale_from_amount(rows: list[dict]) -> tuple[float, float, int] | None:
    """用同一根 bar 内的成交额/成交量反读 close 的缩放系数。

    amount 是真实成交额 (元), volume 是真实成交量 (手), 都不随复权变化;
    close 若被乘过 s, 则均价/close = 1/s。返回 (1/s, 判别阈, 样本数):
    判别阈 = max(3 倍中位数标准误, 5bp, 半个最小变动价位) —— 三者的量纲都是
    "相对价格", 取最大者保证既不把噪声当复权, 也不去追小于一跳的差异。
    样本不足返回 None (此时调用方必须按"不缩放"处理, 不许猜)。
    """
    ratios: list[float] = []
    closes: list[float] = []
    for r in rows:
        v = float(r.get("volume") or 0.0)
        a = float(r.get("amount") or 0.0)
        c = float(r.get("close") or 0.0)
        if v <= 0 or a <= 0 or c <= 0:
            continue
        ratios.append(a / (v * 100.0 * c))
        closes.append(c)
    n = len(ratios)
    if n < _SCALE_MIN_BARS:
        return None
    ratios.sort()
    mid = n // 2
    med = ratios[mid] if n % 2 else 0.5 * (ratios[mid - 1] + ratios[mid])
    mad = sorted(abs(x - med) for x in ratios)[mid]
    se = 1.4826 * mad / (n ** 0.5)
    closes.sort()
    price = closes[mid]
    tol = max(3.0 * se, _SCALE_MIN_SHIFT, _TICK / 2.0 / price if price > 0 else 0.0)
    return med, tol, n


def _calibrate_today(rows: list[dict], raw_price: float | None = None) -> list[dict]:  # noqa: ARG001
    """盘中/当日: 用 bar 内部的成交额/成交量反读系数, 乘回原始价。

    raw_price 仅为签名兼容保留, 不参与缩放 —— 快照价与最后一根 bar 不同时刻,
    用它当分母会把 10 分钟涨跌乘进整日 OHLC (见上方口径说明)。

    判据是"效应量 > 判别阈" (阈值自适应: 薄票的中位数噪声大, 自动要求更大偏离),
    而不是固定阈值。阈值口径与 analysis/_diag_price_basis.py 的 tol 同源 ——
    生产端与判据端不许各有一套定义。反读失败或系数荒谬时返回原行 (不猜)。
    """
    if not rows:
        return rows
    est = _scale_from_amount(rows)
    if est is None:
        logger.debug("通达信分钟校准: 当日可用 bar 不足 %d 根, 按未缩放处理", _SCALE_MIN_BARS)
        return rows
    scale, tol, n = est
    shift = abs(scale - 1.0)
    if shift < tol:
        return rows
    if shift > _SCALE_MAX_SHIFT:
        logger.warning(
            "通达信分钟校准: 反读系数 %.4f 偏离超 %.0f%% (n=%d), 判为口径错, 不缩放",
            scale, _SCALE_MAX_SHIFT * 100, n,
        )
        return rows
    return [
        {**r, "open": r["open"] * scale, "high": r["high"] * scale,
         "low": r["low"] * scale, "close": r["close"] * scale}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class _PageGate:
    """页级节拍器: 与 fuyao._GlobalRateGate 同构 (插件间不互相 import)。

    rate_per_s <= 0 表示不限速(测试/诊断用)。
    """

    def __init__(self, rate_per_s: float) -> None:
        self._interval = (1.0 / rate_per_s) if rate_per_s > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            slot = max(time.monotonic(), self._next)
            self._next = slot + self._interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass
class _TdxConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。

    缺了它 provider_has_dataset 会在 `provider.config` 抛 AttributeError:
    policy._augment_custom_sources 的单个 try 会因此中止整条 _DATASET_CAP_MAP 循环,
    导致 minute/depth5/financial/full_minute 四项能力全部不被授予 (2026-11-12 实测)。
    """

    name: str = "tdx"
    display_name: str = "通达信(裸协议)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class TdxProvider:
    """通达信裸协议源 (realtime / depth5 / minute / full_minute)。"""

    name = "tdx"
    builtin = True

    def __init__(self) -> None:
        self._client: TdxClient | None = None
        # 分钟翻页的全局发牌器: 生命周期跟随 provider, 测试可替换成不限速实例。
        self._minute_gate = _PageGate(_MINUTE_RATE_PER_S)
        self.config = _TdxConfig()

    def _c(self) -> TdxClient:
        if self._client is None:
            self._client = TdxClient()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场快照。批次失败只丢该批, 不拖垮整轮 (与 tdxquant 同语义)。"""
        universe = _load_universe()
        if not universe:
            logger.warning("通达信实时行情: 维表为空, 本轮跳过")
            return []
        fetch_ms = int(time.time() * 1000)
        records: list[dict] = []
        failed = 0
        dropped = 0
        for i in range(0, len(universe), _SNAPSHOT_BATCH):
            chunk = universe[i : i + _SNAPSHOT_BATCH]
            mapped = [(sym, to_tdx(sym)) for sym in chunk]
            pairs = [mv for _, mv in mapped if mv is not None]
            by_code = {code: sym for sym, mv in mapped if mv is not None for code in (mv[1],)}
            if not pairs:
                continue
            try:
                rows = self._c().quotes(pairs)
            except TdxError as e:
                failed += 1
                logger.warning("通达信快照批次失败(第 %d 批, %d 只): %s", i // _SNAPSHOT_BATCH + 1, len(pairs), e)
                continue
            for row in rows:
                symbol = by_code.get(str(row.get("code") or ""))
                if symbol is None:
                    continue
                rec = _quote_record(symbol, row, fetch_ms)
                if rec is None:
                    dropped += 1
                    continue
                records.append(rec)
        if failed:
            logger.warning(
                "通达信实时行情 %d/%d 批失败, 本轮返回 %d 条",
                failed, math.ceil(len(universe) / _SNAPSHOT_BATCH), len(records),
            )
        if not records and universe:
            logger.warning("通达信实时行情本轮 0 条 (请求 %d 只), 疑似连接或协议异常", len(universe))
        return records

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """批量五档 (价: 元, 量: 手)。与 realtime 同端点, 不产生额外请求成本。"""
        wanted: list[tuple[int, str, str]] = []
        for sym in symbols or []:
            mapped = to_tdx(sym)
            if mapped is not None:
                wanted.append((mapped[0], mapped[1], sym))
        if not wanted:
            return {}
        fetch_ms = int(time.time() * 1000)
        out: dict[str, dict] = {}
        by_code = {code: sym for _, code, sym in wanted}
        for i in range(0, len(wanted), _SNAPSHOT_BATCH):
            chunk = wanted[i : i + _SNAPSHOT_BATCH]
            try:
                rows = self._c().quotes([(m, c) for m, c, _ in chunk])
            except TdxError as e:
                logger.warning("通达信五档批次失败(第 %d 批): %s", i // _SNAPSHOT_BATCH + 1, e)
                continue
            for row in rows:
                symbol = by_code.get(str(row.get("code") or ""))
                if symbol is None:
                    continue
                rec = _depth_record(row, fetch_ms)
                if rec is not None:
                    out[symbol] = rec
        # 覆盖率红线: 停牌股不出盘口属正常, 但大面积缺书意味着服务端换节点/限流,
        # 这时若只看"没抛异常"会以为五档还在跑 (封单梯队会静默变空)。
        if by_code and len(out) < 0.5 * len(by_code):
            logger.warning(
                "通达信五档覆盖率异常: %d/%d 只返回盘口 (请求 %d 只), 请检查行情主站或网络",
                len(out), len(by_code), len(symbols or []),
            )
        return out

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002 — 协议按代码路由, 无需资产类型
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """分钟 K, 逐标的翻页。深度上限由服务端决定 (实测 ≈98 个交易日)。

        start_time 为 None 时只取最近一页 (≈3.3 个交易日), 避免无界翻页。
        全市场批次走多线程 (每线程独占连接), 小批次保持单线程省建连开销。
        """
        if freq not in ("1m", "1min", "1"):
            logger.warning("通达信源只提供 1 分钟 K, 收到 freq=%s, 返回空", freq)
            return _frame([])
        win_start = _naive_bj(start_time)
        win_end = _naive_bj(end_time)
        syms = list(symbols or [])
        if len(syms) >= _MINUTE_PARALLEL_MIN:
            return self._get_minute_parallel(syms, win_start, win_end, on_chunk_done)
        return self._get_minute_serial(syms, win_start, win_end, on_chunk_done)

    def _get_minute_serial(
        self,
        symbols: list[str],
        win_start: datetime | None,
        win_end: datetime | None,
        on_chunk_done: Callable[[int, int], None] | None,
    ) -> pl.DataFrame:
        total = len(symbols)
        frames: list[pl.DataFrame] = []
        failures: list[str] = []
        for i, symbol in enumerate(symbols):
            mapped = to_tdx(symbol)
            if mapped is None:
                failures.append(symbol)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
                continue
            market, code = mapped
            rows, complete = self._page_bars(market, code, win_start)
            if not complete:
                # 半截数据不落盘: symbol 级覆盖下, 写半截会把完整旧数据覆盖成半截。
                failures.append(symbol)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
                continue
            kept = _minute_rows(symbol, rows)
            if win_start is not None or win_end is not None:
                kept = [
                    r for r in kept
                    if (win_start is None or r["datetime"] >= win_start)
                    and (win_end is None or r["datetime"] < win_end)
                ]
            kept = _calibrate_by_day(symbol, kept)
            if kept:
                frames.append(_frame(kept))
            if on_chunk_done:
                on_chunk_done(i + 1, total)
        return self._minute_frames_out(frames, failures, total)

    def _get_minute_parallel(
        self,
        symbols: list[str],
        win_start: datetime | None,
        win_end: datetime | None,
        on_chunk_done: Callable[[int, int], None] | None,
    ) -> pl.DataFrame:
        """多线程逐票翻页: 每线程独占一条 TdxClient (非线程安全契约),
        页级请求过 _minute_gate 全局限速; 进度按完成只数推进, 与串行版同语义。
        逐只失败只丢该只并计数, 不拖垮整批 (与串行版一致)。"""
        total = len(symbols)
        frames: list[pl.DataFrame] = []
        failures: list[str] = []
        lock = threading.Lock()
        state = {"done": 0}
        tls = threading.local()
        clients: list[TdxClient] = []

        def _progress() -> None:
            with lock:
                state["done"] += 1
                if on_chunk_done:
                    on_chunk_done(state["done"], total)

        def _work(symbol: str) -> pl.DataFrame | None:
            try:
                mapped = to_tdx(symbol)
                if mapped is None:
                    with lock:
                        failures.append(symbol)
                    _progress()
                    return None
                client = getattr(tls, "client", None)
                if client is None:
                    client = TdxClient()
                    tls.client = client
                    with lock:
                        clients.append(client)
                market, code = mapped
                rows, complete = self._page_bars(
                    market, code, win_start, client=client, gate=self._minute_gate,
                )
                if not complete:
                    # 半截数据不落盘(见 _get_minute_serial 同注释), 归入失败下轮重拉。
                    with lock:
                        failures.append(symbol)
                    _progress()
                    return None
                kept = _minute_rows(symbol, rows)
                if win_start is not None or win_end is not None:
                    kept = [
                        r for r in kept
                        if (win_start is None or r["datetime"] >= win_start)
                        and (win_end is None or r["datetime"] < win_end)
                    ]
                kept = _calibrate_by_day(symbol, kept)
                out = _frame(kept) if kept else None
                _progress()
                return out
            except Exception as e:  # noqa: BLE001 — 一只坏不能丢整批
                with lock:
                    failures.append(symbol)
                logger.warning("通达信分钟K %s 并发拉取失败: %s", symbol, e)
                _progress()
                return None

        try:
            with ThreadPoolExecutor(
                max_workers=_MINUTE_CONCURRENCY, thread_name_prefix="tdx-minute"
            ) as pool:
                for sub in pool.map(_work, symbols):
                    if sub is not None:
                        frames.append(sub)
        finally:
            # 连接的生命周期跟这批任务走: 用完即关, 不占常驻 fd。
            for c in clients:
                c.close()
        return self._minute_frames_out(frames, failures, total)

    def _minute_frames_out(
        self, frames: list[pl.DataFrame], failures: list[str], total: int
    ) -> pl.DataFrame:
        if failures:
            logger.warning(
                "通达信分钟K: %d/%d 只无法映射或拉取失败, 已跳过: %s",
                len(failures), total, ", ".join(failures[:10]),
            )
        return pl.concat(frames, how="diagonal_relaxed") if frames else _frame([])

    def _page_bars(
        self,
        market: int,
        code: str,
        start_time: datetime | None,
        *,
        client: TdxClient | None = None,
        gate: "_PageGate | None" = None,
    ) -> tuple[list[dict], bool]:
        """按 800 根/页向前翻, 直到覆盖 start_time 或页数用尽。

        返回 (升序行, complete)。complete=False 表示中途 TdxError 提前终止 ——
        这是「半截数据」: 上游必须据此把该只当失败处理(不落盘/保留旧数据),
        否则 symbol 级覆盖会把完整旧数据覆盖成半截。数据自然到头(空页/末页)算 complete。

        client: 并发路径传入线程独占连接 (TdxClient 非线程安全, 不得共享)。
        gate: 页级领牌 —— 一页就是一次服务端请求, 并发下必须过全局节拍器。
        """
        pages = _pages_for_window(start_time)
        c = client or self._c()
        collected: list[dict] = []
        complete = True
        for page in range(pages):
            if gate is not None:
                gate.wait()
            try:
                rows = c.bars(market, code, CATEGORY_1MIN, page * _MINUTE_PAGE_ROWS, _MINUTE_PAGE_ROWS)
            except TdxError as e:
                logger.debug("通达信分钟K %s 第 %d 页失败: %s", code, page + 1, e)
                complete = False
                break
            if not rows:
                break
            collected.extend(rows)
            earliest = _bar_datetime(rows[0])
            if len(rows) < _MINUTE_PAGE_ROWS:
                break
            if start_time is not None and earliest is not None and earliest <= start_time:
                break
        collected.sort(key=lambda r: str(r.get("datetime") or ""))
        return collected, complete

    # ---- full_minute ----
    def get_intraday_batch(
        self, symbols: list[str], count: int = 300, asset_type: str = "stock",
        limit: int = _SCAN_BATCH,
    ) -> pl.DataFrame:
        """全量分钟修复轮: 从游标处取一段逐只拉取, 只保留今日行。

        单标的 480 根 (≈2 个交易日) 足够覆盖当日并容忍前一日余量;
        逐只 ≈98ms 且分钟通道约 10 req/s, 全市场一轮 12.5 分钟 —— 而轮询间隔
        是 6s。所以这里**不追求一次跑完全市场**, 而是每轮跑 `limit` 只并把
        持久游标推进一段 (见 `_scan_slice`), 覆盖顺序随时间轮转。
        """
        if not symbols or asset_type != "stock":
            return _frame([])
        batch, cursor_day, next_offset = _scan_slice(symbols, limit)
        today = datetime.now(_BEIJING).date()
        frames: list[pl.DataFrame] = []
        failures = 0
        for symbol in batch:
            mapped = to_tdx(symbol)
            if mapped is None:
                continue
            try:
                rows = self._c().bars(*mapped, CATEGORY_1MIN, 0, _BARS_PER_DAY * 2)
            except TdxError:
                failures += 1
                continue
            kept = [r for r in _minute_rows(symbol, rows) if r["datetime"].date() == today]
            kept = _calibrate_today(kept)
            if kept:
                frames.append(_frame(kept))
        if failures:
            logger.warning("通达信全量分钟修复轮: %d/%d 只失败", failures, len(batch))
        # 游标只反映"这一批处理过", 不保证成功: 整批失败也推进, 否则坏票会把
        # 所有后续标的永远挡在后面 (前缀饥饿), 那正是游标要消除的病。
        _write_cursor(cursor_day, next_offset)
        return pl.concat(frames, how="diagonal_relaxed") if frames else _frame([])

    def get_intraday_latest(self, symbols: list[str] | None = None, count: int = 3) -> pl.DataFrame:
        """稳态增量: 语义与 TickFlow 的 intraday.universe 对齐。

        通达信没有"一次请求返回全市场"的端点 (逐只 98ms), 所以这里不是"每只取
        最后 3 根" —— 那样既补不齐日内缺口, 又和修复轮抢同一个游标。直接委托
        给批量路径: 每轮推进一段、每只取整日, 由轮次节奏决定收敛速度。

        `symbols=None` 时读 `instruments` 维表 (与 minute_refresh._universe 同源),
        不能用 `_load_universe()` —— 那个含 ETF, 会让分钟分区混进非股票标的。
        """
        targets = list(symbols) if symbols else _load_symbols("instruments")
        if not targets:
            return _frame([])
        return self.get_intraday_batch(targets, count=count)

    # ---- settings 页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        from datetime import timedelta

        syms = symbols or ["600519.SH"]
        if dataset == "realtime":
            rows = self.get_realtime()[:5]
            return {"provider": self.name, "dataset": dataset, "rows": len(rows),
                    "columns": sorted(rows[0].keys()) if rows else [], "preview": rows}
        if dataset == "depth5":
            books = self.get_depth_batch(syms)
            first = next(iter(books.items()), None)
            preview = [{"symbol": first[0], **first[1]}] if first else []
            return {"provider": self.name, "dataset": dataset, "rows": len(books),
                    "columns": ["symbol", "bid_prices", "bid_volumes", "ask_prices", "ask_volumes", "timestamp"],
                    "preview": preview}
        if dataset in ("minute", "full_minute"):
            start = datetime.now() - timedelta(days=3)
            df = self.get_minute(syms, start, None) if dataset == "minute" else self.get_intraday_batch(syms)
            return {"provider": self.name, "dataset": dataset, "rows": df.height,
                    "columns": df.columns, "preview": df.head(5).to_dicts()}
        rows = self.get_realtime()[:5]
        return {"provider": self.name, "dataset": dataset, "rows": len(rows), "columns": [], "preview": rows}


# ---------------------------------------------------------------------------
# loader 契约
# ---------------------------------------------------------------------------
def availability() -> tuple[bool, str]:
    """tdxrs 可用性。缺依赖只灰显, 不影响其他数据源。"""
    reason = module_missing_reason()
    if reason:
        return False, reason
    return True, "ok"


def datasets() -> tuple[str, ...]:
    return _DATASETS
