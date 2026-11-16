"""本地扫描失败必须可观测 —— 它曾经被吞成空表, 端点于是静默改从上游拉数。

实测背景(2026-11-13): `kline_daily_enriched/date=2026-11-05/part.parquet` 的
`quote_ts` 是 double, 其余 371 个分区是 int64 ⇒ 全表 glob 扫描抛
`SchemaError: Float64 != Int64` ⇒ `_scan_daily_symbol` 的 `except` 返回空表 ⇒
`get_daily` 判空后**从上游重拉**。用户看到的是"重启后第一张图 4.8~9.0s",
而真相是"数据源换了, turnover_rate 直接没有"。

这里钉住三件事:
1. 读坏了 → `take_scan_error()` 有原因(带表名/标的/原始错误);
2. 表里就是没数据 → 不算失败(否则每次首启都会误报"降级");
3. 端点在这一路径上必须把 `degraded_reason` 写进响应。
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository

DAY = dt.date(2026, 11, 5)
DAY2 = dt.date(2026, 11, 6)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    # 体检与本用例无关: 手造一两个分区必然被判"稀疏/过期", 会让 get_daily 在
    # 进入扫描前就抛 DailyIntegrityError。
    from app.services import daily_health

    monkeypatch.setattr(daily_health, "assert_usable", lambda *a, **k: None)
    return KlineRepository(DataStore(tmp_path))


def _partition(root, day: dt.date, quote_ts_dtype) -> None:
    d = root / "kline_daily_enriched" / f"date={day.isoformat()}"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "date": [day],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
            "volume": [100.0], "amount": [100.0],
            "quote_ts": pl.Series([1793874982000], dtype=quote_ts_dtype),
        }
    ).write_parquet(d / "part.parquet")


def test_schema_drift_is_recorded_not_swallowed(repo, tmp_path):
    _partition(tmp_path, DAY, pl.Int64)
    _partition(tmp_path, DAY2, pl.Float64)  # 模拟那个坏分区

    df = repo.get_daily("000001.SZ", DAY, DAY2)
    assert df.is_empty()  # 扫描确实失败了

    why = repo.take_scan_error()
    assert why is not None
    assert "kline_daily_enriched" in why      # 哪张表
    assert "000001.SZ" in why                 # 哪只标的
    assert "quote_ts" in why                  # 原始错误(能直接指向病根)


def test_take_scan_error_is_one_shot(repo, tmp_path):
    _partition(tmp_path, DAY, pl.Int64)
    _partition(tmp_path, DAY2, pl.Float64)
    repo.get_daily("000001.SZ", DAY, DAY2)
    assert repo.take_scan_error() is not None
    assert repo.take_scan_error() is None     # 取过即清, 不会污染下一次请求


def test_empty_table_is_not_a_failure(repo):
    """表里没有数据 ≠ 读坏了。首启/新装用户不该被标成"降级"。"""
    assert repo.get_daily("000001.SZ", DAY, DAY2).is_empty()
    assert repo.take_scan_error() is None


def test_successful_scan_leaves_no_error(repo, tmp_path):
    _partition(tmp_path, DAY, pl.Int64)
    df = repo.get_daily("000001.SZ", DAY, DAY, columns=["close"])
    assert not df.is_empty()
    assert repo.take_scan_error() is None


# ── 端点契约: 降级必须写在响应里, 而不是只写日志 ──────────────────────────

def _fake_request(scan_error: str | None):
    class _Repo:
        def resolve_asset_type(self, _symbol):
            return "stock"

        def get_daily_asset(self, *a, **k):
            return pl.DataFrame()

        def take_scan_error(self):
            return scan_error

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_Repo(), capabilities=None)))


def test_endpoint_marks_degraded_reason(monkeypatch):
    from app.api import kline as kline_api

    captured: dict = {}
    monkeypatch.setattr(kline_api, "_get_stock_info", lambda repo, symbol: {"name": "X"})
    monkeypatch.setattr(kline_api, "_maybe_inject_live_candle", lambda *a, **k: a[2])
    monkeypatch.setattr(kline_api, "_gzip_payload", lambda req, payload, **k: captured.update(payload) or payload)
    monkeypatch.setattr(kline_api, "compute_enriched", lambda raw, factors=None: raw)
    monkeypatch.setattr(
        kline_api.kline_sync, "sync_daily_batch",
        lambda symbols, count: pl.DataFrame({
            "symbol": ["000001.SZ"], "date": [DAY], "open": [1.0], "high": [1.0],
            "low": [1.0], "close": [1.0], "volume": [1.0],
        }),
    )

    kline_api.get_daily(
        _fake_request("kline_daily_enriched 读取失败(000001.SZ): quote_ts 类型不一致"),
        symbol="000001.SZ", days=120, start_date=None, end_date=None, ext_columns=None,
    )

    assert captured["source"] == "live"          # 确实换了数据源
    assert "本地读取失败" in captured["degraded_reason"]
    assert "quote_ts" in captured["degraded_reason"]


def test_endpoint_has_no_degraded_reason_when_local_read_was_clean(monkeypatch):
    """真的没有这只票时, 不该挂一个"降级"标签 —— 那会让告警永远亮着。"""
    from app.api import kline as kline_api

    captured: dict = {}
    monkeypatch.setattr(kline_api, "_get_stock_info", lambda repo, symbol: {"name": "X"})
    monkeypatch.setattr(kline_api, "_maybe_inject_live_candle", lambda *a, **k: a[2])
    monkeypatch.setattr(kline_api, "_gzip_payload", lambda req, payload, **k: captured.update(payload) or payload)
    monkeypatch.setattr(kline_api, "compute_enriched", lambda raw, factors=None: raw)
    monkeypatch.setattr(
        kline_api.kline_sync, "sync_daily_batch",
        lambda symbols, count: pl.DataFrame({
            "symbol": ["000001.SZ"], "date": [DAY], "open": [1.0], "high": [1.0],
            "low": [1.0], "close": [1.0], "volume": [1.0],
        }),
    )

    kline_api.get_daily(
        _fake_request(None),
        symbol="000001.SZ", days=120, start_date=None, end_date=None, ext_columns=None,
    )
    assert "degraded_reason" not in captured


# ── 批量端点: 静默少行是最坏的失败形态 ──────────────────────────────────

def _batch_repo(rows_symbol: str, scan_error: str | None):
    class _Repo:
        def resolve_asset_type(self, _symbol):
            return "stock"

        def get_daily_batch(self, symbols, start, end, columns=None):
            return pl.DataFrame({
                "symbol": [rows_symbol], "date": [DAY], "open": [1.0], "high": [1.0],
                "low": [1.0], "close": [1.0], "volume": [1.0],
            })

        def take_scan_error(self):
            return scan_error

    return _Repo()


def _run_batch(monkeypatch, repo, symbols):
    import json

    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "_gzip_raw", lambda req, raw, **k: raw)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=None)))
    raw = kline_api.get_daily_batch(req, {"symbols": symbols, "days": 12})
    return json.loads(raw)


def test_batch_lists_symbols_that_returned_nothing(monkeypatch):
    got = _run_batch(monkeypatch, _batch_repo("000001.SZ", None), ["000001.SZ", "600519.SH"])
    assert list(got["data"]) == ["000001.SZ"]
    assert got["missing_symbols"] == ["600519.SH"]   # 说清楚缺谁
    assert "degraded_reason" not in got               # 但没读坏, 不贴降级标签


def test_batch_surfaces_read_failure_reason(monkeypatch):
    got = _run_batch(
        monkeypatch,
        _batch_repo("000001.SZ", "kline_daily_enriched 读取失败(2 只): quote_ts 类型不一致"),
        ["000001.SZ", "600519.SH"],
    )
    assert got["missing_symbols"] == ["600519.SH"]
    assert "quote_ts" in got["degraded_reason"]


def test_batch_payload_unchanged_when_nothing_is_missing(monkeypatch):
    """全部命中时不许新增字段 —— 批量响应有字节级契约测试锁着。"""
    got = _run_batch(monkeypatch, _batch_repo("000001.SZ", None), ["000001.SZ"])
    assert set(got) == {"data"}
