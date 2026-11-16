"""新浪财经数据源契约测试。

覆盖: 代码前缀映射 (含北交所)、快照单位归一 (股→手/元)、五档顺序与量纲、
停牌行丢弃 (避免 change_pct 打成 -100%)、分钟窗口过滤与 volume 换算、
批量分片 (单请求 ≤800) 与批次失败不拖垮整轮。

全部用假客户端, 不依赖真实网络。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl

from app.plugins.sina import provider as sina


# 600000 的真实行形态: open, prev_close, last, high, low, bid1, ask1, vol(股), amount(元),
# 然后 5 组买量/买价、5 组卖量/卖价、日期、时间
_SAMPLE = (
    "600000,10.140,10.170,10.210,10.240,10.030,10.200,10.210,50081746,508447361.000,"
    "10500,10.200,11600,10.190,85162,10.180,15500,10.170,35900,10.160,"
    "34850,10.210,176100,10.220,432000,10.230,321729,10.240,287500,10.250,"
    "2026-11-12,15:34:59,00,D|3600|36756.00"
).split(",")


def _halted() -> list[str]:
    row = list(_SAMPLE)
    for i in (1, 2, 3, 4, 5, 6, 7):
        row[i] = "0.000"
    return row


def test_to_sina_code_prefixes():
    assert sina.to_sina_code("600000.SH") == "sh600000"
    assert sina.to_sina_code("000001.SZ") == "sz000001"
    assert sina.to_sina_code("920002.BJ") == "bj920002"
    assert sina.to_sina_code("600000") is None
    assert sina.to_sina_code("600000.HK") is None
    assert sina.to_sina_code("") is None


def test_snapshot_units_and_fields():
    rec = sina._parse_snapshot(_SAMPLE)
    assert rec is not None
    assert rec["name"] == "600000"  # 假数据首列是代码, 真实响应是简称
    assert rec["last_price"] == 10.21
    assert rec["prev_close"] == 10.17
    assert rec["open"] == 10.14
    assert rec["high"] == 10.24
    assert rec["low"] == 10.03
    # 股 → 手 (50081746 股 = 500817 手), 向下取整
    assert rec["volume"] == 500817
    assert rec["amount"] == 508447361.0
    # 小数制 (0.0393 = +3.93%), 不是百分数
    assert abs(rec["change_pct"] - (10.21 - 10.17) / 10.17) < 1e-12


def test_snapshot_timestamp_is_beijing_wall_clock():
    rec = sina._parse_snapshot(_SAMPLE)
    # 北京时间 15:34:59 = UTC 07:34:59
    assert rec["timestamp"] == 1794468899000


def test_snapshot_drops_halted_rows():
    assert sina._parse_snapshot(_halted()) is None
    assert sina._parse_snapshot(["x"] * 9) is None


def test_depth_order_and_units():
    entry = sina._parse_depth(_SAMPLE, 123)
    assert entry is not None
    assert entry["bid_prices"] == [10.20, 10.19, 10.18, 10.17, 10.16]
    assert entry["ask_prices"] == [10.21, 10.22, 10.23, 10.24, 10.25]
    # 10500 股 → 105 手; 顺序由买一到买五, 不得错位
    assert entry["bid_volumes"] == [105, 116, 851, 155, 359]
    assert entry["ask_volumes"] == [348, 1761, 4320, 3217, 2875]
    assert entry["timestamp"] == 123


def test_depth_none_for_index_book():
    row = list(_SAMPLE)
    for i in list(range(10, 30)):
        row[i] = "0"
    assert sina._parse_depth(row, None) is None


def test_minute_rows_window_and_units():
    bars = [
        {"day": "2026-11-12 09:30:00", "open": "1", "high": "1", "low": "1",
         "close": "1", "volume": "1000", "amount": "100"},
        {"day": "2026-11-12 09:31:00", "open": "1", "high": "2", "low": "1",
         "close": "2", "volume": "221900", "amount": "2226734.0182"},
        {"day": "bad", "close": "2"},
    ]
    rows = sina._minute_rows("600000.SH", bars, datetime(2026, 11, 12, 9, 31), None)
    assert len(rows) == 1
    assert rows[0]["datetime"] == datetime(2026, 11, 12, 9, 31)
    assert rows[0]["volume"] == 2219.0  # 股 → 手
    assert rows[0]["amount"] == 2226734.0182


class _FakeClient:
    def __init__(self, fail_batches: set[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_batches = fail_batches or set()

    def quotes(self, codes: list[str]) -> list[tuple[str, list[str]]]:
        assert len(codes) <= sina.MAX_BATCH
        idx = len(self.calls)
        self.calls.append(codes)
        if idx in self.fail_batches:
            raise sina.SinaError("boom")
        return [(c, _SAMPLE) for c in codes]

    def minute_bars(self, code: str, datalen: int = 1023) -> list[dict]:
        return [{"day": "2026-11-12 09:31:00", "open": "10.14", "high": "10.15",
                 "low": "10.10", "close": "10.11", "volume": "1918800",
                 "amount": "19422491.0"}]

    def close(self) -> None:
        pass


def test_realtime_batches_cap_at_800(monkeypatch):
    fake = _FakeClient()
    symbols = [f"{600000 + i:06d}.SH" for i in range(1700)]
    monkeypatch.setattr(sina, "_load_symbols", lambda: symbols)
    prov = sina.SinaProvider()
    monkeypatch.setattr(prov, "_client", fake)

    rows = prov.get_realtime()
    assert [len(c) for c in fake.calls] == [800, 800, 100]
    assert len(rows) == 1700
    assert rows[0]["symbol"] == "600000.SH"
    assert rows[0]["volume"] == 500817


def test_realtime_partial_batch_failure_keeps_other_batches(monkeypatch):
    fake = _FakeClient(fail_batches={0})
    symbols = [f"{600000 + i:06d}.SH" for i in range(900)]
    monkeypatch.setattr(sina, "_load_symbols", lambda: symbols)
    prov = sina.SinaProvider()
    monkeypatch.setattr(prov, "_client", fake)

    rows = prov.get_realtime()
    assert len(fake.calls) == 2
    assert len(rows) == 100  # 第一批失败, 第二批照常入库


def test_realtime_indices_failure_is_none(monkeypatch):
    class _Boom(_FakeClient):
        def quotes(self, codes):
            raise sina.SinaError("down")

    prov = sina.SinaProvider()
    monkeypatch.setattr(prov, "_client", _Boom())
    assert prov.get_realtime_indices(["000001.SH"]) is None


def test_minute_returns_dataframe_and_empty_schema():
    prov = sina.SinaProvider()
    prov._client = _FakeClient()
    # 不支持的代码 (无交易所后缀) → 空帧但保留 schema, 不抛异常
    df = prov.get_minute(["600000"], None, None)
    assert df.is_empty()
    assert df.schema == pl.Schema(sina._MINUTE_SCHEMA)


def test_minute_converts_units_and_reports_progress():
    prov = sina.SinaProvider()
    prov._client = _FakeClient()
    seen: list[tuple[int, int]] = []
    df = prov.get_minute(["600000.SH", "000001.SZ"], None, None,
                         on_chunk_done=lambda cur, total: seen.append((cur, total)))
    assert df.height == 2
    assert df["volume"].to_list() == [19188.0, 19188.0]  # 1,918,800 股 → 19,188 手
    assert df["amount"].to_list() == [19422491.0, 19422491.0]
    assert seen == [(1, 2), (2, 2)]
