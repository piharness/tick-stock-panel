"""决策账本域模块测试 — 预登记 / 三态门禁 / 结算口径 / 校准计分。

关键口径（改这些断言前先改模块头注释）：
- 限价撮合用不复权价，收益用复权价；
- 到期日无行情 ⇒ 仍 PENDING，不拿旧价假装结算；
- 已结算样本 < 8 ⇒ 结论固定 INSUFFICIENT，不给好看的数字。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.decision import bars, gates, ledger, settle
from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
BENCH = "000300.SH"
START = date(2026, 1, 5)


def _days(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def _write_stock(tmp_path: Path, rows: list[dict]) -> None:
    """写日K + enriched。rows: date/open/low/close/raw_close。"""
    repo = KlineRepository(DataStore(tmp_path))
    daily = pl.DataFrame({
        "symbol": [SYM] * len(rows),
        "date": [r["date"] for r in rows],
        "open": [r["open"] for r in rows],
        "high": [max(r["open"], r["close"]) for r in rows],
        "low": [r["low"] for r in rows],
        "close": [r["close"] for r in rows],
        "volume": [1e4] * len(rows),
        "amount": [1e6] * len(rows),
    })
    repo.append_daily(daily)
    enriched = pl.DataFrame({
        "symbol": [SYM] * len(rows),
        "date": [r["date"] for r in rows],
        "open": [r["open"] for r in rows],
        "high": [max(r["open"], r["close"]) for r in rows],
        "low": [r["low"] for r in rows],
        "close": [r["close"] for r in rows],
        "volume": [1e4] * len(rows),
        "amount": [1e6] * len(rows),
        "raw_close": [r["raw_close"] for r in rows],
        "raw_high": [max(r["open"], r["raw_close"]) for r in rows],
        "raw_low": [r["raw_low"] for r in rows],
    })
    repo.append_enriched(enriched)


def _write_index(tmp_path: Path, rows: list[tuple[date, float]]) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_index_enriched(pl.DataFrame({
        "symbol": [BENCH] * len(rows),
        "date": [d for d, _ in rows],
        "open": [c for _, c in rows],
        "high": [c for _, c in rows],
        "low": [c for _, c in rows],
        "close": [c for _, c in rows],
        "raw_close": [c for _, c in rows],
        "volume": [1e9] * len(rows),
        "amount": [1e12] * len(rows),
    }))


def _flat_market(tmp_path: Path, n: int = 30, price: float = 100.0) -> list[date]:
    """N 天平盘：开=低=收=price，复权因子 1。"""
    days = _days(n)
    _write_stock(tmp_path, [
        {"date": d, "open": price, "low": price, "close": price,
         "raw_close": price, "raw_low": price} for d in days
    ])
    _write_index(tmp_path, [(d, 4000.0) for d in days])
    return days


def _instrument(tmp_path: Path, name: str = "贵州茅台", listing: date | None = None) -> None:
    path = tmp_path / "instruments" / "instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, list] = {"symbol": [SYM], "name": [name]}
    if listing is not None:
        data["listing_date"] = [listing]
    pl.DataFrame(data).write_parquet(path)


# ---------------------------------------------------------------------------
# 预登记：内容哈希
# ---------------------------------------------------------------------------


def _picks(limit: float = 100.0, p: float = 0.6) -> list[dict]:
    return [{"symbol": SYM, "limit_price": limit, "p_win": p}]


def test_digest_ignores_pick_order_and_is_reproducible():
    a = ledger.build_card(as_of="2026-01-05", picks=[
        {"symbol": "600519.SH", "limit_price": 100, "p_win": 0.6},
        {"symbol": "000001.SZ", "limit_price": 12.5, "p_win": 0.55},
    ])
    b = ledger.build_card(as_of="2026-01-05", picks=[
        {"symbol": "000001.SZ", "limit_price": 12.5, "p_win": 0.55},
        {"symbol": "600519.SH", "limit_price": 100, "p_win": 0.6},
    ])
    assert a["sealed"]["sha256"] == b["sealed"]["sha256"]
    assert a["card_id"] == b["card_id"]


def test_digest_changes_when_any_commitment_field_changes():
    base = ledger.build_card(as_of="2026-01-05", picks=_picks(p=0.6))
    changed = ledger.build_card(as_of="2026-01-05", picks=_picks(p=0.61))
    assert base["sealed"]["sha256"] != changed["sealed"]["sha256"]
    assert base["card_id"] != changed["card_id"]


def test_verify_detects_post_seal_edit():
    card = ledger.build_card(as_of="2026-01-05", picks=_picks())
    assert ledger.verify_card(card) == (True, "ok")
    card["picks"][0]["limit_price"] = 99.0          # 事后改价
    ok, why = ledger.verify_card(card)
    assert ok is False and "哈希不匹配" in why


def test_verify_detects_gates_edit():
    card = ledger.build_card(as_of="2026-01-05", picks=_picks(),
                             gates={"decision": "GO"})
    card["gates"]["decision"] = "STOP_NEW"
    ok, why = ledger.verify_card(card)
    assert ok is False and "门状态" in why


def test_save_card_is_append_only(tmp_path: Path):
    card = ledger.build_card(as_of="2026-01-05", picks=_picks())
    path, created = ledger.save_card(tmp_path, card)
    assert created is True
    card["picks"][0]["limit_price"] = 1.0           # 同 id 的"新内容"
    path2, created2 = ledger.save_card(tmp_path, card)
    assert created2 is False and path2 == path
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["picks"][0]["limit_price"] == 100.0


@pytest.mark.parametrize("picks,msg", [
    ([], "不能为空"),
    ([{"symbol": "600519", "limit_price": 10, "p_win": 0.5}], "symbol 格式非法"),
    ([{"symbol": "600519.SH", "limit_price": 0, "p_win": 0.5}], "limit_price"),
    ([{"symbol": "600519.SH", "limit_price": 10, "p_win": 1.0}], "p_win"),
    ([{"symbol": "600519.SH", "limit_price": 10, "p_win": 0.5},
      {"symbol": "600519.SH", "limit_price": 11, "p_win": 0.5}], "重复"),
])
def test_build_card_rejects_invalid_input(picks, msg):
    with pytest.raises(ValueError, match=msg):
        ledger.build_card(as_of="2026-01-05", picks=picks)


def test_override_reason_must_be_readable():
    with pytest.raises(ValueError, match="10 个字符"):
        ledger.build_card(as_of="2026-01-05", picks=_picks(), override_reason="急")


# ---------------------------------------------------------------------------
# 门禁：三态
# ---------------------------------------------------------------------------


def test_gate_go_when_data_current_and_no_account(tmp_path: Path):
    days = _flat_market(tmp_path)
    _instrument(tmp_path)
    res = gates.evaluate(tmp_path, as_of=days[-1].isoformat())
    assert res.decision == gates.GO
    assert res.by_id("K1_LOSS").status == gates.PASS
    assert res.by_id("K4_DATA_STALE").status == gates.PASS


def test_gate_stops_when_data_is_stale(tmp_path: Path):
    days = _flat_market(tmp_path, n=10)
    res = gates.evaluate(tmp_path, as_of=(days[-1] + timedelta(days=3)).isoformat())
    k4 = res.by_id("K4_DATA_STALE")
    assert k4.status == gates.TRIGGERED
    assert res.decision == gates.STOP_NEW and res.blocked


def test_gate_k3_triggers_on_tampered_card(tmp_path: Path):
    _flat_market(tmp_path, n=5)
    card = ledger.build_card(as_of="2026-01-05", picks=_picks())
    path, _ = ledger.save_card(tmp_path, card)
    card["picks"][0]["limit_price"] = 1.0
    path.write_text(json.dumps(card), encoding="utf-8")
    res = gates.evaluate(tmp_path, as_of="2026-01-05")
    assert res.by_id("K3_NO_RECORD").status == gates.TRIGGERED


def test_gate_k2_unknown_when_instruments_missing(tmp_path: Path):
    days = _flat_market(tmp_path, n=10)
    _hold(tmp_path, SYM)
    res = gates.evaluate(tmp_path, as_of=days[-1].isoformat())
    assert res.by_id("K2_ASSUMPTION").status == gates.UNKNOWN
    assert res.decision == gates.GO_WITH_UNKNOWNS
    strict = gates.evaluate(tmp_path, as_of=days[-1].isoformat(), strict=True)
    assert strict.decision == gates.STOP_NEW


def test_gate_k2_triggers_on_st_name(tmp_path: Path):
    days = _flat_market(tmp_path, n=12)
    _instrument(tmp_path, name="*ST茅台")
    _hold(tmp_path, SYM)
    res = gates.evaluate(tmp_path, as_of=days[-1].isoformat())
    assert res.by_id("K2_ASSUMPTION").status == gates.TRIGGERED
    assert res.decision == gates.STOP_NEW_AND_REVIEW


def test_gate_k2_ignores_new_listing_without_bars(tmp_path: Path):
    """窗口内上市的新股没有 bar 是因为还没上市，不是停牌 —— 不能触发 K2 全停。"""
    days = _flat_market(tmp_path, n=12)
    _instrument(tmp_path, name="次新股", listing=days[-2])
    _hold(tmp_path, SYM)
    res = gates.evaluate(tmp_path, as_of=days[-1].isoformat())
    assert res.by_id("K2_ASSUMPTION").status == gates.PASS


def test_gate_k1_unknown_when_holding_has_no_price(tmp_path: Path):
    days = _flat_market(tmp_path, n=10)
    _instrument(tmp_path)
    _hold(tmp_path, "000001.SZ", cost=10.0)
    res = gates.evaluate(tmp_path, as_of=days[-1].isoformat())
    assert res.by_id("K1_LOSS").status == gates.UNKNOWN


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------


def _hold(tmp_path: Path, symbol: str, *, qty: int = 100, cost: float = 100.0) -> None:
    """直接写物化持仓（含 lots：T+1 可卖数量从批次推导，缺 lots 会 KeyError）。"""
    paper.create_account(tmp_path, 100_000)
    pos = tmp_path / "paper" / "accounts" / "default" / "positions.json"
    pos.parent.mkdir(parents=True, exist_ok=True)
    pos.write_text(json.dumps({"positions": {symbol: {
        "symbol": symbol, "qty": qty, "avg_cost": cost,
        "lots": [{"qty": qty, "date": "2026-01-01", "price": cost}],
    }}}), encoding="utf-8")


def _settle_one(tmp_path: Path, *, entry_open: float, entry_low: float,
                entry_close: float = 100.0, entry_raw_close: float = 100.0,
                exit_close: float = 110.0, limit: float = 100.0, n: int = 25,
                horizon: int = 5) -> dict | None:
    """as_of = days[0]，入场 = days[1]（as_of 之后第一个交易日），出场 = days[1+horizon]。"""
    days = _days(n)
    _write_stock(tmp_path, [
        {"date": days[0], "open": 100.0, "low": 100.0, "close": 100.0,
         "raw_close": 100.0, "raw_low": 100.0},
        {"date": days[1], "open": entry_open, "low": entry_low, "close": entry_close,
         "raw_close": entry_raw_close, "raw_low": entry_low},
    ] + [
        {"date": d, "open": exit_close, "low": exit_close, "close": exit_close,
         "raw_close": exit_close, "raw_low": exit_close}
        for d in days[2:]
    ])
    _write_index(tmp_path, [(d, 4000.0) for d in days])
    card = ledger.build_card(as_of=days[0].isoformat(), picks=_picks(limit=limit),
                             horizon_sessions=horizon)
    return settle._settle_pick(tmp_path, card, card["picks"][0],
                               [d.isoformat() for d in days])


def test_fill_at_open_when_open_below_limit(tmp_path: Path):
    rec = _settle_one(tmp_path, entry_open=95.0, entry_low=90.0, limit=100.0)
    assert rec["fill"]["status"] == "FILLED"
    assert rec["fill"]["basis"] == "OPEN"
    assert rec["fill"]["price"] == pytest.approx(95.0)


def test_fill_at_limit_when_only_low_touches(tmp_path: Path):
    rec = _settle_one(tmp_path, entry_open=105.0, entry_low=98.0, limit=100.0)
    assert rec["fill"]["basis"] == "LIMIT"
    assert rec["fill"]["price"] == pytest.approx(100.0)


def test_not_filled_is_recorded_not_dropped(tmp_path: Path):
    rec = _settle_one(tmp_path, entry_open=105.0, entry_low=102.0, limit=100.0)
    assert rec["fill"]["status"] == "NOT_FILLED"
    assert rec["outcome"] is None
    assert "高于限价" in rec["fill"]["detail"]


def test_adjusted_return_removes_exrights_jump(tmp_path: Path):
    """复权因子 2：复权开盘 100 = raw 50 成交；到期复权 110 ⇒ +10%，不是 +120%。"""
    rec = _settle_one(tmp_path, entry_open=100.0, entry_low=100.0,
                      entry_close=100.0, entry_raw_close=50.0,
                      exit_close=110.0, limit=50.0)
    assert rec["fill"]["status"] == "FILLED"
    assert rec["fill"]["price"] == pytest.approx(50.0)
    assert rec["price_basis"] == "ADJUSTED"
    assert rec["exit"]["ret"] == pytest.approx(0.10, abs=1e-9)
    assert rec["outcome"] == 1
    assert rec["brier"] == pytest.approx((0.6 - 1) ** 2, abs=1e-9)


def test_excess_against_benchmark(tmp_path: Path):
    days = _days(25)
    _write_stock(tmp_path, [
        {"date": d, "open": 100.0, "low": 100.0,
         "close": 100.0 if i <= 1 else 110.0,
         "raw_close": 100.0 if i <= 1 else 110.0, "raw_low": 100.0}
        for i, d in enumerate(days)
    ])
    _write_index(tmp_path, [(d, 4000.0 if i <= 1 else 4200.0) for i, d in enumerate(days)])
    card = ledger.build_card(as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5)
    rec = settle._settle_pick(tmp_path, card, card["picks"][0], [d.isoformat() for d in days])
    assert rec["benchmark_ret"] == pytest.approx(0.05, abs=1e-9)
    assert rec["excess"] == pytest.approx(0.10 - 0.05, abs=1e-9)


def test_pending_when_horizon_not_reached(tmp_path: Path):
    days = _days(4)
    _write_stock(tmp_path, [
        {"date": d, "open": 100.0, "low": 100.0, "close": 100.0,
         "raw_close": 100.0, "raw_low": 100.0} for d in days
    ])
    card = ledger.build_card(as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5)
    assert settle._settle_pick(tmp_path, card, card["picks"][0],
                               [d.isoformat() for d in days]) is None


def test_settle_records_pwin_source(tmp_path: Path):
    """p_win 的出处必须随结算落盘，否则事后分不清谁在得分。"""
    days = _days(25)
    _write_stock(tmp_path, [
        {"date": d, "open": 100.0, "low": 100.0, "close": 100.0 if i == 0 else 110.0,
         "raw_close": 100.0 if i == 0 else 110.0, "raw_low": 100.0}
        for i, d in enumerate(days)
    ])
    card = ledger.build_card(
        as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5,
        forecast={"pwin": {"source": "ledger", "n": 40, "verdict": "RELIABLE"}},
    )
    ledger.save_card(tmp_path, card)
    settle.settle(tmp_path)
    row = ledger.load_settlements(tmp_path)[0]
    assert row["p_win_source"] == "ledger"
    assert row["p_win_n"] == 40
    assert row["p_win_verdict"] == "RELIABLE"


def test_settle_marks_manual_pwin_as_unknown(tmp_path: Path):
    """手工建卡没有 forecast.pwin ⇒ 出处为 None，不冒充账本自举。"""
    days = _days(25)
    _write_stock(tmp_path, [
        {"date": d, "open": 100.0, "low": 100.0, "close": 100.0 if i == 0 else 110.0,
         "raw_close": 100.0 if i == 0 else 110.0, "raw_low": 100.0}
        for i, d in enumerate(days)
    ])
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5))
    settle.settle(tmp_path)
    row = ledger.load_settlements(tmp_path)[0]
    assert row["p_win_source"] is None
    assert settle.score([row], basis="abs")["by_source"]["unknown"]["n"] == 1


def test_settle_is_idempotent(tmp_path: Path):
    days = _days(25)
    _write_stock(tmp_path, [
        {"date": d, "open": 100.0, "low": 100.0, "close": 100.0 if i == 0 else 110.0,
         "raw_close": 100.0 if i == 0 else 110.0, "raw_low": 100.0}
        for i, d in enumerate(days)
    ])
    card = ledger.build_card(as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5)
    ledger.save_card(tmp_path, card)
    first = settle.settle(tmp_path)
    second = settle.settle(tmp_path)
    assert first["written"] == 1
    assert second["written"] == 0
    assert len(ledger.load_settlements(tmp_path)) == 1


def test_settle_concurrent_no_duplicate_rows(tmp_path: Path, monkeypatch):
    """并发 settle：唯一键数必须等于写入行数（P2，fail-closed 幂等）。

    缺陷形态：`settle()` 读 done → 算记录 → append 三步之间无锁，并发调用
    都读到空 done 集合并各自 append 同样的 key ⇒ settlements.jsonl 出现
    重复行，`score()` 把 1 个真实样本计成多个（Windows 上还会因并发打开
    同一账本文件抛 PermissionError）。

    测试不靠调度运气：monkeypatch 让**第一个**到达计算点的线程停留 2 秒，
    其余线程在此期间必然都已读完空 done——竞争窗口被钉成确定性。修复
    （整段序列持锁串行化）后，其余线程在锁内重读 done，看见已写入的键
    而跳过。
    """
    days = _days(25)
    _write_stock(tmp_path, [
        {"date": d, "open": 100.0, "low": 100.0, "close": 100.0 if i == 0 else 110.0,
         "raw_close": 100.0 if i == 0 else 110.0, "raw_low": 100.0}
        for i, d in enumerate(days)
    ])
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5))

    orig = settle._settle_pick
    arrivals = 0
    arrivals_lock = threading.Lock()

    def slow_first_pick(data_dir, card, pick, calendar):
        nonlocal arrivals
        with arrivals_lock:
            arrivals += 1
            is_first = arrivals == 1
        if is_first:
            time.sleep(2.0)  # 拉住第一个线程，等其余线程越过 done 读取
        return orig(data_dir, card, pick, calendar)

    monkeypatch.setattr(settle, "_settle_pick", slow_first_pick)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            settle.settle(tmp_path)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发 settle 抛出异常：{errors!r}"
    keys = [str(r.get("key")) for r in ledger.load_settlements(tmp_path)]
    assert len(keys) == len(set(keys)) == 1


# ---------------------------------------------------------------------------
# 校准
# ---------------------------------------------------------------------------


def _row(p: float, y: int, ret: float = 0.05, excess: float = 0.02) -> dict:
    return {"outcome": y, "p_win": p, "brier": (p - y) ** 2,
            "logloss": 0.0, "exit": {"ret": ret}, "excess": excess,
            "fill": {"status": "FILLED"}, "strategy_id": "s1"}


def test_score_insufficient_below_min_n():
    rows = [_row(0.6, 1) for _ in range(settle.MIN_N - 1)]
    assert settle.score(rows)["verdict"] == "INSUFFICIENT"


def test_score_splits_by_pwin_source():
    """混合出处的总分不可解释：同一批样本必须能拆开看谁在得分。

    同一个 p=0.9，在「账本算的」那组全中、在「人手填的」那组全错。
    合并成一个 skill 会得到一个既不是前者也不是后者的数字。
    """
    rows = []
    for y, source in ((1, "ledger"), (0, "manual")):
        for _ in range(10):
            r = _row(0.9, y)
            r["outcome_med"] = y
            r["p_win_source"] = source
            rows.append(r)
    out = settle.score(rows, basis="median")
    assert out["by_source"]["ledger"]["n"] == 10
    assert out["by_source"]["manual"]["n"] == 10
    assert out["by_source"]["ledger"]["skill"] > 0
    assert out["by_source"]["manual"]["skill"] < 0


def test_score_beats_constant_when_forecast_is_sharper():
    # 命中率 50%，但 p 在命中时高、未命中时低 ⇒ 有信息
    rows = [_row(0.9, 1) for _ in range(8)] + [_row(0.1, 0) for _ in range(8)]
    out = settle.score(rows)
    assert out["n"] == 16 and out["hit_rate"] == pytest.approx(0.5)
    assert out["brier"] < out["brier_const"]
    assert out["skill"] > 0 and out["verdict"] == "BEATS_CONSTANT"


def test_score_no_information_when_forecast_is_constant():
    rows = [_row(0.5, 1) for _ in range(8)] + [_row(0.5, 0) for _ in range(8)]
    out = settle.score(rows)
    assert out["skill"] == pytest.approx(0.0)
    assert out["verdict"] == "NO_INFORMATION"


def test_prior_counts_forward_returns(tmp_path: Path):
    days = _flat_market(tmp_path, n=40, price=100.0)
    out = settle.prior(tmp_path, horizon=5, sample=3, step=5)
    assert out["n_obs"] > 0
    assert out["p0"] == 0.0          # 平盘：没有任何标的上涨
    assert out["n_dates"] == 3
    assert days  # 保持 fixture 被使用


# ---------------------------------------------------------------------------
# K3 损坏卡 fail-closed（load_cards 不静默跳过，2026-11-07）
# ---------------------------------------------------------------------------


def _corrupt_card_file(tmp_path: Path, name: str = "dc-2099-01-01-ffffffff.json",
                       content: str = "{not json") -> Path:
    path = ledger.cards_dir(tmp_path) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_load_cards_raises_on_corrupt_card(tmp_path: Path):
    """损坏 JSON 直接抛错并列出文件名。

    缺陷形态（K3 缺口）：旧实现 `except: continue` 静默跳过损坏卡，
    K3 门和结算统计把残缺账本当成完整账本继续用。
    """
    days = _days(5)
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5))
    _corrupt_card_file(tmp_path)
    with pytest.raises(ledger.LedgerCorruptError) as ei:
        ledger.load_cards(tmp_path)
    assert "dc-2099-01-01-ffffffff.json" in str(ei.value)


def test_load_cards_raises_on_foreign_kind(tmp_path: Path):
    """cards 目录里 kind 非决策卡的 dc-*.json 同样 fail-closed。

    cards 目录只有 save_card 一个写入方，出现非决策卡就是损坏或污染。
    """
    _corrupt_card_file(tmp_path, content=json.dumps({"kind": "SOMETHING-ELSE"}))
    with pytest.raises(ledger.LedgerCorruptError):
        ledger.load_cards(tmp_path)


def test_load_cards_empty_when_dir_missing(tmp_path: Path):
    """目录不存在仍是空账本，不是损坏。"""
    assert ledger.load_cards(tmp_path) == []


def test_settle_refuses_on_corrupt_ledger(tmp_path: Path):
    """结算入口同样 fail-closed：残缺账本上不产生任何结算行。"""
    days = _days(5)
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5))
    _corrupt_card_file(tmp_path)
    with pytest.raises(ledger.LedgerCorruptError):
        settle.settle(tmp_path)
    assert ledger.load_settlements(tmp_path) == []


def test_k3_gate_triggers_on_corrupt_card(tmp_path: Path):
    """K3 门把损坏转成 TRIGGERED（阻断放行），而不是崩溃或视作空账本。

    门自己必须保持可用：evaluate 整体崩溃会让其余三条门也一起失效。
    """
    days = _days(5)
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0].isoformat(), picks=_picks(), horizon_sessions=5))
    _corrupt_card_file(tmp_path)
    check = gates.check_k3_record(tmp_path, days[0].isoformat())
    assert check.status == gates.TRIGGERED
    assert "dc-2099-01-01-ffffffff.json" in check.detail
