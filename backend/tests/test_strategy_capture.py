"""策略自动捕获管道的单元测试。

只测可独立验证的纯函数 (信号提取 / 判分 / 落盘 / 列出), 不触发真实 AI 生成与回测。
"""
from __future__ import annotations

import json

import polars as pl

from app.config import settings
from app.services import strategy_capture as sc


def test_extract_signals_from_trades():
    """从回测 trades 提取 (entry_signal_date, symbol), 去重且日期截到 YYYY-MM-DD。"""
    trades = [
        {"symbol": "600000.SH", "entry_date": "2026-01-05", "entry_signal_date": "2026-01-05"},
        {"symbol": "600000.SH", "entry_date": "2026-01-05", "entry_signal_date": "2026-01-05"},  # 重复
        {"symbol": "000001.SZ", "entry_date": "2026-01-06", "entry_signal_date": None},  # 回退 entry_date
    ]
    df = sc._extract_signals_from_trades(trades)
    assert df.height == 2
    assert set(df["symbol"].to_list()) == {"600000.SH", "000001.SZ"}


def test_extract_signals_empty():
    df = sc._extract_signals_from_trades([])
    assert df.height == 0
    assert df.columns == ["date", "symbol"]


def test_eval_claim_rejects_incomplete(monkeypatch, tmp_path):
    """判分: 不完整的 Claim 必须被五道门 REJECT。"""
    verdict = sc._eval_claim({
        "name": "test",
        "universe": "all_a",
        "entry": "open_t1",
        "exit": "close_t2",
        # 缺 unfillable_share / capacity 等 ⇒ REJECT
    })
    assert verdict["decision"] != "PASS"
    assert "unfillable_share_missing" in verdict["reasons"]


def test_eval_claim_passes_complete():
    """判分: 口径齐全的正对照必须 PASS (防止恒拒绝)。"""
    verdict = sc._eval_claim({
        "name": "synthetic",
        "universe": "all_a",
        "entry": "open_t1",
        "exit": "close_t2",
        "holding_days": 1,
        "entry_unfillable_share": 0.0,
        "agg_unit": "per_day",
        "per_day_t": 9.3,
        "capacity_annual_pct": {10: 18.0, 100: 17.5},
        "effect_bp": 245.2,
        "t_stat": 9.3,
        "n_obs": 300,
        "n_days": 30,
        "benchmark": "matched",
        "benchmark_dims": frozenset({"board", "pct"}),
        "event_type": "turnover_shock",
        "matched_effect_bp": 220.0,
        "matched_t": 8.5,
        "mde_bp": 52.7,
        "mde_universe": "all_a",
        "n_tests": 200,
        "cost_bp_roundtrip": 20.0,
        "trigger_rate_per_stock_day": 300 / 8000,
        "hits_per_stock_year": 9.075,
        "annual_gross_pct": 22.0,
        "annual_net_pct": 20.1,
    })
    assert verdict["decision"] == "PASS"


def test_capture_record_persistence(monkeypatch, tmp_path):
    """落盘: meta/source 写入后 list_captures 能列出。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    uid = "test12345678"
    rec = sc.CaptureRecord(
        uuid=uid, status="failed", source_text="打板要打龙头",
        error="AI 生成失败", created_at="2026-11-15T00:00:00",
        finished_at="2026-11-15T00:00:01",
    )
    sc._write_meta(tmp_path, rec)
    sc._write_source(tmp_path, uid, "打板要打龙头")

    items = sc.list_captures(tmp_path)
    assert len(items) == 1
    assert items[0]["uuid"] == uid
    assert items[0]["status"] == "failed"
    # source.txt 确实落盘
    assert (sc.capture_dir(tmp_path) / uid / "source.txt").read_text(encoding="utf-8") == "打板要打龙头"


def test_default_prompt_builder_wraps_community_text():
    prompt = sc._default_prompt_builder("首板接力：昨日首板今日竞价高开买入")
    assert "首板接力" in prompt
    assert "策略方向" in prompt
    # 必须硬约束日线策略(防止 AI 生成分钟策略导致回测 ColumnNotFoundError)
    assert "日线" in prompt
    assert '["1d"]' in prompt
    assert "minute_filter" in prompt


def test_default_prompt_builder_downgrades_intraday():
    """「开盘后 N 分钟」等分钟级描述必须被降级为日线近似。"""
    prompt = sc._default_prompt_builder("开盘后 5 分钟放量买入")
    assert "开盘后 N 分钟" in prompt or "分钟级描述" in prompt
    assert "日线近似" in prompt
