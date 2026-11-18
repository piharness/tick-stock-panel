"""空仓决策（stance=flat）作为账本一等公民的测试。

要保护的三件事，每一条都能独立失败：
1. **旧卡不受影响**：`stance` 只在非 long 时进哈希，否则所有在册承诺作废。
2. **空仓不能被伪造成「策略坏了」**：`produce_card` 的 `skipped_no_picks` 语义
   保持不变；空仓必须由调用方显式声明并给出可读理由。
3. **读数口径唯一**：`avoided_pct = −benchmark_ret`，只在 `flat_summary()` 里
   取负号；流水里只存 `benchmark_ret` 一个数，避免两份定义漂移。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.decision import ledger, settle
from app.decision.ledger import (
    STANCE_FLAT,
    build_card,
    card_stance,
    flat_summary,
    verify_card,
)


def _long_pick(symbol: str = "600519.SH") -> dict:
    return {"symbol": symbol, "limit_price": 10.0, "p_win": 0.5}


# ── 1. 封卡与哈希 ────────────────────────────────────────────────────


def test_flat_card_seals_and_round_trips():
    card = build_card(
        as_of="2026-11-18",
        picks=[],
        horizon_sessions=20,
        stance=STANCE_FLAT,
        forecast={"stance": STANCE_FLAT, "reason": "广度 41%，无候选"},
    )
    assert card["stance"] == STANCE_FLAT
    assert card["picks"] == []
    assert card_stance(card) == STANCE_FLAT
    ok, why = verify_card(card)
    assert ok, why


def test_long_card_does_not_carry_stance_key():
    """旧卡没有 `stance` 键；新 long 卡也不能有，否则历史承诺全部验签失败。"""
    card = build_card(as_of="2026-11-18", picks=[_long_pick()], horizon_sessions=20)
    assert "stance" not in card
    assert card_stance(card) == "long"
    assert verify_card(card)[0]


def test_stance_must_match_picks():
    with pytest.raises(ValueError, match="空仓卡不能带标的"):
        build_card(
            as_of="2026-11-18", picks=[_long_pick()], horizon_sessions=20,
            stance=STANCE_FLAT,
        )
    # 反向不能放松：long 依然拒绝空 picks（原契约不变）
    with pytest.raises(ValueError, match="picks 不能为空"):
        build_card(as_of="2026-11-18", picks=[], horizon_sessions=20)


def test_flat_card_changes_digest_so_it_cannot_be_confused_with_long():
    flat = build_card(
        as_of="2026-11-18", picks=[], horizon_sessions=20, stance=STANCE_FLAT,
        forecast={"stance": STANCE_FLAT, "reason": "不参与"},
    )
    long = build_card(as_of="2026-11-18", picks=[_long_pick()], horizon_sessions=20)
    assert flat["card_id"] != long["card_id"]


# ── 2. 生产入口的两条硬约束 ──────────────────────────────────────────


def test_flat_card_rejects_short_reason(tmp_path: Path):
    from app.decision.producer import produce_flat_card

    with pytest.raises(ValueError, match="空仓理由至少 10 个字符"):
        produce_flat_card(
            tmp_path, as_of="2026-11-18", horizon_sessions=20, reason="空仓",
        )


def test_flat_card_rejects_zero_horizon(tmp_path: Path):
    from app.decision.producer import produce_flat_card

    # horizon=0 ⇒ entry==exit ⇒ 基准收益恒 0 ⇒ 结算出来是「完美空仓」
    with pytest.raises(ValueError, match="horizon_sessions >= 1"):
        produce_flat_card(
            tmp_path, as_of="2026-11-18", horizon_sessions=0,
            reason="今天不参与，市场广度低于一半",
        )


# ── 3. 结算：窗口、幂等、未到期 ──────────────────────────────────────


CALENDAR = [f"2026-01-{d:02d}" for d in range(5, 31)]


def _save_flat(tmp_path: Path, *, as_of: str, horizon: int = 3, reason: str = "广度萎缩，决定不参与"):
    card = build_card(
        as_of=as_of,
        picks=[],
        horizon_sessions=horizon,
        stance=STANCE_FLAT,
        forecast={"stance": STANCE_FLAT, "reason": reason},
    )
    ledger.save_card(tmp_path, card)
    return card


@pytest.fixture()
def patched(monkeypatch):
    monkeypatch.setattr(settle.bars, "sessions", lambda _d, table=None: list(CALENDAR))
    monkeypatch.setattr(settle, "_benchmark_ret", lambda _d, _s, entry, exit_: -0.05)
    return monkeypatch


def test_settle_flats_records_window_and_benchmark(tmp_path: Path, patched):
    card = _save_flat(tmp_path, as_of="2026-01-05", horizon=3)
    result = settle.settle_flats(tmp_path)
    assert result["written"] == 1
    rows = ledger.load_flats(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == ledger.FLAT_KIND
    assert row["key"] == ledger.flat_key(card["card_id"])
    assert row["as_of"] == "2026-01-05"
    # 决策日收盘之后第一个交易日入场，持有 horizon 个交易日
    assert row["entry"] == "2026-01-06"
    assert row["exit"] == "2026-01-09"
    assert row["benchmark_ret"] == pytest.approx(-0.05)
    # 流水里只存 benchmark_ret，「避开了多少」由 summary 取负号
    assert "avoided_pct" not in row


def test_settle_flats_is_idempotent(tmp_path: Path, patched):
    _save_flat(tmp_path, as_of="2026-01-05", horizon=3)
    settle.settle_flats(tmp_path)
    again = settle.settle_flats(tmp_path)
    assert again["written"] == 0
    assert len(ledger.load_flats(tmp_path)) == 1


def test_settle_flats_waits_when_window_not_closed(tmp_path: Path, patched):
    _save_flat(tmp_path, as_of="2026-01-28", horizon=10)
    result = settle.settle_flats(tmp_path)
    assert result["written"] == 0
    assert result["pending"]["not_due"] == 1
    assert ledger.load_flats(tmp_path) == []


def test_settle_flats_skips_long_cards(tmp_path: Path, patched):
    """持仓卡不能进空仓流水：那会把「买了」记成「没买」。"""
    card = build_card(as_of="2026-01-05", picks=[_long_pick()], horizon_sessions=20)
    ledger.save_card(tmp_path, card)
    result = settle.settle_flats(tmp_path)
    assert result["cards"] == 0
    assert result["written"] == 0


# ── 4. 成绩单口径 ────────────────────────────────────────────────────


def test_flat_summary_keeps_missed_side_visible(tmp_path: Path):
    """只统计「躲开的跌幅」会得出一个必然正确的策略。踏空必须并列。"""
    base = {
        "kind": ledger.FLAT_KIND,
        "card_id": "dc-1",
        "as_of": "2026-01-05",
        "benchmark": "000300.SH",
        "horizon_sessions": 20,
        "entry": "2026-01-06",
        "exit": "2026-02-03",
    }
    ledger.append_flats(tmp_path, [
        {**base, "key": "dc-1:flat", "benchmark_ret": -0.05},
        {**base, "key": "dc-2:flat", "card_id": "dc-2", "benchmark_ret": 0.03},
        {**base, "key": "dc-3:flat", "card_id": "dc-3", "benchmark_ret": None},
    ])
    s = flat_summary(tmp_path)
    assert s["records"] == 3
    assert s["settled"] == 2
    assert s["avoided"] == 1
    assert s["missed"] == 1
    assert s["hit_rate"] == pytest.approx(0.5)
    assert s["avoided_sum_pct"] == pytest.approx(5.0)
    assert s["missed_sum_pct"] == pytest.approx(-3.0)
    # 净效果 = 躲开的 5% 减去踏空的 3%
    assert s["net_pct"] == pytest.approx(2.0)


def test_flat_summary_is_empty_before_any_record(tmp_path: Path):
    s = flat_summary(tmp_path)
    assert s["records"] == 0
    assert s["hit_rate"] is None
    assert s["net_pct"] == 0.0


# ── 5. 账本健康度把空仓流水的可读性算进去 ────────────────────────────


def test_ledger_health_degrades_on_broken_flat_line(tmp_path: Path):
    path = ledger.flat_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "wrong", "key": "x"}\n', encoding="utf-8")
    health = ledger.ledger_health(tmp_path)
    assert health["flat"]["exists"] is True
    assert health["flat"]["problems"]
    assert health["status"] == ledger.HEALTH_CORRUPT


def test_ledger_health_reports_flat_counts(tmp_path: Path):
    _save_flat(tmp_path, as_of="2026-01-05")
    health = ledger.ledger_health(tmp_path)
    assert health["flat"]["exists"] is False
    assert health["flat"]["rows"] == 0
    assert health["status"] == ledger.HEALTH_OK


# ── 6. 承诺哈希冻结（账本只追加，载荷一变在册承诺全废） ──────────────


def test_long_commitment_digest_is_frozen():
    """long 卡的哈希载荷必须与引入 `stance` 之前逐位相同。

    上一版测试只断言「卡上没有 stance 键」，那是 `build_card` 的返回值，
    抓不到 `commitment()` 偷偷多写一个键的变异 —— 实测变异确实是绿的。
    所以这里钉死 digest 本身：**任何**改变 long 卡承诺载荷的改动都会让它红，
    那正是我们要它做的事（改载荷必须显式更新这个常量并说明理由）。
    """
    card = build_card(
        as_of="2026-11-18",
        picks=[{"symbol": "600519.SH", "limit_price": 10.0, "p_win": 0.5}],
        horizon_sessions=20,
        strategy_id="l1_gap_reversal",
    )
    assert card["card_id"] == "dc-2026-11-18-8a6a67d4bc"


def test_flat_commitment_digest_is_frozen():
    card = build_card(
        as_of="2026-11-18",
        picks=[],
        horizon_sessions=20,
        stance=STANCE_FLAT,
        strategy_id="l1_gap_reversal",
        forecast={"stance": STANCE_FLAT, "reason": "广度萎缩"},
    )
    assert card["card_id"] == "dc-2026-11-18-4ed93a5e86"
