"""自动策略实验室战报: 守护 + 挖掘 + 名额 + 研究闭环的聚合只读视图 (P3)。"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import preferences
from app.services.guardian_store import read_combinations
from app.services.mining_jobs import MiningRunStoreError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest/lab", tags=["backtest"])


def _project_mining_run(store: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    summary = store.read_summary(str(manifest["run_id"]))
    return {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
        "algorithm_version": summary.get("algorithm_version"),
        "confidence": summary.get("confidence"),
        "candidate_count": summary.get("candidate_count"),
        "valid_fold_count": summary.get("valid_fold_count"),
        "refinement": summary.get("refinement"),
        "trials_used": summary.get("trials_used"),
    }


@router.get("/report")
def get_report(request: Request) -> dict[str, Any]:
    data_dir = request.app.state.repo.store.data_dir
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="strategy engine is unavailable")

    guardian_manager = getattr(request.app.state, "guardian_manager", None)
    guardian: dict[str, Any] = {"profiles": 0, "by_status": {}, "alerts": 0,
                                "last_run": None}
    try:
        from app.services.guardian_store import GuardianStore

        profiles = GuardianStore(data_dir).list_profiles()
    except Exception:  # noqa: BLE001 — 战报对档案读取失败降级为空统计
        profiles = []
    guardian["profiles"] = len(profiles)
    by_status: dict[str, int] = {}
    alerts = 0
    for profile in profiles:
        status = str(profile.get("status") or "pending")
        by_status[status] = by_status.get(status, 0) + 1
        checks = profile.get("checks") or []
        streak = 0
        for check in reversed(checks):
            if check.get("verdict") == "watch" and not check.get("error"):
                streak += 1
            else:
                break
        alerts += 1 if streak >= 2 else 0
    guardian["by_status"] = by_status
    guardian["alerts"] = alerts
    if guardian_manager is not None:
        try:
            runs = guardian_manager.store.list_runs(limit=5)
            guardian["last_run"] = (
                _project_guardian_run(runs[0]) if runs else None
            )
        except MiningRunStoreError:
            guardian["last_run"] = None

    mining_manager = getattr(request.app.state, "mining_manager", None)
    mining: dict[str, Any] = {"last_run": None}
    if mining_manager is not None:
        try:
            runs = mining_manager.store.list_runs(limit=5)
            mining["last_run"] = _project_mining_run(mining_manager.store, runs[0]) if runs else None
        except (MiningRunStoreError, ValueError):
            mining["last_run"] = None

    config = preferences.get_guardian_schedule()
    active_mined = sorted(
        str(meta.get("id"))
        for meta in engine.list_strategies()
        if str(meta.get("id", "")).startswith("mined_factor_")
    )
    combinations = read_combinations(data_dir) or {}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mining": mining,
        "guardian": guardian,
        "loop": _loop_block(request),
        "combinations": {
            "generated_at": combinations.get("generated_at"),
            "suggestions": combinations.get("suggestions") or [],
        },
        "slots": {
            "active_mined": len(active_mined),
            "active_mined_ids": active_mined,
            "limit": config["lab_mined_slot_limit"],
            "full": (
                config["lab_mined_slot_limit"] > 0
                and len(active_mined) >= config["lab_mined_slot_limit"]
            ),
        },
    }


def _loop_block(request: Request) -> dict[str, Any]:
    """研究闭环状态: 三页新鲜度 + 准入 + 联动。

    战报本身不因这一块失败而 500 —— 它是「附带诊断」, 不是核心数据;
    但失败必须显式呈现 (`available=false` + error), 不能伪装成「一切正常」。
    """
    from app.services.research_loop import build_loop_status

    try:
        status = build_loop_status(
            request.app.state.repo.store.data_dir,
            request.app.state.repo,
            request.app.state,
        )
    except Exception as exc:  # noqa: BLE001 — 见 docstring: 失败显式呈现
        logger.warning("lab loop status unavailable: %s", exc)
        return {"available": False, "error": str(exc)[:200]}
    return {"available": True, **status}


def _project_guardian_run(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
    }


# ── 主动 Alpha 搜索：调度状态 / 配置 / 手工触发 ────────────────────────────
#
# 三个端点分别对应三件不同的事，不要合并：
#   status  读「上次跑成什么样、现在有没有在跑」
#   config  改「下次什么时候跑、跑多大」
#   run     现在就跑一次（不看开关，见 alpha_schedule.run_alpha_search_now）


class AlphaSearchConfigUpdate(BaseModel):
    enabled: bool
    day: int = Field(ge=1, le=28)
    profile: str
    max_formulas: int = Field(ge=1, le=600)


class AlphaSearchRunRequest(BaseModel):
    #: 确认期终点。缺省 = 最近一个交易日（不传「今天」，周末会多算非交易日）。
    as_of: str | None = None


@router.get("/alpha-search/status")
def alpha_search_status(request: Request) -> dict[str, Any]:
    """调度状态 + 最近一次报告 + 当前配置。

    `latest` 为空不等于出错：它表示**从来没跑过**。两件事必须可分辨，
    所以 `schedule.latest_claim` 单独给出。
    """
    from app.services.alpha_schedule import schedule_status
    from app.services.research_loop import latest_alpha_run

    data_dir = request.app.state.repo.store.data_dir
    try:
        schedule = schedule_status(data_dir)
    except Exception as exc:  # noqa: BLE001 — 状态文件坏了不该让页面 500
        schedule = {"latest_claim": None, "latest": None, "running": False,
                    "error": f"{type(exc).__name__}: {exc}"}
    try:
        latest = latest_alpha_run(data_dir)
    except Exception as exc:  # noqa: BLE001
        latest = None
        schedule["latest_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "config": preferences.get_alpha_search_schedule(),
        "schedule": schedule,
        "latest": latest,
    }


@router.put("/alpha-search/config")
def update_alpha_search_config(req: AlphaSearchConfigUpdate) -> dict[str, Any]:
    try:
        return preferences.set_alpha_search_schedule(
            req.enabled, req.day, req.profile, req.max_formulas
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/alpha-search/run")
def trigger_alpha_search(
    request: Request,
    req: AlphaSearchRunRequest | None = None,
) -> dict[str, Any]:
    """手工触发一次主动搜索。立即返回，搜索在后台线程跑（约 5 分钟 / 360 公式）。

    返回 `status=already_running` 时**没有**新起任务：同一时间只允许一个，
    否则两个线程写同一个 runs 目录，report.json 会互相覆盖。
    """
    from app.services.alpha_schedule import run_alpha_search_now

    as_of = None
    if req is not None and req.as_of:
        from datetime import date as _date

        try:
            as_of = _date.fromisoformat(req.as_of)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"as_of 不是合法日期: {req.as_of}"
            ) from exc
    state = request.app.state
    if getattr(state, "repo", None) is None:
        raise HTTPException(status_code=503, detail="数据层未初始化")
    return run_alpha_search_now(state, as_of=as_of)
