"""红利全收益核验的三个口径：分红日、现金周末累积、月度再平衡成本。

任一错一处，5.28% / 8.46% 这类数字就会偏向有利方向：
- 分红日不还原 → 全收益被记成价格收益（少 2–4pct/年）；
- 周末不计息 → 现金收益少约 29%；
- 再平衡不收费 → 组合年化白拿几个 bp。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch_dividend as dv  # noqa: E402


def test_total_return_takes_accumulated_nav_jump_as_the_return():
    idx = pd.bdate_range("2025-07-01", periods=130)
    ac = pd.Series(1.0, index=idx)
    ac.iloc[60:] = 1.05            # 累计净值跳 5%：这 5% 就是分红再投资后的收益，不能再加一次股息
    r, _ = dv.total_return({"ac": ac, "income": None})
    assert r.loc[idx[60]] == pytest.approx(0.05)
    assert r.loc[idx[61]] == pytest.approx(0.0)


def test_cash_daily_includes_weekend_income():
    # 周五收盘后到周一收盘，含周六周日两条万份收益
    income = pd.Series([1.0, 1.0, 1.0],
                       index=pd.to_datetime(["2026-01-03", "2026-01-04", "2026-01-05"]))
    sessions = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    r = dv.cash_daily(income, sessions)
    assert r.loc[pd.Timestamp("2026-01-05")] == pytest.approx((1 + 1e-4) ** 3 - 1, rel=1e-6)


def test_portfolio_charges_monthly_rebalance_cost():
    idx = pd.bdate_range("2026-01-01", periods=60)
    r1 = pd.Series(0.002, index=idx)      # 两腿同涨，权重不漂移 ⇒ 只在建仓日收费
    rx = pd.Series(-0.001, index=idx)     # 一涨一跌才会真的触发换手
    free = dv.portfolio(r1, rx, w1=0.5, cost=0.0)
    paid = dv.portfolio(r1, rx, w1=0.5)
    assert paid.iloc[-1] < free.iloc[-1]          # 每月再平衡的 6bp 必须真的扣掉


def test_dividends_from_ac_extracts_each_distribution():
    idx = pd.bdate_range("2025-01-01", periods=6)
    nav = pd.Series([1.00, 1.00, 1.00, 0.95, 0.95, 0.95], index=idx)
    ac = pd.Series([1.00, 1.00, 1.00, 1.00, 1.00, 1.00], index=idx)  # 除息日净值掉 0.05，累计净值不动
    d = dv.dividends_from_ac(nav, ac)
    assert d.loc[idx[3]] == pytest.approx(0.05)
    assert d.sum() == pytest.approx(0.05)


def test_dividends_from_ac_rejects_series_that_are_not_nav_plus_dividends():
    idx = pd.bdate_range("2025-01-01", periods=40)
    nav = pd.Series(3.4, index=idx)
    ac = pd.Series([3.3 + (0.02 if i % 2 else -0.02) for i in range(40)], index=idx)  # 来回跳，不是单调
    assert dv.dividends_from_ac(nav, ac) is None
