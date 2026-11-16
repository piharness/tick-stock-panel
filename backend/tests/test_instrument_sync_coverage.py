"""标的维表同步的覆盖率契约 —— 单交易所失败不许把整表缩水。

## 为什么（真实事故，2026-11-12 / 11-13）

free-api 的 BJ 端点在 09:10 连续两天返回 503：

    11-12 09:10:21 WARNING get_instruments(BJ) failed: 503
    11-13 09:10:20 WARNING get_instruments(BJ) failed: 503
    11-12 15:35:08 INFO    instruments BJ: 355 stocks  ← 下午成功过

旧实现只记一条 WARNING，继续把「SH+SZ」整表写入 ⇒ 15:35 写好的 355 只北交所
在次日 09:10 消失，而日线仍有 355 只（11-12 占全市场成交额 0.72%）。
任何 `join instruments` 静默丢它们；覆盖率判据只用维表自身行数算下限，看不见。

## 契约

1. 单交易所失败 → 用旧表里该交易所的行补齐，总数不许变小。
2. 失败且旧表也没有该交易所 → 抛错，**不许**用残缺结果覆盖。
3. 瞬时失败 → 重试后成功，不进入补齐分支。
4. 全部为空 → 返回 0，旧表原样保留。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services import instrument_sync


class _FakeExchanges:
    def __init__(self, items: dict[str, list[dict] | Exception], flaky: dict[str, int] | None = None):
        self.items = items
        self.flaky = dict(flaky or {})
        self.calls: list[str] = []

    def get_instruments(self, ex: str, instrument_type: str = "stock"):
        self.calls.append(ex)
        if self.flaky.get(ex, 0) > 0:
            self.flaky[ex] -= 1
            raise ConnectionError("<html><body><h1>503 Service Unavailable</h1></body></html>")
        value = self.items.get(ex)
        if isinstance(value, Exception):
            raise value
        return value or []


class _FakeClient:
    def __init__(self, exchanges: _FakeExchanges) -> None:
        self.exchanges = exchanges


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(instrument_sync.time, "sleep", lambda *_: None)
    monkeypatch.setattr(instrument_sync, "_fetch_instruments_via_provider", lambda: None)
    monkeypatch.setattr(instrument_sync.tf_client, "current_mode", lambda: "free")


def _seed(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "instruments" / "instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path)
    return path


def _item(symbol: str, name: str) -> dict:
    ex = symbol[-2:]
    return {"symbol": symbol, "name": name, "code": symbol[:6], "exchange": ex,
            "region": "CN", "type": "stock"}


def _sh_sz_items() -> dict[str, list[dict]]:
    return {
        "SH": [_item("600000.SH", "浦发银行")],
        "SZ": [_item("000001.SZ", "平安银行")],
    }


def test_bj_failure_backfills_from_existing_table(tmp_path: Path, monkeypatch) -> None:
    """BJ 503 → 沿用旧表的 355 只，绝不允许整表丢掉 BJ。

    这是 11-12 15:35 → 11-13 09:10 的真实形态：前一晚有 BJ，次日早上没了。
    """
    _seed(tmp_path, [
        {"symbol": "920001.BJ", "name": "北交样本", "code": "920001", "exchange": "BJ"},
        {"symbol": "600000.SH", "name": "旧浦发", "code": "600000", "exchange": "SH"},
    ])
    items = _sh_sz_items()
    items["BJ"] = ConnectionError("503 Service Unavailable")
    monkeypatch.setattr(instrument_sync, "get_client", lambda: _FakeClient(_FakeExchanges(items)))

    rows = instrument_sync.sync_instruments(tmp_path)

    table = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert rows == table.height == 3
    assert table.filter(pl.col("exchange") == "BJ")["symbol"].to_list() == ["920001.BJ"]
    # 新结果优先: SH 的旧名被本次结果覆盖
    assert table.filter(pl.col("symbol") == "600000.SH")["name"].to_list() == ["浦发银行"]


def test_bj_failure_without_history_refuses_to_overwrite(tmp_path: Path, monkeypatch) -> None:
    """旧表也没有 BJ → 抛错且不写盘，不用「SH+SZ」冒充全市场。"""
    items = _sh_sz_items()
    items["BJ"] = ConnectionError("503 Service Unavailable")
    monkeypatch.setattr(instrument_sync, "get_client", lambda: _FakeClient(_FakeExchanges(items)))

    with pytest.raises(RuntimeError, match="BJ"):
        instrument_sync.sync_instruments(tmp_path)

    assert not (tmp_path / "instruments" / "instruments.parquet").exists()


def test_transient_failure_retries_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    """首次 503、第二次成功 → 走正常路径，不记录为失败交易所。"""
    items = _sh_sz_items()
    items["BJ"] = [_item("920009.BJ", "丹娜生物")]
    fake = _FakeExchanges(items, flaky={"BJ": 1})
    monkeypatch.setattr(instrument_sync, "get_client", lambda: _FakeClient(fake))

    rows = instrument_sync.sync_instruments(tmp_path)

    assert rows == 3
    assert fake.calls.count("BJ") == 2
    table = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert table.filter(pl.col("exchange") == "BJ").height == 1


def test_all_empty_keeps_existing_table(tmp_path: Path, monkeypatch) -> None:
    """三个交易所都返回空 → 返回 0，旧表原样保留（既有行为）。"""
    path = _seed(tmp_path, [{"symbol": "920001.BJ", "name": "北交样本",
                             "code": "920001", "exchange": "BJ"}])
    before = path.read_bytes()
    monkeypatch.setattr(
        instrument_sync, "get_client",
        lambda: _FakeClient(_FakeExchanges({"SH": [], "SZ": [], "BJ": []})),
    )

    assert instrument_sync.sync_instruments(tmp_path) == 0
    assert path.read_bytes() == before
