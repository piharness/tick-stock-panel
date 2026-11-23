"""历史回放的纯函数：轨迹分段与事后收益。

这两个函数决定「首次触发/连续/再进入」和「5/10/20 日 + MAE/MFE」，
必须能在不加载真实行情的情况下用合成序列钉住，否则页面看起来有数、口径却无人守。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch_replay as rp  # noqa: E402


def _day(day: str, status: str, name: str = "载体A") -> dict:
    return {
        "date": day, "status": status, "dd": -0.40, "vol_pct": 0.80,
        "amt20": 1e8, "best_symbol": "X.SH", "best_name": name,
        "r1": True, "r2": True, "r3": True, "f6": True,
        "reasons": [], "market_state": "下行·高波",
    }


def test_track_first_consecutive_and_reentry():
    days = [
        _day("2026-11-10", "NEUTRAL"),
        _day("2026-11-11", "CANDIDATE"),
        _day("2026-11-12", "CANDIDATE"),
        _day("2026-11-13", "BLOCKED"),
        _day("2026-11-16", "CANDIDATE"),
        _day("2026-11-17", "CANDIDATE"),
    ]
    t = rp.build_track("电子", days)

    assert t["first_trigger"] == "2026-11-11"
    assert t["trigger_count"] == 4
    assert t["max_consecutive"] == 2
    assert [(s["start"], s["end"], s["days"]) for s in t["segments"]] == [
        ("2026-11-11", "2026-11-12", 2),
        ("2026-11-16", "2026-11-17", 2),
    ]
    assert t["left_censored"] is False


def test_track_flags_left_censored_when_window_starts_in_candidate():
    t = rp.build_track("电子", [
        _day("2026-11-11", "CANDIDATE"),
        _day("2026-11-12", "CANDIDATE"),
    ])
    assert t["left_censored"] is True
    assert t["max_consecutive"] == 2
    assert t["first_trigger"] == "2026-11-11"


def test_outcome_enters_next_close_and_uses_entry_as_base():
    days = [_day("2026-11-10", "CANDIDATE")]
    t = rp.build_track("电子", days)
    dates = pd.to_datetime([
        "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
        "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19",
        "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25",
    ])
    closes = [100, 100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120]
    lows = list(map(float, closes))
    lows[1] = 80.0   # 进场日盘内下探 —— 发生在买入之前，不该算进 MAE
    lows[2] = 90.0   # 进场后最深
    highs = list(map(float, closes))
    highs[11] = 121.0
    bars = {"X.SH": pd.DataFrame(
        {"close_adj": closes, "lo_adj": lows, "hi_adj": highs}, index=pd.DatetimeIndex(dates))}
    pos = {d: i for i, d in enumerate(dates.tolist())}

    o = rp.build_outcome(t, dates.tolist(), pos, bars, pd.Timestamp("2026-11-25"))

    assert o["entry"] == "2026-11-11"      # t+1
    assert o["ret5"] == pytest.approx(0.10)   # 110/100 - 1
    assert o["ret10"] == pytest.approx(0.20)  # 120/100 - 1
    assert o["to_end"] == pytest.approx(0.20)
    assert o["mae"] == pytest.approx(-0.10)        # 盘中 90/100-1
    assert o["mae_close"] == pytest.approx(0.0)    # 收盘口径看不到这一跌
    assert o["mfe"] == pytest.approx(0.21)
    assert "不计成本" not in o["note"]  # 公共口径在页面表头，不逐行重复


def test_mae_gap_reports_worst_industry_and_ignores_missing():
    g = rp.mae_gap([
        {"industry": "A", "mae": -0.10, "mae_close": -0.02},
        {"industry": "B", "mae": -0.06, "mae_close": -0.06},
        {"industry": "C", "mae": None, "mae_close": None},
    ])
    assert g["n"] == 2
    assert g["worst"]["industry"] == "A"
    assert g["worst"]["delta_bp"] == pytest.approx(-800)
    assert g["median_delta_bp"] == pytest.approx(-400)


def test_outcome_no_trigger_is_explicit_not_zero():
    t = rp.build_track("电子", [_day("2026-11-10", "NEUTRAL"), _day("2026-11-11", "BLOCKED")])
    o = rp.build_outcome(t, [pd.Timestamp("2026-11-10")], {pd.Timestamp("2026-11-10"): 0}, {}, pd.Timestamp("2026-11-10"))

    assert o["first_trigger"] is None
    assert o["ret5"] is None and o["to_end"] is None
    assert "未触发" in o["note"]
