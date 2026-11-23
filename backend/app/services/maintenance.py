"""数据维护：把「行情同步」与「投资手册链」合成一个可观测、可触发的动作。

为什么不是前端点两次按钮：
  两条链有先后依赖（手册读行情），又写同一批 parquet。前端串两次请求，中途关页就
  留下半套数据；并发点两次会撞重任务槽。这里用同一个 job **顺序**执行：
  行情落后 → 先跑盘后管道，再跑手册链（扫描 → 账户/现金腿 → 回放）。

为什么状态要单独算：
  「数据旧了」有两个来源 —— 行情没同步、或行情有但分析脚本没跑。前者看本地分区，
  后者看四个产物（扫描/回放/账户/现金腿）。两者都不能用产物互相比得出来：
  2026-11-23 的假绿就是「ETF 帧与指数帧一起停在 11-20」，差值为 0。
  所以 expected_session 只认交易日历（analysis/trading_calendar.py），
  由 handbook_audit.py --json 产出，后端只消费，不复制第二套日历。

副作用边界：
  - GET 只读产物与目录名，不写任何文件；
  - POST 只通过 job_store 单飞调度，执行体在 run_all()，可取消。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from app.services.pipeline_jobs import JobCancelledError, job_store

logger = logging.getLogger(__name__)

#: audit 子进程的缓存时长：状态轮询 2s 一次，不缓存会把 1s 的进程开销放大 30 倍。
#: 任务成功后会主动 clear（见 reset_cache），所以不会把旧结论留在页面上。
_AUDIT_TTL_S = 20.0
_audit_cache: tuple[float, dict[str, Any] | None] = (0.0, None)

#: 阶段百分比：行情 0–60，手册 60–100。数值只影响进度条，不影响判定。
_PIPE_MAX = 60


def root() -> Path:
    """仓库根。容器内是 /app（analysis 与 data 的相对位置与宿主机一致）。"""
    return Path(__file__).resolve().parents[2]


def reset_cache() -> None:
    global _audit_cache
    _audit_cache = (0.0, None)


def _run_json(script: Path, timeout: float) -> dict[str, Any] | None:
    if not script.exists():
        logger.warning("maintenance: 脚本不存在 %s", script)
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--json"],
            cwd=str(root()), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("maintenance: %s 执行失败：%s", script.name, exc)
        return None
    # 退出码 1 是「有产物落后」、2 是「关键产物缺失」—— 两者仍然带合法 JSON。
    # 2026-11-23 的教训：把 1 当成「脚本坏了」，status 就退化成 unknown，
    # 页面显示「无法判断」而不是「回放落后」，用户依然不知道该点哪里。
    if proc.returncode not in (0, 1, 2):
        logger.warning("maintenance: %s exit %s：%s", script.name, proc.returncode, (proc.stderr or "")[-300:])
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("maintenance: %s 输出不是 JSON（exit %s）：%s", script.name, proc.returncode, exc)
        return None


def audit_snapshot(*, ttl: float = _AUDIT_TTL_S) -> dict[str, Any] | None:
    global _audit_cache
    now = time.monotonic()
    ts, cached = _audit_cache
    if cached is not None and now - ts <= ttl:
        return cached
    payload = _run_json(root() / "analysis/handbook_audit.py", timeout=25)
    _audit_cache = (now, payload)
    return payload


def _kline_last(data_dir: Path) -> str | None:
    """本地日 K 覆盖到的最后一个交易日（目录名直读，不扫 parquet）。"""
    try:
        from app.services.trading_days import kline_trading_days

        days: list[date] = kline_trading_days(data_dir)
        return days[-1].isoformat() if days else None
    except Exception as exc:  # noqa: BLE001 —— 目录不可读就让页面降级，不影响其它域
        logger.warning("maintenance: 读取日K覆盖失败：%s", exc)
        return None


def _active_job() -> dict[str, Any] | None:
    """先看 active_id，再退回列表。

    不能只扫 list_recent：它按 started_at 倒序，pending 任务没有 started_at，
    会被排到最后并被 limit 截掉 —— 2026-11-23 的现场就是「任务在排队，
    维护卡却显示『更新数据』可点」。active_id 是单飞的唯一权威。
    """
    job = None
    jid = job_store.active_id()
    if jid:
        job = job_store.get(jid)
    if not job or job.get("status") not in ("pending", "running"):
        job = next((j for j in job_store.list_recent(20) if j.get("status") in ("pending", "running")), None)
    if not job:
        return None
    log = job.get("log") or []
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "stage": job.get("stage") or "",
        "progress": int(job.get("progress") or 0),
        "message": (log[-1].get("msg") if log else "") or "",
        "started_at": job.get("started_at"),
    }


def _last_job() -> dict[str, Any] | None:
    """最近一条已结束的任务。失败原因要留在页面上，否则用户只知道「又变旧了」。"""
    for job in job_store.list_recent(8):
        if job.get("status") in ("succeeded", "failed"):
            return {
                "id": job.get("id"),
                "status": job.get("status"),
                "error": job.get("error"),
                "finished_at": job.get("finished_at"),
            }
    return None


def build_status(
    audit: dict[str, Any] | None,
    kline_last: str | None,
    job: dict[str, Any] | None,
    last: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """纯函数：把「日历期望 + 本地覆盖 + 四个产物」折叠成页面要的两行状态。

    缺产物时给 stale=True 而不是抛错：页面要能说「未生成」，不是空白。
    """
    facts = (audit or {}).get("facts") or {}
    expected = facts.get("expected_session")
    phase = facts.get("phase") or "unknown"
    # 体检不可用（脚本缺失/超时/输出坏了）时 fail-closed：判不了就按「需要更新」处理，
    # 否则页面会拿一个「已是最新」的假绿去安慰人 —— 这正是 2026-11-23 的教训。
    unknown = not expected

    def behind(value: str | None) -> bool:
        if unknown:
            return True
        return not value or value[:10] < expected

    scan = facts.get("asof")
    replay = facts.get("replay_end")
    account = facts.get("account_asof")
    cash = facts.get("cash_leg_asof")

    kline_stale = behind(kline_last)
    handbook_parts = {"扫描": scan, "回放": replay, "账户": account, "现金腿": cash}
    missing_parts = [name for name, value in handbook_parts.items() if behind(value)]
    handbook_stale = bool(missing_parts)
    handbook_asof = min((v for v in handbook_parts.values() if v), default=None)

    if unknown:
        detail = "无法判断：体检脚本不可用"
    elif handbook_stale:
        detail = f"落后：{'、'.join(missing_parts)}"
    else:
        detail = " · ".join(f"{name} {value}" for name, value in handbook_parts.items())

    domains = [
        {
            "id": "kline",
            "label": "行情数据",
            "asof": kline_last,
            "stale": kline_stale,
            "detail": "无法判断：体检脚本不可用" if unknown else "本地日 K 覆盖（含指数/ETF/增强）",
        },
        {
            "id": "handbook",
            "label": "投资手册",
            "asof": handbook_asof,
            "stale": handbook_stale,
            "detail": detail,
        },
    ]
    return {
        "now": facts.get("now"),
        "expected": expected,
        "phase": phase,
        "stale": kline_stale or handbook_stale,
        "domains": domains,
        "job": job,
        "last": last if (last or {}).get("status") == "failed" else None,
    }


def status(data_dir: Path) -> dict[str, Any]:
    return build_status(audit_snapshot(), _kline_last(data_dir), _active_job(), _last_job())


# ---------------------------------------------------------------- 执行体


def _handbook(progress, base: int = _PIPE_MAX) -> dict[str, Any]:
    """在容器内跑整条手册链。逐行上报，取消时先杀子进程再抛。"""
    script = root() / "analysis/handbook_refresh.py"
    if not script.exists():
        raise RuntimeError("analysis/handbook_refresh.py 不存在：镜像没有内置 analysis/")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(  # noqa: S603 —— 固定脚本路径，无用户输入
        [sys.executable, str(script), "--force"],
        cwd=str(root()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env, bufsize=1,
    )
    pct = base
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            tail = (tail + [line])[-20:]
            # 只认脚本自己打印的阶段头：不解析业务数字，避免这里成为第二套判定。
            if line.startswith("==="):
                if "扫描" in line:
                    pct = base + 6
                elif "账户" in line:
                    pct = base + 18
                elif "回放" in line:
                    pct = base + 30
                elif "体检" in line:
                    pct = 96
            progress("handbook", min(pct, 99), line[-180:])
        code = proc.wait()
    except JobCancelledError:
        proc.kill()
        raise
    if code != 0:
        raise RuntimeError(f"手册链失败（exit {code}）：{tail[-1] if tail else '无输出'}")
    return {"exit": code, "tail": tail[-5:]}


def run_all(repo, capset, data_dir: Path, progress) -> dict[str, Any]:
    """job 执行体：只有落后的域才跑；全最新时立即返回（不写任何文件）。"""
    from app.jobs import daily_pipeline

    st = build_status(audit_snapshot(ttl=0), _kline_last(data_dir), None)
    if not st["stale"]:
        progress("maintenance", 100, "行情与手册都已是最新，未执行任何脚本")
        return {"skipped": True, "status": st}

    out: dict[str, Any] = {"status": st}

    # 体检不可用时不动行情：判不了「该不该同步」就贸然拉全市场，代价远大于收益。
    # 手册链仍可跑（它自己会在末尾报出体检失败的原因），但绝不顺带触发付费数据同步。
    if st.get("phase") == "unknown":
        progress("maintenance", 5, "体检不可用：跳过行情同步，只重跑手册链")
        out["handbook"] = _handbook(progress)
        reset_cache()
        progress("maintenance", 100, "维护完成（行情未动）")
        return out

    if st["domains"][0]["stale"]:
        progress("pipeline", 1, "行情同步：开始")

        def pipe_progress(stage: str, pct: int, msg: str, **_kw) -> None:
            progress(f"pipeline:{stage}", max(1, min(_PIPE_MAX - 2, int(pct * (_PIPE_MAX - 2) / 100))), msg)

        out["pipeline"] = daily_pipeline.run_now(repo, capset, on_progress=pipe_progress)
        progress("pipeline", _PIPE_MAX, "行情同步完成")

    out["handbook"] = _handbook(progress)
    reset_cache()
    progress("maintenance", 100, "维护完成")
    return out
