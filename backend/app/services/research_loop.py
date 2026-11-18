"""研究闭环控制面: 策略 / 因子 / 回测三页「自动维护」的唯一状态口径。

存在理由
--------
自动化的零件已经齐了: 周度挖掘 (`mining_schedule`)、日度守护
(`guardian_schedule`)、主动 Alpha 搜索 (`services.alpha_search`)、决策账本
生产者 (`decision.producer`)、模拟盘跟单 (`strategy.paper_auto`)。缺的是
**一句能回答「这个页面现在需不需要人管」的话**:

1. 新鲜度: 三页各自「最后一次有效产出」是哪天, 相对最新数据日多久 (自然日,
   口径写进字段名 `age_days`, 不让 UI 猜)；
2. 准入: 研究结论能不能进账本 / 模拟仓。**默认拒绝**——没入护、守护未复验、
   基线不是样本外、退化超阈值、交易数枯竭, 任一条成立就不放行;
3. 联动: 已准入的策略在账本和模拟仓里到底有没有被接上 (开关 + 规则)。

纪律
----
- 本模块**不写研究状态、不改任何开关**: 准入是建议, 执行由用户点。唯一的
  文件系统动作是读取既有模拟盘账户的跟单规则, 走 `paper` 自己的目录布局
  (账户不存在时直接返回空, 不去创建)。
- 不重复守护/挖掘的判定逻辑: 状态直接取档案里的 `verdict`, 退化阈值取
  `GuardianThresholds`, 这里只做「要不要让它出门」的最后一道。
- 不引入第二个「今天」: 参考日一律取 enriched 最新交易日, 取不到才退回北京今天。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from app.backtest.guardian import GuardianThresholds
from app.market_time import cn_today

logger = logging.getLogger(__name__)

PageKey = Literal["strategy", "factor", "backtest"]
FreshnessState = Literal["fresh", "aging", "stale", "never"]
AdmissionVerdict = Literal["admitted", "blocked"]

PAGE_KEYS: tuple[PageKey, ...] = ("strategy", "factor", "backtest")

PAGE_LABELS: dict[str, str] = {
    "strategy": "策略",
    "factor": "因子",
    "backtest": "回测",
}

#: 每页「维护对象」的唯一定义: 新鲜度看的是这条产出, 不是页面访问时间。
PAGE_SUBJECTS: dict[str, str] = {
    "strategy": "策略池的候选沉淀 (研究候选 -> 发布)",
    "factor": "因子研究产出 (主动搜索 / 周度挖掘)",
    "backtest": "样本外复验 (守护复验记录)",
}


@dataclass(frozen=True)
class FreshnessPolicy:
    """按自然日计龄的门槛；`fresh_days` 内算新鲜, 超过 `stale_days` 算过期。

    门槛按「这条产出本来多久更新一次」定, 不是按用户耐心定:
    - 因子研究是周度节奏 (主动搜索 ~3 分钟, 挖掘更重) ⇒ 7 / 21 天;
    - 候选发布同样周度 ⇒ 7 / 21 天;
    - 守护复验是日度节奏 ⇒ 2 / 5 天。
    """

    fresh_days: int
    stale_days: int

    def __post_init__(self) -> None:
        if self.fresh_days < 0 or self.stale_days <= self.fresh_days:
            raise ValueError("新鲜度门槛必须满足 0 <= fresh_days < stale_days")


PAGE_POLICIES: dict[str, FreshnessPolicy] = {
    "strategy": FreshnessPolicy(fresh_days=7, stale_days=21),
    "factor": FreshnessPolicy(fresh_days=7, stale_days=21),
    "backtest": FreshnessPolicy(fresh_days=2, stale_days=5),
}


def parse_day(value: Any) -> date | None:
    """把 ISO 日期/时间串解析成北京日历日；读不懂返回 None (不猜)。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def age_days(last: Any, as_of: date) -> int | None:
    """`as_of` 与 `last` 的自然日差；`last` 在未来时返回 0 (时钟偏差不算过期)。"""
    day = parse_day(last)
    if day is None:
        return None
    return max(0, (as_of - day).days)


def classify(age: int | None, policy: FreshnessPolicy) -> FreshnessState:
    if age is None:
        return "never"
    if age <= policy.fresh_days:
        return "fresh"
    if age <= policy.stale_days:
        return "aging"
    return "stale"


def freshness_entry(
    *,
    page: PageKey,
    last_at: Any,
    as_of: date,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = PAGE_POLICIES[page]
    age = age_days(last_at, as_of)
    return {
        "page": page,
        "label": PAGE_LABELS[page],
        "subject": PAGE_SUBJECTS[page],
        "last_at": str(last_at) if last_at is not None else None,
        "age_days": age,
        "state": classify(age, policy),
        "fresh_days": policy.fresh_days,
        "stale_days": policy.stale_days,
        "detail": dict(detail or {}),
    }


# ── 准入闸门 ──────────────────────────────────────────────────────────


def _last_check(profile: Mapping[str, Any]) -> Mapping[str, Any] | None:
    checks = profile.get("checks") or []
    if not isinstance(checks, Sequence) or not checks:
        return None
    last = checks[-1]
    return last if isinstance(last, Mapping) else None


def admission_verdict(
    profile: Mapping[str, Any] | None,
    *,
    thresholds: GuardianThresholds | None = None,
) -> dict[str, Any]:
    """研究结论 → 账本/模拟仓 的准入判定。**fail-closed**: 每条不确定都拒绝。

    通过条件 (全部成立):
    1. 已纳入守护 (有档案)；
    2. 档案状态 healthy (watch/retired/pending 都不算)；
    3. 基线类型是 `mining_gate`——那才是真样本外；`trailing_reference`
       只是「入护日往前一段」的参考值, 拿它当基线等于自证；
    4. 最近一次复验有明确 verdict 且无 error；
    5. 最近一次复验的退化度在 watch 阈值以内。

    交易数枯竭不在这里判: `evaluate_check` 已把它译成 watch/retired 状态,
    这里再判一次就是第二个判据来源。
    """
    base: dict[str, Any] = {
        "verdict": "blocked",
        "stage": "unadopted",
        "reasons": ["未纳入守护档案: 没有样本外复验基线"],
        "status": None,
        "baseline_kind": None,
        "degradation_pct": None,
        "last_checked_at": None,
        "last_verdict": None,
    }
    if not isinstance(profile, Mapping):
        return base

    status = str(profile.get("status") or "pending")
    baseline = profile.get("baseline") or {}
    baseline_kind = str(baseline.get("kind") or "") if isinstance(baseline, Mapping) else ""
    last = _last_check(profile)
    checked_at = last.get("checked_at") if last else None
    base.update(
        {
            "status": status,
            "baseline_kind": baseline_kind or None,
            "last_checked_at": checked_at,
            "last_verdict": (last.get("verdict") if last else None),
            "degradation_pct": (last.get("degradation_pct") if last else None),
        }
    )

    if status != "healthy":
        label = {"pending": "未复验", "watch": "观察中", "retired": "已退役"}.get(
            status, status
        )
        base["stage"] = "guardian_status"
        base["reasons"] = [f"守护状态为「{label}」: 只有 healthy 才允许出门"]
        return base

    if baseline_kind != "mining_gate":
        base["stage"] = "baseline_not_oos"
        base["reasons"] = [
            "基线不是挖掘门槛 (mining_gate) 而是尾随参考: "
            "用入护前的自己当基线不构成样本外证据"
        ]
        return base

    if last is None:
        base["stage"] = "no_check"
        base["reasons"] = ["档案没有复验记录: healthy 状态缺少可核对的来源"]
        return base

    error = last.get("error")
    if error:
        base["stage"] = "no_check"
        base["reasons"] = [f"最近一次复验失败: {str(error)[:80]}"]
        return base

    thresholds = thresholds or GuardianThresholds()
    degrade = last.get("degradation_pct")
    if isinstance(degrade, (int, float)) and degrade > thresholds.watch_pct:
        base["stage"] = "degraded"
        base["reasons"] = [
            f"较基线退化 {degrade:.0%}, 超过观察阈值 {thresholds.watch_pct:.0%}"
        ]
        return base

    base["verdict"] = "admitted"
    base["stage"] = "admitted"
    base["reasons"] = ["守护健康的样本外策略"]
    return base


# ── 状态聚合 ──────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def latest_alpha_run(data_dir: Path | str) -> dict[str, Any] | None:
    """最近一次主动搜索的状态摘要 (读 `research/alpha_search/runs/*/report.json`)。

    只读最新一个 run 目录: 目录名以时间戳开头, 按名排序即时间排序。

    `top_themes` 取 `clusters` (每个 template×seeds 主题只留确认期 q 最小的一条),
    不是 `candidates` 的原始条数 —— 同一主题换窗口的高度相关变体不能算多个发现。
    `dsr_pass` 是这些主题里 DSR ≥ `min_dsr` 的个数: 为 0 时本次搜索的正确结论是
    「没有找到能过噪声门槛的信号」, 前端必须把它当结论显示, 不能只报 confirmed 数。
    """
    runs = Path(data_dir) / "research" / "alpha_search" / "runs"
    if not runs.is_dir():
        return None
    try:
        candidates = sorted((p for p in runs.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return None
    for run_dir in reversed(candidates):
        report = _read_json(run_dir / "report.json")
        if report is None:
            continue
        counts = report.get("counts") or {}
        config = report.get("config") or {}
        min_dsr = config.get("min_dsr")
        themes = report.get("clusters") or []
        theme_rows = []
        for theme in themes:
            if not isinstance(theme, Mapping):
                continue
            theme_rows.append(
                {
                    "candidate_id": theme.get("candidate_id"),
                    "formula": theme.get("formula"),
                    "template": theme.get("template"),
                    "seeds": theme.get("seeds"),
                    "direction": theme.get("direction"),
                    "confirmation_ic": theme.get("confirmation_ic"),
                    "confirmation_q": theme.get("confirmation_q"),
                    "dsr": theme.get("dsr"),
                }
            )
        threshold = min_dsr if isinstance(min_dsr, (int, float)) else 0.90
        return {
            "run_id": report.get("run_id") or run_dir.name,
            "status": report.get("status"),
            "created_at": report.get("created_at"),
            "elapsed_s": report.get("elapsed_s"),
            "confirmed": counts.get("confirmed"),
            "provisional": counts.get("provisional"),
            "confirmed_themes": counts.get("confirmed_themes"),
            "generated": counts.get("generated"),
            "min_dsr": threshold,
            "dsr_pass": sum(
                1
                for row in theme_rows
                if isinstance(row["dsr"], (int, float)) and row["dsr"] >= threshold
            ),
            "top_themes": theme_rows[:8],
        }
    return None


def _latest_run(manager: Any) -> dict[str, Any] | None:
    if manager is None:
        return None
    try:
        runs = manager.store.list_runs(limit=1)
    except Exception:  # noqa: BLE001 — 战报对运行目录损坏降级为空
        return None
    if not runs:
        return None
    manifest = runs[0]
    return {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
    }


def _latest_guardian_check(profiles: Sequence[Mapping[str, Any]]) -> str | None:
    latest: str | None = None
    for profile in profiles:
        check = _last_check(profile)
        if not check:
            continue
        value = check.get("checked_at")
        if isinstance(value, str) and (latest is None or value > latest):
            latest = value
    return latest


def _latest_candidate_update(candidates: Sequence[Mapping[str, Any]]) -> tuple[str | None, int]:
    published = 0
    latest: str | None = None
    for item in candidates:
        if item.get("published_strategy_id"):
            published += 1
        for key in ("updated_at", "created_at"):
            value = item.get(key)
            if isinstance(value, str) and (latest is None or value > latest):
                latest = value
                break
    return latest, published


def _paper_linkage(data_dir: Path, strategy_ids: Sequence[str]) -> dict[str, Any]:
    from app.strategy import paper, paper_auto

    wanted = set(strategy_ids)
    matches: dict[str, list[str]] = {}
    accounts = 0
    enabled_rules = 0
    # 账户根目录不存在 = 从未用过模拟盘: 直接返回, 不让 paper 的惰性目录创建
    # 因为「看一眼状态」而落盘。
    if not paper.accounts_base(data_dir).is_dir():
        return {"accounts": 0, "enabled_rules": 0, "by_strategy": matches}
    try:
        account_ids = paper.list_account_ids(data_dir)
    except Exception:  # noqa: BLE001 — 模拟盘目录不可读时视为未联动
        account_ids = []
    for account_id in account_ids:
        accounts += 1
        try:
            rules = paper_auto.load_auto_rules(data_dir, account_id)
        except Exception:  # noqa: BLE001
            continue
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            enabled_rules += 1
            if rule.get("match_kind") != "strategy":
                continue
            match_id = str(rule.get("match_id") or "")
            if match_id in wanted:
                matches.setdefault(match_id, []).append(account_id)
    return {"accounts": accounts, "enabled_rules": enabled_rules, "by_strategy": matches}


def build_loop_status(
    data_dir: Path | str,
    repo: Any,
    app_state: Any,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """三页新鲜度 + 准入 + 联动。任何单块失败都不影响其余块 (fail-soft)。"""
    data_path = Path(data_dir)
    as_of = None
    try:
        as_of = repo.latest_enriched_date("stock")
    except Exception:  # noqa: BLE001
        as_of = None
    as_of = as_of or today or cn_today()

    from app.services import preferences

    mining_cfg = preferences.get_mining_schedule()
    guardian_cfg = preferences.get_guardian_schedule()
    decision_cfg = preferences.get_decision_auto_config()

    alpha = latest_alpha_run(data_path)
    mining = _latest_run(getattr(app_state, "mining_manager", None))

    # 市场环境: 只描述、不阻断。见 market_context 模块头的回算记录 ——
    # 阈值 50% 附近是尖峰而非平台, 且前瞻 20 日无预测力, 因此不做硬闸门。
    try:
        from app.services.market_context import build_market_context

        market = build_market_context(data_path, as_of)
    except Exception as exc:  # noqa: BLE001 — 环境层不可用不该影响三页状态
        market = {
            "state": "unknown",
            "as_of": as_of.isoformat(),
            "note": "环境层不可用",
            "error": f"{type(exc).__name__}: {exc}"[:120],
        }

    # 拥挤度: 当日换手率分位表 + 各策略持仓的平均分位。
    # 判据来自 `analysis/_diag_regime_gate.py` 的分层回算（近 120 日 Q5 −36.5%
    # vs Q1 −25.8%）。它**不阻断**准入: 拥挤是暴露读数, 不是「这个策略错了」;
    # 一个高换手策略在牛市里正是靠这个暴露赚钱。
    crowding_levels: dict[str, float] = {}
    crowding: dict[str, Any] = {"available": False, "note": "拥挤度未计算"}
    try:
        from app.services.market_context import crowding_quantiles, crowding_summary

        crowding_levels = crowding_quantiles(data_path, as_of)
        crowding = crowding_summary(crowding_levels)
    except Exception as exc:  # noqa: BLE001
        crowding = {"available": False, "note": f"拥挤度不可用: {type(exc).__name__}"}

    cards: list[dict[str, Any]] = []
    try:
        from app.decision import ledger as decision_ledger

        cards = decision_ledger.load_cards(data_path)
    except Exception:  # noqa: BLE001 — 账本损坏时准入仍按档案判定, 只是缺持仓暴露
        cards = []

    profiles: list[dict[str, Any]] = []
    # GuardianStore 的构造函数会 mkdir; 目录不存在说明从未入护, 别为读一眼而建库。
    if (data_path / "user_data" / "strategy_guardian").is_dir():
        try:
            from app.services.guardian_store import GuardianStore

            profiles = GuardianStore(data_path).list_profiles()
        except Exception:  # noqa: BLE001
            profiles = []

    candidates: list[dict[str, Any]] = []
    try:
        from app.backtest.candidates import CandidateStore

        candidates = CandidateStore(data_path).list()
    except Exception:  # noqa: BLE001
        candidates = []

    # 因子页: 主动搜索与挖掘取较新者 —— 两者都是「因子研究产出」的合法来源。
    factor_last = max(
        (value for value in (
            (alpha or {}).get("created_at"),
            (mining or {}).get("finished_at") or (mining or {}).get("created_at"),
        ) if isinstance(value, str)),
        default=None,
    )
    candidate_last, published_count = _latest_candidate_update(candidates)
    guardian_last = _latest_guardian_check(profiles)

    freshness = [
        freshness_entry(
            page="factor",
            last_at=factor_last,
            as_of=as_of,
            detail={
                "alpha_run": alpha,
                "mining_run": mining,
            },
        ),
        freshness_entry(
            page="strategy",
            last_at=candidate_last,
            as_of=as_of,
            detail={"candidates": len(candidates), "published": published_count},
        ),
        freshness_entry(
            page="backtest",
            last_at=guardian_last,
            as_of=as_of,
            detail={
                "profiles": len(profiles),
                "by_status": _count_by_status(profiles),
            },
        ),
    ]

    # 准入: 只对「曾经进入研究管道」的策略做判定, 不扫全部策略。
    admission: list[dict[str, Any]] = []
    for profile in profiles:
        strategy_id = str(profile.get("id") or profile.get("strategy_id") or "")
        if not strategy_id:
            continue
        verdict = admission_verdict(profile)
        admission.append({"strategy_id": strategy_id, **verdict})
    admitted_ids = [item["strategy_id"] for item in admission if item["verdict"] == "admitted"]

    linkage = {
        "ledger": {
            "enabled": bool(decision_cfg.get("enabled")),
            "execute": bool(decision_cfg.get("execute")),
            "strategy_id": decision_cfg.get("strategy_id") or None,
            # 定义点在 preferences.get_decision_auto_config(), 这里只读不复制判定。
            "lu_rebound_enabled": bool(decision_cfg.get("lu_rebound_enabled")),
        },
        "paper": _paper_linkage(data_path, admitted_ids),
    }
    paper_by_strategy = linkage["paper"]["by_strategy"]
    headwind = market.get("state") == "risk_off"
    for item in admission:
        item["in_ledger"] = bool(
            linkage["ledger"]["strategy_id"]
            and item["strategy_id"] == linkage["ledger"]["strategy_id"]
        )
        item["in_paper"] = item["strategy_id"] in paper_by_strategy
        # 环境不改判定: 策略本身的样本外健康度与环境无关。这里只标出「能不能
        # 指望 β 顺风」, 让「准入通过」不被误读成「现在就该加仓」。
        item["regime"] = "headwind" if headwind else "neutral"
        # 持仓拥挤度: 该策略最近一张有标的的卡, 其选股落在当日换手率分位何处。
        # 分位用 `as_of` 当天算, 卡可能是几天前的 —— 这是近似, 所以在读数里
        # 一并给出卡日期, 不让「上周的持仓」冒充「今天的暴露」。
        item["crowding"] = _strategy_crowding(cards, item["strategy_id"], crowding_levels)

    alpha_cfg = preferences.get_alpha_search_schedule()
    autonomy = {
        "mining": {
            "enabled": bool(mining_cfg["mining_schedule_enabled"]),
            "weekday": mining_cfg["mining_schedule_weekday"],
            "profile": mining_cfg["mining_budget_profile"],
        },
        "guardian": {"enabled": bool(guardian_cfg["guardian_schedule_enabled"])},
        "alpha": {
            "enabled": bool(alpha_cfg["enabled"]),
            "day": alpha_cfg["alpha_search_day"],
            "profile": alpha_cfg["alpha_search_profile"],
            "max_formulas": alpha_cfg["alpha_search_max_formulas"],
        },
        "decision": {"enabled": bool(decision_cfg.get("enabled"))},
    }
    # 「研究自动化」数挖掘 / 守护 / 主动搜索: 决策账本是消费者, 它开着不等于研究在跑;
    # 把它算进来会让「研究全关但账本在发卡」显示成「部分开启」。
    # 主动搜索必须算进来 —— 它是唯一一个「自己生成假设」的环节, 不算就等于
    # 把「每月自动找新因子」显示成「研究自动化未开启」。
    autonomy["any_enabled"] = bool(
        autonomy["mining"]["enabled"]
        or autonomy["guardian"]["enabled"]
        or autonomy["alpha"]["enabled"]
    )

    return {
        "as_of": as_of.isoformat(),
        "basis": "age_days 为自然日, 参考日 = enriched 最新交易日 (取不到时用北京今天)",
        "freshness": freshness,
        "autonomy": autonomy,
        "market": market,
        "crowding": crowding,
        "admission": {
            "total": len(admission),
            "admitted": len(admitted_ids),
            "blocked": len(admission) - len(admitted_ids),
            "items": admission,
        },
        "linkage": linkage,
        "next_actions": _next_actions(freshness, autonomy, admission, market),
    }


def _strategy_crowding(
    cards: Sequence[Mapping[str, Any]],
    strategy_id: str,
    levels: Mapping[str, float],
) -> dict[str, Any] | None:
    """该策略最近一张「有标的」的卡, 选股落在当日换手率分位何处。

    只取最近一张而不是全部历史: 拥挤度是**当前暴露**, 用半年前的持仓算出来的
    读数会给出「它很拥挤」的错误印象 —— 那时它可能持有的是完全不同的票。
    分位表用 `as_of` 当天, 卡日期随读数一起给出, 让时间错位可见。
    """
    if not levels:
        return None
    relevant = [
        card
        for card in cards
        if str(card.get("strategy_id") or "") == strategy_id and (card.get("picks") or [])
    ]
    if not relevant:
        return None
    card = max(relevant, key=lambda c: str(c.get("as_of") or ""))
    hits = [
        levels[str(pick["symbol"])]
        for pick in card.get("picks") or []
        if str(pick.get("symbol") or "") in levels
    ]
    if not hits:
        return None
    avg = sum(hits) / len(hits)
    return {
        "pct": round(avg, 3),
        "max_pct": round(max(hits), 3),
        "covered": len(hits),
        "card_as_of": card.get("as_of"),
        "card_id": card.get("card_id"),
        #: 平均分位在前 20% ⇒ 这个策略主要在拥挤区选股。是读数, 不是判决。
        "crowded": avg >= 0.8,
    }


def _count_by_status(profiles: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for profile in profiles:
        status = str(profile.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _next_actions(
    freshness: Sequence[Mapping[str, Any]],
    autonomy: Mapping[str, Any],
    admission: Sequence[Mapping[str, Any]],
    market: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """把状态翻译成「今晚该做什么」。最多三条, 按紧急度排序。"""
    actions: list[dict[str, str]] = []
    market = market or {}
    if market.get("state") == "risk_off" and market.get("breadth_20d") is not None:
        # 这条排在最前: 它是唯一「现在就能改变下注」的结论, 而且不依赖任何
        # 未经检验的择时参数 —— 只说 β 没有顺风, 不说市场要怎么走。
        actions.append(
            {
                "page": "market",
                "level": "warn",
                "text": (
                    f"β 逆风: 近 {market.get('window')} 日仅 "
                    f"{market['breadth_20d']:.0%} 个股上涨, 等权日均 "
                    f"{float(market.get('eq_ma20_bp') or 0.0):+.1f}bp —— 中长期多头腿没有顺风, "
                    "当日回合类不受影响"
                ),
            }
        )
    stale = [item for item in freshness if item["state"] in {"stale", "never"}]
    for item in stale:
        if item["state"] == "stale":
            text = f"{item['label']}页 {item['age_days']} 天没有新的{_subject_short(item['page'])}"
        else:
            text = f"{item['label']}页还没有任何{_subject_short(item['page'])}"
            # 「没有自动产出」不等于「页面坏了」: 手工内容不受这条影响, 必须说清,
            # 否则用户会把一条从未触发的自动化当成故障。
            if item["page"] == "strategy":
                text += "（手工创建/导入的策略不受影响）"
            elif item["page"] == "backtest":
                text += "（策略未入护时不会产生复验）"
        actions.append(
            {
                "page": str(item["page"]),
                "level": "warn" if item["state"] == "stale" else "info",
                "text": text,
            }
        )
    if not autonomy.get("any_enabled"):
        actions.append(
            {
                "page": "settings",
                "level": "info",
                "text": "研究自动化未开启: 挖掘与守护都关着, 只有手动触发才产出",
            }
        )
    blocked_unadopted = [
        item for item in admission if item["stage"] in {"unadopted", "guardian_status"}
    ]
    if blocked_unadopted and not stale:
        actions.append(
            {
                "page": "backtest",
                "level": "info",
                "text": f"{len(blocked_unadopted)} 个策略未通过准入: 先入护并等复验",
            }
        )
    return actions[:3]


def _subject_short(page: str) -> str:
    return {
        "factor": "因子研究产出",
        "strategy": "候选沉淀",
        "backtest": "样本外复验",
    }.get(page, "产出")
