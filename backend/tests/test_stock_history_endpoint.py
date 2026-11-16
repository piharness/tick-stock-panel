"""个股分析长区间接口 —— 分段拼接的三条硬承诺。

这个接口存在的理由是「本地只覆盖某天之后」，所以判据全在边界上：

  1. 本地够用时**不许**联网（`upstream.attempted is False`）—— 一次页面加载
     不该因为「也许缺一段」就去打上游；
  2. 缺的那一段来自上游时，`segments` 必须如实分段，且日期不能重复
     （重复就是同一根 K 被画两遍，价格轴会被挤歪）；
  3. 上游失败时**返回本地段**并带上 error，不能整页 500，也不能假装补上了。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import stock_analysis as api


def _bars(rows: list[tuple[str, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "date": [date.fromisoformat(d) for d, _ in rows],
        "open": [c for _, c in rows],
        "high": [c * 1.01 for _, c in rows],
        "low": [c * 0.99 for _, c in rows],
        "close": [c for _, c in rows],
        "volume": [1000.0] * len(rows),
        "amount": [1e8] * len(rows),
    })


class _Repo:
    def __init__(self, local: pl.DataFrame) -> None:
        self.local = local
        self.calls: list[tuple[date, date]] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        self.calls.append((start, end))
        if self.local.is_empty():
            return self.local
        return self.local.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame({"symbol": ["600340.SH"], "name": ["*ST华幸"]})


def _req(repo) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


LOCAL = _bars([("2025-05-07", 2.48), ("2025-05-08", 2.5), ("2026-11-13", 1.08)])


def test_local_covers_range_no_upstream_call(monkeypatch) -> None:
    """本地够用就不联网 —— 分段接口最常见的退化是"每次页面加载都去问上游"。"""
    called: list[str] = []
    monkeypatch.setattr(api.stock_history_fill, "fetch_forward",
                        lambda *a, **k: called.append("x") or pl.DataFrame())
    repo = _Repo(LOCAL)
    out = api.get_history(_req(repo), symbol="600340.SH", start_date="2025-05-07",
                          end_date="2026-11-13", fill=True)

    assert called == []
    assert out["upstream"]["attempted"] is False
    assert [s["source"] for s in out["segments"]] == ["local"]
    assert out["segments"][0]["n"] == 3
    assert out["rows"][0]["date"] == "2025-05-07"
    assert out["name"] == "*ST华幸"
    assert out["known"] is True


def test_head_gap_filled_and_segments_are_explicit(monkeypatch) -> None:
    head = _bars([("2024-09-02", 1.08), ("2025-05-06", 2.4)])

    def fake_fill(symbol, start, end, local_first, **kw):
        assert symbol == "600340.SH"
        assert start == date(2024, 9, 1)
        assert local_first == date(2025, 5, 7)   # 只补到本地首根之前一天
        return head, {"attempted": True, "fetched_rows": head.height, "error": None}

    monkeypatch.setattr(api.stock_history_fill, "fill_head", fake_fill)
    out = api.get_history(_req(_Repo(LOCAL)), symbol="600340.SH", start_date="2024-09-01",
                          end_date="2026-11-13", fill=True)

    assert [s["source"] for s in out["segments"]] == ["upstream", "local"]
    assert out["segments"][0]["start"] == "2024-09-02"
    assert out["segments"][0]["end"] == "2025-05-06"
    assert out["segments"][1]["start"] == "2025-05-07"
    dates = [r["date"] for r in out["rows"]]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates)), "同一根 K 不能出现两次"


def test_same_day_in_both_sources_is_deduped(monkeypatch) -> None:
    """上游边界与本地首根撞在同一天时，保留本地那份（唯一真相优先）。"""
    head = _bars([("2024-09-02", 1.08), ("2025-05-07", 99.0)])
    monkeypatch.setattr(
        api.stock_history_fill, "fill_head",
        lambda *a, **k: (head, {"attempted": True, "fetched_rows": 2, "error": None}),
    )
    out = api.get_history(_req(_Repo(LOCAL)), symbol="600340.SH", start_date="2024-09-01",
                          end_date="2026-11-13", fill=True)
    row = [r for r in out["rows"] if r["date"] == "2025-05-07"]
    assert len(row) == 1
    assert row[0]["close"] == 2.48, "冲突日必须取本地值，不能被上游覆盖"


def test_upstream_failure_returns_local_and_reports_error(monkeypatch) -> None:
    monkeypatch.setattr(
        api.stock_history_fill, "fill_head",
        lambda *a, **k: (pl.DataFrame(), {"attempted": True, "error": "ReadTimeout: timed out"}),
    )
    out = api.get_history(_req(_Repo(LOCAL)), symbol="600340.SH", start_date="2024-09-01",
                          end_date="2026-11-13", fill=True)
    assert out["upstream"]["error"].startswith("ReadTimeout")
    assert [s["source"] for s in out["segments"]] == ["local"]
    assert out["rows"][0]["date"] == "2025-05-07"


def test_no_local_rows_fills_whole_range(monkeypatch) -> None:
    """本地一根都没有（退市/未同步）时，整段都来自上游，且标注为 upstream。"""
    head = _bars([("2024-09-02", 1.08), ("2024-09-03", 1.1)])
    monkeypatch.setattr(
        api.stock_history_fill, "fill_head",
        lambda *a, **k: (head, {"attempted": True, "fetched_rows": 2, "error": None}),
    )
    out = api.get_history(_req(_Repo(pl.DataFrame())), symbol="600340.SH",
                          start_date="2024-09-01", end_date="2026-11-13", fill=True)
    assert out["coverage"]["local_first"] is None
    assert [s["source"] for s in out["segments"]] == ["upstream"]
    assert len(out["rows"]) == 2


def test_unknown_symbol_is_flagged(monkeypatch) -> None:
    """维表里查不到 ⇒ known=False。前端据此区分"票不存在"与"这段没K线"，
    否则用户会被"换一段区间"反复劝退（换了也没用）。"""
    class _NoInst(_Repo):
        def get_instruments_asset(self, asset_type):
            return pl.DataFrame({"symbol": [], "name": []})

    monkeypatch.setattr(
        api.stock_history_fill, "fill_head",
        lambda *a, **k: (pl.DataFrame(), {"attempted": True, "error": None}),
    )
    out = api.get_history(_req(_NoInst(pl.DataFrame())), symbol="999999.SH",
                          start_date="2024-09-01", end_date="2026-11-13", fill=True)
    assert out["known"] is False
    assert out["name"] is None
