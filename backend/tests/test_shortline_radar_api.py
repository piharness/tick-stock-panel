"""短线雷达排序体检 API —— 只读产出物 + 缺产出物时给可执行的下一步。

路由不做任何计算（计算在 shortline_audit.rebuild，由 17:10/20:10 定时任务触发），
所以测试只钉两件事：
1. 有产出物时原样返回，不改写、不重算；
2. 没产出物时 404 且提示怎么生成，而不是 500 或静默空对象。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import shortline_radar


class _Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir


class _Repo:
    def __init__(self, data_dir: Path):
        self.store = _Store(data_dir)


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(shortline_radar.router)
    app.state.repo = _Repo(tmp_path)
    return TestClient(app)


def test_serves_artifact_verbatim(tmp_path: Path):
    art = {
        "prereg_id": "heat-sort-2026-11-24",
        "rule_current": "heat-v1",
        "rule_shadow": "heat-v2-noactivity",
        "all": {"months": 24, "cur_lift_bp": -140.9},
        "forward": None,
        "days_registered": 500,
        "fwd_days": 0,
        "backfill_days": 500,
    }
    f = tmp_path / "shortline" / "sort_audit.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(art, ensure_ascii=False), encoding="utf-8")

    r = _client(tmp_path).get("/api/shortline-radar/sort-audit")
    assert r.status_code == 200, r.text
    assert r.json() == art


def test_404_when_artifact_missing(tmp_path: Path):
    r = _client(tmp_path).get("/api/shortline-radar/sort-audit")
    assert r.status_code == 404
    assert "rebuild" in r.json()["detail"]
