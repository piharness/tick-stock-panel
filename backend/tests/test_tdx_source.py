"""通达信裸协议源契约测试。

覆盖: 代码映射 (含北交所旧代码拦截)、快照/五档量纲、停牌行丢弃、
分钟K 量纲 (股→手) 与时间解析、翻页终止条件、窗口过滤、
批量失败隔离、串票防护 (服务端返回非请求代码必须丢弃)。

全部用假传输层, 不依赖网络与 tdxrs。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from app.plugins.tdx import provider as tdx


class FakeClient:
    """最小传输层替身: 按 (category, market, code) 返回预置行。"""

    def __init__(self, bars=None, quotes=None, fail_codes=()):
        self._bars = bars or {}
        self._quotes = quotes or {}
        self.fail_codes = set(fail_codes)
        self.bar_calls: list[tuple] = []
        self.quote_calls: list[list] = []

    def bars(self, market, code, category, offset, count):
        self.bar_calls.append((market, code, category, offset, count))
        if code in self.fail_codes:
            raise tdx.TdxError("boom")
        pages = self._bars.get(code) or []
        idx = offset // 800
        return pages[idx] if idx < len(pages) else []

    def quotes(self, pairs):
        self.quote_calls.append(list(pairs))
        out = []
        for market, code in pairs:
            if code in self.fail_codes:
                raise tdx.TdxError("boom")
            out.extend(self._quotes.get(code) or [])
        return out


def _bar(dt: str, o=10.0, h=10.5, l=9.8, c=10.2, vol=123400.0, amount=1234567.0) -> dict:
    return {"datetime": dt, "open": o, "high": h, "low": l, "close": c, "vol": vol, "amount": amount}


def _quote(code="600519", price=1410.27, prev=1402.3, vol=32372.0, amount=4.5e9) -> dict:
    row = {
        "code": code, "price": price, "last_close": prev, "open": 1395.13,
        "high": 1411.0, "low": 1388.11, "vol": vol, "amount": amount,
        "servertime": "15:15:9174",
    }
    for i in range(1, 6):
        row[f"bid{i}"] = price - i * 0.05
        row[f"bid_vol{i}"] = 10 * i
        row[f"ask{i}"] = price + i * 0.05
        row[f"ask_vol{i}"] = 7 * i
    return row


# ---------------------------------------------------------------------------
# 代码映射
# ---------------------------------------------------------------------------
def test_to_tdx_markets():
    assert tdx.to_tdx("600519.SH") == (tdx.MARKET_SH, "600519")
    assert tdx.to_tdx("000001.SZ") == (tdx.MARKET_SZ, "000001")
    assert tdx.to_tdx("920002.BJ") == (tdx.MARKET_BJ, "920002")
    assert tdx.to_tdx("510300.SH") == (tdx.MARKET_SH, "510300")


def test_to_tdx_rejects_legacy_bj():
    # 服务端对 43x/83x/87x 会返回 600839 的行情, 必须在映射层拦掉
    assert tdx.to_tdx("430047.BJ") is None
    assert tdx.to_tdx("831010.BJ") is None
    assert tdx.to_tdx("870508.BJ") is None


def test_to_tdx_rejects_malformed():
    assert tdx.to_tdx("600519") is None
    assert tdx.to_tdx("600519.HK") is None
    assert tdx.to_tdx("") is None
    assert tdx.to_tdx("60051.SH") is None


# ---------------------------------------------------------------------------
# 快照 / 五档
# ---------------------------------------------------------------------------
def test_quote_record_units_and_pct():
    rec = tdx._quote_record("600519.SH", _quote(), 1_700_000_000_000)
    assert rec["volume"] == 32372.0          # 已是手, 不再除 100
    assert rec["amount"] == 4.5e9            # 元
    assert rec["change_pct"] == pytest.approx((1410.27 - 1402.3) / 1402.3)
    assert rec["timestamp"] == 1_700_000_000_000
    assert rec["amplitude"] is None and rec["turnover_rate"] is None


def test_quote_record_drops_halted():
    assert tdx._quote_record("600519.SH", _quote(price=0.0), 1) is None
    assert tdx._quote_record("600519.SH", _quote(prev=0.0), 1) is None


def test_depth_record_units_and_shape():
    rec = tdx._depth_record(_quote(), 1_700_000_000_000)
    assert len(rec["bid_prices"]) == 5 and len(rec["ask_prices"]) == 5
    assert rec["bid_volumes"] == [10, 20, 30, 40, 50]
    assert rec["ask_volumes"] == [7, 14, 21, 28, 35]
    assert rec["bid_prices"][0] < rec["ask_prices"][0]
    assert isinstance(rec["bid_volumes"][0], int)


def test_depth_record_drops_no_book():
    flat = {"code": "600519", "price": 10.0}
    for i in range(1, 6):
        flat[f"bid{i}"] = 0.0
        flat[f"bid_vol{i}"] = 0.0
        flat[f"ask{i}"] = 0.0
        flat[f"ask_vol{i}"] = 0.0
    assert tdx._depth_record(flat, 1) is None


def test_depth_drops_foreign_code():
    """串票防护: 请求 430047 服务端回 600839, 必须整条丢弃而不是按位置对齐。"""
    p = tdx.TdxProvider()
    p._client = FakeClient(quotes={"430047": [_quote(code="600839")]})
    assert p.get_depth_batch(["430047.BJ"]) == {}
    # 映射层已拦截, 连请求都不会发出
    assert p._client.quote_calls == []


def test_depth_batch_maps_symbols():
    p = tdx.TdxProvider()
    p._client = FakeClient(quotes={"600519": [_quote(code="600519")]})
    out = p.get_depth_batch(["600519.SH", "430047.BJ"])
    assert list(out) == ["600519.SH"]
    assert p._client.quote_calls == [[(tdx.MARKET_SH, "600519")]]


# ---------------------------------------------------------------------------
# 分钟 K
# ---------------------------------------------------------------------------
def test_minute_rows_vol_shares_to_hand():
    rows = tdx._minute_rows("600519.SH", [_bar("2026-11-12 09:31", vol=123400.0)])
    assert rows[0]["volume"] == 1234.0        # floor(123400/100) 手
    assert rows[0]["datetime"] == datetime(2026, 11, 12, 9, 31)
    assert rows[0]["amount"] == 1234567.0


def test_minute_rows_skips_unparsable():
    assert tdx._minute_rows("600519.SH", [{"datetime": "", "close": 1.0}]) == []
    assert tdx._minute_rows("600519.SH", [{"datetime": "2026-11-12 09:31"}]) == []


def test_get_minute_filters_window(monkeypatch):
    monkeypatch.setattr(tdx, "_calibrate_day", lambda symbol, rows: rows)
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={"600519": [[
        _bar("2026-11-10 14:59"), _bar("2026-11-11 09:31"), _bar("2026-11-11 09:32"),
    ]]})
    df = p.get_minute(["600519.SH"], datetime(2026, 11, 11), datetime(2026, 11, 12))
    assert df.height == 2
    assert df["datetime"].to_list() == [datetime(2026, 11, 11, 9, 31), datetime(2026, 11, 11, 9, 32)]
    assert df.schema["volume"] == pl.Float64


def test_get_minute_accepts_tz_aware_window(monkeypatch):
    """调用方窗口带时区 (kline_sync 传 CN_TZ), bar 是墙钟 naive。

    2026-11-12 20:11 起 25 次单股补采全部死在
    `TypeError: can't compare offset-naive and offset-aware datetimes`,
    历史分钟缺口一行都没补上。这条锁的就是"两种写法必须能比"。
    """
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(tdx, "_calibrate_day", lambda symbol, rows: rows)
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={"600519": [[
        _bar("2026-11-11 09:31"), _bar("2026-11-11 09:32"), _bar("2026-11-11 15:00"),
    ]]})
    bj = ZoneInfo("Asia/Shanghai")
    df = p.get_minute(
        ["600519.SH"],
        datetime(2026, 11, 11, 9, 25, tzinfo=bj),
        datetime(2026, 11, 11, 15, 5, tzinfo=bj),
    )
    assert df.height == 3


def test_get_minute_tz_window_still_filters(monkeypatch):
    """折齐口径后窗口语义必须仍然生效 (不是"能比就算过")。"""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(tdx, "_calibrate_day", lambda symbol, rows: rows)
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={"600519": [[
        _bar("2026-11-11 09:25"), _bar("2026-11-11 09:31"), _bar("2026-11-11 15:00"),
    ]]})
    bj = ZoneInfo("Asia/Shanghai")
    df = p.get_minute(
        ["600519.SH"],
        datetime(2026, 11, 11, 9, 30, tzinfo=bj),
        datetime(2026, 11, 11, 15, 0, tzinfo=bj),
    )
    assert df["datetime"].to_list() == [datetime(2026, 11, 11, 9, 31)]


def test_get_minute_pages_until_start_covered(monkeypatch):
    monkeypatch.setattr(tdx, "_calibrate_day", lambda symbol, rows: rows)
    # close 逐根不同: 同价同量的 bar 会被「13:00 是 11:30 副本」的修标逻辑当成副本丢掉
    # (这根 fixture 的时间列会走过 13:00), 那是修标的正确行为, 不是分页的。
    page0 = [_bar(f"2026-11-12 {9 + i // 60:02d}:{i % 60:02d}", c=10.0 + i * 0.001)
             for i in range(800)]
    page1 = [_bar("2026-11-09 09:31")]
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={"600519": [page0, page1]})
    df = p.get_minute(["600519.SH"], datetime(2026, 11, 1), None)
    assert df.height == 801
    assert len(p._client.bar_calls) == 2
    assert p._client.bar_calls[1][3] == 800      # offset 前进一页


def test_get_minute_stops_on_short_page(monkeypatch):
    monkeypatch.setattr(tdx, "_calibrate_day", lambda symbol, rows: rows)
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={"600519": [[_bar("2026-11-12 09:31")]]})
    p.get_minute(["600519.SH"], datetime(2020, 1, 1), None)
    assert len(p._client.bar_calls) == 1         # 不足 800 根即到底, 不再翻


def test_get_minute_isolates_failures(monkeypatch):
    monkeypatch.setattr(tdx, "_calibrate_day", lambda symbol, rows: rows)
    p = tdx.TdxProvider()
    p._client = FakeClient(
        bars={"600519": [[_bar("2026-11-12 09:31")]]},
        fail_codes={"600036"},
    )
    df = p.get_minute(["600519.SH", "600036.SH"], None, None)
    assert df.height == 1
    assert df["symbol"].to_list() == ["600519.SH"]


def test_get_minute_rejects_other_freq():
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={"600519": [[_bar("2026-11-12 09:31")]]})
    assert p.get_minute(["600519.SH"], None, None, freq="5m").height == 0
    assert p._client.bar_calls == []


# ---------------------------------------------------------------------------
# 全量分钟
# ---------------------------------------------------------------------------
def test_intraday_batch_keeps_today_only(monkeypatch):
    monkeypatch.setattr(tdx, "_calibrate_today", lambda rows, price=None: rows)
    today = datetime.now(tdx._BEIJING).date().isoformat()
    yesterday = "2020-01-02"
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={"600519": [[
        _bar(f"{yesterday} 14:59"), _bar(f"{today} 09:31"),
    ]]})
    df = p.get_intraday_batch(["600519.SH"])
    assert df.height == 1
    assert df["datetime"].to_list()[0].date().isoformat() == today


def test_intraday_batch_skips_unsupported_asset():
    p = tdx.TdxProvider()
    p._client = FakeClient()
    assert p.get_intraday_batch(["600519.SH"], asset_type="index").height == 0


# ---------------------------------------------------------------------------
# 实时快照整轮
# ---------------------------------------------------------------------------
def test_realtime_isolates_batch_failures(monkeypatch):
    """一批整体失败时, 只丢该批; 后续批次照常返回 (与 tdxquant 同语义)。"""
    universe = ["600036.SH"] + [f"60{i:04d}.SH" for i in range(100, 159)] + ["600519.SH"]
    monkeypatch.setattr(tdx, "_load_universe", lambda: universe)
    p = tdx.TdxProvider()
    p._client = FakeClient(
        quotes={"600519": [_quote(code="600519")]},
        fail_codes={"600036"},
    )
    records = p.get_realtime()
    assert [r["last_price"] for r in records] == [1410.27]
    assert [len(c) for c in p._client.quote_calls] == [60, 1]


def test_realtime_empty_universe(monkeypatch):
    monkeypatch.setattr(tdx, "_load_universe", lambda: [])
    p = tdx.TdxProvider()
    p._client = FakeClient()
    assert p.get_realtime() == []
    assert p._client.quote_calls == []


def test_realtime_batches_at_60(monkeypatch):
    syms = [f"60{i:04d}.SH" for i in range(130)]
    monkeypatch.setattr(tdx, "_load_universe", lambda: syms)
    p = tdx.TdxProvider()
    p._client = FakeClient()
    p.get_realtime()
    assert [len(c) for c in p._client.quote_calls] == [60, 60, 10]


def test_depth_guard_warns_on_low_coverage(caplog):
    """覆盖率红线: 大批标的不出盘口时必须留下告警 (否则封单梯队静默变空)。"""
    import logging

    p = tdx.TdxProvider()
    quotes = {"600519": [_quote(code="600519")]}
    p._client = FakeClient(quotes=quotes)
    with caplog.at_level(logging.WARNING, logger="app.plugins.tdx.provider"):
        out = p.get_depth_batch(["600519.SH", "600036.SH", "601318.SH", "600030.SH"])
    assert len(out) == 1                      # 缺书的不伪造
    assert any("覆盖率异常" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 复权校准 (TDX 分钟默认前复权, 必须换算回原始价)
# ---------------------------------------------------------------------------
def test_calibrate_day_scales_ohlc(monkeypatch):
    """纯乘法比例: 原始收盘/前复权收盘 → 整日 OHLC 复原, amount/volume 不动。"""
    daily = pl.DataFrame({"symbol": ["600519.SH"], "close": [1212.1]})
    monkeypatch.setattr(tdx, "_daily_close", lambda day: daily)
    rows = tdx._minute_rows("600519.SH", [
        _bar("2026-06-25 09:31", o=1180.0, h=1185.0, l=1179.0, c=1184.076, amount=1e8),
    ])
    out = tdx._calibrate_day("600519.SH", rows)
    ratio = 1212.1 / 1184.076
    assert out[0]["close"] == pytest.approx(1212.1)
    assert out[0]["open"] == pytest.approx(1180.0 * ratio)
    assert out[0]["high"] == pytest.approx(1185.0 * ratio)
    assert out[0]["low"] == pytest.approx(1179.0 * ratio)
    assert out[0]["amount"] == rows[0]["amount"]      # 成交额是真实金额, 不缩放
    assert out[0]["volume"] == rows[0]["volume"]


def test_calibrate_day_drops_without_daily(monkeypatch):
    monkeypatch.setattr(tdx, "_daily_close", lambda day: None)
    rows = tdx._minute_rows("600519.SH", [_bar("2026-06-25 09:31")])
    assert tdx._calibrate_day("600519.SH", rows) == []


def test_calibrate_day_drops_symbol_missing_in_daily(monkeypatch):
    daily = pl.DataFrame({"symbol": ["000001.SZ"], "close": [11.0]})
    monkeypatch.setattr(tdx, "_daily_close", lambda day: daily)
    rows = tdx._minute_rows("600519.SH", [_bar("2026-06-25 09:31")])
    assert tdx._calibrate_day("600519.SH", rows) == []


def test_calibrate_by_day_uses_per_day_ratio(monkeypatch):
    """除权日两侧不是同一个倍数: 必须逐日锚定, 不能拿一个比例套整段。"""
    daily = {"2026-06-24": pl.DataFrame({"symbol": ["600519.SH"], "close": [1200.0]}),
             "2026-06-25": pl.DataFrame({"symbol": ["600519.SH"], "close": [1212.1]})}
    monkeypatch.setattr(tdx, "_daily_close", lambda day: daily.get(day))
    rows = tdx._minute_rows("600519.SH", [
        _bar("2026-06-24 15:00", c=1000.0),
        _bar("2026-06-25 15:00", c=1184.076),
    ])
    out = tdx._calibrate_by_day("600519.SH", rows)
    assert out[0]["close"] == pytest.approx(1200.0)
    assert out[1]["close"] == pytest.approx(1212.1)


def _day_bars(n: int = 30, raw: float = 10.0, scale: float = 1.0, noise: float = 0.0) -> list[dict]:
    """构造 n 根当日 bar: amount/volume 是真实量 (不受复权影响), close 已乘 scale。"""
    rows: list[dict] = []
    for i in range(n):
        vol = 20000.0
        amount = raw * vol * (1 + noise * (1 if i % 2 else -1))
        rows.extend(tdx._minute_rows("600519.SH", [_bar(
            f"2026-11-12 10:{i:02d}", o=raw * scale, h=raw * scale,
            l=raw * scale, c=raw * scale, vol=vol, amount=amount,
        )]))
    return rows


def test_calibrate_today_recovers_injected_scale():
    """正控: 前复权把 close 乘了 0.95, 成交额仍是原始额 ⇒ 必须还原回 10.00。"""
    out = tdx._calibrate_today(_day_bars(scale=0.95))
    assert out[0]["close"] == pytest.approx(10.0, rel=1e-9)
    assert out[0]["open"] == pytest.approx(10.0, rel=1e-9)
    assert out[0]["high"] == pytest.approx(10.0, rel=1e-9)
    assert out[0]["low"] == pytest.approx(10.0, rel=1e-9)


def test_calibrate_today_ignores_snapshot_price():
    """负控: 快照价与最后一根 close 不同时刻, 不许再当分母。

    旧实现 `raw_price / 最后一根 close` 会把这个差乘进整日 OHLC —— 全市场一轮
    9~10 分钟, 排尾标的等于被注入 10 分钟涨跌 (2026-11-12 中位 21bp)。
    """
    rows = _day_bars(scale=1.0)
    assert tdx._calibrate_today(rows, 23.78)[0]["close"] == pytest.approx(10.0)
    assert tdx._calibrate_today(rows, 0.5)[0]["close"] == pytest.approx(10.0)


def test_calibrate_today_leaves_raw_alone_with_noise():
    """薄票: 反读系数偏 10bp, 但逐 bar 散布让 3·se≈24bp ⇒ 不许动手。

    这一条锁的是判据里的 `3*se` 项**本身**。删掉它, tol 退成 5bp, 10bp 的
    偏离会被当成复权乘进去 (10.00 → 10.01)。

    旧写法用交替 ±10bp 的噪声: 30 根里 15 个 +、15 个 -, 中位恰为 1.0 ⇒
    shift=0, 无论 tol 是多少都不动 —— 测的是"零偏离不动", 不是"噪声大所以
    不动"。按 2026-11-12 实测 MAD, 3·se≈2e-4 < 5e-4, 该项在两天数据上
    从未生效, 变异因此存活。
    """
    rows: list[dict] = []
    for i in range(30):
        vol = 20000.0
        # 一半 0.998、一半 1.004 ⇒ 中位 1.001 (10bp), MAD 0.003 ⇒ 3·se≈24bp
        ratio = 0.998 if i % 2 else 1.004
        rows.extend(tdx._minute_rows("600519.SH", [_bar(
            f"2026-11-12 10:{i:02d}", o=10.0, h=10.0, l=10.0, c=10.0,
            vol=vol, amount=10.0 * vol * ratio,
        )]))
    est = tdx._scale_from_amount(rows)
    assert est is not None
    med, tol, n = est
    assert abs(med - 1.0) > tdx._SCALE_MIN_SHIFT   # 偏离确实大于 5bp 的效应量下限
    assert 3.0 * 1.4826 * 0.003 / (n ** 0.5) > abs(med - 1.0)  # 但落在 3·se 之内
    out = tdx._calibrate_today(rows)
    assert out[0]["close"] == pytest.approx(10.0)


def test_calibrate_today_skips_small_sample():
    """样本不足 (半日停牌 / 刚上市): 不动, 不猜。"""
    rows = _day_bars(n=tdx._SCALE_MIN_BARS - 1, scale=0.95)
    assert tdx._calibrate_today(rows)[0]["close"] == pytest.approx(9.5)


def test_calibrate_today_rejects_absurd_scale():
    """量纲混淆 (如 amount 记成万元) 会反读出荒谬系数 ⇒ 拒缩, 不许写出 1000× 价格。"""
    rows = _day_bars(scale=1.0)
    for r in rows:
        r["amount"] = r["amount"] * 1000.0
    assert tdx._calibrate_today(rows)[0]["close"] == pytest.approx(10.0)


def test_calibrate_today_ignores_sub_tick_bias():
    """廉价股 + 10bp 系统偏差: 3.80 元的 10bp = 0.0038 元, 不到半跳, 不许动手。

    这一条锁的是判据里"半个最小变动价位"那一项: 去掉它, 阈值退成 5bp,
    这条会被误缩 10bp (价格从 3.80 变 3.8038)。
    """
    rows = _day_bars(raw=3.80, scale=1.0)
    for r in rows:
        r["amount"] = r["amount"] * 1.001
    assert tdx._calibrate_today(rows)[0]["close"] == pytest.approx(3.80)


def test_calibrate_today_passthrough_on_single_bar():
    rows = tdx._minute_rows("600519.SH", [_bar("2026-11-12 15:00", c=1410.27)])
    assert tdx._calibrate_today(rows, None)[0]["close"] == pytest.approx(1410.27)


def test_config_shim_exposes_declared_datasets():
    """provider_has_dataset 读 provider.config.datasets; 缺 shim 时抛 AttributeError。

    该异常发生在 policy._augment_custom_sources 的唯一一个 try 内, 会让整条
    _DATASET_CAP_MAP 循环中止 —— minute/depth5/financial/full_minute 四项能力
    一起不被授予 (2026-11-12 19:45 实测)。所以这里锁的是「shim 必须存在且覆盖
    plugin.yaml 声明的全部数据集」, 不是某个具体数据集。
    """
    p = tdx.TdxProvider()
    assert getattr(p, "config", None) is not None
    assert set(p.config.datasets) == set(tdx._DATASETS)
    for ds in tdx._DATASETS:
        assert ds in p.config.datasets


# ---------------------------------------------------------------------------
# 全市场扫描游标
# ---------------------------------------------------------------------------
def _syms(n: int) -> list[str]:
    return [f"{i:06d}.SZ" for i in range(n)]


def test_scan_slice_advances_and_wraps(tmp_path, monkeypatch):
    """每轮推进一段并环回 —— 这条锁的是"落盘不许钉在前缀上"。

    没有游标时每轮都从 sorted 头部开始, 2026-11-12 全市场只落了
    000001..000050 (32 只, 逐字连续), 000426 那种排在 100 位的真实除权
    偏离一次都没被采到。
    """
    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    syms = _syms(10)
    batch, day, nxt = tdx._scan_slice(syms, 4)
    assert batch == syms[:4] and nxt == 4
    tdx._write_cursor(day, nxt)
    batch, _, nxt = tdx._scan_slice(syms, 4)
    assert batch == syms[4:8] and nxt == 8
    tdx._write_cursor(day, nxt)
    batch, _, nxt = tdx._scan_slice(syms, 4)
    assert batch == syms[8:] + syms[:2] and nxt == 2


def test_scan_slice_resets_across_days(tmp_path, monkeypatch):
    """跨日归零: 新的一天整表重扫, 不继承昨日的 offset。"""
    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    tdx._write_cursor("2026-11-11", 8)
    syms = _syms(10)
    batch, day, _ = tdx._scan_slice(syms, 4)
    assert day != "2026-11-11"
    assert batch == syms[:4]


def test_scan_slice_tolerates_corrupt_cursor(tmp_path, monkeypatch):
    """游标损坏只许退化成"从头开始", 不许抛异常挡住取数。"""
    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    path = tdx._cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    batch, _, _ = tdx._scan_slice(_syms(10), 4)
    assert batch == _syms(10)[:4]


def test_intraday_batch_advances_cursor_even_when_all_fail(tmp_path, monkeypatch):
    """整批失败也必须推进: 否则坏票会把后面所有标的永远挡在外面 (前缀饥饿),
    而这正是游标要消除的病。"""
    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    monkeypatch.setattr(tdx, "_calibrate_today", lambda rows: rows)
    syms = ["600519.SH", "600520.SH", "600521.SH"]
    p = tdx.TdxProvider()
    p._client = FakeClient(bars={}, fail_codes={"600519", "600520", "600521"})
    assert p.get_intraday_batch(syms, limit=2).is_empty()
    day = tdx.datetime.now(tdx._BEIJING).date().isoformat()
    assert tdx._read_cursor(day, len(syms)) == 2


def test_intraday_latest_uses_instruments_not_etf(tmp_path, monkeypatch):
    """symbols=None 时增量轮必须读股票维表, 不许空转 (旧实现 `if not symbols:
    return 空` ⇒ 增量轮恒空 ⇒ 每轮退化成全量), 也不许混进 ETF。"""
    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    seen: list[str] = []

    def fake_batch(symbols, count=300, asset_type="stock", limit=tdx._SCAN_BATCH):
        seen.extend(symbols)
        return tdx._frame([])

    monkeypatch.setattr(tdx.TdxProvider, "get_intraday_batch", staticmethod(fake_batch))
    monkeypatch.setattr(tdx, "_load_symbols", lambda name: ["600519.SH", "000001.SZ"])
    p = tdx.TdxProvider()
    p.get_intraday_latest()
    assert seen == ["600519.SH", "000001.SZ"]


def test_pages_for_window_covers_declared_depth():
    """翻页上限必须够到源声明的 98 个交易日。

    固定 8 页只够 26 个交易日, 而服务端实测 23520 根 / 98 天 —— 更早的日期
    翻不到窗口, 返回空帧, 上游把「翻页不够」报成「数据源未提供」。
    """
    now = datetime(2026, 11, 12, 20, 0)
    pages = tdx._pages_for_window(datetime(2026, 6, 28), now=now)
    assert pages * tdx._MINUTE_PAGE_ROWS >= 98 * tdx._BARS_PER_DAY
    # 硬上限仍要压住无界翻页 (服务端深度之外再多翻是纯耗时)
    assert tdx._pages_for_window(datetime(2015, 1, 1), now=now) == tdx._MINUTE_MAX_PAGES


def test_pages_for_window_single_day_is_one_page():
    """单日窗口只需 1 页: 固定 8 页等于每次点开历史分时白发 7 个请求。"""
    now = datetime(2026, 11, 12, 20, 0)
    assert tdx._pages_for_window(datetime(2026, 11, 11, 9, 25), now=now) == 1
    assert tdx._pages_for_window(None, now=now) == 1


def test_pages_for_window_shrinks_with_earlier_start():
    """窗口越早页数越多 (单调), 否则「按窗口算」只是换了个常量。"""
    now = datetime(2026, 11, 12, 20, 0)
    near = tdx._pages_for_window(datetime(2026, 10, 20), now=now)
    far = tdx._pages_for_window(datetime(2026, 8, 20), now=now)
    assert 1 < near < far


def test_minute_empty_frame_carries_call_failure_reason(monkeypatch):
    """通道抛错与源返回空必须分成两个原因码。

    二者都以空帧 fail-closed 返回, 但只有前者值得重试; 合成一句「数据源未提供」
    会让用户去换数据源。2026-11-12 实测: tdx 通道抛 TypeError 的那两天,
    界面对每一天、每一只都显示这句。
    """
    from app.services import kline_sync

    class Boom:
        def get_minute(self, *args, **kwargs):
            raise RuntimeError("can't compare offset-naive and offset-aware datetimes")

    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "tdx")
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda name: (Boom(), False, None))
    reason: list[str] = []
    df, fallback = kline_sync._try_custom_minute(
        ["600519.SH"], datetime(2026, 11, 11, 9, 25), datetime(2026, 11, 11, 15, 5),
        "stock", reason_out=reason,
    )
    assert df is not None and df.is_empty()
    assert fallback is False              # 仍然 fail-closed, 不跨源回退
    assert reason == ["provider_call_failed"]


def test_minute_empty_frame_carries_no_rows_reason(monkeypatch):
    """源确实没有这一天: 原因码必须是 no_rows, 不能与通道故障同码。"""
    from app.services import kline_sync

    class Empty:
        def get_minute(self, *args, **kwargs):
            return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "tdx")
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda name: (Empty(), False, None))
    reason: list[str] = []
    df, _ = kline_sync._try_custom_minute(
        ["600519.SH"], None, None, "stock", reason_out=reason,
    )
    assert df is not None and df.is_empty()
    assert reason == ["provider_no_rows"]


# ── 日线锚点: 必须跟得上 kline_daily 的分区布局 ──────────────────────────


def test_daily_close_reads_monthly_partition(tmp_path, monkeypatch):
    """2026-11 起 kline_daily 按月分区, 锚点读取不能拼 `date=D`。

    事故: `_daily_close` 硬编码 `kline_daily/date={day}/part.parquet` ⇒ 月分区
    落盘后**所有日期**都找不到锚点, `_calibrate_day` 逐只逐日跳过 —— 分钟回补
    一行没落, 而调用方只看到"已跳过"的 warning 刷屏 (2026-11-12 实测)。
    """
    day = "2026-11-05"
    part = tmp_path / "kline_daily" / "date=2026-11"
    part.mkdir(parents=True)
    # 月分区里有多天; 取错行就等于拿别的交易日的 close 当锚点
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600001.SH"],
        "close": [10.0, 20.0, 11.0],
        "date": [day, day, "2026-11-04"],
    }).write_parquet(part / "part.parquet")

    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    tdx._daily_cache.clear()

    df = tdx._daily_close(day)
    assert df is not None
    got = dict(zip(df["symbol"].to_list(), df["close"].to_list()))
    assert got == {"600001.SH": 10.0, "600002.SH": 20.0}, got


def test_daily_close_still_reads_legacy_day_partition(tmp_path, monkeypatch):
    """旧日分区 (2026-10 及以前) 必须继续可读 —— 混合布局是长期状态。"""
    day = "2026-10-30"
    part = tmp_path / "kline_daily" / f"date={day}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600001.SH"], "close": [9.5], "date": [day],
    }).write_parquet(part / "part.parquet")

    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    tdx._daily_cache.clear()

    df = tdx._daily_close(day)
    assert df is not None
    assert df["close"].to_list() == [9.5]


# ── 日线锚点: 必须跟得上 kline_daily 的分区布局 ──────────────────────────


def test_daily_close_reads_monthly_partition(tmp_path, monkeypatch):
    """2026-11 起 kline_daily 按月分区, 锚点读取不能拼 `date=D`。

    事故: `_daily_close` 硬编码 `kline_daily/date={day}/part.parquet` ⇒ 月分区
    落盘后**所有日期**都找不到锚点, `_calibrate_day` 逐只逐日跳过 —— 分钟回补
    一行没落, 而调用方只看到"已跳过"的 warning 刷屏 (2026-11-12 实测)。
    """
    day = "2026-11-05"
    part = tmp_path / "kline_daily" / "date=2026-11"
    part.mkdir(parents=True)
    # 月分区里有多天; 取错行就等于拿别的交易日的 close 当锚点
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600001.SH"],
        "close": [10.0, 20.0, 11.0],
        "date": [day, day, "2026-11-04"],
    }).write_parquet(part / "part.parquet")

    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    tdx._daily_cache.clear()

    df = tdx._daily_close(day)
    assert df is not None
    got = dict(zip(df["symbol"].to_list(), df["close"].to_list()))
    assert got == {"600001.SH": 10.0, "600002.SH": 20.0}, got


def test_daily_close_still_reads_legacy_day_partition(tmp_path, monkeypatch):
    """旧日分区 (2026-10 及以前) 必须继续可读 —— 混合布局是长期状态。"""
    day = "2026-10-30"
    part = tmp_path / "kline_daily" / f"date={day}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600001.SH"], "close": [9.5], "date": [day],
    }).write_parquet(part / "part.parquet")

    monkeypatch.setattr(tdx.settings, "data_dir", tmp_path)
    tdx._daily_cache.clear()

    df = tdx._daily_close(day)
    assert df is not None
    assert df["close"].to_list() == [9.5]
