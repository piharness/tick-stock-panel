"""持仓排雷（席位锁仓状态）测试：透传口径 / 最新日选择 / 防未来函数 / 缺表降级。

不测阈值本身——阈值在 `analysis/_build_seat_relay_state.py` 离线算好写进 parquet，
服务层只做读取与过滤。这里断言的是「读对了没有」，不是「阈值对不对」。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services import seat_risk


def _write_state(root: Path, rows: list[dict]) -> None:
    d = root / "dragon_tiger_seats"
    d.mkdir(parents=True, exist_ok=True)
    base = {"relay5": 0.4, "net5_ratio": 0.05, "streak": 3, "dist_hi": -0.05, "level": "ok"}
    pl.DataFrame([{**base, **r} for r in rows]).write_parquet(d / seat_risk.STATE_FILE)


def test_missing_table_returns_empty(tmp_path: Path) -> None:
    as_of, state = seat_risk.load_state(tmp_path)
    assert as_of is None
    assert state == {}


def test_reads_latest_day_only(tmp_path: Path) -> None:
    _write_state(tmp_path, [
        {"symbol": "002242.SZ", "date": "2026-11-18", "level": "review", "streak": 9},
        {"symbol": "002242.SZ", "date": "2026-11-19", "level": "watch", "streak": 3},
        {"symbol": "000530.SZ", "date": "2026-11-19", "level": "ok", "streak": 7},
    ])
    as_of, state = seat_risk.load_state(tmp_path)
    assert as_of == "2026-11-19"
    # 同票只留最新一日，旧日的 review 不能泄漏进来
    assert state["002242.SZ"]["level"] == "watch"
    assert state["002242.SZ"]["streak"] == 3
    assert state["000530.SZ"]["level"] == "ok"
    assert len(state) == 2


def test_as_of_blocks_future_days(tmp_path: Path) -> None:
    from datetime import date

    _write_state(tmp_path, [
        {"symbol": "002242.SZ", "date": "2026-11-18", "level": "review"},
        {"symbol": "002242.SZ", "date": "2026-11-19", "level": "watch"},
    ])
    as_of, state = seat_risk.load_state(tmp_path, as_of=date(2026, 11, 18))
    assert as_of == "2026-11-18"
    assert state["002242.SZ"]["level"] == "review"

    as_of2, state2 = seat_risk.load_state(tmp_path, as_of=date(2026, 11, 1))
    assert as_of2 is None and state2 == {}


def test_pick_keeps_only_requested(tmp_path: Path) -> None:
    _write_state(tmp_path, [
        {"symbol": "002242.SZ", "date": "2026-11-19"},
        {"symbol": "000530.SZ", "date": "2026-11-19"},
    ])
    _, state = seat_risk.load_state(tmp_path)
    got = seat_risk.pick(state, ["000530.SZ", "510300.SH"])
    # ETF 不在表里 → 不出现，调用方据此显示「无数据」而非「安全」
    assert set(got) == {"000530.SZ"}


def test_none_fields_survive(tmp_path: Path) -> None:
    _write_state(tmp_path, [
        {"symbol": "002242.SZ", "date": "2026-11-19", "relay5": None, "net5_ratio": None},
    ])
    _, state = seat_risk.load_state(tmp_path)
    assert state["002242.SZ"]["relay5"] is None


def test_endpoint_parses_symbols_csv(tmp_path: Path) -> None:
    """端点接线：逗号分隔解析 + 透出 as_of + 只返回请求的标的。"""
    from types import SimpleNamespace

    from app.api import lots as lots_api

    _write_state(tmp_path, [
        {"symbol": "002242.SZ", "date": "2026-11-19", "level": "review", "streak": 6},
        {"symbol": "000530.SZ", "date": "2026-11-19", "level": "ok"},
    ])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))

    resp = lots_api.seat_risk(request, symbols=" 002242.SZ , 000530.SZ ,510300.SH ")
    assert resp["as_of"] == "2026-11-19"
    assert set(resp["items"]) == {"002242.SZ", "000530.SZ"}
    assert resp["items"]["002242.SZ"]["level"] == "review"

    empty = lots_api.seat_risk(request, symbols="")
    assert empty["items"] == {}
    assert empty["as_of"] == "2026-11-19"


def test_route_not_shadowed_by_lot_id(tmp_path: Path) -> None:
    """真实 HTTP 路径：GET /api/lots/seat-risk 必须命中本端点，不被 /{lot_id} 吞掉。"""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import lots as lots_api

    _write_state(tmp_path, [
        {"symbol": "002242.SZ", "date": "2026-11-19", "level": "review", "streak": 6},
    ])
    app = FastAPI()
    app.include_router(lots_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with TestClient(app) as client:
        resp = client.get("/api/lots/seat-risk", params={"symbols": "002242.SZ"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["as_of"] == "2026-11-19"
        assert body["items"]["002242.SZ"]["level"] == "review"
        assert body["items"]["002242.SZ"]["streak"] == 6


def test_real_table_sanity() -> None:
    """真实表若存在：review 必须是极少数（阈值写错会让全表变 review）。"""
    root = Path(__file__).resolve().parents[2] / "data"
    path = root / "dragon_tiger_seats" / seat_risk.STATE_FILE
    if not path.is_file():
        pytest.skip("未生成 seat_relay_state.parquet")
    _, state = seat_risk.load_state(root)
    if not state:
        pytest.skip("状态表为空")
    n_review = sum(1 for v in state.values() if v["level"] == "review")
    assert n_review / len(state) < 0.10, f"review 占比异常 {n_review}/{len(state)}"
