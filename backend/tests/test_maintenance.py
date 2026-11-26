"""数据维护状态：把「日历期望 + 本地覆盖 + 四个产物」折叠成两行状态的纯函数测试。

这个模块最容易犯的错不是算错日期，而是**判不了时给假绿**：
2026-11-23 的现场就是体检不可用/口径不对，页面却显示「已是最新」。
所以这里单独钉住 fail-closed 分支。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import maintenance as api
from app.services import maintenance as svc
from app.services import pipeline_jobs
from app.services.pipeline_jobs import JobStore


@pytest.fixture(autouse=True)
def _isolate_job_store(tmp_path: Path, monkeypatch):
    """把 job_store 隔离到临时目录，杜绝触碰真实 data/job_store。

    这里的测试多用 ``monkeypatch.setattr(svc.job_store, ...)`` 或
    ``pipeline_jobs.job_store`` 打桩。monkeypatch 保存原属性时要先 ``getattr``，
    而 ``_LazyJobStore`` 会因此惰性实例化到 ``settings.data_dir/job_store``
    （真实数据目录）；实例化的 ``_reap_orphans`` 一旦扫到 running 任务文件就会
    补录写盘，触发沙箱 bulk-guard 的 ``SystemExit(1)`` —— 6 条测试同时爆掉，
    且只在生产数据更新进行中才复现。换成 tmp 下的真实 store，打桩与调用都安全。
    """
    store = JobStore(store_dir=tmp_path / "job_store")
    monkeypatch.setattr(svc, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    yield


def _audit(**facts) -> dict:
    base = {
        "now": "2026-11-23T19:50",
        "expected_session": "2026-11-23",
        "phase": "closed",
        "asof": "2026-11-23",
        "replay_end": "2026-11-23",
        "account_asof": "2026-11-23",
        "cash_leg_asof": "2026-11-23",
    }
    base.update(facts)
    return {"facts": base, "rows": []}


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.state.data_dir = tmp_path
    return TestClient(app)


def test_fresh_domains_have_no_stale():
    st = svc.build_status(_audit(), "2026-11-23", None)
    assert st["stale"] is False
    assert [d["stale"] for d in st["domains"]] == [False, False]
    assert st["expected"] == "2026-11-23"


def test_handbook_behind_names_the_missing_parts():
    st = svc.build_status(_audit(replay_end="2026-11-20", account_asof="2026-11-20"), "2026-11-23", None)
    assert st["stale"] is True
    handbook = st["domains"][1]
    assert handbook["stale"] is True
    assert "回放" in handbook["detail"] and "账户" in handbook["detail"]


def test_kline_behind_is_reported_separately():
    st = svc.build_status(_audit(), "2026-11-20", None)
    assert st["domains"][0]["stale"] is True
    assert st["domains"][1]["stale"] is False


def test_audit_unavailable_fails_closed():
    """体检不可用时判「需要更新」，不能判「已最新」。"""
    for audit in (None, {}, {"facts": {}}):
        st = svc.build_status(audit, "2026-11-23", None)
        assert st["stale"] is True, audit
        assert all(d["stale"] for d in st["domains"])
        assert "无法判断" in st["domains"][0]["detail"]


def test_kline_asof_takes_pipeline_products_not_just_daily(tmp_path: Path, monkeypatch):
    """行情域 asof = 日K / enriched / ETF / 指数 的最小值。

    2026-11-24 的现场：日K 已到当日、enriched 与 ETF 停在昨天，维护入口却判
    「行情最新」，于是只跑手册、不补数据，手册拿着旧 ETF 必然报错，点多少次都一样。
    """
    import polars as pl

    from app.services import preferences

    def _write(table: str, part: str, days: list[str]) -> None:
        path = tmp_path / table / part
        path.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"date": days, "symbol": ["a"] * len(days)}).write_parquet(path / "part.parquet")

    _write("kline_daily", "date=2026-11", ["2026-11-23", "2026-11-24"])
    _write("kline_daily_enriched", "date=2026-11-23", ["2026-11-23"])
    _write("kline_etf_daily", "date=2026-11", ["2026-11-24"])
    _write("kline_index_daily", "date=2026-11", ["2026-11-24"])
    monkeypatch.setattr(preferences, "get_pipeline_pull_etf", lambda: True)
    monkeypatch.setattr(preferences, "get_pipeline_pull_index", lambda: True)
    monkeypatch.setattr(svc, "_market_parts_cache", (0.0, {}))

    # enriched 与 ETF 都落后 → 取最早的那个
    assert svc._kline_last(tmp_path) == "2026-11-23"

    # 关掉 ETF 拉取后不再拿 ETF 当落后依据（否则永远提示更新）
    _write("kline_daily_enriched", "date=2026-11-24", ["2026-11-24"])
    _write("kline_etf_daily", "date=2026-11", ["2026-11-23"])
    monkeypatch.setattr(preferences, "get_pipeline_pull_etf", lambda: False)
    monkeypatch.setattr(svc, "_market_parts_cache", (0.0, {}))
    assert svc._kline_last(tmp_path) == "2026-11-24"

    # 产物缺失时 fail-closed：判不了按落后处理
    only_stock = tmp_path / "only_stock"
    (only_stock / "kline_daily" / "date=2026-11").mkdir(parents=True)
    pl.DataFrame({"date": ["2026-11-24"], "symbol": ["a"]}).write_parquet(
        only_stock / "kline_daily" / "date=2026-11" / "part.parquet"
    )
    (only_stock / "kline_daily_enriched" / "date=2026-11-24").mkdir(parents=True)
    pl.DataFrame({"date": ["2026-11-24"], "symbol": ["a"]}).write_parquet(
        only_stock / "kline_daily_enriched" / "date=2026-11-24" / "part.parquet"
    )
    monkeypatch.setattr(preferences, "get_pipeline_pull_etf", lambda: True)
    monkeypatch.setattr(preferences, "get_pipeline_pull_index", lambda: True)
    monkeypatch.setattr(svc, "_market_parts_cache", (0.0, {}))
    assert svc._kline_last(only_stock) is None


def test_active_job_is_surfaced_for_takeover(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(svc, "audit_snapshot", lambda **_k: _audit(replay_end="2026-11-20"))
    monkeypatch.setattr(svc, "_kline_last", lambda _d: "2026-11-23")
    monkeypatch.setattr(svc, "_active_job", lambda: {"id": "abc123", "status": "running", "stage": "handbook", "pct": 70, "message": "回放"})

    body = _client(tmp_path).get("/api/maintenance").json()
    assert body["job"]["id"] == "abc123"
    assert body["stale"] is True
    assert body["domains"][0]["asof"] == "2026-11-23"


def test_audit_exit_one_still_parses_json(tmp_path: Path):
    """退出码 1 = 有产物落后，不是脚本坏了；JSON 必须照常解析。

    这一条是 2026-11-23 的回归：把 1 当失败，状态就成了 unknown，
    页面只会说「无法判断」，看不出到底哪条链落后。
    """
    script = tmp_path / "fake_audit.py"
    script.write_text(
        "import json,sys\nprint(json.dumps({'facts':{'expected_session':'2026-11-23'}}))\nsys.exit(1)\n",
        encoding="utf-8",
    )
    got = svc._run_json(script, timeout=10)
    assert got and got["facts"]["expected_session"] == "2026-11-23"


def test_audit_exit_two_with_bad_payload_degrades(tmp_path: Path):
    script = tmp_path / "broken.py"
    script.write_text("print('not json')\nraise SystemExit(1)\n", encoding="utf-8")
    assert svc._run_json(script, timeout=10) is None


def test_pending_job_is_not_lost_by_list_truncation(monkeypatch):
    """pending 任务没有 started_at，list_recent 会把它排到末尾截掉。

    2026-11-23 的现场：任务在排队，维护卡却显示「更新数据」可点。
    """
    monkeypatch.setattr(svc.job_store, "active_id", lambda: "pending1")
    monkeypatch.setattr(svc.job_store, "get", lambda _id: {
        "id": "pending1", "status": "pending", "stage": "init", "progress": 0,
        "started_at": None, "log": [{"msg": "等待其他计算任务完成…"}],
    })
    job = svc._active_job()
    assert job and job["id"] == "pending1"
    assert job["message"].startswith("等待")


def test_scheduled_refresh_skips_when_already_current(monkeypatch):
    """定时器也走同一判断：已覆盖最近交易日就不建任务，不污染同步历史。"""
    from app.jobs import daily_pipeline as dp

    monkeypatch.setattr(svc, "status", lambda _d: {"stale": False, "expected": "2026-11-23"})
    called: list[str] = []
    monkeypatch.setattr(dp, "_run_tracked", lambda fn, label, **kw: called.append(label) or True)

    class _Store:
        data_dir = Path(".")

    class _Repo:
        store = _Store()

    assert dp._run_handbook_refresh(_Repo()) is False
    assert called == []


def test_scheduled_refresh_runs_tracked_when_stale(monkeypatch):
    from app.jobs import daily_pipeline as dp

    monkeypatch.setattr(svc, "status", lambda _d: {"stale": True, "expected": "2026-11-23"})
    called: list[str] = []
    monkeypatch.setattr(dp, "_run_tracked", lambda fn, label, **kw: called.append(label) or True)

    class _Store:
        data_dir = Path(".")

    class _Repo:
        store = _Store()

    assert dp._run_handbook_refresh(_Repo()) is True
    assert called == ["handbook_refresh"]


def test_scheduled_refresh_reaps_stalled_job_before_deciding(monkeypatch):
    """阶梯不能被人挡在门外：上一次任务卡死时，调度这一刻先回收再判断。"""
    from app.jobs import daily_pipeline as dp
    from app.services import pipeline_jobs

    reaped: list[str] = []
    monkeypatch.setattr(pipeline_jobs.job_store, "reap_stale", lambda: reaped.append("reap"))
    monkeypatch.setattr(svc, "status", lambda _d: {"stale": False, "expected": "2026-11-24"})

    class _Store:
        data_dir = Path(".")

    class _Repo:
        store = _Store()

    assert dp._run_handbook_refresh(_Repo()) is False
    assert reaped == ["reap"]


def test_run_skips_without_creating_a_job(tmp_path: Path, monkeypatch):
    """全最新时不建空任务：空任务会占住单飞槽，让真正的更新排不上。"""
    monkeypatch.setattr(svc, "audit_snapshot", lambda **_k: _audit())
    monkeypatch.setattr(svc, "_kline_last", lambda _d: "2026-11-23")

    body = _client(tmp_path).post("/api/maintenance/run").json()
    assert body["skipped"] is True
    assert "job_id" not in body


# ---------------------------------------------------------------- 自愈：计划 + 复核


def _domain_status(
    *, kline_stale: bool, handbook_stale: bool, phase: str = "closed",
    expected: str = "2026-11-24",
) -> dict:
    return {
        "phase": phase,
        "expected": expected,
        "stale": kline_stale or handbook_stale,
        "domains": [
            {"id": "kline", "label": "行情数据", "asof": "2026-11-23", "stale": kline_stale,
             "detail": "落后：增强 2026-11-23、ETF 2026-11-23", "parts": []},
            {"id": "handbook", "label": "投资手册", "asof": "2026-11-23", "stale": handbook_stale,
             "detail": "落后：回放", "parts": []},
        ],
    }


def _run_all(monkeypatch, snapshots: list[dict]) -> list[str]:
    """按顺序喂状态快照，记录实际执行了哪几条链。"""
    from app.jobs import daily_pipeline as dp

    seq = iter(snapshots)
    monkeypatch.setattr(svc, "_snapshot", lambda _d: next(seq))
    monkeypatch.setattr(svc, "reset_cache", lambda: None)
    calls: list[str] = []
    monkeypatch.setattr(dp, "run_now", lambda *a, **k: calls.append("pipeline") or {})
    monkeypatch.setattr(
        svc, "_handbook", lambda progress, base=60: calls.append("handbook") or {"exit": 0},
    )
    return calls


def test_plan_puts_pipeline_before_handbook():
    plan = svc.build_plan(_domain_status(kline_stale=True, handbook_stale=True))
    assert [p["action"] for p in plan] == ["pipeline", "handbook"]
    assert "增强" in plan[0]["reason"]  # 说清缺哪张表，不是笼统「行情落后」


def test_run_all_runs_pipeline_then_handbook_and_verifies(monkeypatch):
    calls = _run_all(monkeypatch, [
        _domain_status(kline_stale=True, handbook_stale=True),
        _domain_status(kline_stale=False, handbook_stale=True),
        _domain_status(kline_stale=False, handbook_stale=False),
    ])

    out = svc.run_all(None, None, Path("."), lambda *a, **k: None)

    assert calls == ["pipeline", "handbook"]
    assert out["executed"] == ["pipeline", "handbook"]
    assert out["converged"] is True


def test_run_all_reruns_handbook_when_pipeline_moves_etf_forward(monkeypatch):
    """跑之前的计划不算数：行情推进一天会让手册立刻落后，必须当场重新判定。"""
    calls = _run_all(monkeypatch, [
        _domain_status(kline_stale=True, handbook_stale=False),
        _domain_status(kline_stale=False, handbook_stale=True),
        _domain_status(kline_stale=False, handbook_stale=False),
    ])

    out = svc.run_all(None, None, Path("."), lambda *a, **k: None)

    assert calls == ["pipeline", "handbook"]
    assert out["executed"] == ["pipeline", "handbook"]


def test_run_all_stops_before_handbook_when_market_not_converged(monkeypatch):
    """管道「没抛异常」不等于每张产物都到了 expected（供应迟交时 ETF 写 0 行）。"""
    calls = _run_all(monkeypatch, [
        _domain_status(kline_stale=True, handbook_stale=True),
        _domain_status(kline_stale=True, handbook_stale=True),  # 管道跑完 ETF 仍缺
    ])

    with pytest.raises(svc.MaintenanceNotConverged) as exc:
        svc.run_all(None, None, Path("."), lambda *a, **k: None)

    assert calls == ["pipeline"]          # 不能拿旧 ETF 去跑手册
    assert "行情链未补齐" in str(exc.value)
    assert "ETF 2026-11-23" in str(exc.value)


def test_run_all_skips_when_nothing_stale(monkeypatch):
    calls = _run_all(monkeypatch, [_domain_status(kline_stale=False, handbook_stale=False)])

    out = svc.run_all(None, None, Path("."), lambda *a, **k: None)

    assert calls == []
    assert out["skipped"] is True and out["converged"] is True


def test_run_all_marks_unknown_when_the_day_rolls_over_mid_run(monkeypatch):
    """跑到一半跨过收盘+10 分钟：期望日变了，不能拿新期望判这一轮（否则天天假失败）。"""
    calls = _run_all(monkeypatch, [
        _domain_status(kline_stale=False, handbook_stale=True, expected="2026-11-24"),
        _domain_status(kline_stale=False, handbook_stale=True, expected="2026-11-25"),
    ])

    out = svc.run_all(None, None, Path("."), lambda *a, **k: None)

    assert calls == ["handbook"]
    assert out["converged"] is None
    assert "2026-11-25" in out["note"]
