"""主动 Alpha 搜索: 生成新假设, 用现有因子检验引擎做发现/确认两段验证。

与 `auto_mining` 的区别: 挖掘只在已有因子目录上做组合搜索; 本模块先生成
**新的 DSL 公式** (时序变换 + 少量经济上有意义的交互), 再走同一条
`FactorBacktestService.run_batch` 检验链路。搜索空间是预先写死的模板网格,
不是遗传规划/随机搜索 —— 试验次数必须可数, 否则多重检验无法校正。

统计纪律:
- 发现期 (discovery) 只用于生成与筛选; 确认期 (confirmation) 在筛选完成后
  才读取一次, 不参与方向、阈值或模板选择。
- 发现期 q 值取批量 BH-FDR (与 auto_mining 同源); 确认期再对幸存者做一次 BH。
- 试验台账记录每个公式的发现/确认读数, 重复运行默认跳过已测公式; 这是
  "研究记忆", 也是 DSR 的 trial count 来源。
- DSR 只作诊断, 不作唯一门槛; 通过发现 + 确认 + 同向 + 新颖性才标记 confirmed。
- 不自动发布策略, 不写用户策略目录; 候选只落研究目录, 由人工决定下一步。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from app.backtest import stats_v2
from app.backtest.factor import (
    FactorBacktestService,
    FactorBatchConfig,
    FactorBatchItem,
)
from app.factors.dsl import compile_formula
from app.factors.registry import FactorSpec, get_factor, isolated_registry, register_factor

logger = logging.getLogger(__name__)

Profile = Literal["exploratory", "balanced", "strict"]
Direction = Literal["high", "low"]

ANNUAL_TRADING_DAYS = 252
MIN_WINDOW_DAYS = 60
MAX_FORMULAS = 600
MAX_NOVELTY_SAMPLE_DAYS = 60

# 只放经济含义清楚、且 warmup 不会吃掉发现期的因子。财务因子在 v1 排除:
# 公告日口径下覆盖率低, 与价量因子的 IC 不可直接比。
SEEDS: tuple[str, ...] = (
    "momentum_20d",
    "momentum_60d",
    "momentum_120d",
    "rsi_14",
    "macd_hist_pct",
    "boll_position",
    "annual_vol_20d",
    "atr_pct",
    "downside_vol_20d",
    "vol_regime_5_60",
    "vol_ratio_5d",
    "amount_ratio_5d",
    "turnover_z_60d",
    "amihud_20d",
    "max_ret_20d",
    "ret_skew_20d",
    "limit_up_count_20d",
    "close_position",
    "distance_to_high_60d",
    "log_float_mv",
)

WINDOWS: tuple[int, ...] = (5, 20, 60, 120)

# 单因子模板: 全部先做截面 rank, 输出尺度一致, 避免不同量纲直接相加。
UNARY_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("delta", "rank(ts_delta({s}, {w}))"),
    ("ts_rank", "rank(ts_rank({s}, {w}))"),
    ("decay", "rank(decay_linear({s}, {w}))"),
    ("risk_adj", "rank({s} / ts_std({s}, {w}))"),
    ("deviation", "rank({s} - ts_mean({s}, {w}))"),
)

# 交互模板只取经济上能讲通的配对, 不做全组合 (全组合会把试验数推高一个量级)。
PAIRS: tuple[tuple[str, str], ...] = (
    ("momentum_20d", "annual_vol_20d"),
    ("momentum_20d", "turnover_z_60d"),
    ("momentum_60d", "amihud_20d"),
    ("rsi_14", "vol_ratio_5d"),
    ("boll_position", "amount_ratio_5d"),
    ("max_ret_20d", "ret_skew_20d"),
    ("close_position", "turnover_z_60d"),
    ("distance_to_high_60d", "vol_regime_5_60"),
    ("limit_up_count_20d", "turnover_z_60d"),
    ("log_float_mv", "amihud_20d"),
)

PAIR_WINDOWS: tuple[int, ...] = (5, 20, 60)

PAIR_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("corr", "rank(ts_corr({a}, {b}, {w}))"),
    ("spread", "rank(ts_mean({a}, {w}) - ts_mean({b}, {w}))"),
)


@dataclass(frozen=True)
class DiscoveryGate:
    min_abs_ic: float
    min_abs_ir: float
    min_abs_t: float
    max_q: float
    min_coverage: float
    max_turnover: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class ConfirmationGate:
    min_abs_ic: float
    min_abs_t: float
    max_q: float
    min_coverage: float
    max_turnover: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


# 门槛按 profile 放宽/收紧; q 在 exploratory 允许缺失 (小样本 BH 保守),
# balanced/strict 必须拿到 q, 否则无法证明多重检验已过。
DISCOVERY_GATES: dict[str, DiscoveryGate] = {
    "exploratory": DiscoveryGate(0.015, 0.15, 1.5, 0.20, 0.60, 0.95),
    "balanced": DiscoveryGate(0.020, 0.30, 2.0, 0.10, 0.70, 0.90),
    "strict": DiscoveryGate(0.030, 0.50, 2.5, 0.05, 0.80, 0.80),
}

CONFIRMATION_GATES: dict[str, ConfirmationGate] = {
    "exploratory": ConfirmationGate(0.010, 1.0, 0.20, 0.60, 0.95),
    "balanced": ConfirmationGate(0.010, 1.5, 0.10, 0.70, 0.90),
    "strict": ConfirmationGate(0.015, 2.0, 0.05, 0.80, 0.80),
}


@dataclass(frozen=True)
class AlphaSearchConfig:
    profile: Profile = "balanced"
    discovery_start: date = date(2025, 9, 1)
    discovery_end: date = date(2026, 4, 30)
    confirmation_start: date = date(2026, 5, 1)
    confirmation_end: date = date(2026, 11, 6)
    max_formulas: int = 360
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "daily"
    asset_type: str = "stock"
    novelty_max_corr: float = 0.80
    novelty_sample_days: int = 20
    allow_repeat: bool = False
    min_dsr: float = 0.90
    confirmed_max_q: float = 0.01

    def validate(self) -> None:
        if self.profile not in DISCOVERY_GATES:
            raise ValueError(f"unknown profile: {self.profile}")
        if not 1 <= self.max_formulas <= MAX_FORMULAS:
            raise ValueError(f"max_formulas must be between 1 and {MAX_FORMULAS}")
        if self.discovery_end >= self.confirmation_start:
            raise ValueError("discovery window must end before confirmation starts")
        if self.confirmation_end <= self.confirmation_start:
            raise ValueError("confirmation window must be non-empty")
        for name, start, end in (
            ("discovery", self.discovery_start, self.discovery_end),
            ("confirmation", self.confirmation_start, self.confirmation_end),
        ):
            if (end - start).days < MIN_WINDOW_DAYS:
                raise ValueError(f"{name} window must span at least {MIN_WINDOW_DAYS} days")
        if not 0.0 < self.novelty_max_corr <= 1.0:
            raise ValueError("novelty_max_corr must be in (0, 1]")
        if not 1 <= self.novelty_sample_days <= MAX_NOVELTY_SAMPLE_DAYS:
            raise ValueError(
                f"novelty_sample_days must be between 1 and {MAX_NOVELTY_SAMPLE_DAYS}"
            )
        if not 0.0 <= self.min_dsr <= 1.0:
            raise ValueError("min_dsr must be in [0, 1]")
        if not 0.0 < self.confirmed_max_q <= 1.0:
            raise ValueError("confirmed_max_q must be in (0, 1]")

    @property
    def discovery_gate(self) -> DiscoveryGate:
        return DISCOVERY_GATES[self.profile]

    @property
    def confirmation_gate(self) -> ConfirmationGate:
        return CONFIRMATION_GATES[self.profile]


@dataclass(frozen=True)
class GeneratedFormula:
    candidate_id: str
    formula: str
    canonical: str
    label: str
    template: str
    seeds: tuple[str, ...]
    window: int
    warmup_bars: int
    dependencies: frozenset[str] = field(default_factory=frozenset)


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def canonical_formula(text: str) -> str:
    """公式去空白/统一小写; 只用于去重和台账主键, 不改变语义。"""
    return re.sub(r"\s+", "", text).lower()


def _formula_id(canonical: str) -> str:
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"uf_as_{digest}"


def _candidate_id(canonical: str, config: AlphaSearchConfig) -> str:
    payload = "|".join((
        canonical,
        config.discovery_start.isoformat(),
        config.discovery_end.isoformat(),
        config.confirmation_start.isoformat(),
        config.confirmation_end.isoformat(),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _make_formula(template: str, text: str, seeds: Sequence[str], window: int) -> GeneratedFormula | None:
    compiled = compile_formula(text)
    if not compiled.ok:
        logger.debug("skip invalid alpha formula %s: %s", text, compiled.errors[:1])
        return None
    canonical = canonical_formula(text)
    return GeneratedFormula(
        candidate_id=_formula_id(canonical),
        formula=text,
        canonical=canonical,
        label=f"{template}:{'+'.join(seeds)}:{window}",
        template=template,
        seeds=tuple(seeds),
        window=window,
        warmup_bars=int(compiled.warmup_bars),
        # 生成因子是临时研究定义, dependencies 必须包含被引用的 virtual 因子:
        # FactorBacktestService 用它做 base_deps 递归物化; 只放展开后的基础列会
        # 让 frame_transform 找不到 virtual 列 (例如 macd_hist_pct) 而整列缺失。
        dependencies=frozenset(compiled.dependencies | compiled.referenced_factors),
    )


def _round_robin(buckets: list[list[GeneratedFormula]], limit: int) -> list[GeneratedFormula]:
    """按模板轮转取, 保证截断时每个模板都有代表, 不让前几个模板吃掉预算。"""
    result: list[GeneratedFormula] = []
    cursor = 0
    while len(result) < limit and any(cursor < len(bucket) for bucket in buckets):
        for bucket in buckets:
            if cursor < len(bucket) and len(result) < limit:
                result.append(bucket[cursor])
        cursor += 1
    return result


def generate_candidates(max_formulas: int = 360) -> list[GeneratedFormula]:
    """生成预注册的公式族; 纯函数, 不读行情数据。"""
    if max_formulas <= 0:
        return []
    buckets: list[list[GeneratedFormula]] = []
    for template_name, template in UNARY_TEMPLATES:
        bucket: list[GeneratedFormula] = []
        for window in WINDOWS:
            for seed in SEEDS:
                formula = _make_formula(
                    template_name,
                    template.format(s=seed, w=window),
                    (seed,),
                    window,
                )
                if formula is not None:
                    bucket.append(formula)
        buckets.append(bucket)

    for template_name, template in PAIR_TEMPLATES:
        bucket = []
        for window in PAIR_WINDOWS:
            for left, right in PAIRS:
                formula = _make_formula(
                    template_name,
                    template.format(a=left, b=right, w=window),
                    (left, right),
                    window,
                )
                if formula is not None:
                    bucket.append(formula)
        buckets.append(bucket)

    ordered = _round_robin(buckets, min(max_formulas, MAX_FORMULAS))
    seen: set[str] = set()
    deduped: list[GeneratedFormula] = []
    for item in ordered:
        if item.canonical in seen:
            continue
        seen.add(item.canonical)
        deduped.append(item)
    return deduped


def family_hash(formulas: Sequence[GeneratedFormula]) -> str:
    payload = "\n".join(sorted(item.canonical for item in formulas))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _direction(ic: float | None) -> Direction | None:
    if ic is None or ic == 0:
        return None
    return "high" if ic > 0 else "low"


def evaluate_discovery(item: FactorBatchItem, config: AlphaSearchConfig) -> GateResult:
    """发现期门槛: 只在 discovery 数据上判断, 不看 confirmation。"""
    gate = config.discovery_gate
    reasons: list[str] = []
    if item.error:
        return GateResult(False, [f"计算失败: {item.error[:80]}"])
    ic = _finite(item.ic_mean)
    ir = _finite(item.ir)
    t_value = _finite(item.t_newey_west)
    q_value = _finite(item.q_value)
    coverage = _finite(item.coverage)
    turnover = _finite(item.turnover)
    if ic is None:
        reasons.append("无有效 IC")
    elif abs(ic) < gate.min_abs_ic:
        reasons.append(f"|IC|={abs(ic):.3f} < {gate.min_abs_ic:.3f}")
    if ir is None:
        reasons.append("无有效 ICIR")
    elif abs(ir) < gate.min_abs_ir:
        reasons.append(f"|ICIR|={abs(ir):.2f} < {gate.min_abs_ir:.2f}")
    if t_value is None:
        reasons.append("无 NW t 值")
    elif abs(t_value) < gate.min_abs_t:
        reasons.append(f"|t|={abs(t_value):.2f} < {gate.min_abs_t:.2f}")
    if q_value is None:
        if config.profile != "exploratory":
            reasons.append("无 BH q 值")
    elif q_value > gate.max_q:
        reasons.append(f"q={q_value:.3f} > {gate.max_q:.3f}")
    if coverage is None:
        reasons.append("无覆盖率")
    elif coverage < gate.min_coverage:
        reasons.append(f"覆盖率={coverage:.2f} < {gate.min_coverage:.2f}")
    if turnover is None:
        reasons.append("无换手率")
    elif turnover > gate.max_turnover:
        reasons.append(f"换手率={turnover:.2f} > {gate.max_turnover:.2f}")
    return GateResult(not reasons, reasons)


def evaluate_confirmation(
    item: FactorBatchItem,
    *,
    discovery_direction: Direction | None,
    config: AlphaSearchConfig,
    q_value: float | None = None,
) -> GateResult:
    """确认期门槛: 方向必须与发现期一致, 幅度可以更小但不能反向。"""
    gate = config.confirmation_gate
    reasons: list[str] = []
    if item.error:
        return GateResult(False, [f"计算失败: {item.error[:80]}"])
    ic = _finite(item.ic_mean)
    t_value = _finite(item.t_newey_west)
    coverage = _finite(item.coverage)
    turnover = _finite(item.turnover)
    if ic is None:
        reasons.append("无有效 IC")
    elif (discovery_direction == "high" and ic <= 0) or (
        discovery_direction == "low" and ic >= 0
    ):
        reasons.append("确认期 IC 方向反转")
    elif abs(ic) < gate.min_abs_ic:
        reasons.append(f"|IC|={abs(ic):.3f} < {gate.min_abs_ic:.3f}")
    if t_value is None:
        reasons.append("无 NW t 值")
    elif abs(t_value) < gate.min_abs_t:
        reasons.append(f"|t|={abs(t_value):.2f} < {gate.min_abs_t:.2f}")
    if q_value is not None and q_value > gate.max_q:
        reasons.append(f"q={q_value:.3f} > {gate.max_q:.3f}")
    if coverage is None:
        reasons.append("无覆盖率")
    elif coverage < gate.min_coverage:
        reasons.append(f"覆盖率={coverage:.2f} < {gate.min_coverage:.2f}")
    if turnover is None:
        reasons.append("无换手率")
    elif turnover > gate.max_turnover:
        reasons.append(f"换手率={turnover:.2f} > {gate.max_turnover:.2f}")
    return GateResult(not reasons, reasons)


def deflated_sharpe_for_trials(
    trial_irs: Sequence[float | None],
    *,
    candidate_ir: float | None,
    n_obs: int | None,
) -> tuple[float | None, float | None]:
    """返回 (DSR 概率, 年化期望最大 |IR|)。

    输入是日频 ICIR (`mean(IC)/std(IC)`), 不是多空组合 Sharpe。原因:
    分层多空组合在样本短、因子集中时 Sharpe 会出现 ±50 的离群值, 直接代入
    DSR 会把 EM 抬到 12+ 年化, 任何候选都被判成噪声。ICIR 与筛选口径同源,
    方差稳定, 适合做"搜索了多少次"的校正。

    方向校正: 每个公式允许做多/做空两个方向, 因此有效试验数按 2N 计;
    候选值取 |IR|。否则"按发现期 IC 符号翻方向"会把每个试验都变成正 IR,
    零分布被系统性抬高, DSR 会虚高。
    """
    raw = [float(value) for value in trial_irs if value is not None]
    if len(raw) < 2 or candidate_ir is None or n_obs is None or n_obs < 5:
        return None, None
    variance = float(np.var(raw, ddof=1))
    expected = stats_v2.expected_max_sharpe(2 * len(raw), variance)
    dsr = stats_v2.deflated_sharpe_psr(
        abs(candidate_ir),
        n_obs,
        expected_max_sharpe=expected,
    )
    return dsr, expected * math.sqrt(ANNUAL_TRADING_DAYS)


def classify_candidate(
    *,
    discovery_pass: bool,
    discovery_reason: str | None,
    confirmation_pass: bool,
    confirmation_reason: str | None,
    confirmation_q: float | None,
    novelty_pass: bool,
    novelty_reason: str | None,
    dsr: float | None,
    config: AlphaSearchConfig,
) -> tuple[str, str]:
    """三态判定: 确认期 FDR 是硬门槛, DSR 只做诊断标注。

    确认期是筛选完成后才读取的独立样本; 对幸存者做 BH 后 q≤confirmed_max_q
    是"样本外复现"的直接证据。DSR 依赖试验间独立、正态零分布等近似, 不应
    单独否决一个已在独立窗口复现的候选; 低于 min_dsr 时写入原因, 让人看到
    两种证据的分歧。
    """
    if not discovery_pass:
        return "rejected", discovery_reason or "发现期未通过"
    if not confirmation_pass:
        return "rejected", confirmation_reason or "确认期未通过"
    if not novelty_pass:
        return "rejected", f"冗余: {novelty_reason or '与现有因子高度相关'}"
    if confirmation_q is None:
        return "provisional", "确认期通过, 但 BH q 不可计算"
    if confirmation_q > config.confirmed_max_q:
        return (
            "provisional",
            f"确认期 q={confirmation_q:.4f} > {config.confirmed_max_q:.4f}",
        )
    reason = f"确认期 q={confirmation_q:.4f} ≤ {config.confirmed_max_q:.4f}"
    if dsr is not None and dsr < config.min_dsr:
        reason += f"; DSR={dsr:.2f} < {config.min_dsr:.2f} (以确认期复现为主)"
    return "confirmed", reason


def _data_fingerprint(engine: Any, config: AlphaSearchConfig) -> dict[str, Any]:
    from app.tickflow.repository import enriched_dirname

    repo = getattr(engine, "repo", None)
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if data_dir is None:
        return {"status": "unavailable"}
    root = Path(data_dir) / enriched_dirname(config.asset_type)
    partitions = sorted(
        path.name for path in root.glob("date=*") if (path / "part.parquet").is_file()
    )
    digest = hashlib.sha256("\n".join(partitions).encode("utf-8")).hexdigest()[:16]
    return {
        "n_partitions": len(partitions),
        "first_date": partitions[0].removeprefix("date=") if partitions else None,
        "last_date": partitions[-1].removeprefix("date=") if partitions else None,
        "partition_digest": digest,
    }


def _batch(
    engine: Any,
    names: Sequence[str],
    start: date,
    end: date,
    config: AlphaSearchConfig,
) -> list[FactorBatchItem]:
    result = FactorBacktestService(engine).run_batch(
        FactorBatchConfig(
            factor_names=list(names),
            symbols=None,
            start=start,
            end=end,
            n_groups=config.n_groups,
            rebalance=config.rebalance,
            asset_type=config.asset_type,
        )
    )
    if result.error:
        raise RuntimeError(f"alpha search batch failed: {result.error}")
    return list(result.results)


def _metric_row(item: FactorBatchItem) -> dict[str, Any]:
    return {
        "ic": _finite(item.ic_mean),
        "ir": _finite(item.ir),
        "t": _finite(item.t_newey_west),
        "p": _finite(item.p_value),
        "q": _finite(item.q_value),
        "coverage": _finite(item.coverage),
        "turnover": _finite(item.turnover),
        "ls_return": _finite(item.long_short_return),
        "ls_sharpe": _finite(item.long_short_sharpe),
        "n_dates": item.n_dates,
        "n_symbols": item.n_symbols,
        "error": item.error,
    }


def _register_candidates(formulas: Sequence[GeneratedFormula]) -> None:
    """在 isolated registry 中注册生成公式; 退出上下文即整体丢弃。"""
    for formula in formulas:
        existing = get_factor(formula.candidate_id)
        if existing is not None:
            if existing.formula_text == formula.formula:
                continue
            raise ValueError(
                f"alpha candidate id collision: {formula.candidate_id} "
                "already registered with a different formula"
            )
        register_factor(
            FactorSpec(
                id=formula.candidate_id,
                label=formula.label,
                group="主动Alpha",
                formula_text=formula.formula,
                kind="custom",
                dependencies=formula.dependencies,
                warmup_bars=formula.warmup_bars,
                stability="experimental",
            )
        )


def _novelty_check(
    engine: Any,
    formulas: Sequence[GeneratedFormula],
    config: AlphaSearchConfig,
) -> dict[str, dict[str, Any]]:
    """与现有因子目录的最大截面秩相关; 数据不足时返回 degraded 而不是拒掉。"""
    if not formulas:
        return {}
    from app.factors.registry import factor_columns_view

    candidate_ids = {formula.candidate_id for formula in formulas}
    catalog: list[str] = []
    for item in factor_columns_view():
        fid = str(item["id"])
        if fid in candidate_ids:
            continue
        spec = get_factor(fid)
        if spec is None or spec.kind not in ("base", "virtual"):
            continue
        if "stock" not in spec.asset_types:
            continue
        catalog.append(fid)
    # 新颖性样本取发现期尾部; 必须从发现期起点之前加载, 否则 60/120 日窗口
    # 在样本尾部仍然是 null (相关全 NaN), 会把"算不出"误报成"无冗余"。
    start = config.discovery_start - timedelta(days=30)
    novelty_columns = [
        "symbol", "date", "open", "high", "low", "close", "volume", "amount",
        "turnover_rate", "consecutive_limit_ups",
    ]
    try:
        panel = engine.load_panel(
            None,
            start,
            config.discovery_end,
            columns=novelty_columns,
            asset_type=config.asset_type,
        )
    except Exception as exc:  # noqa: BLE001 - 新颖性检查是旁路, 不阻断主结果
        logger.warning("alpha novelty panel load failed: %s", exc)
        return {
            formula.candidate_id: {
                "status": "unavailable",
                "max_abs_corr": None,
                "max_corr_with": None,
                "pass": True,
                "reason": f"panel load failed: {exc}",
            }
            for formula in formulas
        }
    if panel.is_empty():
        return {
            formula.candidate_id: {
                "status": "insufficient_data",
                "max_abs_corr": None,
                "max_corr_with": None,
                "pass": True,
                "reason": "empty novelty panel",
            }
            for formula in formulas
        }

    dates = sorted(panel["date"].unique().to_list())[-config.novelty_sample_days:]
    needed = list(dict.fromkeys(
        [formula.candidate_id for formula in formulas] + catalog
    ))
    try:
        # 与批量检验同一条物化路径: virtual 因子走 compute_indicators,
        # custom 因子走 DSL; 不另写第二套计算。先物化再截样本日, 否则
        # 60/120 日窗口会因为没有预热数据而整列为 null。
        panel = FactorBacktestService._compute_missing_factors(panel, set(needed))
    except Exception as exc:  # noqa: BLE001 - 新颖性检查是旁路
        logger.warning("alpha novelty materialization failed: %s", exc)
        return {
            formula.candidate_id: {
                "status": "unavailable",
                "max_abs_corr": None,
                "max_corr_with": None,
                "pass": True,
                "reason": f"materialization failed: {exc}",
            }
            for formula in formulas
        }
    panel = panel.filter(pl.col("date").is_in(dates))
    available = [name for name in needed if name in panel.columns]
    if not available:
        return {
            formula.candidate_id: {
                "status": "insufficient_data",
                "max_abs_corr": None,
                "max_corr_with": None,
                "pass": True,
                "reason": "no materialized columns",
            }
            for formula in formulas
        }
    ranked = panel.select(
        ["date", *available]
    ).with_columns([
        pl.col(name).rank().over("date").alias(f"_r_{name}") for name in available
    ])
    results: dict[str, dict[str, Any]] = {}
    for formula in formulas:
        if formula.candidate_id not in available:
            results[formula.candidate_id] = {
                "status": "insufficient_data",
                "max_abs_corr": None,
                "max_corr_with": None,
                "pass": True,
                "reason": "formula column unavailable",
            }
            continue
        corr_exprs = [
            pl.corr(f"_r_{formula.candidate_id}", f"_r_{name}").alias(name)
            for name in catalog
            if name in available
        ]
        if not corr_exprs:
            results[formula.candidate_id] = {
                "status": "insufficient_data",
                "max_abs_corr": None,
                "max_corr_with": None,
                "pass": True,
                "reason": "no catalog factors available",
            }
            continue
        daily = ranked.group_by("date").agg(corr_exprs).sort("date")
        means = {
            name: _finite(daily[name].mean())
            for name in catalog
            if name in daily.columns
        }
        means = {name: value for name, value in means.items() if value is not None}
        if not means:
            results[formula.candidate_id] = {
                "status": "insufficient_data",
                "max_abs_corr": None,
                "max_corr_with": None,
                "pass": True,
                "reason": "no finite correlations",
            }
            continue
        worst_name = max(means, key=lambda name: abs(means[name]))
        worst = means[worst_name]
        results[formula.candidate_id] = {
            "status": "ok",
            "max_abs_corr": abs(worst),
            "max_corr_with": worst_name,
            "pass": abs(worst) <= config.novelty_max_corr,
            "reason": (
                None
                if abs(worst) <= config.novelty_max_corr
                else f"与 {worst_name} 的秩相关 {worst:+.2f} 超过阈值"
            ),
        }
    return results


def _prior_trial_keys(output_dir: Path) -> set[str]:
    keys: set[str] = set()
    for path in sorted((output_dir / "runs").glob("*/trials.parquet")):
        try:
            frame = pl.read_parquet(
                path,
                columns=[
                    "canonical",
                    "discovery_start",
                    "discovery_end",
                    "confirmation_start",
                    "confirmation_end",
                ],
            )
        except Exception as exc:  # noqa: BLE001 - 坏台账只跳过, 不影响新运行
            logger.warning("alpha search prior trial ledger unreadable %s: %s", path, exc)
            continue
        for row in frame.iter_rows(named=True):
            keys.add("|".join(str(row[column]) for column in (
                "canonical",
                "discovery_start",
                "discovery_end",
                "confirmation_start",
                "confirmation_end",
            )))
    return keys


def _trial_key(formula: GeneratedFormula, config: AlphaSearchConfig) -> str:
    return "|".join((
        formula.canonical,
        config.discovery_start.isoformat(),
        config.discovery_end.isoformat(),
        config.confirmation_start.isoformat(),
        config.confirmation_end.isoformat(),
    ))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_parquet(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.write_parquet(tmp)
    os.replace(tmp, path)


def _write_candidates(output_dir: Path, rows: Sequence[dict[str, Any]]) -> int:
    """全局候选台账 append-only; 以 ledger_id 幂等。

    ledger_id 含发现/确认窗口: 同一公式换一段样本重测是一次新实验,
    不能被公式级 candidate_id 去重掉。
    """
    if not rows:
        return 0
    path = output_dir / "candidates.jsonl"
    existing: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                existing.add(str(json.loads(line)["ledger_id"]))
            except Exception:  # noqa: BLE001 - 坏行不影响追加
                continue
    appended = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            if str(row["ledger_id"]) in existing:
                continue
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            existing.add(str(row["ledger_id"]))
            appended += 1
    return appended


def _format_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 主动 Alpha 搜索报告",
        "",
        f"- run: `{report['run_id']}`",
        f"- profile: `{report['config']['profile']}`",
        f"- 发现期: {report['config']['discovery_start']} .. {report['config']['discovery_end']}",
        f"- 确认期: {report['config']['confirmation_start']} .. {report['config']['confirmation_end']}",
        f"- 生成/编译/发现通过/确认通过: "
        f"{report['counts']['generated']} / {report['counts']['compiled']} / "
        f"{report['counts']['discovery_passed']} / {report['counts']['confirmation_passed']}",
        f"- confirmed 条数/独立主题: {report['counts'].get('confirmed', 0)} / "
        f"{report['counts'].get('confirmed_themes', 0)}",
        f"- 家族哈希: `{report['family_hash']}`",
        f"- 数据指纹: `{report['data_fingerprint'].get('partition_digest', 'n/a')}`",
        "",
        "## 确认候选",
        "",
    ]
    candidates = report.get("candidates", [])
    if candidates:
        lines.append(
            "| 公式 | 方向 | 发现 IC | 发现 t | 确认 IC | 确认 t | DSR | 最大相关 | 状态 |"
        )
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |")
        for row in candidates[:20]:
            def fmt(value: Any, digits: int = 3) -> str:
                return "—" if value is None else f"{value:.{digits}f}"

            lines.append(
                "| `{formula}` | {direction} | {dic} | {dt} | {cic} | {ct} | {dsr} | {corr} | {status} |".format(
                    formula=row["formula"],
                    direction=row["direction"],
                    dic=fmt(row.get("discovery_ic")),
                    dt=fmt(row.get("discovery_t"), 2),
                    cic=fmt(row.get("confirmation_ic")),
                    ct=fmt(row.get("confirmation_t"), 2),
                    dsr=fmt(row.get("dsr"), 2),
                    corr=(
                        "—"
                        if (row.get("novelty") or {}).get("max_abs_corr") is None
                        else f"{row['novelty']['max_abs_corr']:.2f} ({row['novelty']['max_corr_with']})"
                    ),
                    status=row["status"],
                )
            )
        if len(candidates) > 20:
            lines.append("")
            lines.append(f"仅展示前 20 条; 其余 {len(candidates) - 20} 条见 report.json / trials.parquet。")
        confirmed = [row for row in candidates if row["status"] == "confirmed"]
        clusters = report.get("clusters", [])
        if clusters:
            lines.append("")
            lines.append("## 独立主题（同 template+seeds 只保留确认期 q 最小的一条）")
            lines.append("")
            lines.append("| 主题 | 代表公式 | 确认 q | 确认 t | DSR |")
            lines.append("| --- | --- | ---: | ---: | ---: |")
            for row in clusters:
                lines.append(
                    "| `{cluster}` | `{formula}` | {q} | {t} | {dsr} |".format(
                        cluster=row.get("cluster", ""),
                        formula=row["formula"],
                        q=("—" if row.get("confirmation_q") is None else f"{row['confirmation_q']:.2e}"),
                        t=("—" if row.get("confirmation_t") is None else f"{row['confirmation_t']:+.2f}"),
                        dsr=("—" if row.get("dsr") is None else f"{row['dsr']:.2f}"),
                    )
                )
        if confirmed:
            lines.append("")
            lines.append("**confirmed 判定说明**（DSR 低时以确认期复现为主）:")
            for row in confirmed[:10]:
                lines.append(f"- `{row['formula']}`: {row.get('status_reason') or '未给出原因'}")
        if not confirmed:
            lines.append("")
            lines.append("**本次没有 `confirmed` 候选**。前三条的判定原因:")
            for row in candidates[:3]:
                lines.append(f"- `{row['formula']}`: {row.get('status_reason') or '未给出原因'}")
    else:
        lines.append("无候选同时通过发现期与确认期门槛。")
    lines.extend([
        "",
        "## 口径与限制",
        "",
        "- 发现期只用于筛选; 确认期在筛选完成后才读取, 不参与方向/阈值/模板选择。",
        "- 发现期 q 值为批量 BH-FDR; 确认期对幸存者再做一次 BH。",
        "- DSR 使用全部有限试验的日频 ICIR 方差与试验数 (方向按 2N 计), 只作诊断。",
        "- 因子多空收益是理论价差 (含双边成本), A 股不可直接做空; 负 IC 因子需反向使用。",
        "- 未计容量、冲击成本、停牌/涨跌停不可成交与幸存者偏差; 结果不能直接当交易信号。",
        "- 通过确认只代表该窗口内样本外未退化, 不代表未来持续。",
        "",
    ])
    return "\n".join(lines)


def run_alpha_search(
    engine: Any,
    config: AlphaSearchConfig | None = None,
    *,
    output_dir: Path,
    formulas: Sequence[GeneratedFormula] | None = None,
) -> dict[str, Any]:
    """执行一次主动搜索并落盘; 返回报告 dict。

    `formulas` 只供测试注入; 生产路径由 `generate_candidates` 生成。
    """
    config = config or AlphaSearchConfig()
    config.validate()
    output_dir = Path(output_dir)
    generated = list(formulas) if formulas is not None else generate_candidates(config.max_formulas)
    if not generated:
        raise ValueError("no candidates generated")
    family = family_hash(generated)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = output_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    fingerprint = _data_fingerprint(engine, config)

    prior = set() if config.allow_repeat else _prior_trial_keys(output_dir)
    fresh = [item for item in generated if _trial_key(item, config) not in prior]
    skipped = len(generated) - len(fresh)

    preregistration = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "family_hash": family,
        "config": asdict(config),
        "data_fingerprint": fingerprint,
        "n_formulas": len(generated),
        "n_fresh": len(fresh),
        "n_skipped_prior": skipped,
        "formulas": [
            {
                "candidate_id": item.candidate_id,
                "formula": item.formula,
                "template": item.template,
                "seeds": list(item.seeds),
                "window": item.window,
                "warmup_bars": item.warmup_bars,
            }
            for item in generated
        ],
    }
    _atomic_write_text(
        run_dir / "preregistration.json",
        json.dumps(preregistration, ensure_ascii=False, indent=2, default=str),
    )

    counts = {
        "generated": len(generated),
        "compiled": len(generated),
        "skipped_prior": skipped,
        "evaluated": 0,
        "discovery_passed": 0,
        "confirmation_evaluated": 0,
        "confirmation_passed": 0,
        "confirmed": 0,
        "provisional": 0,
        "rejected": 0,
    }
    rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    with isolated_registry():
        _register_candidates(generated)
        if fresh:
            discovery_items = _batch(
                engine,
                [item.candidate_id for item in fresh],
                config.discovery_start,
                config.discovery_end,
                config,
            )
            by_id = {item.factor_name: item for item in discovery_items}
            counts["evaluated"] = len(discovery_items)
            for formula in fresh:
                item = by_id.get(formula.candidate_id)
                if item is None:
                    rows.append({
                        "candidate_id": formula.candidate_id,
                        "canonical": formula.canonical,
                        "formula": formula.formula,
                        "template": formula.template,
                        "seeds": ",".join(formula.seeds),
                        "window": formula.window,
                        "discovery_start": config.discovery_start.isoformat(),
                        "discovery_end": config.discovery_end.isoformat(),
                        "confirmation_start": config.confirmation_start.isoformat(),
                        "confirmation_end": config.confirmation_end.isoformat(),
                        "direction": None,
                        "discovery_pass": False,
                        "discovery_reasons": "missing batch result",
                        "confirmation_pass": False,
                        "confirmation_reasons": None,
                        "status": "rejected",
                        "status_reason": "missing batch result",
                    })
                    continue
                metrics = _metric_row(item)
                direction = _direction(metrics["ic"])
                sign = 0.0 if direction is None else (1.0 if direction == "high" else -1.0)
                gate = evaluate_discovery(item, config)
                if gate.passed:
                    counts["discovery_passed"] += 1
                rows.append({
                    "candidate_id": formula.candidate_id,
                    "canonical": formula.canonical,
                    "formula": formula.formula,
                    "template": formula.template,
                    "seeds": ",".join(formula.seeds),
                    "window": formula.window,
                    "warmup_bars": formula.warmup_bars,
                    "discovery_start": config.discovery_start.isoformat(),
                    "discovery_end": config.discovery_end.isoformat(),
                    "confirmation_start": config.confirmation_start.isoformat(),
                    "confirmation_end": config.confirmation_end.isoformat(),
                    "direction": direction,
                    **{f"discovery_{key}": value for key, value in metrics.items()},
                    "discovery_signed_return": (
                        None if metrics["ls_return"] is None else sign * metrics["ls_return"]
                    ),
                    "discovery_signed_sharpe": (
                        None if metrics["ls_sharpe"] is None else sign * metrics["ls_sharpe"]
                    ),
                    "discovery_signed_ir": (
                        None if metrics["ir"] is None else sign * metrics["ir"]
                    ),
                    "discovery_pass": gate.passed,
                    "discovery_reasons": "; ".join(gate.reasons) or None,
                    "confirmation_pass": False,
                    "confirmation_reasons": None,
                    "status": "rejected",
                    "status_reason": "; ".join(gate.reasons) or None,
                })

            discovery_passers = [
                row for row in rows
                if row["discovery_pass"] and row["direction"] is not None
            ]
            if discovery_passers:
                ids = [row["candidate_id"] for row in discovery_passers]
                confirmation_items = _batch(
                    engine,
                    ids,
                    config.confirmation_start,
                    config.confirmation_end,
                    config,
                )
                counts["confirmation_evaluated"] = len(confirmation_items)
                by_conf = {item.factor_name: item for item in confirmation_items}
                p_values = [
                    _finite(by_conf[row["candidate_id"]].p_value)
                    if row["candidate_id"] in by_conf else None
                    for row in discovery_passers
                ]
                q_values = stats_v2.bh_fdr_qvalues(p_values)
                trial_irs = [
                    row.get("discovery_ir")
                    for row in rows
                    if row.get("discovery_ir") is not None
                ]
                for row, q_value in zip(discovery_passers, q_values, strict=True):
                    item = by_conf.get(row["candidate_id"])
                    if item is None:
                        row["confirmation_reasons"] = "missing batch result"
                        continue
                    metrics = _metric_row(item)
                    gate = evaluate_confirmation(
                        item,
                        discovery_direction=row["direction"],
                        config=config,
                        q_value=q_value,
                    )
                    row.update({
                        **{f"confirmation_{key}": value for key, value in metrics.items()},
                        "confirmation_q": q_value,
                        "confirmation_pass": gate.passed,
                        "confirmation_reasons": "; ".join(gate.reasons) or None,
                    })
                    if gate.passed:
                        counts["confirmation_passed"] += 1
                    row["status"] = "provisional" if gate.passed else "rejected"
                    row["status_reason"] = (
                        "确认期通过; 待新颖性/DSR 判定"
                        if gate.passed
                        else "; ".join(gate.reasons)
                    )

                provisional = [row for row in discovery_passers if row["confirmation_pass"]]
                if provisional:
                    novelty = _novelty_check(
                        engine,
                        [item for item in fresh if item.candidate_id in {
                            row["candidate_id"] for row in provisional
                        }],
                        config,
                    )
                    for row in provisional:
                        info = novelty.get(row["candidate_id"], {
                            "status": "unavailable",
                            "max_abs_corr": None,
                            "max_corr_with": None,
                            "pass": True,
                            "reason": "no novelty result",
                        })
                        row["novelty"] = info
                        dsr, expected_max = deflated_sharpe_for_trials(
                            trial_irs,
                            candidate_ir=row.get("discovery_ir"),
                            n_obs=row.get("discovery_n_dates"),
                        )
                        row["dsr"] = dsr
                        row["expected_max_ir_annual"] = expected_max
                        status, reason = classify_candidate(
                            discovery_pass=row["discovery_pass"],
                            discovery_reason=row.get("discovery_reasons"),
                            confirmation_pass=row["confirmation_pass"],
                            confirmation_reason=row.get("confirmation_reasons"),
                            confirmation_q=row.get("confirmation_q"),
                            novelty_pass=bool(info["pass"]),
                            novelty_reason=info.get("reason"),
                            dsr=dsr,
                            config=config,
                        )
                        row["status"] = status
                        row["status_reason"] = reason

    for row in rows:
        if row["status"] == "confirmed":
            counts["confirmed"] += 1
        elif row["status"] == "provisional":
            counts["provisional"] += 1
        else:
            counts["rejected"] += 1

    candidates = [
        row for row in rows if row["status"] in {"confirmed", "provisional"}
    ]
    for row in candidates:
        row["cluster"] = f"{row['template']}:{row['seeds']}"
    candidates.sort(key=lambda row: (
        0 if row["status"] == "confirmed" else 1,
        -abs(row.get("discovery_ic") or 0.0),
    ))

    # 同一 (template, seeds) 只换窗口的变体高度相关, 不能当成多个独立发现。
    # 每个主题保留确认期 q 最小的一条, 用于报告"独立主题数"。
    theme_best: dict[str, dict[str, Any]] = {}
    for row in candidates:
        if row["status"] != "confirmed":
            continue
        key = row["cluster"]
        current = theme_best.get(key)
        if current is None or (
            (row.get("confirmation_q") if row.get("confirmation_q") is not None else 1.0)
            < (current.get("confirmation_q") if current.get("confirmation_q") is not None else 1.0)
        ):
            theme_best[key] = row
    clusters = sorted(
        theme_best.values(),
        key=lambda row: row.get("confirmation_q") if row.get("confirmation_q") is not None else 1.0,
    )
    counts["confirmed_themes"] = len(clusters)

    report = {
        "run_id": run_id,
        "status": "ok" if rows else "no_new_hypotheses",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - started, 1),
        "config": asdict(config),
        "family_hash": family,
        "data_fingerprint": fingerprint,
        "counts": counts,
        "candidates": candidates,
        "clusters": clusters,
        "trials": rows,
        "artifacts": {
            "preregistration": str(run_dir / "preregistration.json"),
            "trials": str(run_dir / "trials.parquet") if rows else None,
            "report_json": str(run_dir / "report.json"),
            "report_md": str(run_dir / "report.md"),
        },
    }
    if rows:
        _atomic_write_parquet(run_dir / "trials.parquet", pl.DataFrame(rows))
    _atomic_write_text(
        run_dir / "report.json",
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
    )
    _atomic_write_text(run_dir / "report.md", _format_markdown(report))
    _atomic_write_text(
        run_dir / "manifest.json",
        json.dumps({
            "run_id": run_id,
            "status": report["status"],
            "created_at": report["created_at"],
            "elapsed_s": report["elapsed_s"],
            "family_hash": family,
            "counts": counts,
            "artifacts": report["artifacts"],
        }, ensure_ascii=False, indent=2, default=str),
    )
    if candidates:
        _write_candidates(output_dir, [
            {
                "ledger_id": _candidate_id(row["canonical"], config),
                "candidate_id": row["candidate_id"],
                "run_id": run_id,
                "created_at": report["created_at"],
                "formula": row["formula"],
                "canonical": row["canonical"],
                "direction": row["direction"],
                "template": row["template"],
                "seeds": row["seeds"],
                "window": row["window"],
                "status": row["status"],
                "status_reason": row.get("status_reason"),
                "cluster": row.get("cluster"),
                "discovery": {
                    "ic": row.get("discovery_ic"),
                    "ir": row.get("discovery_ir"),
                    "t": row.get("discovery_t"),
                    "q": row.get("discovery_q"),
                    "signed_ir": row.get("discovery_signed_ir"),
                    "signed_sharpe": row.get("discovery_signed_sharpe"),
                },
                "confirmation": {
                    "ic": row.get("confirmation_ic"),
                    "t": row.get("confirmation_t"),
                    "q": row.get("confirmation_q"),
                },
                "dsr": row.get("dsr"),
                "novelty": row.get("novelty"),
            }
            for row in candidates
        ])
    return report
