"""持有期限 / 退出 / 事件簇三个测量的纯函数钉子。

这些函数决定「事件怎么算、收益从哪天起算、k 分组按月还是按天」，
错一天就会让整张表偏向有利方向，所以用合成序列钉住，而不是靠肉眼看结果。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch_experiments as xp  # noqa: E402


def _status(rows: dict[str, list[str]], start="2026-01-05") -> pd.DataFrame:
    days = pd.bdate_range(start, periods=len(next(iter(rows.values()))))
    return pd.DataFrame(rows, index=days)


def test_event_is_first_day_of_run_and_cooldown_blocks_early_retrigger():
    # 第 0-2 日候选、第 3 日掉出、第 40 日再进（<60 ⇒ 被冷却吞掉）、第 70 日再进（>60 ⇒ 计一次）
    s = ["CANDIDATE"] * 3 + ["NEUTRAL"] * 37 + ["CANDIDATE"] + ["NEUTRAL"] * 29 + ["CANDIDATE"]
    st = _status({"电子": s})
    dd = pd.DataFrame(-0.40, index=st.index, columns=st.columns)
    ev = xp.build_events(st, dd)

    assert [str(d.date()) for d in ev["date"]] == ["2026-01-05", str(st.index[70].date())]
    assert ev["industry"].tolist() == ["电子", "电子"]
    assert ev["i"].tolist() == [0, 70]


def test_hold_return_is_entry_to_exit_close():
    px = pd.DataFrame(
        {"close_adj": [100, 110, 90, 120], "hi_adj": [100, 111, 91, 121], "lo_adj": [99, 109, 89, 119]},
        index=pd.bdate_range("2026-01-05", periods=4),
    )
    sessions = list(px.index)
    assert xp._hold_return(px, sessions, 0, 1) == pytest.approx(0.10)
    assert xp._hold_return(px, sessions, 0, 2) == pytest.approx(-0.10)


def test_day_k_buckets_by_same_day_members_and_counts_months():
    days = pd.bdate_range("2026-01-05", periods=60)
    nav = pd.DataFrame(
        {"A": np.linspace(1.0, 1.4, 60), "B": np.linspace(1.0, 1.1, 60), "C": np.linspace(1.0, 0.9, 60)},
        index=days,
    )
    st = pd.DataFrame("NEUTRAL", index=days, columns=["A", "B", "C"])
    st.iloc[5, 0] = "CANDIDATE"                 # 一个人
    st.iloc[30, [0, 1, 2]] = "CANDIDATE"        # 三个同时
    out = xp.exp_day_k(st, nav, list(days))

    assert out["days"] == 2
    assert out["by_k"]["k=1"]["n_days"] == 1
    assert out["by_k"]["k>=3"]["n_days"] == 1
    # 20 日收益从 t+1 收盘起算，两个分组都应为正（A 单调上行）
    assert out["by_k"]["k=1"]["h20"]["ret_bp"] > 0
    assert out["by_k"]["k>=3"]["h20"]["ret_bp"] > 0
    # 同一自然月内的两天只算 1 个月，不能把重叠日当独立观测
    assert out["by_k"]["k=1"]["n_months"] == 1
