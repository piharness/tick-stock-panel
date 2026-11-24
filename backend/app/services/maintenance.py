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
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.services.job_context import current_job_id
from app.services.pipeline_jobs import JobCancelledError, job_store

logger = logging.getLogger(__name__)

#: audit 子进程的缓存时长：状态轮询 2s 一次，不缓存会把 1s 的进程开销放大 30 倍。
#: 任务成功后会主动 clear（见 reset_cache），所以不会把旧结论留在页面上。
_AUDIT_TTL_S = 20.0
_audit_cache: tuple[float, dict[str, Any] | None] = (0.0, None)

#: 行情链产物数据日的缓存。状态每 2s 被轮询一次，而它要探 4 张表的目录 + 读日K快照，
#: 不缓存等于把同一批 stat 放大十倍。
_market_parts_cache: tuple[float, dict[str, str | None]] = (0.0, {})

#: 数据日探针的回溯天数。只为拿「最近一个分区」，不该为此遍历全部历史目录。
_ASOF_PROBE_DAYS = 14

#: 阶段百分比：行情 0–60，手册 60–100。数值只影响进度条，不影响判定。
_PIPE_MAX = 60


def root() -> Path:
    """仓库根。容器内是 /app（analysis 与 data 的相对位置与宿主机一致）。"""
    return Path(__file__).resolve().parents[2]


def reset_cache() -> None:
    global _audit_cache, _market_parts_cache
    _audit_cache = (0.0, None)
    _market_parts_cache = (0.0, {})


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


def _partition_table_asof(data_dir: Path, table: str) -> str | None:
    """分区表最近的数据日。

    日分区目录名即日期，直接读名字；月分区（kline_daily 一类）只读最后一个分区
    的 date 列 —— 目录名只到「月」，拿不到日。

    先用「今天往回探 14 天」代替遍历全部目录：enriched 有 5 千多个日分区，在
    9p 挂载上 glob 一次要 4.6s，而状态每 2s 被轮询一次。探不到（停更很久/换成
    月分区）才退回全目录扫描。
    """
    base = Path(data_dir) / table
    if not base.is_dir():
        return None
    from app.market_time import cn_today

    today = cn_today()
    for i in range(_ASOF_PROBE_DAYS):
        day = (today - timedelta(days=i)).isoformat()
        if (base / f"date={day}").is_dir():
            return day
    return _scan_latest_asof(base)


def _scan_latest_asof(base: Path) -> str | None:
    """兜底：扫全部日期目录取最新一个（日分区读名字，月分区读 date 列）。"""
    parts = [p for p in base.glob("date=*") if p.is_dir()]
    if not parts:
        return None
    newest = max(parts, key=lambda p: p.name)
    label = newest.name.split("=", 1)[1]
    if len(label) == 10:  # date=YYYY-MM-DD
        return label
    try:
        import polars as pl

        frame = pl.read_parquet(next(newest.glob("*.parquet")), columns=["date"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("maintenance: 读取 %s 最新分区失败：%s", base.name, exc)
        return None
    return str(frame["date"].max())[:10] if frame.height else None


_MARKET_PART_LABELS = {"daily": "日K", "enriched": "增强", "etf": "ETF", "index": "指数"}


def _compute_market_parts(data_dir: Path) -> dict[str, str | None]:
    """行情链每张产物的数据日。ETF / 指数按拉取开关决定是否纳入。"""
    from app.services import preferences
    from app.services.trading_days import kline_trading_days

    days: list[date] = kline_trading_days(data_dir)
    parts: dict[str, str | None] = {"daily": days[-1].isoformat() if days else None}
    # 顺序即页面展示顺序: 先个股, 再派生, 最后 ETF/指数
    parts["enriched"] = _partition_table_asof(data_dir, "kline_daily_enriched")
    if preferences.get_pipeline_pull_etf():
        parts["etf"] = _partition_table_asof(data_dir, "kline_etf_daily")
    if preferences.get_pipeline_pull_index():
        parts["index"] = _partition_table_asof(data_dir, "kline_index_daily")
    return parts


def market_parts(data_dir: Path) -> dict[str, str | None]:
    """带 TTL 的行情链产物数据日（状态每 2s 被轮询，读盘不能跟着放大）。"""
    global _market_parts_cache
    now = time.monotonic()
    ts, cached = _market_parts_cache
    if cached and now - ts <= _AUDIT_TTL_S:
        return cached
    try:
        parts = _compute_market_parts(data_dir)
    except Exception as exc:  # noqa: BLE001 —— 目录不可读就让页面降级，不影响其它域
        logger.warning("maintenance: 读取行情链覆盖失败：%s", exc)
        parts = {}
    _market_parts_cache = (now, parts)
    return parts


def _kline_last(data_dir: Path) -> str | None:
    """行情链产物的共同数据日 = 日K / enriched / ETF / 指数日K 取最小。

    只看 kline_daily 会把「管道半途失败」显示成「已最新」：2026-11-24 的现场是
    日K 已到当日、enriched 与 ETF 还停在昨天，维护入口因此跳过行情、只跑手册；
    手册拿前一天的 ETF 数据必然体检失败，而且点多少次都是同一个错。
    页面上那行字本来就写着「含指数/ETF/增强」，判定必须与它同口径。

    任一产物缺失时返回 None（判不了按落后处理，与本模块其它分支一致）。
    """
    parts = market_parts(data_dir)
    values = list(parts.values())
    return min(values) if parts and all(values) else None


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
    parts: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """纯函数：把「日历期望 + 本地覆盖 + 四个产物」折叠成页面要的两行状态。

    缺产物时给 stale=True 而不是抛错：页面要能说「未生成」，不是空白。
    parts: 行情链每张产物的数据日（日K/增强/ETF/指数），用来把「落后」说到具体那张表
    —— 只说「行情落后」等于让用户自己猜该补什么。
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

    if unknown:
        kline_detail = "无法判断：体检脚本不可用"
    elif parts:
        gaps = [
            f"{_MARKET_PART_LABELS.get(pid, pid)} {value or '缺失'}"
            for pid, value in parts.items() if behind(value)
        ]
        kline_detail = (
            f"落后：{'、'.join(gaps)}" if gaps
            else " · ".join(f"{_MARKET_PART_LABELS.get(pid, pid)} {value}" for pid, value in parts.items())
        )
    else:
        kline_detail = "本地日 K 覆盖（含指数/ETF/增强）"

    domains = [
        {
            "id": "kline",
            "label": "行情数据",
            "asof": kline_last,
            "stale": kline_stale,
            "detail": kline_detail,
            "parts": [
                {"id": pid, "label": _MARKET_PART_LABELS.get(pid, pid), "asof": value,
                 "stale": behind(value)}
                for pid, value in (parts or {}).items()
            ],
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


class MaintenanceNotConverged(RuntimeError):
    """跑完仍未补齐。必须带上「哪张表缺到哪天」——否则用户只知道「又失败了」。"""


def build_plan(st: dict[str, Any]) -> list[dict[str, str]]:
    """从状态生成执行计划（顺序即依赖：先行情，后手册）。

    只读两个域各自的 stale，不复制判定逻辑。行情缺一天时不能先跑手册：手册读的是
    ETF 日K，在旧行情上跑出来的仍是昨天的结论，报错还会指向 etf_watch（指错方向）。
    """
    kline, handbook = st["domains"][0], st["domains"][1]
    plan: list[dict[str, str]] = []
    if kline["stale"]:
        plan.append({"action": "pipeline", "reason": kline["detail"]})
    if handbook["stale"]:
        plan.append({"action": "handbook", "reason": handbook["detail"]})
    return plan


def _snapshot(data_dir: Path) -> dict[str, Any]:
    """当前状态快照（不带任务信息，供执行体内部复核）。"""
    return build_status(
        audit_snapshot(ttl=0), _kline_last(data_dir), None, None, market_parts(data_dir),
    )


def status(data_dir: Path) -> dict[str, Any]:
    st = build_status(
        audit_snapshot(), _kline_last(data_dir), _active_job(), _last_job(),
        market_parts(data_dir),
    )
    # 计划也一起给出去：页面能直接说「将补：行情 → 手册」，用户不必猜点完会发生什么。
    st["plan"] = build_plan(st)
    return st


# ---------------------------------------------------------------- 执行体


#: 正在跑手册链的子进程，按 job 记账。
#: 不记的话：子进程卡住时父线程阻塞在 stdout 读上（没有任何 progress 回调），
#: terminate 只置取消标志也解不开；子进程还会变成孤儿，继续占用 CPU / 写 JSON。
_HANDBOOK_CHILDREN: dict[str, subprocess.Popen] = {}
_HANDBOOK_LOCK = threading.Lock()


def kill_handbook_child(job_id: str) -> bool:
    """terminate 钩子：终止该任务正在跑的手册链子进程。

    杀掉后管道 EOF ⇒ 阻塞在 stdout 的父线程读完退出，作业线程才能释放
    （否则 reap 之后线程仍然占着 worker 线程与资源）。
    """
    with _HANDBOOK_LOCK:
        proc = _HANDBOOK_CHILDREN.pop(job_id, None)
    if proc is None or proc.poll() is not None:
        return False
    proc.kill()
    logger.warning("terminate: 已终止 job %s 的手册链子进程 pid=%s", job_id, proc.pid)
    return True


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
    job_id = current_job_id()
    if job_id:
        with _HANDBOOK_LOCK:
            _HANDBOOK_CHILDREN[job_id] = proc
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
    finally:
        if job_id:
            with _HANDBOOK_LOCK:
                _HANDBOOK_CHILDREN.pop(job_id, None)
    if code != 0:
        raise RuntimeError(f"手册链失败（exit {code}）：{tail[-1] if tail else '无输出'}")
    return {"exit": code, "tail": tail[-5:]}


def run_all(repo, capset, data_dir: Path, progress) -> dict[str, Any]:
    """job 执行体：算缺口 → 只跑缺的 → 跑完复核。

    三条纪律（自愈的完整定义）:
      1. 只跑落后的域；全最新直接返回，不写任何文件。
      2. 体检不可用时不碰付费行情（判不了该不该拉就别拉），只跑手册链。
      3. 跑完必须复核：管道「没抛异常」不等于每张产物都到了 expected —— 供应商
         迟交时 ETF 会写 0 行、日志还报「完成」。没补齐就停在行情，不拿旧 ETF 去
         跑手册；报错里写明缺哪张表、缺到哪天，而不是让用户去读手册链的 exit 1。
    """
    from app.jobs import daily_pipeline

    st = _snapshot(data_dir)
    plan = build_plan(st)
    if not plan:
        progress("maintenance", 100, "行情与手册都已是最新，未执行任何脚本")
        return {"skipped": True, "converged": True, "plan": [], "executed": [], "status": st}

    out: dict[str, Any] = {"status": st, "plan": plan, "executed": []}
    state = st
    _PLAN_LABELS = {"pipeline": "补行情", "handbook": "补手册"}
    progress("maintenance", 1, "计划：" + " → ".join(_PLAN_LABELS.get(i["action"], i["action"]) for i in plan))

    # 体检不可用时不动行情：判不了「该不该同步」就贸然拉全市场，代价远大于收益。
    # 手册链仍可跑（它自己会在末尾报出体检失败的原因），但绝不顺带触发付费数据同步。
    if st.get("phase") == "unknown":
        progress("maintenance", 5, "体检不可用：跳过行情同步，只重跑手册链")
        out["handbook"] = _handbook(progress)
        reset_cache()
        progress("maintenance", 100, "维护完成（行情未动）")
        # 体检不可用 ⇒ 无从判断是否收敛，如实留空而不是给一个假的 True
        return {**out, "executed": ["handbook"], "converged": None}

    if state["domains"][0]["stale"]:
        progress("pipeline", 1, "行情同步：开始")

        def pipe_progress(stage: str, pct: int, msg: str, **_kw) -> None:
            progress(f"pipeline:{stage}", max(1, min(_PIPE_MAX - 2, int(pct * (_PIPE_MAX - 2) / 100))), msg)

        out["pipeline"] = daily_pipeline.run_now(repo, capset, on_progress=pipe_progress)
        out["executed"].append("pipeline")
        progress("pipeline", _PIPE_MAX, "行情同步完成")
        reset_cache()
        state = _snapshot(data_dir)
        out["post_pipeline"] = state["domains"][0]
        if state["domains"][0]["stale"]:
            raise MaintenanceNotConverged(
                f"行情链未补齐（{state['domains'][0]['detail']}）。已跳过手册链："
                "手册读的是 ETF 日K，行情缺一天时跑出来的仍是昨天的结论。"
                "若这张表本就不该有（数据源没有该能力），请在 设置 → 数据源 关闭对应拉取开关"
                "—— 否则每轮都会按「落后」处理"
            )

    # 行情推进一天会让手册立刻落后 ⇒ 在这里重新判定，而不是沿用跑之前的计划。
    if state["domains"][1]["stale"]:
        out["handbook"] = _handbook(progress)
        out["executed"].append("handbook")
        reset_cache()
        state = _snapshot(data_dir)

    out["converged"] = not state["stale"]
    if not out["converged"]:
        if state.get("expected") and state["expected"] != st.get("expected"):
            # 跑到一半跨过了「收盘 +10 分钟」：期望数据日变了，不能拿新期望判这一轮。
            # 如实标未知，新日期交给下一轮（16:20/18:20/20:20 的阶梯会接上）。
            out["converged"] = None
            out["note"] = (
                f"运行期间期望数据日从 {st.get('expected')} 变为 {state['expected']}，"
                "下一轮按新日期判定"
            )
            progress("maintenance", 100, "维护完成（跨交易日，下一轮补新日期）")
            return out
        blockers = "；".join(
            f"{d['label']}：{d['detail']}" for d in state["domains"] if d["stale"]
        )
        raise MaintenanceNotConverged(f"维护跑完仍未补齐（{blockers}）")
    progress("maintenance", 100, "维护完成")
    return out
