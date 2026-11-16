from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.factor import FactorBatchItem
from app.services.alpha_search import (
    AlphaSearchConfig,
    classify_candidate,
    deflated_sharpe_for_trials,
    evaluate_confirmation,
    evaluate_discovery,
    generate_candidates,
)


def _item(**overrides) -> FactorBatchItem:
    values = {
        "factor_name": "candidate",
        "label": "candidate",
        "group": "test",
        "ic_mean": 0.03,
        "ir": 0.40,
        "t_newey_west": 2.80,
        "q_value": 0.05,
        "coverage": 0.95,
        "turnover": 0.40,
        "long_short_return": 0.12,
        "long_short_sharpe": 1.20,
        "n_dates": 120,
        "n_symbols": 500,
    }
    values.update(overrides)
    return FactorBatchItem(**values)


def test_generate_candidates_is_bounded_unique_and_compiles():
    formulas = generate_candidates(30)

    assert len(formulas) == 30
    assert len({item.canonical for item in formulas}) == 30
    assert len({item.template for item in formulas}) >= 3
    assert all(item.formula for item in formulas)
    assert all(item.warmup_bars >= 1 for item in formulas)


def test_generate_candidates_keeps_virtual_reference_in_dependencies():
    formulas = generate_candidates(360)
    by_formula = {item.formula: item for item in formulas}

    item = by_formula["rank(ts_delta(macd_hist_pct, 5))"]
    assert "macd_hist_pct" in item.dependencies
    assert "macd_hist" in item.dependencies


def test_discovery_gate_rejects_high_q_and_high_turnover():
    config = AlphaSearchConfig()
    high_q = evaluate_discovery(_item(q_value=0.30), config)
    high_turnover = evaluate_discovery(_item(turnover=0.99), config)

    assert not high_q.passed
    assert any("q=" in reason for reason in high_q.reasons)
    assert not high_turnover.passed
    assert any("换手率" in reason for reason in high_turnover.reasons)


def test_discovery_gate_passes_a_clean_candidate():
    result = evaluate_discovery(_item(), AlphaSearchConfig())

    assert result.passed
    assert result.reasons == []


def test_discovery_gate_requires_q_outside_exploratory():
    item = _item(q_value=None)
    assert not evaluate_discovery(item, AlphaSearchConfig(profile="balanced")).passed
    assert evaluate_discovery(item, AlphaSearchConfig(profile="exploratory")).passed


def test_confirmation_gate_rejects_sign_flip():
    result = evaluate_confirmation(
        _item(ic_mean=0.03, t_newey_west=2.0),
        discovery_direction="low",
        config=AlphaSearchConfig(),
    )

    assert not result.passed
    assert any("方向反转" in reason for reason in result.reasons)


def test_confirmation_gate_accepts_same_direction_with_weaker_magnitude():
    result = evaluate_confirmation(
        _item(ic_mean=-0.015, t_newey_west=1.8),
        discovery_direction="low",
        config=AlphaSearchConfig(),
    )

    assert result.passed


def test_deflated_sharpe_penalizes_more_trials():
    irs = [-0.30, -0.18, -0.05, 0.08, 0.20, 0.31]
    small, small_em = deflated_sharpe_for_trials(irs, candidate_ir=0.50, n_obs=120)
    large, large_em = deflated_sharpe_for_trials(irs * 4, candidate_ir=0.50, n_obs=120)

    assert small is not None and large is not None
    assert small_em is not None and large_em is not None
    assert large_em > small_em
    assert large < small


def test_deflated_sharpe_returns_none_without_enough_trials():
    assert deflated_sharpe_for_trials([0.1], candidate_ir=0.2, n_obs=100) == (None, None)
    assert deflated_sharpe_for_trials([0.1, 0.2], candidate_ir=None, n_obs=100) == (None, None)


def test_classify_candidate_confirms_on_replicated_confirmation_even_with_low_dsr():
    status, reason = classify_candidate(
        discovery_pass=True,
        discovery_reason=None,
        confirmation_pass=True,
        confirmation_reason=None,
        confirmation_q=0.005,
        novelty_pass=True,
        novelty_reason=None,
        dsr=0.20,
        config=AlphaSearchConfig(),
    )

    assert status == "confirmed"
    assert "DSR=0.20" in reason


def test_classify_candidate_keeps_weak_confirmation_provisional():
    status, reason = classify_candidate(
        discovery_pass=True,
        discovery_reason=None,
        confirmation_pass=True,
        confirmation_reason=None,
        confirmation_q=0.05,
        novelty_pass=True,
        novelty_reason=None,
        dsr=0.99,
        config=AlphaSearchConfig(),
    )

    assert status == "provisional"
    assert "0.05" in reason


def test_classify_candidate_rejects_redundant_candidate():
    status, reason = classify_candidate(
        discovery_pass=True,
        discovery_reason=None,
        confirmation_pass=True,
        confirmation_reason=None,
        confirmation_q=0.001,
        novelty_pass=False,
        novelty_reason="与 momentum_20d 的秩相关 +0.91 超过阈值",
        dsr=0.99,
        config=AlphaSearchConfig(),
    )

    assert status == "rejected"
    assert "冗余" in reason


def test_classify_candidate_rejects_when_discovery_failed():
    status, reason = classify_candidate(
        discovery_pass=False,
        discovery_reason="q=0.30 > 0.10",
        confirmation_pass=True,
        confirmation_reason=None,
        confirmation_q=0.001,
        novelty_pass=True,
        novelty_reason=None,
        dsr=0.99,
        config=AlphaSearchConfig(),
    )

    assert status == "rejected"
    assert "q=0.30" in reason


def test_config_rejects_overlapping_windows():
    with pytest.raises(ValueError, match="discovery window must end before confirmation"):
        AlphaSearchConfig(
            discovery_end=date(2026, 5, 1),
            confirmation_start=date(2026, 5, 1),
        ).validate()


def test_config_rejects_too_short_window():
    with pytest.raises(ValueError, match="at least 60 days"):
        AlphaSearchConfig(
            discovery_start=date(2026, 1, 1),
            discovery_end=date(2026, 1, 20),
            confirmation_start=date(2026, 2, 1),
            confirmation_end=date(2026, 5, 1),
        ).validate()


class _FakeEngine:
    """最小引擎: 只实现 FactorBacktestService 需要的 load_panel。"""

    def __init__(self, panel: pl.DataFrame) -> None:
        self.panel = panel

    def load_panel(self, symbols, start, end, columns=None, asset_type="stock"):
        frame = self.panel.filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
        )
        if symbols:
            frame = frame.filter(pl.col("symbol").is_in(symbols))
        if columns:
            keep = [column for column in columns if column in frame.columns]
            frame = frame.select(keep)
        return frame


def _synthetic_panel() -> pl.DataFrame:
    rows = []
    start = date(2025, 9, 1)
    for day in range(270):
        current = start + timedelta(days=day)
        for index, symbol in enumerate(("000001.SZ", "000002.SZ", "600000.SH")):
            trend = 0.01 * (index + 1) * day
            wave = 0.03 * ((day + index) % 7)
            close = 10.0 + trend + wave
            rows.append({
                "symbol": symbol,
                "date": current,
                "open": close * 0.99,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1000.0 + 100.0 * index + day,
                "amount": 1e6 + 1e4 * day,
                "turnover_rate": 0.01 + 0.001 * index + 0.0001 * day,
                "momentum_20d": 0.02 * (index + 1) + 0.001 * day,
            })
    return pl.DataFrame(rows)


def test_run_alpha_search_writes_artifacts_and_skips_prior_trials(tmp_path):
    from app.services.alpha_search import run_alpha_search

    config = AlphaSearchConfig(
        discovery_start=date(2026, 1, 1),
        discovery_end=date(2026, 3, 31),
        confirmation_start=date(2026, 4, 1),
        confirmation_end=date(2026, 6, 5),
        max_formulas=3,
    )
    config.validate()
    formulas = generate_candidates(3)
    engine = _FakeEngine(_synthetic_panel())

    first = run_alpha_search(engine, config, output_dir=tmp_path, formulas=formulas)
    assert first["counts"]["generated"] == 3
    assert first["counts"]["evaluated"] == 3
    assert first["family_hash"]
    assert (tmp_path / "runs" / first["run_id"] / "preregistration.json").is_file()
    assert (tmp_path / "runs" / first["run_id"] / "trials.parquet").is_file()
    assert (tmp_path / "runs" / first["run_id"] / "report.md").is_file()
    trials = pl.read_parquet(tmp_path / "runs" / first["run_id"] / "trials.parquet")
    assert trials.height == 3
    assert set(trials["candidate_id"]) == {item.candidate_id for item in formulas}

    second = run_alpha_search(engine, config, output_dir=tmp_path, formulas=formulas)
    assert second["status"] == "no_new_hypotheses"
    assert second["counts"]["skipped_prior"] == 3
    assert second["counts"]["evaluated"] == 0
    assert second["artifacts"]["trials"] is None
