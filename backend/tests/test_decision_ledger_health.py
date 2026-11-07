"""账本可读性：坏行必须报错，且必须能问出「0 条是哪一种 0」。

`load_settlements` 曾经在解析失败时 `continue`。这个测试钉两件事：

1. 坏行、重复键、非结算记录一律 `LedgerCorruptError`，不降级成「少一条样本」；
2. `ledger_health` 把「目录不存在（从未写入）」「有行读不出（损坏）」
   「全部可读但有被 done 键固化的残缺行（degraded）」分成三种状态，
   并且**永不抛异常**——它是账本坏了之后唯一还能问的端点。

健康快照的验收方式是变异：塞一行坏 JSON 必须转红，删掉必须转绿。只断言
「坏的时候是红的」不够——那也可能是一个永远红的实现。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.decision import router
from app.decision import ledger, producer, settle

SYM = "600519.SH"
BENCH = "000300.SH"
START = date(2026, 1, 5)


# ---------------------------------------------------------------------------
# 种子：直接落分区文件，不经 repository（这一层测的是账本读法，不是同步）
# ---------------------------------------------------------------------------


def _write_part(path: Path, df: pl.DataFrame) -> None:
    path.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path / "part.parquet")


def _seed(tmp_path: Path, n: int = 25) -> list[str]:
    days = [START + timedelta(days=i) for i in range(n)]
    for i, d in enumerate(days):
        iso = d.isoformat()
        close = 110.0 if i > 1 else 100.0
        _write_part(tmp_path / "kline_daily" / f"date={iso}", pl.DataFrame({
            "symbol": [SYM], "date": [d], "open": [100.0], "high": [close],
            "low": [100.0], "close": [close], "volume": [1e4], "amount": [1e6],
        }))
        _write_part(tmp_path / "kline_daily_enriched" / f"date={iso}", pl.DataFrame({
            "symbol": [SYM], "date": [d], "open": [100.0], "high": [close],
            "low": [100.0], "close": [close], "volume": [1e4], "amount": [1e6],
            "raw_close": [close], "raw_high": [close], "raw_low": [100.0],
        }))
        _write_part(tmp_path / "kline_index_enriched" / f"date={iso}", pl.DataFrame({
            "symbol": [BENCH], "date": [d], "open": [4000.0], "high": [4000.0],
            "low": [4000.0], "close": [4000.0], "raw_close": [4000.0],
            "volume": [1e9], "amount": [1e12],
        }))
    return [d.isoformat() for d in days]


def _settle_once(tmp_path: Path, days: list[str], *, horizon: int = 5) -> dict:
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0], picks=[{"symbol": SYM, "limit_price": 100.0, "p_win": 0.6}],
        horizon_sessions=horizon))
    return settle.settle(tmp_path)


def _good_row(key: str = "dc-2026-01-05-x:600519.SH") -> dict:
    return {
        "kind": ledger.SETTLEMENT_KIND, "key": key,
        "card_id": "dc-2026-01-05-x", "as_of": START.isoformat(),
        "symbol": SYM, "strategy_id": None, "account_id": "default",
        "horizon_sessions": 5, "p_win": 0.6, "limit_price": 100.0,
        "outcome": 1, "outcome_med": 1,
        "fill": {"status": "FILLED", "date": START.isoformat()},
        "exit": {"date": START.isoformat(), "ret": 0.01},
    }


def _append_raw(tmp_path: Path, *lines: str) -> None:
    path = ledger.settlements_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# 1. 空账本 vs 损坏账本
# ---------------------------------------------------------------------------


def test_missing_ledger_dir_is_empty_not_corrupt(tmp_path: Path):
    """目录不存在是可判定的「从未写入」，不是损坏，更不是「读不出来」。"""
    assert ledger.load_settlements(tmp_path) == []
    health = ledger.ledger_health(tmp_path)
    assert health["status"] == ledger.HEALTH_NEVER_WRITTEN
    assert health["root_exists"] is False
    assert health["cards"]["count"] == 0
    assert health["settlements"]["exists"] is False


def test_corrupt_line_raises_and_names_the_line(tmp_path: Path):
    _append_raw(tmp_path, json.dumps(_good_row()), "{not json")
    with pytest.raises(ledger.LedgerCorruptError) as ei:
        ledger.load_settlements(tmp_path)
    assert "第 2 行" in str(ei.value)


def test_duplicate_key_raises(tmp_path: Path):
    """同键两行会把一笔样本计成两笔，不是「多一条记录」。"""
    row = json.dumps(_good_row())
    _append_raw(tmp_path, row, row)
    with pytest.raises(ledger.LedgerCorruptError) as ei:
        ledger.load_settlements(tmp_path)
    assert "key 重复" in str(ei.value)


def test_foreign_kind_raises(tmp_path: Path):
    row = _good_row()
    row["kind"] = "SOMETHING-ELSE"
    _append_raw(tmp_path, json.dumps(row))
    with pytest.raises(ledger.LedgerCorruptError):
        ledger.load_settlements(tmp_path)


def test_missing_key_raises(tmp_path: Path):
    """缺 key 的行既不会被 done 跳过、也不会被覆盖，每读一次都算一条新样本。"""
    row = _good_row()
    row.pop("key")
    _append_raw(tmp_path, json.dumps(row))
    with pytest.raises(ledger.LedgerCorruptError) as ei:
        ledger.load_settlements(tmp_path)
    assert "缺 key" in str(ei.value)


def test_settle_refuses_on_corrupt_settlement_line(tmp_path: Path):
    """结算入口也 fail-closed：坏账本上不产生任何新行。"""
    days = _seed(tmp_path)
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0], picks=[{"symbol": SYM, "limit_price": 100.0, "p_win": 0.6}],
        horizon_sessions=5))
    _append_raw(tmp_path, "{broken")
    with pytest.raises(ledger.LedgerCorruptError):
        settle.settle(tmp_path)


def test_producer_raises_corrupt_not_no_evidence(tmp_path: Path):
    """账本坏了不能报成「没有证据」：两句话的下一动作相反。"""
    _append_raw(tmp_path, "{broken")
    with pytest.raises(ledger.LedgerCorruptError):
        producer.resolve_picks_with_pwin(
            tmp_path, [{"symbol": SYM, "limit_price": 100.0}],
            auto_pwin=True, strategy_id="mom", horizon=5, basis="median",
            allow_prior=False, as_of=START.isoformat())


# ---------------------------------------------------------------------------
# 2. 变异验证：坏行注入转红，删除转绿
# ---------------------------------------------------------------------------


def test_health_turns_red_on_bad_line_and_green_after_removal(tmp_path: Path):
    path = ledger.settlements_path(tmp_path)
    _append_raw(tmp_path, json.dumps(_good_row()))
    assert ledger.ledger_health(tmp_path)["status"] == ledger.HEALTH_OK

    good = path.read_text(encoding="utf-8")
    path.write_text(good + "{not json\n", encoding="utf-8")
    red = ledger.ledger_health(tmp_path)
    assert red["status"] == ledger.HEALTH_CORRUPT
    assert red["settlements"]["problems"]
    assert red["settlements"]["rows"] == 1  # 好行仍然被数进来：健康快照不因为坏行失明

    path.write_text(good, encoding="utf-8")
    assert ledger.ledger_health(tmp_path)["status"] == ledger.HEALTH_OK


# ---------------------------------------------------------------------------
# 3. 固化：degraded 不是「轻微损坏」，是「永远不会自己好」
# ---------------------------------------------------------------------------


def test_single_symbol_day_leaves_median_missing_and_is_frozen(tmp_path: Path):
    """单标的种子下全市场不足 100 只 ⇒ 中位数读不出 ⇒ 该行永久缺 median。

    `settle()` 按 `done` 键跳过已写行，所以第二遍不会补算——这正是
    `median_covered < written` 会长期存在的原因，也是健康快照要报 degraded 的场景。
    """
    days = _seed(tmp_path)
    first = _settle_once(tmp_path, days)
    assert first["written"] == 1
    assert first["median_covered"] == 0

    second = settle.settle(tmp_path)
    assert second["written"] == 0

    health = ledger.ledger_health(tmp_path)
    assert health["status"] == ledger.HEALTH_DEGRADED
    assert health["settlements"]["median_missing"] == 1
    assert health["settlements"]["filled_without_outcome"] == 0


def test_filled_without_outcome_counts_as_degraded(tmp_path: Path):
    row = _good_row()
    row["outcome"] = None
    row["outcome_med"] = None
    _append_raw(tmp_path, json.dumps(row))
    health = ledger.ledger_health(tmp_path)
    assert health["status"] == ledger.HEALTH_DEGRADED
    assert health["settlements"]["filled_without_outcome"] == 1


def test_not_filled_row_is_not_degraded(tmp_path: Path):
    """未成交行没有 outcome 是**正常**终态，不该把账本报成 degraded。"""
    row = _good_row()
    row["fill"] = {"status": "NOT_FILLED", "date": START.isoformat()}
    row["exit"] = None
    row["outcome"] = None
    row["outcome_med"] = None
    _append_raw(tmp_path, json.dumps(row))
    health = ledger.ledger_health(tmp_path)
    assert health["status"] == ledger.HEALTH_OK
    assert health["settlements"]["filled_without_outcome"] == 0


# ---------------------------------------------------------------------------
# 4. HTTP：健康端点永不 409，其余端点把损坏映射成 409
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def test_health_endpoint_reports_never_written(client: TestClient):
    body = client.get("/api/decision/health").json()
    assert body["status"] == "never_written"


def test_health_endpoint_still_answers_when_corrupt(client: TestClient, tmp_path: Path):
    _append_raw(tmp_path, "{broken")
    resp = client.get("/api/decision/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "corrupt"


@pytest.mark.parametrize("path", [
    "/api/decision/overview",
    "/api/decision/score",
    "/api/decision/pwin",
    "/api/decision/settlements",
])
def test_read_endpoints_map_corruption_to_409(client: TestClient, tmp_path: Path, path: str):
    _append_raw(tmp_path, "{broken")
    assert client.get(path).status_code == 409
