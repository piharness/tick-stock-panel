"""前端构建产物的静态路由 — 根级文件不能被 SPA 兜底成 HTML。

Docker/桌面安装包由 FastAPI 托管 frontend/dist。favicon.svg 等根级资源若被
SPA 兜底返回 index.html, 浏览器会因 MIME 不符丢弃图标, 页签显示默认占位图。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import _register_static_routes

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"></svg>'
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
INDEX_HTML = "<!doctype html><div id=root></div>"
NO_STORE = "no-store, must-revalidate"


def _client(static_dir: Path) -> TestClient:
    (static_dir / "assets").mkdir()
    (static_dir / "assets" / "index-abc123.js").write_text("console.log(1)")
    (static_dir / "favicon.svg").write_text(SVG)
    (static_dir / "donate-wechat.jpg").write_bytes(JPEG)
    (static_dir / "index.html").write_text(INDEX_HTML)

    app = FastAPI()
    _register_static_routes(app, static_dir)
    return TestClient(app)


def test_favicon_is_served_as_svg_not_spa_html(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text == SVG


def test_root_image_is_served_instead_of_spa_html(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/donate-wechat.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG


def test_unknown_route_still_falls_back_to_index_html(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/screener")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == NO_STORE
    assert response.text == INDEX_HTML


def test_index_html_direct_request_stays_uncached(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/index.html")

    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_STORE
    assert response.text == INDEX_HTML


def test_unknown_api_route_is_404_not_spa_html(tmp_path: Path) -> None:
    """`/api/*` 缺席必须 404 JSON —— 否则部署检查无从区分「上线」与「没上线」。

    2026-11-12 实测：镜像里没有 `app/decision/l1_cohort.py`，
    `/api/decision/l1/candidates?as_of=2026-11-11` 返回 **200 + text/html**
    （SPA 的 index.html）。用 `curl -w '%{http_code}'` 判断部署状态的人，
    在这种回应下永远看不到失败。
    """
    response = _client(tmp_path).get("/api/decision/l1/candidates?as_of=2026-11-11")

    assert response.status_code == 404, "未注册的 API 路径不许被 SPA 兜底成 200"
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "route_not_found"
    assert INDEX_HTML not in response.text


def test_api_prefix_alone_is_404(tmp_path: Path) -> None:
    """`/api` 本身（无尾斜线）也属于 API 命名空间。"""
    response = _client(tmp_path).get("/api")

    assert response.status_code == 404
    assert response.json()["code"] == "route_not_found"

