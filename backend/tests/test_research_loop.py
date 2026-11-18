from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.backtest.guardian import GuardianThresholds
from app.services.research_loop import (
    PAGE_POLICIES,
    FreshnessPolicy,
    admission_verdict,
    age_days,
    classify,
    freshness_entry,
    latest_alpha_run,
    parse_day,
)


def _profile(
    *,
    status: str = "healthy",
    baseline_kind: str = "mining_gate",
    checks: list[dict] | None = None,
) -> dict:
    return {
        "strategy_id": "mined_factor_x",
        "status": status,
        "baseline": {"kind": baseline_kind, "data_as_of": "2026-06-01"},
        "checks": checks if checks is not None else [],
    }


def _check(*, degradation: float | None = 0.05, error: str | None = None) -> dict:
    value = {
        "checked_at": "2026-11-17T08:00:00+00:00",
        "verdict": "healthy",
        "degradation_pct": degradation,
    }
    if error:
        value["error"] = error
    return value


# ── 日期解析与计龄 ────────────────────────────────────────────────────


def test_parse_day_accepts_iso_variants_and_rejects_noise():
    assert parse_day("2026-11-18") == date(2026, 11, 18)
    assert parse_day("2026-11-18T07:30:00Z") == date(2026, 11, 18)
    assert parse_day("2026-11-18T23:30:00+08:00") == date(2026, 11, 18)
    assert parse_day(datetime(2026, 11, 18, 12)) == date(2026, 11, 18)
    assert parse_day("not-a-date") is None
    assert parse_day("") is None
    assert parse_day(None) is None


def test_age_days_never_goes_negative():
    as_of = date(2026, 11, 18)
    assert age_days("2026-11-18", as_of) == 0
    assert age_days("2026-11-11", as_of) == 7
    # 时钟偏差(档案写在未来) 不能变成「-3 天」骗过新鲜度判定
    assert age_days("2026-11-21", as_of) == 0
    assert age_days(None, as_of) is None


def test_classify_boundaries_are_inclusive_on_fresh_side():
    policy = FreshnessPolicy(fresh_days=7, stale_days=21)
    assert classify(None, policy) == "never"
    assert classify(0, policy) == "fresh"
    assert classify(7, policy) == "fresh"
    assert classify(8, policy) == "aging"
    assert classify(21, policy) == "aging"
    assert classify(22, policy) == "stale"


def test_freshness_policy_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        FreshnessPolicy(fresh_days=10, stale_days=10)
    with pytest.raises(ValueError):
        FreshnessPolicy(fresh_days=-1, stale_days=5)


def test_freshness_entry_carries_its_own_caliber():
    entry = freshness_entry(
        page="factor",
        last_at="2026-11-04T10:00:00",
        as_of=date(2026, 11, 18),
        detail={"alpha_run": None},
    )
    assert entry["page"] == "factor"
    assert entry["label"] == "因子"
    assert entry["age_days"] == 14
    assert entry["state"] == "aging"
    # 门槛随响应一起下发: 前端不自己写一份阈值
    assert entry["fresh_days"] == PAGE_POLICIES["factor"].fresh_days
    assert entry["stale_days"] == PAGE_POLICIES["factor"].stale_days


# ── 准入闸门 (fail-closed) ───────────────────────────────────────────


def test_admission_blocks_when_not_adopted():
    result = admission_verdict(None)
    assert result["verdict"] == "blocked"
    assert result["stage"] == "unadopted"


def test_admission_blocks_pending_watch_and_retired():
    for status in ("pending", "watch", "retired"):
        result = admission_verdict(_profile(status=status, checks=[_check()]))
        assert result["verdict"] == "blocked", status
        assert result["stage"] == "guardian_status", status


def test_admission_rejects_trailing_reference_baseline():
    """healthy + 尾随参考基线仍然拒绝: 那不是样本外证据。"""
    result = admission_verdict(
        _profile(baseline_kind="trailing_reference", checks=[_check(degradation=0.0)])
    )
    assert result["verdict"] == "blocked"
    assert result["stage"] == "baseline_not_oos"


def test_admission_requires_a_check_record():
    result = admission_verdict(_profile(checks=[]))
    assert result["verdict"] == "blocked"
    assert result["stage"] == "no_check"


def test_admission_blocks_failed_check():
    result = admission_verdict(_profile(checks=[_check(error="未产生买入信号")]))
    assert result["verdict"] == "blocked"
    assert result["stage"] == "no_check"
    assert "未产生买入信号" in result["reasons"][0]


def test_admission_blocks_degradation_beyond_watch_threshold():
    thresholds = GuardianThresholds(watch_pct=0.20)
    result = admission_verdict(_profile(checks=[_check(degradation=0.31)]), thresholds=thresholds)
    assert result["verdict"] == "blocked"
    assert result["stage"] == "degraded"
    # 退化度原样带出, 供 UI 显示「差多少」而不是黑白结论
    assert result["degradation_pct"] == pytest.approx(0.31)


def test_admission_passes_healthy_oos_with_small_degradation():
    thresholds = GuardianThresholds(watch_pct=0.20)
    result = admission_verdict(_profile(checks=[_check(degradation=0.20)]), thresholds=thresholds)
    assert result["verdict"] == "admitted"
    assert result["stage"] == "admitted"
    assert result["last_verdict"] == "healthy"


def test_admission_tolerates_missing_degradation_value():
    """退化度算不出来 (基线<=0 时返回 None) 不构成拒绝理由 —— 状态已判过。"""
    result = admission_verdict(_profile(checks=[_check(degradation=None)]))
    assert result["verdict"] == "admitted"


def test_admission_survives_malformed_checks_payload():
    profile = _profile()
    profile["checks"] = "not-a-list"
    result = admission_verdict(profile)
    assert result["verdict"] == "blocked"
    assert result["stage"] == "no_check"


# ── 主动搜索产物读取 ─────────────────────────────────────────────────


def _write_run(root: Path, run_id: str, payload: dict, *, name: str = "report.json") -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return run_dir


def test_latest_alpha_run_reads_newest_directory(tmp_path: Path):
    root = tmp_path / "research" / "alpha_search"
    _write_run(root, "20261101-100000-aaaaaa", {"run_id": "old", "created_at": "2026-11-01"})
    _write_run(
        root,
        "20261109-124555-d71d9a",
        {
            "run_id": "new",
            "status": "ok",
            "created_at": "2026-11-09T12:45:55",
            "counts": {"confirmed": 16, "confirmed_themes": 11, "provisional": 13},
        },
    )
    result = latest_alpha_run(tmp_path)
    assert result is not None
    assert result["run_id"] == "new"
    assert result["confirmed"] == 16
    assert result["confirmed_themes"] == 11


def test_latest_alpha_run_skips_unreadable_and_returns_none_when_absent(tmp_path: Path):
    assert latest_alpha_run(tmp_path) is None
    root = tmp_path / "research" / "alpha_search"
    _write_run(root, "20261109-124555-d71d9a", {}, name="broken.json")
    assert latest_alpha_run(tmp_path) is None


def test_policies_cover_every_page():
    from app.services.research_loop import PAGE_KEYS, PAGE_LABELS, PAGE_SUBJECTS

    for page in PAGE_KEYS:
        assert page in PAGE_LABELS
        assert page in PAGE_SUBJECTS
        assert page in PAGE_POLICIES


def test_backtest_page_is_the_strictest_cadence():
    """守护是日度节奏, 门槛必须比周度研究更紧 —— 否则页面坏了五天还显示 fresh。"""
    assert PAGE_POLICIES["backtest"].fresh_days < PAGE_POLICIES["factor"].fresh_days
    assert PAGE_POLICIES["backtest"].stale_days < PAGE_POLICIES["factor"].stale_days


def test_age_days_uses_calendar_days_not_weekdays():
    """口径断言: age_days 是自然日。跨周末只算差值, 不排除非交易日。"""
    as_of = date(2026, 11, 18)  # 周三
    friday = as_of - timedelta(days=5)
    assert age_days(friday, as_of) == 5


# ── 聚合: 三页新鲜度 / 准入 / 联动 (用假依赖跑真代码路径) ─────────────


class _FakeRepo:
    def __init__(self, day: date) -> None:
        self._day = day

    def latest_enriched_date(self, asset_type: str) -> date:
        return self._day


class _FakeRunStore:
    def __init__(self, runs: list[dict]) -> None:
        self._runs = runs

    def list_runs(self, limit: int = 5) -> list[dict]:
        return self._runs[:limit]


class _FakeManager:
    def __init__(self, runs: list[dict]) -> None:
        self.store = _FakeRunStore(runs)


class _FakeState:
    def __init__(self, runs: list[dict] | None = None) -> None:
        self.mining_manager = _FakeManager(runs or [])


def _alpha_root(tmp_path: Path) -> Path:
    root = tmp_path / "research" / "alpha_search"
    _write_run(
        root,
        "20261109-124555-d71d9a",
        {
            "run_id": "fresh-alpha",
            "status": "ok",
            "created_at": "2026-11-16T08:00:00",
            "counts": {"confirmed": 16, "confirmed_themes": 11, "provisional": 13, "generated": 360},
        },
    )
    return root


def test_build_loop_status_reports_three_pages_without_side_effects(tmp_path: Path):
    from app.services.research_loop import build_loop_status

    _alpha_root(tmp_path)
    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 11, 18)), _FakeState())

    assert state["as_of"] == "2026-11-18"
    pages = {item["page"]: item for item in state["freshness"]}
    assert set(pages) == {"strategy", "factor", "backtest"}
    assert pages["factor"]["state"] == "fresh"
    assert pages["factor"]["last_at"] == "2026-11-16T08:00:00"
    assert pages["factor"]["detail"]["alpha_run"]["confirmed"] == 16
    # 没有候选/守护记录 = never, 不是 fresh: 「没数据」不能显示成「刚更新」
    assert pages["strategy"]["state"] == "never"
    assert pages["backtest"]["state"] == "never"
    assert state["admission"]["total"] == 0
    assert state["linkage"]["paper"]["accounts"] == 0
    assert any("还没有任何" in action["text"] for action in state["next_actions"])
    # 只读契约: 不该为「看一眼状态」建出守护/模拟盘目录
    assert not (tmp_path / "user_data" / "strategy_guardian").exists()
    assert not (tmp_path / "paper").exists()


def test_build_loop_status_picks_the_newer_research_source(tmp_path: Path):
    from app.services.research_loop import build_loop_status

    _alpha_root(tmp_path)  # 主动搜索: 11-16
    state = build_loop_status(
        tmp_path,
        _FakeRepo(date(2026, 11, 18)),
        _FakeState([{"run_id": "weekly", "status": "succeeded",
                     "created_at": "2026-11-10T22:00:00", "finished_at": "2026-11-17T01:00:00"}]),
    )
    factor = next(item for item in state["freshness"] if item["page"] == "factor")
    # 挖掘在 11-17 完成, 比主动搜索更新 ⇒ 取挖掘
    assert factor["last_at"] == "2026-11-17T01:00:00"
    assert factor["age_days"] == 1


def test_build_loop_status_admits_healthy_oos_strategy(tmp_path: Path):
    from app.services.guardian_store import GuardianStore
    from app.services.research_loop import build_loop_status

    store = GuardianStore(tmp_path)
    store.create_baseline(
        "mined_factor_ok",
        kind="mining_gate",
        data_as_of="2026-06-01",
        objective="sharpe",
        metrics={"sharpe": 1.2, "n_trades": 120},
    )
    store.append_check(
        "mined_factor_ok",
        {"checked_at": "2026-11-17T08:00:00+00:00", "verdict": "healthy",
         "degradation_pct": 0.04, "n_trades_total": 80},
    )
    store.set_status("mined_factor_ok", "healthy")

    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 11, 18)), _FakeState())
    item = next(i for i in state["admission"]["items"] if i["strategy_id"] == "mined_factor_ok")
    assert item["verdict"] == "admitted"
    # 准入 ≠ 已联动: 账本和模拟仓都还没接上, 必须显式显示为未联动
    assert item["in_ledger"] is False
    assert item["in_paper"] is False
    assert state["admission"]["admitted"] == 1
    backtest = next(i for i in state["freshness"] if i["page"] == "backtest")
    assert backtest["last_at"] == "2026-11-17T08:00:00+00:00"
    assert backtest["state"] == "fresh"


# ── 聚合: 三页新鲜度 / 准入 / 联动 (用假依赖跑真代码路径) ─────────────


class _FakeRepo:
    def __init__(self, day: date) -> None:
        self._day = day

    def latest_enriched_date(self, asset_type: str) -> date:
        return self._day


class _FakeRunStore:
    def __init__(self, runs: list[dict]) -> None:
        self._runs = runs

    def list_runs(self, limit: int = 5) -> list[dict]:
        return self._runs[:limit]


class _FakeManager:
    def __init__(self, runs: list[dict]) -> None:
        self.store = _FakeRunStore(runs)


class _FakeState:
    def __init__(self, runs: list[dict] | None = None) -> None:
        self.mining_manager = _FakeManager(runs or [])


def _alpha_root(tmp_path: Path) -> Path:
    root = tmp_path / "research" / "alpha_search"
    _write_run(
        root,
        "20261109-124555-d71d9a",
        {
            "run_id": "fresh-alpha",
            "status": "ok",
            "created_at": "2026-11-16T08:00:00",
            "counts": {"confirmed": 16, "confirmed_themes": 11, "provisional": 13, "generated": 360},
        },
    )
    return root


def test_build_loop_status_reports_three_pages_without_side_effects(tmp_path: Path):
    from app.services.research_loop import build_loop_status

    _alpha_root(tmp_path)
    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 11, 18)), _FakeState())

    assert state["as_of"] == "2026-11-18"
    pages = {item["page"]: item for item in state["freshness"]}
    assert set(pages) == {"strategy", "factor", "backtest"}
    assert pages["factor"]["state"] == "fresh"
    assert pages["factor"]["last_at"] == "2026-11-16T08:00:00"
    assert pages["factor"]["detail"]["alpha_run"]["confirmed"] == 16
    # 没有候选/守护记录 = never, 不是 fresh: 「没数据」不能显示成「刚更新」
    assert pages["strategy"]["state"] == "never"
    assert pages["backtest"]["state"] == "never"
    assert state["admission"]["total"] == 0
    assert state["linkage"]["paper"]["accounts"] == 0
    assert any("还没有任何" in action["text"] for action in state["next_actions"])
    # 只读契约: 不该为「看一眼状态」建出守护/模拟盘目录
    assert not (tmp_path / "user_data" / "strategy_guardian").exists()
    assert not (tmp_path / "paper").exists()


def test_build_loop_status_picks_the_newer_research_source(tmp_path: Path):
    from app.services.research_loop import build_loop_status

    _alpha_root(tmp_path)  # 主动搜索: 11-16
    state = build_loop_status(
        tmp_path,
        _FakeRepo(date(2026, 11, 18)),
        _FakeState([{"run_id": "weekly", "status": "succeeded",
                     "created_at": "2026-11-10T22:00:00", "finished_at": "2026-11-17T01:00:00"}]),
    )
    factor = next(item for item in state["freshness"] if item["page"] == "factor")
    # 挖掘在 11-17 完成, 比主动搜索更新 ⇒ 取挖掘
    assert factor["last_at"] == "2026-11-17T01:00:00"
    assert factor["age_days"] == 1


def test_build_loop_status_admits_healthy_oos_strategy(tmp_path: Path):
    from app.services.guardian_store import GuardianStore
    from app.services.research_loop import build_loop_status

    store = GuardianStore(tmp_path)
    store.create_baseline(
        "mined_factor_ok",
        kind="mining_gate",
        data_as_of="2026-06-01",
        objective="sharpe",
        metrics={"sharpe": 1.2, "n_trades": 120},
    )
    store.append_check(
        "mined_factor_ok",
        {"checked_at": "2026-11-17T08:00:00+00:00", "verdict": "healthy",
         "degradation_pct": 0.04, "n_trades_total": 80},
    )
    store.set_status("mined_factor_ok", "healthy")

    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 11, 18)), _FakeState())
    item = next(i for i in state["admission"]["items"] if i["strategy_id"] == "mined_factor_ok")
    assert item["verdict"] == "admitted"
    # 准入 ≠ 已联动: 账本和模拟仓都还没接上, 必须显式显示为未联动
    assert item["in_ledger"] is False
    assert item["in_paper"] is False
    assert state["admission"]["admitted"] == 1
    backtest = next(i for i in state["freshness"] if i["page"] == "backtest")
    assert backtest["last_at"] == "2026-11-17T08:00:00+00:00"
    assert backtest["state"] == "fresh"


def test_latest_alpha_run_surfaces_dsr_verdict(tmp_path: Path):
    """confirmed 数 ≠ 结论。主题按 DSR 过门槛计数, 全不过时 dsr_pass=0。"""
    root = tmp_path / "research" / "alpha_search"
    _write_run(
        root,
        "20261109-124555-d71d9a",
        {
            "run_id": "run",
            "status": "ok",
            "created_at": "2026-11-09T12:45:55",
            "config": {"min_dsr": 0.90},
            "counts": {"confirmed": 16, "confirmed_themes": 2, "generated": 360},
            "clusters": [
                {"candidate_id": "a", "formula": "rank(ts_corr(x, y, 5))", "direction": "low",
                 "confirmation_ic": -0.039, "dsr": 0.95},
                {"candidate_id": "b", "formula": "rank(zscore(momentum_20d))", "direction": "high",
                 "confirmation_ic": 0.021, "dsr": 0.20},
            ],
        },
    )
    result = latest_alpha_run(tmp_path)
    assert result is not None
    assert result["confirmed"] == 16
    assert result["confirmed_themes"] == 2
    assert result["dsr_pass"] == 1
    assert result["min_dsr"] == 0.90
    assert [row["candidate_id"] for row in result["top_themes"]] == ["a", "b"]


def test_latest_alpha_run_reports_zero_when_nothing_clears_dsr(tmp_path: Path):
    root = tmp_path / "research" / "alpha_search"
    _write_run(
        root,
        "20261109-124555-d71d9a",
        {
            "run_id": "run",
            "config": {"min_dsr": 0.90},
            "counts": {"confirmed": 16, "confirmed_themes": 1},
            "clusters": [{"candidate_id": "a", "formula": "f", "dsr": 0.21}],
        },
    )
    result = latest_alpha_run(tmp_path)
    assert result is not None
    assert result["dsr_pass"] == 0
    assert result["top_themes"][0]["dsr"] == 0.21


def test_latest_alpha_run_defaults_threshold_and_tolerates_missing_fields(tmp_path: Path):
    """老 run 没有 config/clusters 时不能炸: 门槛退回 0.90, 主题为空。"""
    root = tmp_path / "research" / "alpha_search"
    _write_run(root, "20260101-000000-aaaaaa", {"run_id": "legacy", "counts": {"confirmed": 2}})
    result = latest_alpha_run(tmp_path)
    assert result is not None
    assert result["min_dsr"] == 0.90
    assert result["dsr_pass"] == 0
    assert result["top_themes"] == []


def test_lab_report_endpoint_carries_loop_block(tmp_path: Path, monkeypatch):
    """接线断言: 战报必须真的带上 loop 块 (否则前端状态条永远显示「不可用」)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace

    from app.api.lab import router as lab_router
    from app.services import preferences

    monkeypatch.setattr(
        preferences, "get_mining_schedule",
        lambda: {"mining_schedule_enabled": False, "mining_schedule_weekday": 4,
                 "mining_budget_profile": "balanced"},
    )
    monkeypatch.setattr(
        preferences, "get_guardian_schedule",
        lambda: {"guardian_schedule_enabled": False, "guardian_watch_pct": 0.2,
                 "guardian_retire_pct": 0.5, "guardian_k_max": 4,
                 "lab_mined_slot_limit": 10},
    )
    monkeypatch.setattr(
        preferences, "get_decision_auto_config",
        lambda: {"enabled": False, "execute": False, "strategy_id": "",
                 "lu_rebound_enabled": False},
    )

    class _Repo:
        def __init__(self, data_dir: Path) -> None:
            self.store = SimpleNamespace(data_dir=data_dir)

        @staticmethod
        def latest_enriched_date(asset_type: str = "stock") -> date:
            return date(2026, 11, 18)

    class _Engine:
        @staticmethod
        def list_strategies() -> list[dict]:
            return []

    _alpha_root(tmp_path)
    app = FastAPI()
    app.include_router(lab_router)
    app.state.repo = _Repo(tmp_path)
    app.state.strategy_engine = _Engine()
    app.state.mining_manager = SimpleNamespace(store=_FakeRunStore([]))
    app.state.guardian_manager = SimpleNamespace(store=_FakeRunStore([]))

    body = TestClient(app).get("/api/backtest/lab/report").json()
    loop = body["loop"]
    assert loop["available"] is True, loop
    assert loop["as_of"] == "2026-11-18"
    assert len(loop["freshness"]) == 3
    assert loop["autonomy"]["any_enabled"] is False
    # 因子页读到了真实运行目录里的主动搜索报告
    factor = next(item for item in loop["freshness"] if item["page"] == "factor")
    assert factor["detail"]["alpha_run"]["confirmed"] == 16


# ── 市场环境: 只描述、不阻断 ─────────────────────────────────────────


def _write_kline(data_dir: Path, days: int, up: int, down: int) -> None:
    """在 data_dir/kline_daily_enriched 下造出 days 个交易日分区。"""
    from datetime import timedelta

    import polars as pl

    root = data_dir / "kline_daily_enriched"
    start = date(2026, 1, 5)
    calendar = [start + timedelta(days=i) for i in range(days)]
    for day in calendar:
        rows = []
        for idx in range(up + down):
            rate = 1.01 if idx < up else 0.99
            offset = (day - start).days
            rows.append(
                {
                    "symbol": f"S{idx:02d}",
                    "date": day,
                    "close": 10.0 * (rate ** offset),
                }
            )
        target = root / f"date={day.isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows).write_parquet(target / "part.parquet")


def test_loop_status_reports_headwind_but_keeps_admission_verdict(tmp_path: Path):
    """环境逆风 ≠ 策略不合格。

    准入判定的是策略的样本外健康度, 与今天市场怎么走无关。若让环境改写
    verdict, 就会把「策略没问题」和「现在不该加仓」两件事混成一个开关,
    日后无法分辨是策略退化还是环境变化。
    """
    from app.services.guardian_store import GuardianStore
    from app.services.market_context import clear_cache
    from app.services.research_loop import build_loop_status

    clear_cache()
    _write_kline(tmp_path, days=25, up=2, down=8)

    store = GuardianStore(tmp_path)
    store.create_baseline(
        "mined_headwind",
        kind="mining_gate",
        data_as_of="2026-06-01",
        objective="sharpe",
        metrics={"sharpe": 1.1, "n_trades": 90},
    )
    store.append_check(
        "mined_headwind",
        {
            "checked_at": "2026-11-17T08:00:00+00:00",
            "verdict": "healthy",
            "degradation_pct": 0.03,
            "n_trades_total": 60,
        },
    )
    store.set_status("mined_headwind", "healthy")

    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 1, 29)), _FakeState())

    assert state["market"]["state"] == "risk_off"
    assert state["market"]["breadth_20d"] == pytest.approx(0.2, abs=1e-6)
    item = next(i for i in state["admission"]["items"] if i["strategy_id"] == "mined_headwind")
    # 策略本身仍然 admitted —— 环境不算它的错
    assert item["verdict"] == "admitted"
    # 但必须标出它现在没有 β 顺风
    assert item["regime"] == "headwind"
    # 环境提示排在最前: 它是唯一「现在就能改变下注」的结论
    assert state["next_actions"][0]["page"] == "market"
    assert "β 逆风" in state["next_actions"][0]["text"]
    assert state["market"]["evidence"]["source"].startswith("analysis/")


def test_loop_status_marks_neutral_when_breadth_recovers(tmp_path: Path):
    from app.services.market_context import clear_cache
    from app.services.research_loop import build_loop_status

    clear_cache()
    _write_kline(tmp_path, days=25, up=8, down=2)

    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 1, 29)), _FakeState())
    assert state["market"]["state"] == "risk_on"
    assert all(a["page"] != "market" for a in state["next_actions"])


# ── 拥挤度: 暴露读数, 不改判定 ───────────────────────────────────────


def test_strategy_crowding_uses_only_the_latest_card(tmp_path: Path):
    """拥挤度是「当前暴露」。用全部历史算均值, 会让刚调仓的策略背着旧持仓的读数。"""
    from app.decision.ledger import build_card
    from app.services.research_loop import _strategy_crowding

    levels = {"600519.SH": 0.95, "000001.SZ": 0.10}
    old = build_card(
        as_of="2026-10-01",
        picks=[{"symbol": "000001.SZ", "limit_price": 10.0, "p_win": 0.5}],
        horizon_sessions=20,
        strategy_id="s1",
    )
    new = build_card(
        as_of="2026-11-17",
        picks=[{"symbol": "600519.SH", "limit_price": 10.0, "p_win": 0.5}],
        horizon_sessions=20,
        strategy_id="s1",
    )
    got = _strategy_crowding([old, new], "s1", levels)
    assert got["pct"] == pytest.approx(0.95)
    assert got["crowded"] is True
    assert got["card_as_of"] == "2026-11-17"
    assert got["covered"] == 1


def test_strategy_crowding_is_none_without_data(tmp_path: Path):
    from app.decision.ledger import build_card
    from app.services.research_loop import _strategy_crowding

    assert _strategy_crowding([], "s1", {"600519.SH": 0.9}) is None
    card = build_card(
        as_of="2026-11-17",
        picks=[{"symbol": "600519.SH", "limit_price": 10.0, "p_win": 0.5}],
        horizon_sessions=20,
        strategy_id="s1",
    )
    # 分位表为空 ⇒ 未知, 不是 0 分位
    assert _strategy_crowding([card], "s1", {}) is None


def test_loop_status_exposes_crowding_without_touching_verdict(tmp_path: Path):
    """拥挤度只进读数, 不准改 verdict —— 高换手策略在牛市里正是靠这个暴露赚钱。"""
    from app.services.guardian_store import GuardianStore
    from app.services.market_context import clear_cache
    from app.services.research_loop import build_loop_status

    clear_cache()
    _write_kline(tmp_path, days=25, up=2, down=8)
    store = GuardianStore(tmp_path)
    store.create_baseline(
        "mined_crowded", kind="mining_gate", data_as_of="2026-06-01",
        objective="sharpe", metrics={"sharpe": 1.0, "n_trades": 80},
    )
    store.append_check("mined_crowded", {
        "checked_at": "2026-11-17T08:00:00+00:00", "verdict": "healthy",
        "degradation_pct": 0.02, "n_trades_total": 60,
    })
    store.set_status("mined_crowded", "healthy")

    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 1, 29)), _FakeState())
    assert "crowding" in state
    item = next(i for i in state["admission"]["items"] if i["strategy_id"] == "mined_crowded")
    assert item["verdict"] == "admitted"
    # 没有 card ⇒ 拥挤度未知, 但不能因此把策略判死
    assert item["crowding"] is None


# ── 拥挤度: 暴露读数, 不改判定 ───────────────────────────────────────


def test_strategy_crowding_uses_only_the_latest_card(tmp_path: Path):
    """拥挤度是「当前暴露」。用全部历史算均值, 会让刚调仓的策略背着旧持仓的读数。"""
    from app.decision.ledger import build_card
    from app.services.research_loop import _strategy_crowding

    levels = {"600519.SH": 0.95, "000001.SZ": 0.10}
    old = build_card(
        as_of="2026-10-01",
        picks=[{"symbol": "000001.SZ", "limit_price": 10.0, "p_win": 0.5}],
        horizon_sessions=20,
        strategy_id="s1",
    )
    new = build_card(
        as_of="2026-11-17",
        picks=[{"symbol": "600519.SH", "limit_price": 10.0, "p_win": 0.5}],
        horizon_sessions=20,
        strategy_id="s1",
    )
    got = _strategy_crowding([old, new], "s1", levels)
    assert got["pct"] == pytest.approx(0.95)
    assert got["crowded"] is True
    assert got["card_as_of"] == "2026-11-17"
    assert got["covered"] == 1


def test_strategy_crowding_is_none_without_data(tmp_path: Path):
    from app.decision.ledger import build_card
    from app.services.research_loop import _strategy_crowding

    assert _strategy_crowding([], "s1", {"600519.SH": 0.9}) is None
    card = build_card(
        as_of="2026-11-17",
        picks=[{"symbol": "600519.SH", "limit_price": 10.0, "p_win": 0.5}],
        horizon_sessions=20,
        strategy_id="s1",
    )
    # 分位表为空 ⇒ 未知, 不是 0 分位
    assert _strategy_crowding([card], "s1", {}) is None


def test_loop_status_exposes_crowding_without_touching_verdict(tmp_path: Path):
    """拥挤度只进读数, 不准改 verdict —— 高换手策略在牛市里正是靠这个暴露赚钱。"""
    from app.services.guardian_store import GuardianStore
    from app.services.market_context import clear_cache
    from app.services.research_loop import build_loop_status

    clear_cache()
    _write_kline(tmp_path, days=25, up=2, down=8)
    store = GuardianStore(tmp_path)
    store.create_baseline(
        "mined_crowded", kind="mining_gate", data_as_of="2026-06-01",
        objective="sharpe", metrics={"sharpe": 1.0, "n_trades": 80},
    )
    store.append_check("mined_crowded", {
        "checked_at": "2026-11-17T08:00:00+00:00", "verdict": "healthy",
        "degradation_pct": 0.02, "n_trades_total": 60,
    })
    store.set_status("mined_crowded", "healthy")

    state = build_loop_status(tmp_path, _FakeRepo(date(2026, 1, 29)), _FakeState())
    assert "crowding" in state
    item = next(i for i in state["admission"]["items"] if i["strategy_id"] == "mined_crowded")
    assert item["verdict"] == "admitted"
    # 没有 card ⇒ 拥挤度未知, 但不能因此把策略判死
    assert item["crowding"] is None
