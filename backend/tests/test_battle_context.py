"""参照系层测试：分位、条件基数、以及「不许看未来」。

这里的判据不是「函数能跑」，而是三条会算错的口径：
  1. 分位必须是 ≤ 的占比，不是插值，也不是 > ；
  2. 条件样本只能取目标日之前的日子，且次日收益取下一行；
  3. 样本不足时不许给均值（小样本结论比没有结论更贵）。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.services import battle_context as bc


def _frame(n: int, *, up_ratios: list[float] | None = None, states: list[str] | None = None) -> pl.DataFrame:
    start = date(2026, 1, 1)
    rows = []
    for i in range(n):
        rows.append({
            "date": start + timedelta(days=i),
            "state": (states[i] if states else "weak"),
            "up_ratio": (up_ratios[i] if up_ratios else 0.5 + i * 0.01),
            "median_pct": -0.01 + i * 0.001,
            "seal_rate": 0.5,
            "broken_limit": 20 + i,
            "max_consecutive": 4,
            "total_amount": 2e12,
            "score": 30 + i,
            "avg_pct": 0.001 * ((i % 5) - 2),
        })
    return pl.DataFrame(rows)


def test_pct_rank_is_le_share():
    assert bc.pct_rank([1, 2, 3, 4], 3) == 75.0
    assert bc.pct_rank([1, 2, 3, 4], 0) == 0.0
    assert bc.pct_rank([1, 2, 3, 4], 9) == 100.0


def test_metrics_report_rank_and_quantiles():
    out = bc.build(_frame(60), window=60)
    up = next(m for m in out["metrics"] if m["key"] == "up_ratio")
    assert up["pct_rank"] == 100.0          # 单调递增 → 最后一天最高
    assert up["n"] == 60
    assert up["p05"] <= up["p50"] <= up["p95"]
    assert out["as_of"] == str(date(2026, 1, 1) + timedelta(days=59))


def test_analogs_never_use_the_last_row():
    """最后一天没有「次日」，不能进条件样本，也不能进基准。"""
    out = bc.build(_frame(40), window=40)
    assert out["analogs"]["baseline"]["n"] == 39
    # 第二天起才有下一行，首行的 fwd 存在但最后一行不存在
    assert out["analogs"]["narrow"]["n"] <= 39


def test_small_sample_reports_n_only():
    """样本 < 20 时不给均值：33 天和 3 天不是一回事。"""
    out = bc.build(_frame(25), window=25)
    narrow = out["analogs"]["narrow"]
    if 0 < narrow["n"] < bc.MIN_ANALOG_DAYS:
        assert narrow["mean_bp"] is None
        assert narrow["hit"] is None
    assert out["analogs"]["baseline"]["mean_bp"] is not None


def test_empty_history_is_safe():
    out = bc.build(pl.DataFrame())
    assert out["metrics"] == []
    assert out["n_history"] == 0


def test_same_state_uses_the_last_state_label():
    states = ["weak"] * 20 + ["strong"] * 20
    out = bc.build(_frame(40, states=states), window=40)
    assert out["analogs"]["same_state"]["label"] == "同为「strong」"
    assert out["analogs"]["same_state"]["n"] == 19   # 最后一行不算
