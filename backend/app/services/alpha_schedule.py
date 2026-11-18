"""主动 Alpha 搜索的调度与滚动窗口。

`services/alpha_search.py` 本身就是完整的搜索器：生成候选 → 发现期闸门 →
确认期闸门 → 新颖性去重 → 试验台账 → DSR。缺的从来不是能力，是**触发**：
它的四个日期是硬编码的（2025-09-01~2026-04-30 / 2026-05-01~2026-11-06），
硬编码窗口的搜索只能跑一次——第二次跑出来的「确认」仍然落在同一段历史上。

这个模块只补两件事，别的一概不碰：

1. **滚动窗口**（`rolling_config`）：给定 as_of，推出「确认期 = 最近 N 天、
   发现期 = 紧邻其前的 M 天」。窗口是 as_of 的函数，不是常量。
2. **触发与防重**（`run_scheduled_alpha_search`）：月度、单一 claim、后台线程。
   搜索是分钟级的批量回测，放同步路径会把收盘管道堵死。

频率为什么是**月度**而不是周度：每次窗口滚动都产生一批新的 `trial_key`，
它们全部计入 DSR 的多重检验分母（`_prior_trial_keys`）。周度滚动 = 一年 52 批
新试验，只会让通过门槛越来越难，而每周新进来的 5 天数据并不能提供 52 倍的证据。
月度的节奏既在攒前向样本，又不至于把噪声当成新发现来罚自己。

纪律：**这个模块不写任何「结论」**。确认/拒绝的判断全在 `alpha_search`
和 `run_alpha_search` 落盘的 `report.json` 里，这里只负责让它按时发生。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.alpha_search import AlphaSearchConfig

logger = logging.getLogger(__name__)

#: 确认期长度。6 个月 ≈ 120 个交易日：够算 IC 与分组收益，又不至于让「最近」
#: 变成一个两年前的词。
CONFIRM_DAYS = 183
#: 发现期长度。8 个月 ≈ 160 个交易日，紧跟确认期之前，不重叠。
DISCOVERY_DAYS = 244
#: 调度默认跑多少个公式。**实测 0.8 秒/公式**（`analysis/_probe_alpha_cost.py`，
#: 6 公式 4.8s，含引擎冷启动），360 个 ≈ 5 分钟 —— 月度跑一次的成本可以忽略，
#: 所以不缩减候选池：缩了之后「本周没有发现」就分不清是市场没有、还是没搜到。
SCHEDULE_MAX_FORMULAS = 360
#: 台账重算 DSR 时用的试验数上限，防止跑久之后 trial 表无限膨胀。
MAX_TRIALS_FOR_DSR = 5000

_STATE_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_ACTIVE: dict[str, Any] = {"run_id": None, "started_at": None, "thread": None}


def rolling_config(
    as_of: date,
    *,
    profile: str = "balanced",
    max_formulas: int = SCHEDULE_MAX_FORMULAS,
    **overrides: Any,
) -> AlphaSearchConfig:
    """以 `as_of` 为确认期终点，推出两段互不重叠的窗口。

    `as_of` 是**确认期的最后一天**，不是「今天」的另一种写法：确认期必须全部
    落在已知历史里，否则「确认」用到的就是未来数据。调用方传最近一个交易日。
    """
    confirm_end = as_of
    confirm_start = as_of - timedelta(days=CONFIRM_DAYS)
    discovery_end = confirm_start - timedelta(days=1)
    discovery_start = discovery_end - timedelta(days=DISCOVERY_DAYS)
    config = AlphaSearchConfig(
        profile=profile,
        discovery_start=discovery_start,
        discovery_end=discovery_end,
        confirmation_start=confirm_start,
        confirmation_end=confirm_end,
        max_formulas=max_formulas,
        **overrides,
    )
    config.validate()
    return config


def state_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "research" / "alpha_search" / "schedule.json"


def read_state(data_dir: Path | str) -> dict[str, Any]:
    path = state_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"claims": {}}
    return data if isinstance(data, dict) else {"claims": {}}


def write_state(data_dir: Path | str, state: dict[str, Any]) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    tmp.replace(path)


def schedule_claim(day: date) -> str:
    """月度 claim：同一个月只跑一次，跑失败也不自动重试（重试要有理由）。"""
    return f"alpha-{day.year}-{day.month:02d}"


def _beijing_today(now: datetime | None) -> date:
    from app.market_time import cn_today

    return cn_today() if now is None else now.date()


def run_scheduled_alpha_search(
    app_state: Any,
    *,
    now: datetime | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """到点就在后台起一次搜索；**不阻塞调用方**。

    返回值描述「这次触发发生了什么」，不是「搜索的结果」——结果在
    `report.json` 里，前端从 `latest_alpha_run()` 读。
    """
    from app.config import settings
    from app.services import preferences

    cfg = preferences.get_alpha_search_schedule()
    if not cfg["enabled"]:
        return {"status": "disabled"}

    day = _beijing_today(now)
    if day.day < cfg["alpha_search_day"]:
        return {"status": "day_mismatch", "day": day.day, "target": cfg["alpha_search_day"]}

    data_dir = Path(settings.data_dir)
    claim = schedule_claim(day)
    target = as_of or day
    return _start_claim(app_state, data_dir, claim, target)


def run_alpha_search_now(
    app_state: Any,
    *,
    as_of: date | None = None,
    claim: str | None = None,
) -> dict[str, Any]:
    """手工触发一次，不看 `enabled` 与日期窗口。

    为什么手工路径不检查开关：开关管的是「自动跑不跑」。人站在这里点了一次，
    他就是要跑；再拿开关拒掉他，只会让人去改配置文件绕过，反而更危险。

    claim 加时间戳后缀，不占月度 claim —— 手工跑过不该让这个月的自动跑消失。
    """
    from app.config import settings

    data_dir = Path(settings.data_dir)
    target = as_of or _last_session(data_dir) or _beijing_today(None)
    if claim is None:
        claim = "alpha-manual-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    return _start_claim(app_state, data_dir, claim, target)


def _last_session(data_dir: Path) -> date | None:
    """as_of 必须是交易日：确认期的最后一天落在周末，会让「确认」多含两天非交易数据。"""
    try:
        from app.decision import bars

        sessions = bars.sessions(data_dir)
        return date.fromisoformat(sessions[-1]) if sessions else None
    except Exception:  # noqa: BLE001 — 拿不到就退回今天，由调用方决定怎么用
        logger.warning("alpha search: last session unavailable, falling back to today")
        return None


def _start_claim(app_state: Any, data_dir: Path, claim: str, target: date) -> dict[str, Any]:
    """起一个后台 claim。已在跑或本周已 claim 过就拒绝，不排队、不覆盖。

    **`repo` 缺失必须在写 claim 之前就拒绝**。原先只在后台线程里检查，后果是：
    claim 先落进状态文件、线程随后失败、状态变成 `failed` —— 而 `failed` 同样
    霸占本月的名额，于是「依赖没装配好」被记成「这个月的搜索已经跑过了」，
    下个月之前再也不会有第二次机会。2026-11-18 实测踩到：一个没 mock 的测试
    用 `SimpleNamespace()` 触发了调度，把 `alpha-2026-11` 写成了 failed。
    """
    if getattr(app_state, "repo", None) is None:
        return {"status": "not_ready", "claim": claim,
                "error": "app.state.repo 未初始化：不写 claim，否则本月名额会被一次装配失败吃掉"}

    with _STATE_LOCK:
        state = read_state(data_dir)
        claims = state.setdefault("claims", {})
        existing = claims.get(claim)
        if existing is not None:
            return {"status": "already_claimed", "claim": claim,
                    "state": existing.get("status")}

    with _RUN_LOCK:
        if _ACTIVE.get("run_id") is not None:
            return {"status": "already_running", "claim": _ACTIVE["run_id"],
                    "started_at": _ACTIVE.get("started_at")}

    with _STATE_LOCK:
        state = read_state(data_dir)
        claims = state.setdefault("claims", {})
        claims[claim] = {
            "status": "running",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "as_of": target.isoformat(),
        }
        write_state(data_dir, state)

    thread = threading.Thread(
        target=_run_claim,
        args=(app_state, claim, target),
        name=f"alpha-search-{claim}",
        daemon=True,
    )
    with _RUN_LOCK:
        _ACTIVE["run_id"] = claim
        _ACTIVE["started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _ACTIVE["thread"] = thread
    thread.start()
    return {"status": "started", "claim": claim, "as_of": target.isoformat()}


def _run_claim(app_state: Any, claim: str, as_of: date) -> None:
    """后台执行体。任何异常都落进状态文件——静默失败等于「从没跑过」。"""
    from app.config import settings
    from app.services import preferences

    data_dir = Path(settings.data_dir)
    ok = False
    try:
        from app.backtest.engine import BacktestEngine
        from app.services.alpha_search import run_alpha_search

        repo = getattr(app_state, "repo", None)
        if repo is None:
            raise RuntimeError("alpha search requires app.state.repo")
        engine = getattr(app_state, "backtest_engine", None)
        if engine is None:
            engine = BacktestEngine(repo)
            app_state.backtest_engine = engine

        cfg = preferences.get_alpha_search_schedule()
        config = rolling_config(
            as_of,
            profile=cfg["alpha_search_profile"],
            max_formulas=cfg["alpha_search_max_formulas"],
        )
        report = run_alpha_search(
            engine, config,
            output_dir=data_dir / "research" / "alpha_search",
        )
        counts = report.get("counts") or {}
        ok = True
        payload = {
            "status": "done",
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": report.get("run_id"),
            "counts": counts,
        }
    except Exception as exc:  # noqa: BLE001 — 失败要留下可见的原因
        logger.exception("scheduled alpha search failed")
        payload = {
            "status": "failed",
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        with _RUN_LOCK:
            if _ACTIVE.get("run_id") == claim:
                _ACTIVE["run_id"] = None
                _ACTIVE["thread"] = None
    with _STATE_LOCK:
        state = read_state(data_dir)
        claims = state.setdefault("claims", {})
        merged = {**(claims.get(claim) or {}), **payload}
        claims[claim] = merged
        write_state(data_dir, state)
    logger.info("scheduled alpha search %s: %s (ok=%s)", claim, payload["status"], ok)


def schedule_status(data_dir: Path | str) -> dict[str, Any]:
    """给前端看的调度状态：最近一次 claim + 是否正在跑。"""
    state = read_state(data_dir)
    claims = state.get("claims") or {}
    latest_key = max(claims) if claims else None
    latest = claims.get(latest_key) if latest_key else None
    with _RUN_LOCK:
        active = _ACTIVE.get("run_id")
    return {
        "latest_claim": latest_key,
        "latest": latest,
        "running": active is not None,
        "running_claim": active,
    }
