"""自动策略实验室调度单入口 (P3): 周度挖掘 + 每日守护 + 月度主动搜索一次挂载。

替代 daily_pipeline 直接调用各调度函数的旧接线; 原入口
(run_weekly_mining / run_daily_guardian / run_scheduled_alpha_search)
保持独立可用, 便于单独触发与测试。

三个任务的失败域必须互相隔离：任何一个抛异常都不能让另外两个不跑。
挖掘锁被占用时要降级而不是报错 —— 那是并发保护，不是故障。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_scheduled_lab(app_state: Any) -> dict[str, Any]:
    """收盘管道成功后依次触发三个调度 (各自独立防重)。"""
    from app.services.alpha_schedule import run_scheduled_alpha_search
    from app.services.guardian_schedule import run_daily_guardian
    from app.services.mining_lock_admin import ensure_mining_lock
    from app.services.mining_schedule import run_weekly_mining

    results: dict[str, Any] = {"mining": None, "guardian": None, "alpha": None}
    try:
        if not ensure_mining_lock(app_state):
            # 降级模式 (挖掘锁被其他进程占用): 跳过周度挖掘而不是报错,
            # 守护复验不受影响
            logger.warning("scheduled lab mining skipped: mining lock unavailable")
            results["mining"] = {"skipped": "mining lock unavailable"}
        else:
            results["mining"] = run_weekly_mining(app_state)
    except Exception:
        logger.exception("scheduled lab mining enqueue failed")
    try:
        results["guardian"] = run_daily_guardian(app_state)
    except Exception:
        logger.exception("scheduled lab guardian enqueue failed")
    try:
        # 主动搜索是后台线程 + 月度 claim: 这里只负责「到点就点火」,
        # 不等它跑完 (每次约 5 分钟, 等它会拖住整条收盘管道)。
        results["alpha"] = run_scheduled_alpha_search(app_state)
    except Exception:
        logger.exception("scheduled lab alpha search enqueue failed")
    return results
