"""锁仓横盘 · 首次放量预注册（prereg-lock-surge-2026-11-25）的单元测试。

这套代码的结论是「不采纳」，但判据与统计口径仍要锁住 —— 否则以后有人
把均值换成中位数、把月聚簇换成日级 t，就能在同一个台账上「发现」显著。
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from app.services import lock_surge as L


def test_split_uses_frozen_ratio():
    band = [
        {"symbol": "A", "tr": 1.99},
        {"symbol": "B", "tr": 2.00},
        {"symbol": "C", "tr": 5.10},
        {"symbol": "D", "tr": None},
    ]
    s, c = L._split(band)
    assert s == ["B", "C"]
    assert c == ["A", "D"]


def test_split_can_be_recomputed_at_other_ratios_for_diagnostics():
    band = [{"symbol": "A", "tr": 1.6}, {"symbol": "B", "tr": 3.2}]
    assert L._split(band, 1.5)[0] == ["A", "B"]
    assert L._split(band, 3.0)[0] == ["B"]


def _daily(month: str, diff: float, n_surge: int = 3) -> dict:
    return {
        "date": f"{month}-15", "month": month, "window": "design",
        "source": "backfill", "n_surge": n_surge, "n_calm": 50,
        "surge_ex": diff, "calm_ex": 0.0, "diff": diff, "oneword": 0.0,
    }


def test_summarize_clusters_by_month_not_by_day():
    """同一个月 20 天只算 1 个观测：日级 t 会把重叠窗口当独立样本。"""
    daily = [_daily("2026-01", 0.01) for _ in range(20)]
    daily += [_daily("2026-02", -0.01) for _ in range(20)]
    s = L.summarize(daily)
    assert s["days"] == 40
    assert s["months"] == 2
    # 两个月均值 +100bp / −100bp → 均值 0
    assert abs(s["diff_bp"]) < 1e-6


def test_summarize_t_uses_month_series():
    daily = [_daily(f"2026-{m:02d}", 0.01 * (1 if m % 2 else -1)) for m in range(1, 9)]
    s = L.summarize(daily)
    assert s["months"] == 8
    assert s["diff_t"] == 0.0   # 完全对称的正负交替，t 应为 0


def test_verdict_is_insufficient_before_six_months():
    s = L.summarize([_daily(f"2026-{m:02d}", 0.05) for m in range(1, 6)])
    v = L._verdict(s)
    assert v["verdict"] == "insufficient"
    assert "5/" in v["reason"]


def test_verdict_rejects_when_diff_below_gate():
    # 7 个月，全部 +10bp：t 很大但远低于 80bp 门槛
    s = L.summarize([_daily(f"2026-{m:02d}", 0.001 + m * 1e-6) for m in range(1, 8)])
    v = L._verdict(s)
    assert v["verdict"] == "reject"
    assert v["checks"]["main_diff"] is False


def test_verdict_passes_only_when_all_gates_hold():
    s = L.summarize([_daily(f"2026-{m:02d}", 0.02 + m * 1e-4, n_surge=5) for m in range(1, 8)])
    v = L._verdict(s)
    assert v["checks"]["main_diff"] is True
    assert v["checks"]["main_t"] is True
    assert v["checks"]["capacity"] is True
    assert v["verdict"] == "pass"


def _write_day(root: Path, day: str, hhmm: str) -> None:
    d = root / "kline_daily_enriched" / f"date={day}"
    d.mkdir(parents=True)
    ts = int(datetime.fromisoformat(f"{day}T{hhmm}:00").timestamp() * 1000)
    pl.DataFrame({"symbol": ["000001.SZ"], "quote_ts": [ts]}).write_parquet(d / "part.parquet")


def test_is_final_rejects_intraday_partition(tmp_path: Path):
    _write_day(tmp_path, "2026-11-25", "12:32")
    assert L._is_final(tmp_path, "2026-11-25") is False


def test_is_final_accepts_after_close(tmp_path: Path):
    _write_day(tmp_path, "2026-11-25", "17:10")
    assert L._is_final(tmp_path, "2026-11-25") is True


def test_is_final_missing_partition(tmp_path: Path):
    assert L._is_final(tmp_path, "2026-11-25") is False


def test_gate_constants_are_frozen():
    """改这些数必须同时改 docs/structural-edge/prereg-lock-surge.md；这里只防手滑。"""
    assert L.SURGE_RATIO == 2.0
    assert L.GATE_DIFF_BP == 80.0
    assert L.GATE_T == 1.65
    assert L.GATE_MIN_MONTHS == 6
    assert L.FWD == 20
    assert L.PREREG_START == "2026-11-26"
