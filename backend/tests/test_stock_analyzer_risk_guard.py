"""AI 四维分析的退市约束 —— 价位成立的前提必须写进提示词。

实测动机（600340.SH / *ST华幸，2026-11-17）：
  收盘 0.92 元跌破面值，2025 年报净资产 -48.31 亿元。旧版提示词只喂
  K 线 + 关键价位 + 两期财务表，模型会照着「支撑位—压力位」的模板写
  「下方 0.88 元为前期密集区」，而 0.88 元对面值退市倒计时的票没有意义。

本文件守三条：
  1. 有红线时，提示词里必须有具体规则与剩余交易日（不是「注意风险」这种套话）；
  2. 没有红线时，一个字都不注入（不给普通票增加噪声）；
  3. 三条硬约束必须同时在场，尤其「不得给概率」—— 本地没有退市样本。
"""
from __future__ import annotations

from app.services.delisting_guard import Gate, RiskGuard
from app.services.stock_analyzer import _build_user_prompt, _risk_guard_part


def _st_guard() -> RiskGuard:
    return RiskGuard(
        symbol="600340.SH", name="*ST华幸", board="主板",
        is_st=True, st_kind="*ST", as_of="2026-11-17",
        gates=[
            Gate(id="par", label="面值退市", rule="连续 20 个交易日收盘价均低于 1.00 元",
                 status="counting", value=0.92, threshold=1.0, distance_pct=-8.0,
                 streak=1, streak_limit=20, detail="已连续 1/20 个交易日低于面值"),
            Gate(id="equity", label="净资产为负", rule="最近一个会计年度经审计期末净资产为负",
                 status="triggered", value=-4.831e9, threshold=0.0,
                 streak=1, detail="2025-12-31 净资产 -48.31 亿元，已触发退市风险警示"),
        ],
        counts={"equity_trend": "2026-09-30 净资产 -127.80 亿元"},
        caveats=["本面板只算规则距离，不给摘帽/退市概率"],
    )


def _calm_guard() -> RiskGuard:
    return RiskGuard(
        symbol="000001.SZ", name="平安银行", board="主板",
        is_st=False, st_kind="", as_of="2026-11-17",
        gates=[Gate(id="par", label="面值退市", rule="…", status="safe", value=11.8)],
        caveats=[],
    )


def test_st_prompt_carries_remaining_days_and_rules() -> None:
    part = _risk_guard_part(_st_guard())
    assert "1/20" in part
    assert "1.00 元" in part
    assert "-48.31 亿元" in part
    assert "净资产趋势" in part


def test_st_prompt_forbids_fake_probability() -> None:
    part = _risk_guard_part(_st_guard())
    assert "不得给出摘帽 / 不退市的概率判断" in part
    assert "不得输出与该前提矛盾" in part


def test_calm_stock_gets_no_risk_section() -> None:
    assert _risk_guard_part(_calm_guard()) == ""
    assert _risk_guard_part(None) == ""


def test_user_prompt_injects_risk_section() -> None:
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-11-17", "close": 0.92}],
        fins={}, levels={"sr": [], "pivot": []}, close=0.92,
        symbol="600340.SH", focus="", risk_guard=_st_guard(),
    )
    assert "强制退市红线判定" in prompt
    assert "已连续 1/20 个交易日低于面值" in prompt


def test_user_prompt_clean_without_guard() -> None:
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-11-17", "close": 11.8}],
        fins={}, levels={"sr": [], "pivot": []}, close=11.8,
        symbol="000001.SZ", focus="",
    )
    assert "强制退市红线" not in prompt
