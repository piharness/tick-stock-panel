"""第二条腿筛查的统计口径：年化、回撤、最差 20 日的符号。

这三项决定「谁过谁不过」，错一个符号就会把宽基 β 判成互补腿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch_leg2_screen as sc  # noqa: E402


def test_stats_annualizes_and_signs_drawdown():
    idx = pd.bdate_range("2024-01-01", periods=252)
    up = pd.Series(0.001, index=idx)
    s = sc.stats(up)
    assert 0.28 < s["ann"] < 0.32     # 252 个交易日略短于一年，年化略高于 1.001^252-1
    assert s["mdd"] == 0.0
    assert s["worst20"] > 0

    crash = pd.Series(0.001, index=idx)
    crash.iloc[100:110] = -0.01          # 十天各跌 1%
    s2 = sc.stats(crash)
    assert s2["mdd"] == pytest.approx(0.99 ** 10 - 1, abs=1e-4)
    assert s2["mdd"] < 0


def test_worst20_is_a_return_not_a_level():
    idx = pd.bdate_range("2024-01-01", periods=60)
    r = pd.Series(0.0, index=idx)
    r.iloc[30:50] = -0.005
    s = sc.stats(r)
    assert s["worst20"] == pytest.approx(0.995 ** 20 - 1, abs=1e-4)
