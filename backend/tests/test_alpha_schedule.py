"""主动 Alpha 搜索调度的判据测试。

这个模块只做两件事：滚动窗口、到点触发。所以测试也只守两条线：

1. **窗口不许重叠，且确认期终点必须就是 as_of**。
   重叠 = 确认期用的是发现期见过的数据，那「确认」二字没有意义。
   终点错一天 = 隔一天就把同一天的数据当新证据。
2. **不许重复触发**。同一 claim 只能起一次后台任务；两个线程写同一个
   `runs/` 目录会让 report.json 互相覆盖，而覆盖后的报告看起来同样「正常」。

跑搜索本身不在这里测（那是 `test_alpha_search.py` 的范围，且要分钟级）。
这里把 `_run_claim` 换成记录器 —— 测的是「有没有点火」，不是「火烧得怎么样」。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

#: 一个「装配好了」的 app_state：调度只要求 `repo` 存在。
READY = SimpleNamespace(repo=object())

import pytest

from app.services import alpha_schedule as mod
from app.services.alpha_search import AlphaSearchConfig


@pytest.fixture(autouse=True)
def reset_active(monkeypatch):
    """每个用例从「没有任务在跑」开始。

    `_ACTIVE` 是模块级单例；不重置的话，上一个用例起的（假）线程会留在里面，
    后续用例会收到 `already_running` —— 那是测试之间的耦合，不是被测行为。
    """
    monkeypatch.setattr(mod, "_ACTIVE", {
        "run_id": None, "started_at": None, "thread": None,
    })


@pytest.fixture(autouse=True)
def reset_active(monkeypatch):
    """每个用例从「没有任务在跑」开始。

    `_ACTIVE` 是模块级单例；不重置的话，上一个用例起的（假）线程会留在里面，
    后续用例会收到 `already_running` —— 那是测试之间的耦合，不是被测行为。
    """
    monkeypatch.setattr(mod, "_ACTIVE", {
        "run_id": None, "started_at": None, "thread": None,
    })


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    return tmp_path


@pytest.fixture()
def fired(monkeypatch):
    """把后台执行体换成记录器，返回被点火的 claim 列表。"""
    calls: list[tuple[str, date]] = []

    def _fake(app_state, claim, as_of):
        calls.append((claim, as_of))

    monkeypatch.setattr(mod, "_run_claim", _fake)
    return calls


def test_滚动窗口不重叠且止于as_of():
    as_of = date(2026, 11, 18)
    cfg = mod.rolling_config(as_of, max_formulas=12)
    assert isinstance(cfg, AlphaSearchConfig)
    # 确认期的最后一天就是 as_of：晚一天就是把未来算进「确认」
    assert cfg.confirmation_end == as_of
    # 发现期必须严格早于确认期（同一天也算重叠）
    assert cfg.discovery_end < cfg.confirmation_start
    # 两段都要够长，否则 IC 的方差大到自己就是噪声
    assert (cfg.discovery_end - cfg.discovery_start).days >= 60
    assert (cfg.confirmation_end - cfg.confirmation_start).days >= 60


def test_滚动窗口随as_of移动而不是常量():
    """硬编码窗口的搜索只能跑一次：第二次跑出的「确认」仍在同一段历史上。

    **跨 run 的窗口重叠是正常的，不算泄漏**：6 个月确认期 + 5 个月间隔必然重叠
    一个月。禁止的是同一次运行内确认期用到发现期的数据（那条由
    `discovery_end < confirmation_start` 守）。重叠的代价体现在多重检验上 ——
    每次滚动的 trial_key 不同，全部计入 DSR 分母，所以重叠会让门槛越来越难过，
    这正是应该付的代价。
    """
    a = mod.rolling_config(date(2026, 6, 30), max_formulas=6)
    b = mod.rolling_config(date(2026, 11, 18), max_formulas=6)
    assert b.confirmation_start > a.confirmation_start
    assert b.confirmation_end > a.confirmation_end
    assert b.discovery_start > a.discovery_start


@pytest.mark.parametrize("day", [
    date(2026, 1, 5), date(2026, 2, 28), date(2026, 6, 30), date(2026, 12, 31),
])
def test_任意as_of都通过校验(day):
    cfg = mod.rolling_config(day, max_formulas=3)
    assert cfg.confirmation_end == day


def test_claim按自然月唯一():
    assert mod.schedule_claim(date(2026, 11, 1)) == mod.schedule_claim(date(2026, 11, 28))
    assert mod.schedule_claim(date(2026, 11, 30)) != mod.schedule_claim(date(2026, 12, 1))


def test_关闭时不点火(data_dir, fired, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_alpha_search_schedule", lambda: {
        "enabled": False, "alpha_search_day": 1,
        "alpha_search_profile": "balanced", "alpha_search_max_formulas": 6,
    })
    out = mod.run_scheduled_alpha_search(READY, now=datetime(2026, 11, 18, 18, 0))
    assert out["status"] == "disabled"
    assert fired == []


def test_未到日子不点火(data_dir, fired, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_alpha_search_schedule", lambda: {
        "enabled": True, "alpha_search_day": 20,
        "alpha_search_profile": "balanced", "alpha_search_max_formulas": 6,
    })
    out = mod.run_scheduled_alpha_search(READY, now=datetime(2026, 11, 18, 18, 0))
    assert out["status"] == "day_mismatch"
    assert out["target"] == 20
    assert fired == []


def test_到点只点火一次(data_dir, fired, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_alpha_search_schedule", lambda: {
        "enabled": True, "alpha_search_day": 1,
        "alpha_search_profile": "balanced", "alpha_search_max_formulas": 6,
    })
    now = datetime(2026, 11, 18, 18, 0)
    first = mod.run_scheduled_alpha_search(READY, now=now)
    second = mod.run_scheduled_alpha_search(READY, now=now)
    assert first["status"] == "started"
    assert second["status"] == "already_claimed"
    assert len(fired) == 1
    assert fired[0][0] == "alpha-2026-11"
    assert fired[0][1] == date(2026, 11, 18)


def test_失败过一次后当月不再重试(data_dir, fired, monkeypatch):
    """重试要有理由：静默重试会把「每次都失败」变成「每月跑两次都失败」。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_alpha_search_schedule", lambda: {
        "enabled": True, "alpha_search_day": 1,
        "alpha_search_profile": "balanced", "alpha_search_max_formulas": 6,
    })
    state = {"claims": {"alpha-2026-11": {"status": "failed", "error": "boom"}}}
    monkeypatch.setattr(mod, "read_state", lambda d: state)
    monkeypatch.setattr(mod, "write_state", lambda d, s: state.update(s))

    out = mod.run_scheduled_alpha_search(READY, now=datetime(2026, 11, 18, 18, 0))
    assert out["status"] == "already_claimed"
    assert out["state"] == "failed"
    assert fired == []


def test_手工触发不看开关(data_dir, fired, monkeypatch):
    """人点了按钮就是要跑；再用开关拒掉只会让人去改配置文件绕过。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_alpha_search_schedule", lambda: {
        "enabled": False, "alpha_search_day": 1,
        "alpha_search_profile": "balanced", "alpha_search_max_formulas": 6,
    })
    out = mod.run_alpha_search_now(READY, as_of=date(2026, 11, 18))
    assert out["status"] == "started"
    assert out["claim"].startswith("alpha-manual-")
    assert fired == [(out["claim"], date(2026, 11, 18))]


def test_手工claim不占用月度claim(data_dir, fired, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_alpha_search_schedule", lambda: {
        "enabled": True, "alpha_search_day": 1,
        "alpha_search_profile": "balanced", "alpha_search_max_formulas": 6,
    })
    manual = mod.run_alpha_search_now(READY, as_of=date(2026, 11, 18))
    assert manual["status"] == "started"
    assert manual["claim"].startswith("alpha-manual-")
    # 手工跑的时候不允许并发第二个（同一时间只一个）；模拟它跑完后本月自动 claim 仍可用
    assert mod.run_scheduled_alpha_search(
        READY, now=datetime(2026, 11, 18, 18, 0))["status"] == "already_running"
    mod._ACTIVE["run_id"] = None
    auto = mod.run_scheduled_alpha_search(READY, now=datetime(2026, 11, 18, 18, 0))
    assert auto["status"] == "started"
    assert auto["claim"] == "alpha-2026-11"


def test_已在跑时拒绝第二个(data_dir, fired, monkeypatch):
    monkeypatch.setattr(mod, "_ACTIVE", {
        "run_id": "alpha-2026-10", "started_at": "2026-10-01T18:00:00", "thread": None,
    })
    out = mod.run_alpha_search_now(READY, as_of=date(2026, 11, 18))
    assert out["status"] == "already_running"
    assert out["claim"] == "alpha-2026-10"
    assert fired == []


def test_状态文件给出最近claim与是否在跑(data_dir):
    mod.write_state(data_dir, {"claims": {
        "alpha-2026-10": {"status": "done", "run_id": "20261001-1"},
        "alpha-2026-11": {"status": "failed", "error": "boom"},
    }})
    status = mod.schedule_status(data_dir)
    assert status["latest_claim"] == "alpha-2026-11"
    assert status["latest"]["status"] == "failed"
    # 状态与「现在有没有线程在跑」是两件事，必须分开给
    assert status["running"] is False


def test_状态文件损坏不当成没有claim(data_dir):
    """读坏了也返回结构，不抛：页面要能说「读不出」，而不是 500 之后什么都看不到。"""
    path = mod.state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    status = mod.schedule_status(data_dir)
    assert status["latest_claim"] is None
    assert status["running"] is False


def test_repo缺失时不写claim(data_dir, fired):
    """装配失败不能吃掉本月名额。

    原先只在后台线程里检查 `repo`：claim 先落盘、线程随后失败、状态变成 failed，
    而 failed 同样霸占本月 —— 于是「依赖没装好」被记成「这个月已经跑过了」。
    2026-11-18 真实踩到：一个没 mock 的测试用 SimpleNamespace() 触发了调度，
    把 alpha-2026-11 写成了 failed。
    """
    out = mod.run_alpha_search_now(SimpleNamespace(), as_of=date(2026, 11, 18))
    assert out["status"] == "not_ready"
    assert fired == []
    # 状态文件里一条 claim 都不该有
    assert mod.read_state(data_dir).get("claims") == {}


def test_repo缺失时不写claim(data_dir, fired):
    """装配失败不能吃掉本月名额。

    原先只在后台线程里检查 `repo`：claim 先落盘、线程随后失败、状态变成 failed，
    而 failed 同样霸占本月 —— 于是「依赖没装好」被记成「这个月已经跑过了」。
    2026-11-18 真实踩到：一个没 mock 的测试用 SimpleNamespace() 触发了调度，
    把 alpha-2026-11 写成了 failed。
    """
    out = mod.run_alpha_search_now(SimpleNamespace(), as_of=date(2026, 11, 18))
    assert out["status"] == "not_ready"
    assert fired == []
    # 状态文件里一条 claim 都不该有
    assert mod.read_state(data_dir).get("claims") == {}
