"""决策卡生产者测试 — 「策略 → 卡」最后一公里的契约。

覆盖三条纪律（`app/decision/producer.py` docstring）：
1. 可见性口径与 API 层共用一份（§9.8 `exit.date < as_of`，当天出场不可见）；
2. fail-closed 的出口如实呈现：没有证据不编 p_win、缺 raw_close 不冒充限价、
   门禁 STOP 只跳过不越权；
3. 幂等：同日重跑同配置是同一张卡（承诺哈希）。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from app.decision import ledger, producer
from app.decision.settle import settle
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
BENCH = "000300.SH"
START = date(2026, 1, 5)


def _seed(tmp_path: Path, n: int = 25) -> list[date]:
    days = [START + timedelta(days=i) for i in range(n)]
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame({
        "symbol": [SYM] * n, "date": days,
        "open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n,
        "close": [100.0] * n, "volume": [1e4] * n, "amount": [1e6] * n,
    }))
    repo.append_enriched(pl.DataFrame({
        "symbol": [SYM] * n, "date": days,
        "open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n,
        "close": [110.0 if i > 1 else 100.0 for i in range(n)],
        "volume": [1e4] * n, "amount": [1e6] * n,
        "raw_close": [110.0 if i > 1 else 100.0 for i in range(n)],
        "raw_high": [110.0] * n, "raw_low": [100.0] * n,
    }))
    repo.append_index_enriched(pl.DataFrame({
        "symbol": [BENCH] * n, "date": days,
        "open": [4000.0] * n, "high": [4000.0] * n, "low": [4000.0] * n,
        "close": [4000.0] * n, "raw_close": [4000.0] * n,
        "volume": [1e9] * n, "amount": [1e12] * n,
    }))
    return days


def _seed_settlements(tmp_path: Path, *, days: int = 40, strategy: str = "mom",
                      wins: int = 14, total: int = 20) -> None:
    """直接写结算流水（与 test_decision_api 同口径的测试捷径）。"""
    rows = []
    for i in range(days):
        d = (START + timedelta(days=i)).isoformat()
        for k in range(total):
            rows.append({
                "kind": "TSP-DECISION-SETTLEMENT-v1",
                "key": f"dc-{d}-x:{k}", "card_id": f"dc-{d}-x", "as_of": d,
                "symbol": f"{k:06d}.SZ", "strategy_id": strategy,
                "account_id": "default", "horizon_sessions": 5, "p_win": 0.7,
                "limit_price": 100.0,
                "outcome": 1 if k < wins else 0, "outcome_med": 1 if k < wins else 0,
                "excess": 0.01, "excess_med": 0.01,
                "fill": {"status": "FILLED", "date": d},
                "exit": {"date": d, "ret": 0.01},
                "brier": 0.09, "logloss": 0.3,
            })
    ledger.append_settlements(tmp_path, rows)


def _prefs(**kw: Any) -> dict:
    cfg = {"enabled": False, "strategy_id": "", "top_n": 20, "horizon": 20,
           "basis": "median", "account_id": "default", "allow_prior": False}
    cfg.update(kw)
    return cfg


# ---------------------------------------------------------------------------
# 可见性口径（与 API 共用一份实现）
# ---------------------------------------------------------------------------


def test_exit_visible_strict_boundary():
    row = {"exit": {"date": "2026-01-10"}}
    assert producer.exit_visible(row, "2026-01-11") is True
    assert producer.exit_visible(row, "2026-01-10") is False  # 当天出场不可见
    assert producer.exit_visible({}, "2026-01-11") is False   # 缺 exit: fail-closed


def test_resolve_picks_requires_pwin_when_auto_off(tmp_path: Path):
    picks = [{"symbol": SYM, "limit_price": 100.0}]
    with pytest.raises(producer.PickValidationError) as exc:
        producer.resolve_picks_with_pwin(
            tmp_path, picks, auto_pwin=False, strategy_id="mom",
            horizon=5, basis="median", allow_prior=False, as_of="2026-01-05")
    assert SYM in str(exc.value)


def test_resolve_picks_auto_fills_from_visible_settlements(tmp_path: Path):
    _seed_settlements(tmp_path, days=40, wins=14)
    # as_of 晚于全部出场日（START+39）⇒ 800 条全部可见。
    as_of = (START + timedelta(days=46)).isoformat()
    picks, source = producer.resolve_picks_with_pwin(
        tmp_path, [{"symbol": SYM, "limit_price": 100.0}],
        auto_pwin=True, strategy_id="mom", horizon=5, basis="abs",
        allow_prior=False, as_of=as_of)
    assert source is not None
    assert source["source"] == "ledger"
    assert source["n"] == 800
    assert source["filled"] == [SYM]
    # 收缩：(560 + 20*0.5) / (800 + 20)
    assert picks[0]["p_win"] == pytest.approx(570 / 820, abs=1e-5)


def test_resolve_picks_auto_ignores_future_settlements(tmp_path: Path):
    """P1 回归：出场日 ≥ as_of 的结算是未来信息，不得进 p_win。

    种子行出场日 = 决策日（同天），as_of=START 时零可见（§9.8 严格小于）。
    同时断言输入不被原地改写——同一份 picks 二次推导必须得到同一出处。
    """
    _seed_settlements(tmp_path, days=40, wins=14)
    as_of = START.isoformat()
    picks = [{"symbol": SYM, "limit_price": 100.0}]
    with pytest.raises(producer.NoEvidenceError):
        producer.resolve_picks_with_pwin(
            tmp_path, picks,
            auto_pwin=True, strategy_id="mom", horizon=5, basis="abs",
            allow_prior=False, as_of=as_of)
    assert "p_win" not in picks[0]  # 纯函数：失败路径也不碰调用方数据


# ---------------------------------------------------------------------------
# scan 结果 → picks：限价口径
# ---------------------------------------------------------------------------


def test_picks_from_scan_rows_drops_missing_raw_close():
    rows = [
        {"symbol": "A.SH", "raw_close": 12.34},
        {"symbol": "B.SH"},                    # 缺 raw_close → 丢
        {"raw_close": 9.9},                    # 缺 symbol → 丢
        {"symbol": "C.SH", "raw_close": 5.0},
    ]
    picks, dropped = producer.picks_from_scan_rows(rows, top_n=10)
    assert dropped == 2
    assert picks == [{"symbol": "A.SH", "limit_price": 12.34},
                     {"symbol": "C.SH", "limit_price": 5.0}]


def test_picks_from_scan_rows_respects_top_n():
    rows = [{"symbol": f"S{i}.SZ", "raw_close": 10.0 + i} for i in range(30)]
    picks, dropped = producer.picks_from_scan_rows(rows, top_n=20)
    assert len(picks) == 20
    assert dropped == 0


# ---------------------------------------------------------------------------
# produce_card：门禁 / 冷启动 / 幂等
# ---------------------------------------------------------------------------


def test_produce_card_cold_start_needs_explicit_prior(tmp_path: Path):
    """没有可见证据时不编 p_win；显式 allow_prior=true 才允许 source=prior。"""
    days = _seed(tmp_path)
    as_of = days[0].isoformat()
    out = producer.produce_card(
        tmp_path, as_of=as_of,
        picks=[{"symbol": SYM, "limit_price": 100.0}],
        horizon_sessions=5, strategy_id="mom")
    assert out["status"] == "skipped_no_evidence"
    assert ledger.load_cards(tmp_path) == []

    ok = producer.produce_card(
        tmp_path, as_of=as_of,
        picks=[{"symbol": SYM, "limit_price": 100.0}],
        horizon_sessions=5, strategy_id="mom", allow_prior=True)
    assert ok["status"] == "created"
    card = ledger.load_cards(tmp_path)[0]
    assert card["picks"][0]["p_win"] == 0.5
    assert card["forecast"]["pwin"]["source"] == "prior"


def test_produce_card_uses_ledger_pwin_and_seals_provenance(tmp_path: Path):
    days = _seed(tmp_path, n=50)
    _seed_settlements(tmp_path, days=40, wins=14)
    as_of = (START + timedelta(days=46)).isoformat()
    out = producer.produce_card(
        tmp_path, as_of=as_of,
        picks=[{"symbol": SYM, "limit_price": 100.0}],
        horizon_sessions=5, strategy_id="mom", scan_meta={"producer": "auto"})
    assert out["status"] == "created"
    card = ledger.get_card(tmp_path, out["card_id"])
    ok, why = ledger.verify_card(card)
    assert ok, why
    assert card["forecast"]["pwin"]["n"] == 800
    assert card["forecast"]["pwin"]["source"] == "ledger"
    assert card["forecast"]["producer"] == "auto"


def test_produce_card_is_idempotent(tmp_path: Path):
    days = _seed(tmp_path)
    as_of = days[0].isoformat()
    kw = dict(as_of=as_of, picks=[{"symbol": SYM, "limit_price": 100.0}],
              horizon_sessions=5, strategy_id="mom", allow_prior=True)
    first = producer.produce_card(tmp_path, **kw)
    assert first["status"] == "created"
    again = producer.produce_card(tmp_path, **kw)
    assert again["status"] == "exists"
    assert again["card_id"] == first["card_id"]
    assert len(ledger.load_cards(tmp_path)) == 1


def test_produce_card_skips_on_blocked_gate(tmp_path: Path):
    """门禁 STOP：自动路径只跳过，绝不 override。"""
    days = _seed(tmp_path)
    stale = (days[-1] + timedelta(days=7)).isoformat()
    out = producer.produce_card(
        tmp_path, as_of=stale,
        picks=[{"symbol": SYM, "limit_price": 100.0}],
        horizon_sessions=5, strategy_id="mom", allow_prior=True)
    assert out["status"] == "skipped_gate"
    assert out["decision"] == "STOP_NEW"
    assert ledger.load_cards(tmp_path) == []


def test_produce_card_no_picks_is_no_card(tmp_path: Path):
    """0 命中：没有判断就没有卡，不造空卡。"""
    days = _seed(tmp_path)
    out = producer.produce_card(
        tmp_path, as_of=days[0].isoformat(), picks=[],
        horizon_sessions=5, strategy_id="mom")
    assert out["status"] == "skipped_no_picks"
    assert ledger.load_cards(tmp_path) == []


def test_produce_card_settles_after_creation(tmp_path: Path):
    """发卡后数据就绪即可结算：策略 → 卡 → 结算的完整链路跑通。"""
    days = _seed(tmp_path)
    as_of = days[0].isoformat()
    out = producer.produce_card(
        tmp_path, as_of=as_of,
        picks=[{"symbol": SYM, "limit_price": 100.0, "p_win": 0.6}],
        horizon_sessions=5, strategy_id="mom")
    assert out["status"] == "created"
    s = settle(tmp_path)
    assert s["written"] == 1


# ---------------------------------------------------------------------------
# run_decision_stage：管道入口的旁路语义
# ---------------------------------------------------------------------------


def test_stage_disabled_by_default(tmp_path: Path, monkeypatch):
    """默认关闭：决策自动发卡必须显式开启（污染校准的代价由用户承担）。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_decision_auto_config",
                        lambda: _prefs())
    repo = KlineRepository(DataStore(tmp_path))
    out = producer.run_decision_stage(repo, engine=None)
    assert out == {"status": "disabled"}


def test_stage_settles_without_strategy(tmp_path: Path, monkeypatch):
    """enabled 但未配策略：只结算不发卡，如实回报（结算不因缺配置而停）。"""
    from app.services import preferences

    days = _seed(tmp_path)
    kw = dict(as_of=days[0].isoformat(),
              picks=[{"symbol": SYM, "limit_price": 100.0, "p_win": 0.6}],
              horizon_sessions=5)
    card = ledger.build_card(strategy_id="mom", **kw)
    ledger.save_card(tmp_path, card)
    _prefs_settle_done = settle(tmp_path)
    assert _prefs_settle_done["written"] == 1

    monkeypatch.setattr(preferences, "get_decision_auto_config",
                        lambda: _prefs(enabled=True))
    repo = KlineRepository(DataStore(tmp_path))
    out = producer.run_decision_stage(repo, engine=None, today=days[1])
    assert out["status"] == "no_strategy"
    assert out["settle"]["written"] == 0  # 幂等：已结算的不重复写


def test_stage_reports_missing_engine(tmp_path: Path, monkeypatch):
    from app.services import preferences

    days = _seed(tmp_path)
    monkeypatch.setattr(preferences, "get_decision_auto_config",
                        lambda: _prefs(enabled=True, strategy_id="mom"))
    repo = KlineRepository(DataStore(tmp_path))
    out = producer.run_decision_stage(repo, engine=None, today=days[0])
    assert out["status"] == "no_engine"
    assert "settle" in out
