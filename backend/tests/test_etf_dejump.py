"""份额折算的回归测试（`_dejump`）。

这类 bug 的特征是「不报错、只是数字错」，且错得刚好落在最有结论价值的那一档。
所以必须用两类测试锁住：合成的已知折算 + 真实标的上的量级下界。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "analysis"))

import _diag_industry_etf_asym as core  # noqa: E402


def _px(vals: list[float]) -> pd.Series:
    return pd.Series(vals, index=pd.bdate_range("2026-01-01", periods=len(vals)))


def test_dejump_removes_one_to_three_split():
    # 3.579 → 1.192 是 1:3 折算（−66.7%），经济收益为 0
    px = _px([3.4, 3.5, 3.579, 1.192, 1.232, 1.271])
    q = core._dejump(px)
    r = q.pct_change().dropna()
    assert r.abs().max() < 0.05, f"折算未被摊平，最大残跳 {r.abs().max():.3f}"
    # 末日必须仍是真实价（f_last = 1）
    assert q.iloc[-1] == pytest.approx(1.271)
    # 折算点两侧价格必须连续（3.579/3 ≈ 1.193）
    assert q.iloc[2] == pytest.approx(q.iloc[3], rel=1e-3)


def test_dejump_removes_share_merge():
    # 反向：份额合并导致单日 +170%
    px = _px([1.0, 1.05, 1.1, 2.97, 3.0])
    q = core._dejump(px)
    # 原始序列本身每天涨 5%，摊平后不应再有 +170% 的跳变
    assert q.pct_change().dropna().abs().max() < 0.06
    assert q.iloc[-1] == pytest.approx(3.0)


def test_dejump_leaves_normal_series_untouched():
    rng = np.random.default_rng(7)
    px = pd.Series(1.0 * np.cumprod(1 + rng.normal(0, 0.015, 300)),
                   index=pd.bdate_range("2025-01-01", periods=300))
    pd.testing.assert_series_equal(core._dejump(px), px.astype(float))


def test_dejump_keeps_20pct_limit_day():
    # 科创/创业板 ETF 单日 ±20% 是真实涨跌，不能被摊平
    px = _px([1.0, 1.0, 1.2, 1.2])       # +20%
    assert core._dejump(px).iloc[2] == pytest.approx(1.2)


def test_real_symbol_dd_is_not_inflated_by_split():
    """588170 在 2026-07-06 有 1:3 折算；未修复时 dd52 显示 −83.7%。"""
    f = ROOT / "data" / "backfill_20261117" / "etf_daily_full.parquet"
    if not f.exists():
        pytest.skip("无本地 ETF 日线")
    d = pd.read_parquet(f, columns=["symbol", "date", "close"])
    d["date"] = pd.to_datetime(d["date"])
    x = d[d.symbol == "588170.SH"].sort_values("date")
    if x.empty:
        pytest.skip("样本内无 588170")
    q = core._dejump(x.set_index("date")["close"])
    assert q.pct_change().abs().max() < core.SPLIT_THR
    dd = q.iloc[-1] / q.tail(250).max() - 1
    assert -0.70 < dd < -0.30, f"dd={dd:.3f}：折算仍被当成暴跌，或真实跌幅已超出合理范围"


def test_split_threshold_is_below_etf_limit():
    # ETF 涨跌幅上限 20%；阈值必须高于它，否则会把真实涨跌当折算
    assert 0.20 < core.SPLIT_THR <= 0.30
