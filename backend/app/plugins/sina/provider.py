"""新浪财经公开行情内置数据源 provider。

定位: **零 Key、零积分** 的盘中流式源。能力边界是「快照 + 浅分钟」, 不是权威历史源。

实现数据集
  - realtime   全市场批量快照 (含买卖五档), 800 只/请求, 全市场约 8 请求
  - depth5     五档盘口 (与快照同端点, 逐批注入代码; 实测不产生额外成本)
  - minute     1 分钟K, 逐标的拉取, 深度硬顶 1023 根 (≈4.3 个交易日)

未实现数据集 (故意不声明, 避免路由到本源后静默缺数据)
  - daily / adj_factor / financial: 由 fuyao 等权威源承担 (本源的复权口径不可控)
  - full_minute: 「全市场 × 每分钟落盘」对本源的逐标的端点等于自残 (5575 次/轮),
    按需分钟 (minute) 已足够覆盖分时图/分钟回测的取数, 故不声明。

单位口径 (CONTRIBUTING §3.1: 不凭字段名推断, 一律按实测)
  - 新浪快照 volume 为 **股** → 内部 realtime/日K 契约为 **手**, 此处 floor(/100)
  - 新浪快照五档量为 **股** → depth5 契约为 **手**, 此处 floor(/100)
  - 新浪分钟 volume 为 **股** → 分钟契约 **手**, 此处 floor(/100); amount 为元, 直通
  - 指数快照量纲与个股不同 (手数量级), 走 get_realtime_indices 时**原样透传**
  - change_pct 为**小数制** (0.0174 = +1.74%), 与 enriched 全项目约定一致

停牌/无行情保护: last_price <= 0 的快照行直接丢弃 —— 否则 change_pct 会被算成
-100%, 污染 enriched 与涨跌停判定。丢弃后该标的本轮不更新, 由既有缓存兜底。

已知限制 (实测, 2026-11-12)
  - 分钟K 无 offset/日期入参, datalen 上限 1023 → 拿不到 4 天以前的历史
  - 单请求 >800 只返回 HTTP 431
  - 无官方 SLA/授权; 仅适合个人自用, 商用需自行解决数据授权
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.config import settings
from app.plugins.sina.client import MAX_BATCH, SinaClient, SinaError

logger = logging.getLogger(__name__)

_DATASETS = ("realtime", "depth5", "minute")
_BJ = ZoneInfo("Asia/Shanghai")
# 标的全集缓存 (instruments 维表每日才变一次; 300s 足够且避免每轮读盘)
_UNIVERSE_TTL_S = 300.0
_universe_cache: tuple[float, list[str]] | None = None

_MINUTE_SCHEMA = {
    "symbol": pl.String, "datetime": pl.Datetime("us"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}


def _prefix_of(symbol: str) -> str | None:
    """内部代码 → 新浪前缀。600000.SH → sh; 920002.BJ → bj。"""
    code, _, exch = symbol.partition(".")
    exch = exch.upper()
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exch)


def to_sina_code(symbol: str) -> str | None:
    """600000.SH → sh600000; 非法/未知交易所 → None (调用方丢弃)。"""
    if not symbol or "." not in symbol:
        return None
    code, _, _ = symbol.partition(".")
    prefix = _prefix_of(symbol)
    if prefix is None or not code:
        return None
    return f"{prefix}{code}"


def _f(value: str | None) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _hand(shares: float | None) -> int | None:
    """股 → 手 (向下取整, 与 fuyao/tdxquant 口径一致)。"""
    return None if shares is None else int(math.floor(shares / 100.0))


def _ts_ms(day: str | None, clock: str | None) -> int | None:
    """新浪 ``日期 + 时间`` → epoch ms (北京时间)。缺时间则 None, 不伪造。"""
    if not day or not clock:
        return None
    try:
        dt = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BJ)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _instruments_path(name: str) -> Path:
    return settings.data_dir / name / f"{name}.parquet"


def _load_symbols() -> list[str]:
    """股票 + ETF 全集 (与 quote_service 的 CN_Equity_A + CN_ETF 同覆盖)。"""
    global _universe_cache
    now = time.monotonic()
    if _universe_cache is not None and now - _universe_cache[0] < _UNIVERSE_TTL_S:
        return _universe_cache[1]
    symbols: list[str] = []
    for name in ("instruments", "instruments_etf"):
        path = _instruments_path(name)
        if not path.exists():
            continue
        try:
            df = pl.read_parquet(path, columns=["symbol"])
        except Exception as e:  # noqa: BLE001 — 维表损坏不应让实时行情整轮失败
            logger.warning("新浪源读取 %s 失败: %s", path, e)
            continue
        symbols.extend(df["symbol"].cast(pl.Utf8).to_list())
    symbols = list(dict.fromkeys(s for s in symbols if to_sina_code(s)))
    _universe_cache = (now, symbols)
    return symbols


def _parse_snapshot(fields: list[str], *, index: bool = False) -> dict | None:
    """新浪快照字段 → 内部 realtime record。字段不足或价格异常 → None。"""
    if len(fields) < 10:
        return None
    last = _f(fields[3])
    prev = _f(fields[2])
    if last is None or last <= 0:
        return None  # 停牌/无行情: 不得进入 enriched (会把 change_pct 打成 -100%)
    volume_raw = _f(fields[8])
    amount = _f(fields[9])
    change_amount = last - prev if prev not in (None, 0) else None
    return {
        "name": fields[0],
        "last_price": last,
        "prev_close": prev,
        "open": _f(fields[1]),
        "high": _f(fields[4]),
        "low": _f(fields[5]),
        # 指数成交量量纲为手, 个股/ETF 为股 → 各自按契约处理
        "volume": volume_raw if index else _hand(volume_raw),
        "amount": amount,
        "change_pct": (change_amount / prev) if change_amount is not None and prev else None,
        "change_amount": change_amount,
        "amplitude": None,      # 由 enriched 统一口径计算, 不在此启发式推导
        "turnover_rate": None,  # 需股本口径, 交给 enriched 管道
        "timestamp": _ts_ms(fields[30] if len(fields) > 31 else None,
                            fields[31] if len(fields) > 31 else None),
        "session": None,
    }


def _parse_depth(fields: list[str], ts_ms: int | None) -> dict | None:
    """新浪快照 → 五档盘口 (价: 元, 量: 手)。无盘口 (指数/无五档) → None。"""
    if len(fields) < 30:
        return None
    bid_prices = [_f(fields[i]) for i in (11, 13, 15, 17, 19)]
    ask_prices = [_f(fields[i]) for i in (21, 23, 25, 27, 29)]
    if all(p is None or p <= 0 for p in bid_prices) or all(p is None or p <= 0 for p in ask_prices):
        return None
    bid_vols = [_f(fields[i]) for i in (10, 12, 14, 16, 18)]
    ask_vols = [_f(fields[i]) for i in (20, 22, 24, 26, 28)]
    return {
        "bid_prices": [p or 0.0 for p in bid_prices],
        "bid_volumes": [_hand(v) or 0 for v in bid_vols],
        "ask_prices": [p or 0.0 for p in ask_prices],
        "ask_volumes": [_hand(v) or 0 for v in ask_vols],
        "timestamp": ts_ms,
    }


class SinaProvider:
    """新浪财经公开行情源 (realtime / depth5 / minute)。"""

    name = "sina"
    builtin = True

    def __init__(self) -> None:
        self._client: SinaClient | None = None

    def _get_client(self) -> SinaClient:
        if self._client is None:
            self._client = SinaClient()
        return self._client

    def close(self) -> None:  # loader.load_all 重建注册表时对每个 provider 调用
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场 (股票 + ETF) 快照。失败批次跳过; 全失败返回 []。"""
        client = self._get_client()
        symbols = _load_symbols()
        if not symbols:
            logger.warning("新浪源: instruments 维表为空, 无法确定全市场代码")
            return []
        codes = [(to_sina_code(s), s) for s in symbols]
        codes = [(c, s) for c, s in codes if c]
        records: list[dict] = []
        failed = 0
        for i in range(0, len(codes), MAX_BATCH):
            batch = codes[i : i + MAX_BATCH]
            try:
                rows = client.quotes([c for c, _ in batch])
            except SinaError as e:
                failed += 1
                logger.warning("新浪快照批次失败 (第 %d 批): %s", i // MAX_BATCH + 1, e)
                continue
            by_code = {c: fields for c, fields in rows}
            for code, symbol in batch:
                fields = by_code.get(code)
                if not fields:
                    continue
                rec = _parse_snapshot(fields)
                if rec is None:
                    continue
                rec["symbol"] = symbol
                records.append(rec)
        if failed and not records:
            logger.warning("新浪源: 全部 %d 批快照请求失败", failed)
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """核心指数补充 (A 股快照不含指数)。None=请求失败, []=成功但无数据。"""
        wanted = [(to_sina_code(s), s) for s in (symbols or [])]
        wanted = [(c, s) for c, s in wanted if c]
        if not wanted:
            return []
        client = self._get_client()
        try:
            rows = client.quotes([c for c, _ in wanted])
        except SinaError as e:
            logger.warning("新浪指数快照失败: %s", e)
            return None
        by_code = {c: fields for c, fields in rows}
        out: list[dict] = []
        for code, symbol in wanted:
            fields = by_code.get(code)
            rec = _parse_snapshot(fields, index=True) if fields else None
            if rec is None:
                continue
            rec["symbol"] = symbol
            out.append(rec)
        return out

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """批量五档。新浪快照与 realtime 同端点, 因此这里是 1~N 次真实请求。"""
        wanted = [(to_sina_code(s), s) for s in (symbols or [])]
        wanted = [(c, s) for c, s in wanted if c]
        if not wanted:
            return {}
        client = self._get_client()
        out: dict[str, dict] = {}
        fallback_ms = int(time.time() * 1000)
        for i in range(0, len(wanted), MAX_BATCH):
            batch = wanted[i : i + MAX_BATCH]
            try:
                rows = client.quotes([c for c, _ in batch])
            except SinaError as e:
                logger.warning("新浪五档批次失败 (第 %d 批): %s", i // MAX_BATCH + 1, e)
                continue
            by_code = {c: fields for c, fields in rows}
            for code, symbol in batch:
                fields = by_code.get(code)
                if not fields:
                    continue
                ts = _ts_ms(fields[30] if len(fields) > 31 else None,
                            fields[31] if len(fields) > 31 else None) or fallback_ms
                entry = _parse_depth(fields, ts)
                if entry is not None:
                    out[symbol] = entry
        return out

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """分钟K → 内部契约 (symbol/datetime 北京墙钟 naive/OHLCV/volume 手/amount 元)。

        单标的可回溯上限 1023 根 (≈4 个交易日); 超过的部分本源拿不到, 由上层决定
        是否回退到权威源。freq 仅支持 1m, 其余空手而还。
        """
        chunks = list(self.iter_minute(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, freq=freq, on_chunk_done=on_chunk_done,
        ))
        parts = [df for df in chunks if not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_MINUTE_SCHEMA)

    def iter_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """逐标的 1m K (0 行也 yield 一帧, 便于上层算进度)。

        请求失败直接抛 SinaError — 历史同步必须能标记失败, 不得把失败伪装成
        「无数据」。
        """
        if not symbols or freq != "1m":
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        client = self._get_client()
        end_dt = end_time or datetime.now()
        if end_dt.tzinfo is not None:
            end_dt = end_dt.astimezone(_BJ).replace(tzinfo=None)
        start_dt = None
        if start_time is not None:
            start_dt = (start_time.astimezone(_BJ).replace(tzinfo=None)
                        if start_time.tzinfo is not None else start_time)
        total = len(symbols)
        for i, symbol in enumerate(symbols):
            code = to_sina_code(symbol)
            if code is None:
                yield pl.DataFrame(schema=_MINUTE_SCHEMA)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
                continue
            bars = client.minute_bars(code)
            rows = _minute_rows(symbol, bars, start_dt, end_dt)
            yield pl.DataFrame(rows, schema=_MINUTE_SCHEMA)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
            time.sleep(0.05)  # 单标的节流: 实测 0.05s 间隔连发未被限频

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = [s for s in (symbols or []) if to_sina_code(s)][:3] or ["600000.SH"]
        try:
            if dataset == "realtime":
                rows = self.get_realtime()
                return {"provider": self.name, "dataset": dataset,
                        "rows": len(rows), "sample": rows[:1]}
            if dataset == "depth5":
                books = self.get_depth_batch(syms)
                return {"provider": self.name, "dataset": dataset,
                        "rows": len(books), "sample": [{"symbol": k, **v} for k, v in list(books.items())[:1]]}
            if dataset == "minute":
                df = self.get_minute(syms, None, None)
                return {"provider": self.name, "dataset": dataset,
                        "rows": df.height, "sample": df.head(1).to_dicts()}
        except SinaError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        return {"provider": self.name, "dataset": dataset, "rows": 0, "error": f"不支持的数据集: {dataset}"}


def _minute_rows(
    symbol: str,
    bars: list[dict],
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> list[dict]:
    """新浪分钟行 → 内部行 (datetime 北京墙钟, volume 股→手, amount 元直通)。"""
    out: list[dict] = []
    for bar in bars:
        day = str(bar.get("day") or "")
        try:
            dt = datetime.strptime(day, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if start_dt is not None and dt < start_dt:
            continue
        if end_dt is not None and dt > end_dt:
            continue
        close = _f(bar.get("close"))
        if close is None:
            continue  # 无成交分钟不伪造
        volume = _f(bar.get("volume"))
        out.append({
            "symbol": symbol,
            "datetime": dt,
            "open": _f(bar.get("open")),
            "high": _f(bar.get("high")),
            "low": _f(bar.get("low")),
            "close": close,
            "volume": float(_hand(volume)) if volume is not None else None,
            "amount": _f(bar.get("amount")),
        })
    return out


def availability() -> tuple[bool, str]:
    """可用性: 无需 Key, 恒可用 (网络失败由运行时降级处理)。"""
    return True, "ok"
