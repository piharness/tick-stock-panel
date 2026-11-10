"""策略声明审计协议：五道门。

设计目标只有一个——任何"这个信号有超额"的声明，在被采纳前必须自己证明
它没有犯过这六轮里已经发生过的每一个假阳性错误。

五道门，每道对应一个真实发生过的错误，不是先验清单：

    G1 executable_window     H1 = close[T+1]/open[T+1] 是当日买卖，T+1 下不成立
    G2 aggregation           "炸板低吸" 按笔 +117.5bp(t=7.19) / 按日 -9.3bp(t=-0.30)
    G3 matched_benchmark     "上榜平盘" -291bp 在换手率匹配后塌到 -85.6bp(t=-0.97)
    G4 mde_precondition      先报"这个样本能测出多大"，再报结论
    G5 cost_multiplicity     Bonferroni 族系阈值 + 往返成本后的年化

调用方只承担一件事：把声明的**口径**说全（成交价、聚合单位、基准维度、检验次数）。
协议不重算收益，只审口径与统计关系——因此可以被任何策略、任何数据源复用。

**它不是验证器，是拒绝器。** 默认答案是不采纳；通过五道门只是"没被这套规则否掉"。

三条硬约束，专门堵"少填一项 / 多报一项就变绿"这类缺陷：

1. **缺省即红。** 任何一项口径缺失都产生 FAIL，不存在"没填 = 没意见"。
   曾经的 `n_tests=1`、`entry_unfillable_share=0.0`、`effect_bp=None`（→ SKIP）
   都是缺省即绿，诚实填写与撒谎在这里没有区别。
2. **判据不能由调用方自报。** 事件定义变量必须来自 `EVENT_REGISTRY`（只增不改），
   检验次数必须显式给出，族系阈值只能比 Bonferroni **更严**，MDE 必须带与自己
   同一个 universe 的标签。把这几项留给调用方，等于把门的开合权交给被审对象。
3. **配对优先于自报。** 持有天数由 (entry, exit) 推导，调用方写下的数只能用来
   与推导值核对；不一致即红。

用法::

    from eval_protocol import Claim, evaluate, render
    verdict = evaluate(Claim(name="...", entry="open_t1", exit="close_t2", ...))
    print(render(verdict))
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any

# --------------------------------------------------------------------------- #
# 登记表：这些常量是判据的一部分，改动等于改协议
# --------------------------------------------------------------------------- #

#: 合法的样本宇宙。跨宇宙的数不能互抄（主板 −160.0bp 与全市场 −120.0bp 是两个数）。
UNIVERSES = frozenset({"all_a", "mainboard", "chinext", "star", "bse"})

#: 已知会单独解释超额的混杂维度。缺一个，效应就可能只是它的另一种写法。
KNOWN_CONFOUNDS = frozenset({"board", "pct", "turnover"})

#: 事件自身的定义变量（不属于混杂维度，只用来解释"这个事件是什么"）。
EVENT_ONLY_VARS = frozenset(
    {"weak_board", "late_weak", "volume_shock", "on_dragon_tiger", "consecutive_boards"}
)

#: 事件类型登记表。**只增不改**：新增条目必须写清 mechanism，且 defines 必须是
#: KNOWN_CONFOUNDS 的真子集——即任何事件都必须留下至少一个可匹配的混杂维度。
#: 一个"由全部混杂共同定义"的事件不存在可匹配的对照，只能判不可检验。
#:
#: 这张表取代了原先由调用方随手传的 `event_defining_vars`：那个集合的减法没有下限，
#: 把三个混杂全声明成"事件定义变量"就能让 G3 对任意基准放行，而且声明越宽门越松。
EVENT_REGISTRY: dict[str, dict[str, Any]] = {
    "dragon_tiger": {
        "defines": frozenset(),
        "mechanism": "上榜与否由交易所披露名单决定；板块/涨幅/换手都是条件变量，不是定义变量",
    },
    "turnover_shock": {
        "defines": frozenset({"turnover"}),
        "mechanism": "事件由换手率分位定义；turnover 不能同时做匹配维度，否则事件组与对照组合并",
    },
    "limit_up": {
        "defines": frozenset({"pct"}),
        "mechanism": "事件由收盘价触及涨停价定义，与板块有关但与换手无关",
    },
    "zha_ban": {
        "defines": frozenset({"pct"}),
        "mechanism": "炸板由涨幅曾触板且收盘未封定义；板块仍需匹配（10cm 与 20cm 同涨幅不同含义）",
    },
    "weak_board": {
        "defines": frozenset({"weak_board"}),
        "mechanism": "烂板由封板时长/开板结构定义，不消耗任何一个混杂维度",
    },
    "volume_shock": {
        "defines": frozenset({"volume_shock"}),
        "mechanism": "放量由成交量相对自身分布定义，与板块/涨幅/换手都独立",
    },
}


def _validate_registry(registry: dict[str, dict[str, Any]] | None = None) -> None:
    """登记表自身也过门：非法条目在 import 阶段就该炸，而不是在使用时悄悄放行。"""
    allowed = KNOWN_CONFOUNDS | EVENT_ONLY_VARS
    for name, entry in (EVENT_REGISTRY if registry is None else registry).items():
        defines = entry["defines"]
        if not isinstance(defines, frozenset):
            raise RuntimeError(f"事件 {name!r} 的 defines 必须是 frozenset")
        if not defines <= allowed:
            raise RuntimeError(f"事件 {name!r} 声明了未登记的变量 {sorted(defines - allowed)}")
        if not entry.get("mechanism"):
            raise RuntimeError(f"事件 {name!r} 缺 mechanism")
        if not (KNOWN_CONFOUNDS - defines):
            raise RuntimeError(
                f"事件 {name!r} 由全部已知混杂定义，不存在可匹配的对照——不可检验，不得登记"
            )


_validate_registry()

#: 持有不足一个交易日 = 当日买卖，T+1 制度下不可执行。
MIN_HOLDING_DAYS = 1

#: 经济性门槛（年化净贡献）。**这是外生假设，不是数据**，调用方必须显式给出。
DEFAULT_HURDLE_ANNUAL_PCT = 3.0

#: 价格口径 -> (交易日序号, 日内分钟)。序号 0 = 信号日 T。
_PRICE_RE = re.compile(r"^(open|close)_t(\d*)$")


def price_mark(code: str) -> tuple[int, int] | None:
    """把 `open_t1` / `close_t3` 这类口径解析成 (交易日序号, 日内分钟)；不认识返回 None。"""
    match = _PRICE_RE.match(code or "")
    if not match:
        return None
    kind, raw = match.group(1), match.group(2)
    day = int(raw) if raw else 0
    return (day, 9 * 60 + 30) if kind == "open" else (day, 15 * 60)


def holding_days_between(entry: str, exit: str) -> int | None:
    """由成交价口径推导相隔的交易日数。任一端不认识返回 None。

    `open_t1 -> close_t1` = 0（当日买卖，T+1 下非法）；`open_t1 -> close_t3` = 2。
    """
    left, right = price_mark(entry), price_mark(exit)
    if left is None or right is None:
        return None
    return right[0] - left[0]


@dataclass
class Check:
    gate: str
    code: str
    status: str  # PASS / FAIL / WARN
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    #: 流程性失败：不是"效应不存在"，而是"这项口径没被报出来"。
    procedural: bool = False


@dataclass
class Claim:
    """一条待审的策略声明。字段缺失即视为口径不全，不视为通过。"""

    name: str
    universe: str = ""  # 必须取自 UNIVERSES：mainboard / all_a / chinext / star / bse

    # --- G1 成交口径 ---
    entry: str = ""
    exit: str = ""
    #: 调用方自报的持仓交易日数；只用于与 (entry, exit) 推导值核对，不作为判据。
    holding_days: int | None = None
    #: 入场时点实际买不进的占比（0~1）。**自报**，第 2 步起应由开板次数导出。
    entry_unfillable_share: float | None = None

    # --- G2 聚合与选股机制 ---
    agg_unit: str = ""  # per_day / per_trade
    per_trade_t: float | None = None
    per_day_t: float | None = None
    #: max_positions -> 年化收益(%)。用于检验结论是否只是"取了哪几只"。
    capacity_annual_pct: dict[int, float] | None = None

    # --- 效应与样本 ---
    effect_bp: float | None = None
    t_stat: float | None = None
    n_obs: int | None = None
    n_days: int | None = None

    # --- G3 基准 ---
    benchmark: str = ""
    benchmark_dims: frozenset[str] = frozenset()
    #: 事件的类型名，必须命中 EVENT_REGISTRY；缺省或未命中即红。
    event_type: str = ""
    #: 补齐混杂后的效应；用于判断"效应是否只是混杂的另一种写法"。
    matched_effect_bp: float | None = None
    matched_t: float | None = None

    # --- G4 检测下限 ---
    mde_bp: float | None = None
    #: MDE 所属宇宙。必须与 `universe` 相同，否则是跨口径借用。
    mde_universe: str = ""

    # --- G5 成本与多重检验 ---
    trigger_rate_per_stock_day: float | None = None
    hits_per_stock_year: float | None = None
    annual_gross_pct: float | None = None
    annual_net_pct: float | None = None
    cost_bp_roundtrip: float | None = None
    #: 族系检验次数。**无默认值**：缺省即"没报"，不是"只测了一次"。
    n_tests: int | None = None
    #: 若族系阈值已由别的口径算出，直接给；只能比 Bonferroni 更严，不能更松。
    bonferroni_threshold: float | None = None
    hurdle_annual_pct: float = DEFAULT_HURDLE_ANNUAL_PCT

    # --- 出处 ---
    provenance: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @property
    def event_definition(self) -> frozenset[str]:
        """该事件在登记表里的定义变量；未登记返回空集（由 G3 判红）。"""
        entry = EVENT_REGISTRY.get(self.event_type)
        return entry["defines"] if entry else frozenset()


@dataclass
class Verdict:
    name: str
    decision: str  # PASS / REJECT / REJECT_ECON
    reasons: list[str]
    checks: list[Check]
    stats: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "decision": self.decision,
            "reasons": self.reasons,
            "stats": self.stats,
            "checks": [
                {
                    "gate": c.gate,
                    "code": c.code,
                    "status": c.status,
                    "procedural": c.procedural,
                    "detail": c.detail,
                    "evidence": c.evidence,
                }
                for c in self.checks
            ],
        }


# --------------------------------------------------------------------------- #
# G1：只在能成交的窗口上谈超额
# --------------------------------------------------------------------------- #


def gate_executable_window(claim: Claim) -> list[Check]:
    out: list[Check] = []

    left, right = price_mark(claim.entry), price_mark(claim.exit)
    derived = holding_days_between(claim.entry, claim.exit)

    if not claim.entry or left is None:
        out.append(
            Check("G1", "unknown_entry", "FAIL", f"未登记的入场口径：{claim.entry!r}。")
        )
    if not claim.exit or right is None:
        out.append(
            Check("G1", "unknown_exit", "FAIL", f"未登记的出场口径：{claim.exit!r}。")
        )

    if claim.holding_days is None:
        out.append(
            Check(
                "G1",
                "holding_days_missing",
                "FAIL",
                "未声明入场到出场相隔几个交易日，无法与成交价口径核对。",
                procedural=True,
            )
        )

    if derived is None:
        out.append(
            Check(
                "G1",
                "window_undecidable",
                "FAIL",
                f"{claim.entry!r} → {claim.exit!r} 无法解析成交易日序号，窗口不可判定。",
                procedural=True,
            )
        )
    elif derived < MIN_HOLDING_DAYS:
        out.append(
            Check(
                "G1",
                "non_executable_window",
                "FAIL",
                f"{claim.entry} → {claim.exit} 相隔 {derived} 个交易日"
                f"（{'当日买入当日卖出' if derived == 0 else '出场早于入场'}）；"
                "A 股 T+1 下不存在这个窗口。",
                {"entry": claim.entry, "exit": claim.exit, "derived_days": derived},
            )
        )
    else:
        out.append(
            Check(
                "G1",
                "window_executable",
                "PASS",
                f"{claim.entry} → {claim.exit}，相隔 {derived} 个交易日。",
                {"derived_days": derived},
            )
        )

    if derived is not None and claim.holding_days is not None and claim.holding_days != derived:
        out.append(
            Check(
                "G1",
                "declared_holding_mismatch",
                "FAIL",
                f"自报持有 {claim.holding_days} 个交易日，但 {claim.entry} → {claim.exit} "
                f"推导为 {derived} 个。自报数不能覆盖成交价口径。",
                {"declared": claim.holding_days, "derived": derived},
            )
        )

    share = claim.entry_unfillable_share
    if share is None:
        out.append(
            Check(
                "G1",
                "unfillable_share_missing",
                "FAIL",
                f"未声明 {claim.entry} 入场时买不进的占比。0 是一个读数，不是一个默认值。",
                procedural=True,
            )
        )
    elif share > 0.5:
        out.append(
            Check(
                "G1",
                "entry_not_fillable",
                "FAIL",
                f"以 {claim.entry} 入场，但 {share:.1%} 的样本当时买不进；"
                "该口径把不可成交的样本算成了收益。",
                {"unfillable_share": share},
            )
        )
    elif share > 0:
        out.append(
            Check(
                "G1",
                "entry_partially_unfillable",
                "WARN",
                f"{share:.1%} 的样本买不进，需在结论里扣除（当前为自报值）。",
                {"unfillable_share": share},
            )
        )
    else:
        out.append(
            Check(
                "G1",
                "entry_fillable",
                "PASS",
                f"{claim.entry} 入场，自报全部可成交（该数为自报，未由开板次数导出）。",
                {"unfillable_share": 0.0},
            )
        )

    return out


# --------------------------------------------------------------------------- #
# G2：聚合单位决定 t 值是否成立
# --------------------------------------------------------------------------- #


def gate_aggregation(claim: Claim) -> list[Check]:
    out: list[Check] = []

    if claim.agg_unit not in {"per_day", "per_trade"}:
        out.append(
            Check(
                "G2",
                "aggregation_unknown",
                "FAIL",
                f"未声明聚合单位（per_day / per_trade），实为 {claim.agg_unit!r}。",
                procedural=True,
            )
        )
    elif claim.agg_unit == "per_trade":
        extra = ""
        if claim.per_day_t is not None:
            extra = f"同一信号按交易日聚合后 t={claim.per_day_t:.2f}。"
        out.append(
            Check(
                "G2",
                "per_trade_aggregation",
                "FAIL",
                "按笔做 t 检验。同日几十只同涨同跌不是独立样本，"
                f"t 值会虚高一个数量级甚至翻向。{extra}",
                {"per_trade_t": claim.per_trade_t, "per_day_t": claim.per_day_t},
            )
        )
    else:
        out.append(
            Check("G2", "aggregation_per_day", "PASS", "按交易日聚合成组合收益后检验。")
        )

    # 即使自称按日，只要同时给出两种口径且符号相反，就是聚合假象的直接证据。
    if (
        claim.per_trade_t is not None
        and claim.per_day_t is not None
        and claim.per_trade_t * claim.per_day_t < 0
    ):
        out.append(
            Check(
                "G2",
                "sign_flip_under_aggregation",
                "FAIL",
                f"按笔 t={claim.per_trade_t:.2f} 与按日 t={claim.per_day_t:.2f} 符号相反，"
                "结论由聚合方式决定，不是由市场决定。",
                {"per_trade_t": claim.per_trade_t, "per_day_t": claim.per_day_t},
            )
        )

    # 组合收益必须和"买哪些"一起看：换一种容量/排序就消失的超额是选择偏差。
    cap = claim.capacity_annual_pct
    if cap and len(cap) >= 2:
        positions = sorted(cap)
        best, worst = cap[positions[0]], cap[positions[-1]]
        if best > 0:
            decay = (best - worst) / best
            if decay > 0.5:
                out.append(
                    Check(
                        "G2",
                        "selection_dependent",
                        "FAIL",
                        f"容量从 {positions[0]} 放宽到 {positions[-1]}，"
                        f"年化从 {best:.1f}% 掉到 {worst:.1f}%（衰减 {decay:.0%}）；"
                        "超额来自'买了哪几只'，不是来自信号。",
                        {"capacity": {str(k): v for k, v in cap.items()}, "decay": decay},
                    )
                )
            else:
                out.append(
                    Check(
                        "G2",
                        "selection_robust",
                        "PASS",
                        f"容量 {positions[0]}→{positions[-1]}，年化衰减 {decay:.0%}。",
                        {"decay": decay},
                    )
                )
    elif claim.annual_net_pct is not None:
        out.append(
            Check(
                "G2",
                "selection_unassessed",
                "FAIL",
                "报了年化净贡献却未给容量曲线（max_positions → 年化）。"
                "不报曲线就无法排除'超额只是买了哪几只'。",
                procedural=True,
            )
        )

    return out


# --------------------------------------------------------------------------- #
# G3：异动股的对照组必须是同板块、同涨幅、同换手
# --------------------------------------------------------------------------- #


def gate_matched_benchmark(claim: Claim) -> list[Check]:
    out: list[Check] = []

    if not claim.benchmark_dims or claim.benchmark in {"", "market", "market_equal"}:
        out.append(
            Check(
                "G3",
                "unmatched_benchmark",
                "FAIL",
                "基准是同日全市场等权。异动股比全市场，等于在比'异动 vs 不动'，"
                "测到的是异动本身，不是手法。",
                {"benchmark": claim.benchmark, "dims": sorted(claim.benchmark_dims)},
            )
        )
        return out

    registry_entry = EVENT_REGISTRY.get(claim.event_type)
    if registry_entry is None:
        out.append(
            Check(
                "G3",
                "unknown_event",
                "FAIL",
                f"事件类型 {claim.event_type!r} 未登记在 EVENT_REGISTRY。"
                "事件定义变量决定基准必须控制哪些维度，因此它不能由调用方自报。",
                {"event_type": claim.event_type, "registered": sorted(EVENT_REGISTRY)},
                procedural=True,
            )
        )
        return out

    defines = registry_entry["defines"]
    required = KNOWN_CONFOUNDS - defines
    missing = required - claim.benchmark_dims
    if not required:  # 登记表已禁止，双保险：减法下限不是可选项
        out.append(
            Check(
                "G3",
                "no_residual_confound",
                "FAIL",
                f"事件 {claim.event_type!r} 由全部已知混杂定义，不存在可匹配的对照，该声明不可检验。",
                procedural=True,
            )
        )
    elif missing:
        out.append(
            Check(
                "G3",
                "uncontrolled_confound",
                "FAIL",
                f"事件 {claim.event_type!r} 的定义变量为 {sorted(defines) or '无'}，"
                f"因此基准必须控制 {sorted(required)}；实际只控制了 "
                f"{sorted(claim.benchmark_dims)}，缺 {sorted(missing)}。",
                {
                    "required": sorted(required),
                    "controlled": sorted(claim.benchmark_dims),
                    "missing": sorted(missing),
                },
            )
        )
    else:
        out.append(
            Check(
                "G3",
                "benchmark_matched",
                "PASS",
                f"基准控制了 {sorted(claim.benchmark_dims)}，覆盖 {sorted(required)}。",
                {"required": sorted(required)},
            )
        )

    if claim.effect_bp is not None and claim.matched_effect_bp is None:
        out.append(
            Check(
                "G3",
                "matched_result_missing",
                "WARN",
                "未报补齐混杂后的效应，无法排除'效应只是混杂的另一种写法'。",
                procedural=True,
            )
        )

    if claim.matched_effect_bp is not None and claim.matched_t is not None:
        shrink = (
            abs(claim.matched_effect_bp) / abs(claim.effect_bp)
            if claim.effect_bp
            else float("nan")
        )
        if abs(claim.matched_t) < 2 and shrink < 0.5:
            out.append(
                Check(
                    "G3",
                    "confound_explains_effect",
                    "FAIL",
                    f"补齐混杂后效应从 {claim.effect_bp:.1f}bp 缩到 "
                    f"{claim.matched_effect_bp:.1f}bp（t={claim.matched_t:.2f}），"
                    "原效应基本可由该混杂解释。",
                    {
                        "raw_bp": claim.effect_bp,
                        "matched_bp": claim.matched_effect_bp,
                        "matched_t": claim.matched_t,
                        "shrink": shrink,
                    },
                )
            )

    return out


# --------------------------------------------------------------------------- #
# G4：先报"能测出多大"，再报"测到了多少"
# --------------------------------------------------------------------------- #


def gate_mde_precondition(claim: Claim) -> list[Check]:
    out: list[Check] = []

    if claim.universe not in UNIVERSES:
        out.append(
            Check(
                "G4",
                "unknown_universe",
                "FAIL",
                f"样本宇宙 {claim.universe!r} 未登记。跨宇宙的数不能互抄，"
                f"合法取值 {sorted(UNIVERSES)}。",
                {"universe": claim.universe},
                procedural=True,
            )
        )

    if claim.effect_bp is None:
        out.append(
            Check(
                "G4",
                "effect_missing",
                "FAIL",
                "未报效应量。没有效应量的声明不是'跳过检测下限'，是没有任何东西可采纳。",
                procedural=True,
            )
        )
        return out

    if claim.mde_bp is None or claim.mde_bp <= 0:
        out.append(
            Check(
                "G4",
                "mde_missing",
                "FAIL",
                "未报检测下限。样本能测出的最小效应是结论的前置条件，"
                "不报下限就报方向，等于不知道这个数是信号还是噪声。",
                procedural=True,
            )
        )
        return out

    if not claim.mde_universe:
        out.append(
            Check(
                "G4",
                "mde_universe_missing",
                "FAIL",
                f"检测下限未标注所属宇宙（样本口径为 {claim.universe!r}）。"
                "MDE 随样本量变化，借用别处的值等于换了一个判据。",
                procedural=True,
            )
        )
    elif claim.mde_universe != claim.universe:
        out.append(
            Check(
                "G4",
                "universe_mismatch",
                "FAIL",
                f"效应取自 {claim.universe!r}，检测下限取自 {claim.mde_universe!r}。"
                "两个宇宙不能共用一个下限：样本更少的那个宇宙，MDE 只会更大。",
                {"universe": claim.universe, "mde_universe": claim.mde_universe},
            )
        )

    ratio = abs(claim.effect_bp) / claim.mde_bp
    if ratio < 1.0:
        out.append(
            Check(
                "G4",
                "below_detection_limit",
                "FAIL",
                f"|效应| {abs(claim.effect_bp):.1f}bp 低于检测下限 {claim.mde_bp:.1f}bp，"
                "这个样本无法把它和 0 区分开。",
                {"effect_bp": claim.effect_bp, "mde_bp": claim.mde_bp, "ratio": ratio},
            )
        )
    elif ratio < 1.5:
        out.append(
            Check(
                "G4",
                "mde_thin",
                "WARN",
                f"|效应| / 检测下限 = {ratio:.2f}，仅略高于可测边界。",
                {"ratio": ratio},
            )
        )
    else:
        out.append(
            Check(
                "G4",
                "mde_cleared",
                "PASS",
                f"|效应| / 检测下限 = {ratio:.2f}（{claim.universe} 口径）。",
                {"ratio": ratio},
            )
        )

    return out


def bonferroni_threshold(n_tests: int, alpha: float = 0.05) -> float:
    """双侧 Bonferroni 阈值（正态近似）。"""
    n = max(int(n_tests), 1)
    return NormalDist().inv_cdf(1.0 - alpha / (2.0 * n))


# --------------------------------------------------------------------------- #
# G5：扣完成本、扣完多重检验还剩多少
# --------------------------------------------------------------------------- #


def gate_cost_multiplicity(claim: Claim) -> list[Check]:
    out: list[Check] = []

    if claim.t_stat is None:
        out.append(
            Check(
                "G5",
                "t_missing",
                "FAIL",
                "未给 t 值：族系校正与经济性都无从判定。",
                procedural=True,
            )
        )
        return out

    if claim.n_tests is None:
        out.append(
            Check(
                "G5",
                "n_tests_missing",
                "FAIL",
                "未声明检验次数。缺省不等于'只测过一次'——那正是族系校正失效的方式。",
                procedural=True,
            )
        )
        threshold = bonferroni_threshold(1)
    elif claim.n_tests < 1:
        out.append(
            Check(
                "G5",
                "n_tests_invalid",
                "FAIL",
                f"检验次数 {claim.n_tests} 不是正数。",
                procedural=True,
            )
        )
        threshold = bonferroni_threshold(1)
    else:
        computed = bonferroni_threshold(claim.n_tests)
        if claim.bonferroni_threshold is not None and claim.bonferroni_threshold < computed - 1e-9:
            out.append(
                Check(
                    "G5",
                    "threshold_too_loose",
                    "FAIL",
                    f"自报族系阈值 {claim.bonferroni_threshold:.2f} 比 "
                    f"{claim.n_tests} 次检验的 Bonferroni 阈值 {computed:.2f} 更松。"
                    "想要更松的校正方法，必须改协议本身并写明理由，不能靠传参。",
                    {
                        "declared": claim.bonferroni_threshold,
                        "computed": computed,
                        "n_tests": claim.n_tests,
                    },
                )
            )
            threshold = computed
        else:
            threshold = (
                claim.bonferroni_threshold
                if claim.bonferroni_threshold is not None
                else computed
            )

    single_pass = abs(claim.t_stat) >= 1.96
    multi_pass = abs(claim.t_stat) >= threshold

    if multi_pass:
        out.append(
            Check(
                "G5",
                "multiplicity_pass",
                "PASS",
                f"|t|={abs(claim.t_stat):.2f} ≥ 族系阈值 {threshold:.2f}"
                f"（{claim.n_tests} 次检验）。",
                {"t": claim.t_stat, "threshold": threshold, "n_tests": claim.n_tests},
            )
        )
    else:
        out.append(
            Check(
                "G5",
                "multiplicity_fail",
                "FAIL",
                f"|t|={abs(claim.t_stat):.2f} < 族系阈值 {threshold:.2f}"
                f"（{claim.n_tests} 次检验）；单次检验过线={single_pass}。"
                "没做过族系校正的显著性不算显著性。",
                {
                    "t": claim.t_stat,
                    "threshold": threshold,
                    "n_tests": claim.n_tests,
                    "single_test_pass": single_pass,
                },
            )
        )

    if claim.annual_net_pct is None:
        out.append(
            Check(
                "G5",
                "economy_unassessed",
                "FAIL",
                "未给年化净贡献。触发率 × 单次效应 × 242 − 成本是决定'要不要建'的那个数，"
                "不报它就只剩显著性。",
                procedural=True,
            )
        )
    elif claim.annual_net_pct < claim.hurdle_annual_pct:
        out.append(
            Check(
                "G5",
                "below_hurdle",
                "FAIL",
                f"年化净贡献 {claim.annual_net_pct:.2f}% < 门槛 "
                f"{claim.hurdle_annual_pct:.1f}%。"
                + (
                    f"毛 {claim.annual_gross_pct:.2f}% 扣成本后所剩无几。"
                    if claim.annual_gross_pct is not None
                    else ""
                ),
                {
                    "annual_net_pct": claim.annual_net_pct,
                    "annual_gross_pct": claim.annual_gross_pct,
                    "hurdle": claim.hurdle_annual_pct,
                },
            )
        )
    else:
        out.append(
            Check(
                "G5",
                "economy_cleared",
                "PASS",
                f"年化净贡献 {claim.annual_net_pct:.2f}% ≥ 门槛 "
                f"{claim.hurdle_annual_pct:.1f}%。",
                {"annual_net_pct": claim.annual_net_pct},
            )
        )

    if claim.cost_bp_roundtrip is None:
        out.append(
            Check(
                "G5",
                "cost_missing",
                "WARN",
                "未声明往返成本口径，净贡献无法核对。",
                procedural=True,
            )
        )

    # 常驻警示：这几个数是调用方报的，协议无从核对。写进判决而不是只写进文档，
    # 是为了让每一个 PASS 都自带"它在哪几项上是自报"的标注。
    self_reported = [
        field_name
        for field_name, value in (
            ("n_tests", claim.n_tests),
            ("entry_unfillable_share", claim.entry_unfillable_share),
            ("mde_bp", claim.mde_bp),
        )
        if value is not None
    ]
    if self_reported:
        out.append(
            Check(
                "G5",
                "self_reported_inputs",
                "WARN",
                f"以下判据输入由调用方自报，协议无法核对：{', '.join(self_reported)}。"
                "n_tests 应改为从只增不改的实验登记表读数，买不进占比应由开板次数导出。",
                {"fields": self_reported},
                procedural=True,
            )
        )

    return out


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def evaluate(claim: Claim) -> Verdict:
    checks: list[Check] = []
    for gate in (
        gate_executable_window,
        gate_aggregation,
        gate_matched_benchmark,
        gate_mde_precondition,
        gate_cost_multiplicity,
    ):
        checks.extend(gate(claim))

    fails = [c for c in checks if c.status == "FAIL"]
    reasons = [c.code for c in fails]

    if not fails:
        decision = "PASS"
    else:
        statistical = [c for c in fails if c.code == "multiplicity_fail"]
        economic = [c for c in fails if c.code == "below_hurdle"]
        other = [c for c in fails if c.code not in {"multiplicity_fail", "below_hurdle"}]
        if not other and not statistical and economic:
            decision = "REJECT_ECON"
        else:
            decision = "REJECT"

    derived = holding_days_between(claim.entry, claim.exit)

    stats = {
        "effect_bp": claim.effect_bp,
        "t_stat": claim.t_stat,
        "n_obs": claim.n_obs,
        "n_days": claim.n_days,
        "mde_bp": claim.mde_bp,
        "mde_ratio": (
            abs(claim.effect_bp) / claim.mde_bp
            if claim.effect_bp is not None and claim.mde_bp
            else None
        ),
        "n_tests": claim.n_tests,
        "bonferroni_threshold": (
            claim.bonferroni_threshold
            if claim.bonferroni_threshold is not None
            else bonferroni_threshold(claim.n_tests or 1)
        ),
        "single_test_pass": abs(claim.t_stat) >= 1.96 if claim.t_stat is not None else None,
        "annual_gross_pct": claim.annual_gross_pct,
        "annual_net_pct": claim.annual_net_pct,
        "hurdle_annual_pct": claim.hurdle_annual_pct,
        "universe": claim.universe,
        "mde_universe": claim.mde_universe,
        "event_type": claim.event_type,
        "event_definition": sorted(claim.event_definition),
        "required_dims": sorted(KNOWN_CONFOUNDS - claim.event_definition),
        "derived_holding_days": derived,
        "declared_holding_days": claim.holding_days,
    }

    return Verdict(claim.name, decision, reasons, checks, stats)


def render(verdict: Verdict, width: int = 96) -> str:
    head = f"{verdict.name}  →  {verdict.decision}"
    lines = [head, "-" * width]
    mark = {"PASS": "·", "FAIL": "X", "WARN": "!", "SKIP": "-"}
    for c in verdict.checks:
        lines.append(f"[{mark.get(c.status, '?')}] {c.gate} {c.code:<28} {c.detail}")
    if verdict.reasons:
        lines.append("-" * width)
        lines.append("拒绝理由: " + ", ".join(verdict.reasons))
    return "\n".join(lines)
