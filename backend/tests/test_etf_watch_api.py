"""行业 ETF 扫描读数 API —— 只读产出物 + 路径/入参校验。

这个路由不做任何计算，所以测试也只钉三件事：
1. 有产出物时原样返回（不重算、不改写字段）；
2. 没产出物时 404 且给出可执行的下一步（提示跑 CLI），不是 500；
3. `day` 只接受 YYYY-MM-DD —— 它直接拼进文件名，必须挡路径穿越。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import etf_watch


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(etf_watch.router)
    app.state.data_dir = tmp_path
    return TestClient(app)


def _payload() -> dict:
    return {
        "meta": {"asof": "2026-11-20", "mode": "record-only"},
        "market": {"state": "下行·低波"},
        "counts": {"industries": 20, "signal": 6, "candidate": 6},
        "industries": [{"industry": "电子", "dd": -0.488, "status": "CANDIDATE"}],
        "ledger": {"events": [], "v1": {"state": "WAITING", "progress": "0/3"}},
    }


def test_serves_latest_verbatim(tmp_path: Path):
    root = tmp_path / "etf_watch"
    root.mkdir()
    payload = _payload()
    (root / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = _client(tmp_path).get("/api/etf-watch")
    assert r.status_code == 200, r.text
    body = r.json()
    # 原始字段一个都不许改写；只允许追加 meta.store_last_session 这一条覆盖率元数据。
    assert body["meta"].pop("store_last_session", "missing") is None
    assert body == payload


def test_store_last_session_reports_local_coverage(tmp_path: Path):
    """本地分区已到 11-23、扫描停在 11-20 时，接口必须把这层差距交出去。

    页面靠它说出「今天的链路没跑」——没有这条元数据，控件里没有今天也无人解释。
    """
    root = tmp_path / "etf_watch"
    root.mkdir()
    (root / "latest.json").write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    for day in ("2026-11-20", "2026-11-23"):
        (tmp_path / "kline_etf_daily" / f"date={day}").mkdir(parents=True)

    body = _client(tmp_path).get("/api/etf-watch").json()
    assert body["meta"]["asof"] == "2026-11-20"
    assert body["meta"]["store_last_session"] == "2026-11-23"


def test_missing_artifact_is_404_with_next_step(tmp_path: Path):
    r = _client(tmp_path).get("/api/etf-watch")
    assert r.status_code == 404
    assert "etf_watch.py" in r.json()["detail"]


def test_daily_snapshot_and_day_validation(tmp_path: Path):
    daily = tmp_path / "etf_watch" / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-11-20.json").write_text(json.dumps(_payload()), encoding="utf-8")
    c = _client(tmp_path)

    assert c.get("/api/etf-watch/daily/2026-11-20").json()["meta"]["asof"] == "2026-11-20"

    for bad in ("2026-11-20.json", "../../latest", "20261120", "2026-11-2"):
        assert c.get(f"/api/etf-watch/daily/{bad}").status_code in (400, 404), bad


def test_corrupt_artifact_is_500_not_silent(tmp_path: Path):
    root = tmp_path / "etf_watch"
    root.mkdir()
    (root / "latest.json").write_text("{not json", encoding="utf-8")
    r = _client(tmp_path).get("/api/etf-watch")
    assert r.status_code == 500
    assert "JSON" in r.json()["detail"]


def test_replay_serves_verbatim(tmp_path: Path):
    root = tmp_path / "etf_watch"
    root.mkdir()
    payload = {"meta": {"end": "2026-11-20", "sessions": 120}, "sessions": [],
               "snapshots": {}, "tracks": [], "outcomes": []}
    (root / "replay.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = _client(tmp_path).get("/api/etf-watch/replay")
    assert r.status_code == 200, r.text
    assert r.json() == payload


def test_replay_missing_is_404_with_next_step(tmp_path: Path):
    r = _client(tmp_path).get("/api/etf-watch/replay")
    assert r.status_code == 404
    assert "etf_watch_replay.py" in r.json()["detail"]


def test_replay_corrupt_is_500(tmp_path: Path):
    root = tmp_path / "etf_watch"
    root.mkdir()
    (root / "replay.json").write_text("{not json", encoding="utf-8")
    r = _client(tmp_path).get("/api/etf-watch/replay")
    assert r.status_code == 500
    assert "JSON" in r.json()["detail"]


def test_account_serves_verbatim(tmp_path: Path):
    root = tmp_path / "etf_watch"
    root.mkdir()
    payload = {"meta": {"policy": "JB0", "init": 1_000_000}, "asof": "2026-11-20",
               "state": {"equity": 1_000_000}, "instructions": []}
    (root / "account.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = _client(tmp_path).get("/api/etf-watch/account")
    assert r.status_code == 200, r.text
    assert r.json() == payload


def test_account_missing_is_404_with_next_step(tmp_path: Path):
    r = _client(tmp_path).get("/api/etf-watch/account")
    assert r.status_code == 404
    assert "etf_watch_account.py" in r.json()["detail"]

def test_cash_leg_serves_verbatim(tmp_path: Path):
    root = tmp_path / "etf_watch"
    root.mkdir()
    payload = {"meta": {"policy": "短融打底 + 国债锚", "bond": {"weight": 0.4}},
               "equity": 600000, "orders": [{"no": 1, "shares": 1700}]}
    (root / "cash_leg.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = _client(tmp_path).get("/api/etf-watch/cash-leg")
    assert r.status_code == 200, r.text
    assert r.json() == payload


def test_cash_leg_missing_is_404_with_next_step(tmp_path: Path):
    r = _client(tmp_path).get("/api/etf-watch/cash-leg")
    assert r.status_code == 404
    # 不能只报「不存在」—— 用户要的是下一条命令，而现金腿挂在账户脚本里顺带产出
    assert "etf_watch_account.py" in r.json()["detail"]
