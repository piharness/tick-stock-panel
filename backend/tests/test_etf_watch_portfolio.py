"""组合回放的两条硬约束：资金不能重复使用、成本真的从权益里扣。

这两条错了，账户曲线会凭空好看：前者把 7 个信号都按 1/6 建仓（超配），
后者让 6bp 只写在文档里、不进净值。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch_portfolio as pf  # noqa: E402


def _world(n_ind: int, days: int = 40):
    sessions = list(pd.bdate_range("2026-01-05", periods=days))
    prices = {}
    cols = {}
    for k in range(n_ind):
        sym = f"X{k}.SH"
        prices[sym] = pd.DataFrame(
            {"close_adj": np.linspace(1.0, 1.1, days), "hi_adj": np.linspace(1.0, 1.1, days),
             "lo_adj": np.linspace(1.0, 1.1, days)},
            index=pd.DatetimeIndex(sessions),
        )
        cols[f"IND{k}"] = sym
    sym_mat = pd.DataFrame([cols] * days, index=pd.DatetimeIndex(sessions))
    ev = pd.DataFrame([{"date": sessions[0], "industry": f"IND{k}", "i": 0, "ym": "2026-01", "dd": -0.4}
                       for k in range(n_ind)])
    vols = {s: pd.Series(0.3, index=pd.DatetimeIndex(sessions)) for s in prices}
    return sessions, prices, vols, sym_mat, ev


def test_alloc_full_invests_everything_and_charges_rebalance_cost():
    pos = {"A": {"sym": "a", "value": 500_000.0}, "B": {"sym": "b", "value": 500_000.0}}
    vols = {"a": pd.Series(0.2, index=[pd.Timestamp("2026-01-05")]),
            "b": pd.Series(0.4, index=[pd.Timestamp("2026-01-05")])}
    target, cash = pf._alloc_full(pos, 0.0, vols, pd.Timestamp("2026-01-05"))

    assert cash == 0.0
    # 逆波动：低波那只拿双倍权重
    assert target["A"] == pytest.approx(2 * target["B"], rel=1e-6)
    # 总权益 100 万，换手 33.3 万 × 6bp = 200 元，扣完全部投出
    assert sum(target.values()) == pytest.approx(1_000_000 - 333_334 * pf.COST, rel=1e-4)


def test_six_slots_never_uses_more_than_six_slots_of_capital():
    sessions, prices, vols, sym_mat, ev = _world(7)
    r = pf.replay(ev, sessions, prices, vols, sym_mat, "six_slots")

    assert r["metrics"]["trades"] == 6          # 第 7 个信号没有资金，不进场
    assert r["metrics"]["final_equity"] > pf.INIT
    # 六格满仓、每只涨 5.1% ⇒ 账户约 +5%；若第 7 个也被塞进 1/6（超配），会到 ~6%
    assert 0.045 < r["metrics"]["total"] < 0.055
