"""回避账本（TSP-DECISION-AVOID-v1）的契约测试。

要保护的三件事，每条都能独立失败：
1. **不冒充空仓**：回避记录不进 flat 流水，`flat_summary` 的样本不因它变化。
2. **幂等靠 key**：同一天同一只票写两次必须被 scan 报出来，而不是累计成两次回避。
3. **结算口径**：entry = 决策日次一交易日，excess = 个股 − 基准；个股跑输记为 avoided。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.decision import avoid, ledger


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def _fake_market(monkeypatch, calendar, prices, bench):
    """prices: {(day,symbol): close}；bench: {day: close}"""
    monkeypatch.setattr(avoid.bars, "sessions", lambda _d: list(calendar))

    def _bar_map(_d, day, symbols, table=None):
        src = bench if table == avoid.bars.INDEX_ENRICHED else prices
        out = {}
        for s in symbols:
            if table == avoid.bars.INDEX_ENRICHED:
                if day in src:
                    out[s] = {"close": src[day]}
            elif (day, s) in src:
                out[s] = {"close": src[(day, s)]}
        return out

    monkeypatch.setattr(avoid.bars, "bar_map", _bar_map)


def test_build_rejects_zero_horizon():
    with pytest.raises(ValueError, match="horizon_sessions"):
        avoid.build_avoid(as_of="2026-11-19", symbol="002685.SZ", ref_close=7.61,
                          rule="r", reason="x", horizon_sessions=0)


def test_round_trip_and_no_flat_pollution(data_dir: Path):
    row = avoid.build_avoid(as_of="2026-11-19", symbol="002685.SZ", ref_close=7.61,
                            rule="high_mom_turn", reason="20日+57%/换手44.8%")
    assert avoid.append_avoids(data_dir, [row]) == 1
    rows = avoid.load_avoids(data_dir)
    assert len(rows) == 1 and rows[0]["key"] == "2026-11-19:002685.SZ"
    # 回避账本不得污染空仓成绩单
    assert ledger.flat_summary(data_dir)["records"] == 0


def test_duplicate_key_is_reported_not_accumulated(data_dir: Path):
    row = avoid.build_avoid(as_of="2026-11-19", symbol="002685.SZ", ref_close=7.61,
                            rule="r", reason="x")
    avoid.append_avoids(data_dir, [row])
    avoid.append_avoids(data_dir, [row])
    got, problems = avoid.scan_avoids(avoid.avoid_path(data_dir))
    assert len(got) == 1
    assert any("key 重复" in p for p in problems)


def test_settle_computes_excess_and_marks_avoided(data_dir: Path, monkeypatch):
    cal = ["2026-11-19", "2026-11-20", "2026-11-23"]
    # 个股从 10 跌到 8（-20%），基准从 100 跌到 99（-1%）⇒ 跑输 19pct ⇒ avoided
    _fake_market(monkeypatch, cal,
                 prices={("2026-11-20", "002685.SZ"): 10.0,
                         ("2026-11-23", "002685.SZ"): 8.0},
                 bench={"2026-11-20": 100.0, "2026-11-23": 99.0})
    row = avoid.build_avoid(as_of="2026-11-19", symbol="002685.SZ", ref_close=7.61,
                            rule="high_mom_turn", reason="x", horizon_sessions=1)
    avoid.append_avoids(data_dir, [row])

    out = avoid.settle_avoids(data_dir)
    assert out["settled"] == 1
    r = out["rows"][0]
    assert r["entry"] == "2026-11-20" and r["exit"] == "2026-11-23"
    assert r["ret"] == pytest.approx(-0.20)
    assert r["excess"] == pytest.approx(-0.19)

    s = avoid.avoid_summary(data_dir)
    assert s["avoided"] == 1 and s["missed"] == 0
    assert s["net_pct"] == pytest.approx(19.0)


def test_settle_marks_missed_when_stock_outperforms(data_dir: Path, monkeypatch):
    cal = ["2026-11-19", "2026-11-20", "2026-11-23"]
    _fake_market(monkeypatch, cal,
                 prices={("2026-11-20", "X"): 10.0, ("2026-11-23", "X"): 12.0},
                 bench={"2026-11-20": 100.0, "2026-11-23": 100.0})
    avoid.append_avoids(data_dir, [avoid.build_avoid(
        as_of="2026-11-19", symbol="X", ref_close=10.0, rule="r", reason="x", horizon_sessions=1)])
    s = avoid.avoid_summary(data_dir)
    assert s["missed"] == 1 and s["avoided"] == 0
    assert s["net_pct"] < 0  # 踏空 20%，净效果为负


def test_not_due_is_pending_not_zero(data_dir: Path, monkeypatch):
    """未到期必须报 pending；静默计成 0 超额会让「还没结算」看起来像「回避无效」。"""
    cal = ["2026-11-19", "2026-11-20"]
    _fake_market(monkeypatch, cal, prices={}, bench={})
    avoid.append_avoids(data_dir, [avoid.build_avoid(
        as_of="2026-11-19", symbol="X", ref_close=10.0, rule="r", reason="x", horizon_sessions=20)])
    out = avoid.settle_avoids(data_dir)
    assert out["settled"] == 0 and out["pending"]["not_due"] == 1


def test_missing_price_is_pending(data_dir: Path, monkeypatch):
    cal = ["2026-11-19", "2026-11-20", "2026-11-23"]
    _fake_market(monkeypatch, cal, prices={}, bench={"2026-11-20": 100.0, "2026-11-23": 99.0})
    avoid.append_avoids(data_dir, [avoid.build_avoid(
        as_of="2026-11-19", symbol="GONE", ref_close=10.0, rule="r", reason="x", horizon_sessions=1)])
    out = avoid.settle_avoids(data_dir)
    assert out["settled"] == 0 and out["pending"]["price_missing"] == 1


def test_scan_rejects_foreign_kind(data_dir: Path):
    p = avoid.avoid_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"kind":"TSP-DECISION-FLAT-v1","key":"a"}\n', encoding="utf-8")
    rows, problems = avoid.scan_avoids(p)
    assert rows == [] and problems
