"""回归测试: 短线雷达响应的预序列化缓存。

回归动机（2026-11-24）：载荷是全市场 ~5000 只的分类结果，实测 5.3MB。
`get_citizenship` 的 dict 缓存命中后计算是 0ms，但**每次请求仍要**
`json.dumps` 84ms + gzip 68ms —— 一个交易日内这份载荷不变，这两笔是纯重复。
端到端 528ms 里有 152ms 耗在这里。

判据：
  1. 同一 (data_dir, as_of) 只序列化/压缩一次；
  2. 字节必须与「直接返回 dict」等价（反序列化回来要一模一样，不许改口径）；
  3. as_of 不同不得复用（否则页面切日期会看到别人的结论）。
"""
from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import shortline_radar
from app.services import citizenship

_PAYLOAD = {
    "as_of": "2026-11-24",
    "rows": [
        {"symbol": "600001.SH", "arena": "institution", "tags": ["蓝筹"]},
        {"symbol": "300001.SZ", "arena": "hotmoney", "tags": []},
    ],
    "disclaimer": "",
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(citizenship, "compute", lambda _d, _a: json.loads(json.dumps(_PAYLOAD)))
    citizenship.clear_citizenship_cache()
    yield
    citizenship.clear_citizenship_cache()


def test_serializes_once_per_key(tmp_path: Path, monkeypatch):
    calls = {"n": 0}
    real = citizenship._dump

    def counted(payload):
        calls["n"] += 1
        return real(payload)

    monkeypatch.setattr(citizenship, "_dump", counted)

    raw1, gz1 = citizenship.get_citizenship_serialized(tmp_path)
    raw2, gz2 = citizenship.get_citizenship_serialized(tmp_path)

    assert calls["n"] == 1, "同一 key 不得重复序列化"
    assert raw1 == raw2 and gz1 == gz2
    assert json.loads(raw1) == _PAYLOAD, "字节必须与 dict 口径一致"
    assert gzip.decompress(gz1) == raw1


def test_as_of_is_part_of_the_key(tmp_path: Path, monkeypatch):
    calls = {"n": 0}
    real = citizenship._dump

    def counted(payload):
        calls["n"] += 1
        return real(payload)

    monkeypatch.setattr(citizenship, "_dump", counted)

    citizenship.get_citizenship_serialized(tmp_path, None)
    citizenship.get_citizenship_serialized(tmp_path, date(2026, 11, 20))

    assert calls["n"] == 2, "不同 as_of 不得复用同一份字节"


def test_clear_forces_reserialize(tmp_path: Path, monkeypatch):
    calls = {"n": 0}
    real = citizenship._dump

    def counted(payload):
        calls["n"] += 1
        return real(payload)

    monkeypatch.setattr(citizenship, "_dump", counted)

    citizenship.get_citizenship_serialized(tmp_path)
    citizenship.clear_citizenship_cache()
    citizenship.get_citizenship_serialized(tmp_path)

    assert calls["n"] == 2, "清除缓存后必须重新序列化"


def test_non_finite_floats_do_not_break_json(tmp_path: Path, monkeypatch):
    """停牌/坏源会给出 NaN；`allow_nan=False` 抛错时要净化后仍然给出合法 JSON。"""
    payload = {"n": float("nan"), "ok": 1.0}
    monkeypatch.setattr(citizenship, "compute", lambda _d, _a: payload)

    raw, _ = citizenship.get_citizenship_serialized(tmp_path)
    decoded = json.loads(raw)

    assert decoded["ok"] == 1.0
    assert decoded["n"] is None


# ── HTTP 层：字节必须原样下发，且按 Accept-Encoding 选形态 ──────────────


class _Repo:
    def __init__(self, data_dir: Path):
        self.store = type("S", (), {"data_dir": data_dir})()


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(shortline_radar.router)
    app.include_router(shortline_radar.legacy_router)
    app.state.repo = _Repo(tmp_path)
    return TestClient(app)


def test_endpoint_serves_cached_bytes(tmp_path: Path):
    """注意：TestClient 默认带 `accept-encoding: gzip` 且 httpx 会自动解压，
    所以「未压缩分支」必须显式要求 identity。"""
    client = _client(tmp_path)

    plain = client.get("/api/shortline-radar", headers={"accept-encoding": "identity"})
    assert plain.status_code == 200
    assert plain.headers.get("content-encoding") is None
    assert plain.json() == _PAYLOAD

    packed = client.get("/api/shortline-radar", headers={"accept-encoding": "gzip"})
    assert packed.status_code == 200
    assert packed.headers.get("content-encoding") == "gzip"
    # httpx 已解压；内容正确即证明服务端下发的是有效 gzip 流
    assert packed.json() == _PAYLOAD


def test_legacy_path_uses_same_cache(tmp_path: Path):
    """旧路径 `/api/citizenship` 是隐藏别名，必须与主路径同一份字节。"""
    client = _client(tmp_path)
    headers = {"accept-encoding": "identity"}
    main = client.get("/api/shortline-radar", headers=headers)
    legacy = client.get("/api/citizenship", headers=headers)
    assert legacy.status_code == 200
    assert legacy.content == main.content
