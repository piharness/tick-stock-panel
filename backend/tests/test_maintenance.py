"""数据维护状态：把「日历期望 + 本地覆盖 + 四个产物」折叠成两行状态的纯函数测试。

这个模块最容易犯的错不是算错日期，而是**判不了时给假绿**：
2026-11-23 的现场就是体检不可用/口径不对，页面却显示「已是最新」。
所以这里单独钉住 fail-closed 分支。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import maintenance as api
from app.services import maintenance as svc


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
    monkeypatch.setattr(dp, "_run_tracked", lambda fn, label: called.append(label) or True)

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
    monkeypatch.setattr(dp, "_run_tracked", lambda fn, label: called.append(label) or True)

    class _Store:
        data_dir = Path(".")

    class _Repo:
        store = _Store()

    assert dp._run_handbook_refresh(_Repo()) is True
    assert called == ["handbook_refresh"]


def test_run_skips_without_creating_a_job(tmp_path: Path, monkeypatch):
    """全最新时不建空任务：空任务会占住单飞槽，让真正的更新排不上。"""
    monkeypatch.setattr(svc, "audit_snapshot", lambda **_k: _audit())
    monkeypatch.setattr(svc, "_kline_last", lambda _d: "2026-11-23")

    body = _client(tmp_path).post("/api/maintenance/run").json()
    assert body["skipped"] is True
    assert "job_id" not in body
