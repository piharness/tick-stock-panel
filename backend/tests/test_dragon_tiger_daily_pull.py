"""龙虎榜每日增量落盘测试 (不依赖真实网络)。

守的是这条链路上最容易复发的一个坑: **当日不落盘**。
在线路径 get_dragon_tiger 只在 trade_date < today 时写缓存, 拉「今天」永远不写,
所以「每天都跑、每天都绿, 但今天永远没缓存」。这组测试钉住三件事:
- pull_latest 在 actual == today 时**确实**写出当日缓存 (在线路径不会)
- 服务端未发布时 is_expected_day 为 False —— 判据不能是 state
- 缓存写失败必须报 store_failed, 不能当成功

日期用 2026-08-26/27/28 (与 test_dragon_tiger.py 同一批绝对日期)。
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from app.jobs import daily_pipeline as dp
from app.plugins.fuyao.client import FuyaoError
from app.services import dragon_tiger as dt

TODAY = date(2026, 8, 28)


def _mk_days(data_dir: Path, *days: str) -> None:
    for d in days:
        (data_dir / "kline_daily" / f"date={d}").mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    _mk_days(tmp_path, "2026-08-26", "2026-08-27", "2026-08-28")
    return tmp_path


class _FakeProvider:
    """serve: 服务端「最近已发布交易日」; fail=True 时三榜全抛。"""

    def __init__(self, serve: str = "2026-08-28", fail: bool = False):
        self.serve = serve
        self.fail = fail
        self.calls: list[tuple] = []

    def dragon_tiger(self, board_type: str, date: str | None) -> dict:
        self.calls.append((board_type, date))
        if self.fail:
            raise FuyaoError("code=3002: 未就绪")
        return {
            "trade_date": self.serve,
            "count": 1,
            "stock_count": 1,
            "stock_items": [
                {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
                 "change": 0.05, "net_value": 1.5e8},
            ],
            "hot_money_items": [
                {"name": "宁波桑田路", "buying": 1.9e8,
                 "rows": [{"thscode": "600519.SH", "name": "贵州茅台",
                           "hot_money_item_net_value": 1.2e8}]},
            ],
        }


def _use_provider(monkeypatch, provider) -> _FakeProvider:
    monkeypatch.setattr(dt, "_provider", lambda: provider)
    return provider


def _cache(data_dir: Path, iso: str) -> Path:
    return data_dir / "dragon_tiger" / f"date={iso}.json"


# ---- pull_latest: 当日也要落盘 (在线路径不会) ----

def test_pull_latest_writes_today_cache(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider(serve="2026-08-28"))
    res = dt.pull_latest(data_dir, TODAY)

    assert res["state"] == "ok"
    assert res["is_expected_day"] is True
    assert res["stocks"] == 1 and res["seats"] == 1
    # 无显式日期 (交给服务端取最近已发布日), 且**当日缓存已存在**
    assert provider.calls == [("all", None), ("org", None), ("hot_money", None)]
    assert _cache(data_dir, "2026-08-28").exists()
    assert dt.has_cache(data_dir, TODAY)

    payload = json.loads(_cache(data_dir, "2026-08-28").read_text(encoding="utf-8"))
    assert payload["state"] == "ok"
    assert payload["trade_date"] == "2026-08-28"
    assert payload["all"]["stock_items"][0]["thscode"] == "600519.SH"


def test_online_path_still_does_not_cache_today(data_dir, monkeypatch):
    """对照: 同一个 provider, 走 get_dragon_tiger 不会产生当日缓存。

    这条不是重复覆盖, 而是把「为什么必须另开一条路径」钉在测试里 ——
    若将来有人把 pull_latest 合并回 get_dragon_tiger, 上面那条会跟着变绿。
    """
    _use_provider(monkeypatch, _FakeProvider(serve="2026-08-28"))
    monkeypatch.setattr(dt, "cn_today", lambda: TODAY)  # 2026-08-28 就是「今天」
    out = dt.get_dragon_tiger(data_dir, TODAY)

    assert out["state"] == "ok"
    assert not _cache(data_dir, "2026-08-28").exists()


def test_pull_latest_flags_unpublished_server_date(data_dir, monkeypatch):
    """服务端仍返回上一交易日 → state 依旧 ok, 但 is_expected_day=False。"""
    _use_provider(monkeypatch, _FakeProvider(serve="2026-08-27"))
    res = dt.pull_latest(data_dir, TODAY)

    assert res["state"] == "ok"
    assert res["is_expected_day"] is False
    assert res["trade_date"] == "2026-08-27"
    assert _cache(data_dir, "2026-08-27").exists()
    assert not _cache(data_dir, "2026-08-28").exists()


def test_pull_latest_without_provider(data_dir, monkeypatch):
    monkeypatch.setattr(dt, "_provider", lambda: None)
    assert dt.pull_latest(data_dir, TODAY)["state"] == "source_unavailable"


def test_pull_latest_fetch_failure_is_not_ok(data_dir, monkeypatch):
    _use_provider(monkeypatch, _FakeProvider(fail=True))
    res = dt.pull_latest(data_dir, TODAY)
    assert res["state"] == "no_data"
    assert not _cache(data_dir, "2026-08-28").exists()


def test_pull_latest_store_failure_is_reported(data_dir, monkeypatch):
    """拉到了但写不下去 ⇒ store_failed, 不能吞成 ok (漏一天不可回补)。"""
    _use_provider(monkeypatch, _FakeProvider())

    def boom(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(dt, "_store_cache", boom)
    res = dt.pull_latest(data_dir, TODAY)
    assert res["state"] == "store_failed"
    assert "disk full" in res["message"]


# ---- 调度任务: 幂等 + 不抛 ----

class _Repo:
    def __init__(self, data_dir: Path):
        self.store = type("S", (), {"data_dir": data_dir})()


@pytest.fixture()
def trading(monkeypatch):
    """把 job 的交易日判定钉成「是交易日」, 避免测试触发真实探测链。"""
    monkeypatch.setattr(dp, "_is_trading_day", lambda: True)


def test_job_skips_when_today_cached(data_dir, trading, monkeypatch):
    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)
    _cache(data_dir, "2026-08-28").parent.mkdir(parents=True, exist_ok=True)
    _cache(data_dir, "2026-08-28").write_text("{}", encoding="utf-8")
    provider = _use_provider(monkeypatch, _FakeProvider())

    dp._run_dragon_tiger_pull(_Repo(data_dir))
    assert provider.calls == []  # 不重复打接口


def test_job_pulls_today(data_dir, trading, monkeypatch):
    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)
    _use_provider(monkeypatch, _FakeProvider(serve="2026-08-28"))

    dp._run_dragon_tiger_pull(_Repo(data_dir))
    assert _cache(data_dir, "2026-08-28").exists()


def test_job_never_raises(data_dir, trading, monkeypatch):
    """任何异常都不得冒泡到调度器 (否则 job 会被 APScheduler 停用)。"""
    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)
    monkeypatch.setattr(dt, "pull_latest", boom)
    dp._run_dragon_tiger_pull(_Repo(data_dir))  # 不抛即通过


def test_job_skips_on_holiday(data_dir, monkeypatch):
    """休市日不空打接口, 也不该留下「今日未发布」的告警。"""
    monkeypatch.setattr("app.market_time.cn_today", lambda: TODAY)
    monkeypatch.setattr(dp, "_is_trading_day", lambda: False)
    provider = _use_provider(monkeypatch, _FakeProvider())

    dp._run_dragon_tiger_pull(_Repo(data_dir))
    assert provider.calls == []


# ---- 注册 ----

class _Sched:
    def __init__(self):
        self.jobs: list[dict] = []

    def add_job(self, fn, **kw):
        self.jobs.append({"fn": fn, **kw})


def test_registers_two_slots_after_disclosure(data_dir):
    sched = _Sched()
    dp._register_dragon_tiger_jobs(sched, _Repo(data_dir))

    ids = [j["id"] for j in sched.jobs]
    assert ids == [dp.DRAGON_TIGER_JOB_ID, dp.DRAGON_TIGER_RETRY_JOB_ID]
    for j in sched.jobs:
        trig = j["trigger"]
        assert "mon-fri" in str(trig)
        assert str(trig.timezone) == "Asia/Shanghai"
    # 披露在 18:00 之后 —— 主跑必须晚于盘后管道 (默认 15:35)
    hours = [int(re.search(r"hour='(\d+)'", str(j["trigger"])).group(1))
             for j in sched.jobs]
    assert hours == [19, 20]


def test_start_scheduler_registers_the_jobs(data_dir, monkeypatch):
    """接线测试: 只注册了 helper 却没在 start_scheduler 里调用, 等于没接。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_pipeline_schedule", lambda: {"hour": 15, "minute": 35})
    monkeypatch.setattr(preferences, "get_instruments_schedule", lambda: {"hour": 9, "minute": 10})
    monkeypatch.setattr(preferences, "get_depth_finalize_time", lambda: {"hour": 15, "minute": 2})
    monkeypatch.setattr(preferences, "get_review_schedule",
                        lambda: {"enabled": False, "hour": 15, "minute": 40})

    sched = _Sched()
    sched.start = lambda: None
    monkeypatch.setattr(dp, "AsyncIOScheduler", lambda **kw: sched)

    dp.start_scheduler(_Repo(data_dir), capset=object())

    ids = [j["id"] for j in sched.jobs]
    assert dp.DRAGON_TIGER_JOB_ID in ids
    assert dp.DRAGON_TIGER_RETRY_JOB_ID in ids
