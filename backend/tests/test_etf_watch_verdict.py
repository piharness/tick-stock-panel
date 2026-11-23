"""V1 判据的两处防自欺：按「事件簇」计数、左尾用 MAE 而不是不存在的 p10。

这两条是 `docs/structural-edge/industry-etf-watch.md` §5 的机械保证：
同一天 6 个行业一起跌破，是一波行情里的 6 个标签，不是 6 次独立机会。
如果把按行计数写回代码，V1 会在第一天就显示「6/3 达成」——这类「绿但无感知」比红灯更贵。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch  # noqa: E402


def _ev(day: str, industry: str, signal: bool = True) -> dict:
    return {"type": "event", "id": f"{day}|{industry}", "date": day, "industry": industry,
            "dd": -0.40, "vol_pct": 0.8, "state": "下行·高波", "signal": signal,
            "qualified": signal, "r3": True, "f6": True, "best_symbol": "x", "best_name": "x",
            "amt20": 1e8, "reasons": [], "source": "live"}


def _st(day: str, industry: str, rel: float, mae: float) -> dict:
    return {"type": "settle", "id": f"{day}|{industry}", "settled_at": "2027-01-01",
            "entry": day, "exit": day, "ret20": rel + 0.003, "rel20": rel, "mae": mae, "mfe": 0.1}


def test_six_industries_in_one_month_is_one_cluster():
    """6 个行业同月触发 ⇒ 1 簇，进度 1/3，不是 6/3。"""
    events = [_ev("2026-11-20", x) for x in ("电子", "军工", "电新", "传媒", "计算机", "机械")]
    settles = [_st("2026-11-20", x, 0.05, -0.02) for x in ("电子", "军工", "电新", "传媒", "计算机", "机械")]

    v1 = etf_watch.v1_verdict(events, settles)

    assert v1["state"] == "WAITING"
    assert v1["progress"] == "1/3"
    assert v1["signal_events"] == 6 and v1["signal_clusters"] == 1


def test_low_vol_events_never_count_toward_v1():
    """低波阴跌（signal=False）只做 F2 对照，不能推进 V1。"""
    events = [_ev("2026-11-20", "交运", signal=False), _ev("2026-12-18", "有色", signal=False)]
    settles = [_st("2026-11-20", "交运", 0.05, -0.02), _st("2026-12-18", "有色", 0.05, -0.02)]

    v1 = etf_watch.v1_verdict(events, settles)

    assert v1["state"] == "WAITING" and v1["progress"] == "0/3"


def test_three_clusters_with_positive_mean_and_shallow_mae_pass():
    months = ["2026-11", "2026-12", "2027-01"]
    events = [_ev(f"{m}-20", "电子") for m in months]
    settles = [_st(f"{m}-20", "电子", 0.02, -0.03) for m in months]

    v1 = etf_watch.v1_verdict(events, settles)

    assert v1["state"] == "PASS"
    assert v1["left_tail_ok"] == 3


def test_left_tail_uses_mae_not_percentile():
    """均值正、但 3 簇里有 2 簇 MAE 跌破 −8% ⇒ 不通过（p10 在 N=3 估不出来）。"""
    months = ["2026-11", "2026-12", "2027-01"]
    events = [_ev(f"{m}-20", "电子") for m in months]
    settles = [_st(f"{m}-20", "电子", 0.06, mae) for m, mae in zip(months, (-0.12, -0.10, -0.01))]

    v1 = etf_watch.v1_verdict(events, settles)

    assert v1["mean_rel_bp"] > 0
    assert v1["left_tail_ok"] == 1
    assert v1["state"] == "FAIL"


@pytest.mark.parametrize("rels", [(-0.15, -0.12, 0.20), (-0.11, 0.05, -0.30)])
def test_two_bad_clusters_retire_unconditionally(rels):
    months = ("2026-11", "2026-12", "2027-01")
    events = [_ev(f"{m}-20", "电子") for m in months]
    settles = [_st(f"{m}-20", "电子", r, -0.02) for m, r in zip(months, rels)]

    v1 = etf_watch.v1_verdict(events, settles)

    assert v1["state"] == "RETIRED"
    assert "V4" in v1["why"]
