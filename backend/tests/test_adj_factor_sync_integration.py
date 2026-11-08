"""同步侧集成：生产入口 → 回执文件 → 结算门判据，一条链走完。

单元测试已经分别钉住了三段：`sync_adj_factor` 的五个出口
（`test_adj_factor_receipt.py`）、回执读侧合同（`test_decision_receipt.py`）、
覆盖读数（`test_decision_event_coverage.py`）。三段都绿仍然答不出这一轮的问题：
**生产调用链有没有把 `scope` 传对，写出来的那行回执能不能被读侧直接拿走用。**

三种失效只在跨层时才暴露：

| 失效方式 | 三段单元测试 | 这里 |
|---|---|---|
| 某个调用点漏传 `scope` | 全绿（默认 `symbols` 只是不给覆盖，不报错） | 门判 False |
| 区间口径变了（如 from/to 取自同一天） | 写侧自己测自己，跟着一起变 | 门判 False |
| 回执落错文件 / 错资产类型 | 全绿（各测各的文件） | 门读错表 |

所以这里不复制写侧或读侧的私有逻辑做断言，只走公开入口：
`sync_adj_factor` / `run_adj_factor_sync` 写，`receipt.window_covered` 读。
门判 True 才算这条链真的通了——它是 `window_uncovered` 将来要接的那个判据。
"""
from __future__ import annotations

import ast
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.decision import receipt
from app.jobs import daily_pipeline
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

A = "000001.SZ"
B = "600519.SH"
START = datetime(2024, 1, 1)
END = datetime(2024, 12, 31)
EVENT = {"trade_date": date(2024, 6, 19), "ex_factor": 1.1}


def _capset() -> CapabilitySet:
    return CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=50, rpm=30)})


def _pin_tickflow(monkeypatch, ex_factors) -> None:
    """把 SDK 换成桩，并把 provider 钉成 tickflow。

    不钉 provider 的话，这台机器的 `preferences.json` 会把请求导到自定义源，
    桩被绕过、测试打到真实数据上——那就不再是在测这条链了。
    """
    monkeypatch.setattr(kline_sync, "preferences",
                        SimpleNamespace(get_adj_factor_provider=lambda: "tickflow"))
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda i, rpm: None)
    monkeypatch.setattr(kline_sync, "get_client",
                        lambda: SimpleNamespace(klines=SimpleNamespace(ex_factors=ex_factors)))


def _lines(tmp_path: Path, asset_type: str = "stock") -> list[dict]:
    path = receipt.receipt_path(tmp_path, asset_type)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_what_the_sync_writes_is_exactly_what_the_settlement_gate_reads(tmp_path, monkeypatch):
    """同步 → 落表 → 回执 → 门判 True，且门在区间外仍然说「不知道」。

    反向断言（区间外必须是 False）不是陪衬：只会答 True 的判据等于没有判据，
    它和「回执缺省不给覆盖」在测试上长得一样。
    """
    _pin_tickflow(monkeypatch, lambda chunk, **kw: {s: [EVENT] for s in chunk})
    repo = KlineRepository(DataStore(tmp_path))

    written, affected = kline_sync.sync_adj_factor(
        [A, B], repo, _capset(), start_time=START, end_time=END, scope="universe",
    )
    assert written == 2 and sorted(affected) == sorted([A, B])

    [row] = _lines(tmp_path)
    assert row["outcome"] == "ok" and row["scope"] == "universe"
    assert (row["requested_from"], row["requested_to"]) == ("2024-01-01", "2024-12-31")

    # `store_*` 必须对得上盘上那张表，而不是对得上桩：读数取错了，事后核对就退化成猜。
    table = pl.read_parquet(tmp_path / "adj_factor" / "all.parquet")
    assert row["store_created"] is True
    assert row["store_rows"] == table.height == 2
    assert row["store_first"] == "2024-06-19" and row["store_last"] == "2024-06-19"

    # 门：窗口内的交易日全部落在请求区间里。
    sessions = ["2024-01-02", "2024-06-19", "2024-06-20", "2024-12-31", "2025-01-02"]
    assert receipt.window_covered(tmp_path, A, "2024-01-02", "2024-06-20", sessions)
    # 门：跨过请求上界就不再声称覆盖——这条是判据的另一半。
    assert not receipt.window_covered(tmp_path, A, "2024-12-31", "2025-01-02", sessions)
    assert receipt.covered_through(tmp_path, A, "2024-12-31") == "2024-12-31"


def test_the_standalone_job_declares_universe_and_the_gate_accepts_it(tmp_path, monkeypatch):
    """`run_adj_factor_sync` 这一层必须自己声明 universe，门才肯给覆盖。

    这一条要的是**参数真的从任务层传到了写侧**：不替换 `sync_adj_factor`，而是
    包一层记录后原样转发，让真实函数去写回执。漏传 `scope` 时它不报错、不抛异常，
    只是默认成 `symbols`，回执照写、`outcome` 仍是 `ok`——唯一的差别是门判 False，
    所以要在这里断言门，而不是断言参数。
    """
    real = kline_sync.sync_adj_factor
    seen: dict = {}

    def spy(*args, **kwargs):
        seen["scope"] = kwargs.get("scope")
        return real(*args, **kwargs)

    monkeypatch.setattr(kline_sync, "sync_adj_factor", spy)
    _pin_tickflow(monkeypatch, lambda chunk, **kw: {s: [EVENT] for s in chunk})
    monkeypatch.setattr(daily_pipeline, "_resolve_universe",
                        lambda capset, repo=None: [A, B])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda repo, name: None)
    monkeypatch.setattr(daily_pipeline._prefs, "get_adj_factor_provider", lambda: "tickflow")

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           earliest_daily_date=lambda: date(2026, 1, 5))
    today = date.today().isoformat()
    daily_pipeline.run_adj_factor_sync(repo, _capset())

    assert seen["scope"] == "universe"
    [row] = _lines(tmp_path)
    assert row["requested_from"] == "2026-01-05"
    # 上界是「现在」而不是某个固定日期：写死窗口的同步会在几天后悄悄变成假覆盖。
    assert row["requested_to"] >= today

    sym = A
    assert receipt.covered_through(tmp_path, sym, "2026-01-06") == row["requested_to"]


def test_a_window_without_a_lower_bound_writes_no_receipt_and_the_gate_stays_closed(
        tmp_path, monkeypatch):
    """没有下界 = 没有区间 = 不写回执；门因此仍然说「不知道」。

    全历史同步（本地还没有任何日K时 `start=None`）在回执的语义里表达不出来：
    `requested_from` 只接受日期。硬填一个哨兵日期会把「我问过全部」写成一个
    具体的谎，所以宁可不写。

    代价要写在明处：这条路径**修不好 `window_uncovered`**。用独立同步按钮切过
    数据源之后，挂起的结算不会因此解锁，因为门看不到任何覆盖。今天没有消费方，
    这是潜在问题；一旦换成回执判据，它会变成一个看起来像「同步没跑」的死结。
    """
    _pin_tickflow(monkeypatch, lambda chunk, **kw: {s: [EVENT] for s in chunk})
    monkeypatch.setattr(daily_pipeline, "_resolve_universe",
                        lambda capset, repo=None: [A])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda repo, name: None)
    monkeypatch.setattr(daily_pipeline._prefs, "get_adj_factor_provider", lambda: "tickflow")

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           earliest_daily_date=lambda: None)
    daily_pipeline.run_adj_factor_sync(repo, _capset())

    assert (tmp_path / "adj_factor" / "all.parquet").is_file(), "数据照常入库"
    assert not receipt.receipt_path(tmp_path).exists(), "没有区间就不该有回执"
    assert receipt.covered_through(tmp_path, A, "2026-01-06") is None


_APP = Path(__file__).resolve().parents[1] / "app"
_SYNC_CALLS = ("sync_adj_factor", "sync_etf_adj_factor")


def test_every_production_call_site_declares_a_scope():
    """静态守卫：新增的调用点必须显式声明 `scope`。

    `run_now` 的两处调用和 `extend_history` 的一处要跑完整个盘后管道才够得着，
    行为测试覆盖不到它们。而漏传的后果是静默的（默认 `symbols`），加一个第四处
    调用点时没人会想起来——所以这里直接读源码。

    它只证明「有显式声明」，不证明值是对的；值由上面两条行为测试守。
    """
    missing: list[str] = []
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in _SYNC_CALLS:
                continue
            if "scope" not in {kw.arg for kw in node.keywords}:
                missing.append(f"{path.relative_to(_APP.parent).as_posix()}:{node.lineno}")
    assert not missing, (
        "这些同步调用没有声明 scope（默认 symbols 会让回执永远不给覆盖，且不报错）："
        + ", ".join(missing)
    )
