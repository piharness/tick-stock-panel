"""请求耗时账本的契约: 没有它, 任何"优化"都无法被证伪。

钉住四件事:

1. `record` 落 JSONL 且字段齐全 —— 缺 `route`/`status` 的账本读不出分布;
2. `route` 必须是**模板**(`/items/{item_id}`)而不是原始路径, 否则 job_id 进
   路径会把"按路由聚合"退化成按 ID 聚合, p99 变成一堆 n=1 的样本;
3. `summarize` 的分位数是 nearest-rank 的**确定值**, 不是估算 —— 判据要能被
   手算复核, 否则"p95 从 2718 降到 50"这句话没有意义;
4. 写账失败(目录不可写/磁盘满)不许把请求弄红 —— 计时器是观测, 不是业务。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import request_metrics as rm


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path):
    """每个用例独立目录 + 清空模块级缓冲, 避免缓冲把上一用例的行写进这一用例。"""
    rm.flush()  # 清掉上个用例残留(写到它自己的 tmp 目录)
    rm.configure(enabled=True, directory=tmp_path)
    yield tmp_path
    rm.flush()
    rm.configure(enabled=True, directory=None)


def _lines(directory):
    rm.flush()  # 账本是缓冲写: 读盘前必须显式刷
    files = sorted(directory.glob("requests-*.jsonl"))
    assert files, "没有落盘任何账本文件"
    return [json.loads(x) for f in files for x in f.read_text("utf-8").splitlines() if x.strip()]


def test_record_writes_complete_jsonl(_isolated_metrics):
    rm.record(
        method="GET", route="/api/kline/daily", path="/api/kline/daily",
        status=200, duration_ms=28.44, bytes_out=1024, stream=False,
    )
    rows = _lines(_isolated_metrics)
    assert len(rows) == 1
    row = rows[0]
    assert row["route"] == "/api/kline/daily"
    assert row["status"] == 200
    assert row["ms"] == pytest.approx(28.44)
    assert row["bytes"] == 1024
    assert row["stream"] is False
    assert row["ts"]  # 带时区的时间戳, 用来对齐跨日


def test_summarize_percentiles_are_exact():
    rows = [{"method": "GET", "route": "/x", "status": 200, "ms": float(i)} for i in range(1, 101)]
    out = rm.summarize(rows)
    assert len(out) == 1
    r = out[0]
    assert r["n"] == 100
    # nearest-rank: ceil(q*n)-1
    assert (r["p50"], r["p95"], r["p99"], r["max"]) == (50.0, 95.0, 99.0, 100.0)


def test_summarize_keeps_streams_out_of_p99():
    rows = [
        {"method": "GET", "route": "/api/events", "status": 200, "ms": 900_000.0, "stream": True},
        {"method": "GET", "route": "/api/kline/daily", "status": 200, "ms": 10.0},
        {"method": "GET", "route": "/api/kline/daily", "status": 200, "ms": 20.0},
    ]
    out = {r["route"]: r for r in rm.summarize(rows)}
    assert out["/api/kline/daily"]["p99"] == 20.0  # 长连接没有把交互路径的 p99 抬走
    assert out["/api/events"]["method"] == "STREAM"


def test_middleware_records_route_template(_isolated_metrics):
    app = FastAPI()

    @app.get("/api/items/{item_id}")
    def _item(item_id: str):
        return {"id": item_id}

    app.add_middleware(rm.RequestMetricsMiddleware)
    with TestClient(app) as client:
        assert client.get("/api/items/42").status_code == 200

    rows = _lines(_isolated_metrics)
    assert [r["route"] for r in rows] == ["/api/items/{item_id}"]
    assert rows[0]["status"] == 200
    assert rows[0]["path"] == "/api/items/42"  # 原始路径仍保留, 供追踪单次请求


def test_middleware_skips_non_api_paths(_isolated_metrics):
    app = FastAPI()

    @app.get("/assets/app.js")
    def _asset():
        return "x"

    app.add_middleware(rm.RequestMetricsMiddleware)
    with TestClient(app) as client:
        client.get("/assets/app.js")
    assert list(_isolated_metrics.glob("requests-*.jsonl")) == []


def test_streaming_response_passes_through_unchanged(_isolated_metrics):
    """SSE 必须逐字节透传 —— 中间件只观测 send, 不许缓冲或改写分块。

    `/api/events`、`/api/intraday/stream` 是长连接; 任何对 body 的聚合/重放
    都会让行情流变成"攒够一批再发"。这里用 3 个分块断言内容与顺序都不变,
    并要求账本把它标成 stream(否则 900s 的连接会把 p99 抬走)。
    """
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    @app.get("/api/stream")
    def _stream():
        def gen():
            for i in range(3):
                yield f"chunk{i}\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    app.add_middleware(rm.RequestMetricsMiddleware)
    with TestClient(app) as client:
        resp = client.get("/api/stream")

    assert resp.status_code == 200
    assert resp.text == "chunk0\nchunk1\nchunk2\n"
    row = _lines(_isolated_metrics)[0]
    assert row["stream"] is True
    assert row["bytes"] == len(b"chunk0\nchunk1\nchunk2\n")


def test_write_failure_does_not_break_request(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")  # 父路径是文件 ⇒ mkdir 必失败
    rm.configure(enabled=True, directory=blocker / "logs")

    app = FastAPI()

    @app.get("/api/ping")
    def _ping():
        return {"ok": True}

    app.add_middleware(rm.RequestMetricsMiddleware)
    with TestClient(app) as client:
        assert client.get("/api/ping").status_code == 200
    rm.flush()  # 刷盘失败也只记 debug, 不许抛


def test_record_is_buffered_then_flushed(_isolated_metrics):
    """record() 不许同步落盘 —— 9p 上一行 1.9ms, 而热端点只要 3~5ms。

    同步写等于给被测对象加 40%~200% 开销, 还把事件循环卡住; 量出来的 p99
    会被账本自己污染。这里钉住"先缓冲、显式 flush 才可见"。
    """
    rm.record(method="GET", route="/api/x", path="/api/x", status=200, duration_ms=1.0)
    assert list(_isolated_metrics.glob("requests-*.jsonl")) == []  # 此刻还没写

    rm.flush()
    assert len(_lines(_isolated_metrics)) == 1


def test_disabled_writes_nothing(_isolated_metrics):
    rm.configure(enabled=False)
    rm.record(method="GET", route="/api/x", path="/api/x", status=200, duration_ms=1.0)
    rm.flush()
    assert list(_isolated_metrics.glob("requests-*.jsonl")) == []
