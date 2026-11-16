"""策略自动捕获管道 — 社区文本 → AI 策略 → 回测 → 横截面 alpha → 五道门 → 入池/归档。

这是「策略自动捕获」全链 (S1~S5) 的编排层。每个采集单元一个 uuid, 贯穿全程,
产物落 data/community_capture/{uuid}/, 从社区原文到最终判决一条链可追溯。

链路 (docs/strategy-auto-capture.md):
  S1 采集      文本/URL → 规范化描述, 原文原样落 source.txt
  S2 生成      AIStrategyGenerator.generate → 策略代码 (ast 安全校验)
  S3 落盘      data/strategies/ai/{ai_xxx}.py (research_only=True 草稿)
  S4 统计      回测 trades → 信号集合 → cross_section_alpha 算 Claim
  S5 判分      eval_protocol.evaluate → PASS 自动发布+入池 / REJECT 归档

关键纪律 (与本仓既有约束一致):
  - 每一步的 provenance 都落盘, 绝不静默采信。
  - PASS 是「五道门没否掉」, 不是「有 edge」; 五道门是拒绝器不是验证器。
  - 自动发布动作显式记录 (strategy_pool 历史 + verdict.provenance), 可回滚。
  - 任一步失败标 failed 并落原因, 不中断其它单元。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import polars as pl

from app.config import settings

logger = logging.getLogger(__name__)

# 回测信号提取时放宽 max_positions, 逼近「全信号」(不截断容量), 反映策略能力本身。
_SIGNAL_MAX_POSITIONS = 10000
# 容量曲线档位 (供 G2 判「选择偏差」: 容量放宽衰减 >50% 即选择偏差)
_CAPACITY_POSITIONS = (5, 20, 100)


def capture_dir(data_dir: Path | None = None) -> Path:
    base = (data_dir or settings.data_dir) / "community_capture"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class CaptureRecord:
    """一个采集单元的全量状态, 落 meta.json。"""
    uuid: str
    status: str  # running / pass / reject / failed
    source_text: str = ""
    strategy_id: str = ""
    decision: str = ""  # PASS / REJECT / REJECT_ECON / (空)
    reasons: list[str] = field(default_factory=list)
    error: str = ""
    created_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "status": self.status,
            "strategy_id": self.strategy_id,
            "decision": self.decision,
            "reasons": self.reasons,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "source_len": len(self.source_text),
        }


def _unit_dir(data_dir: Path, uid: str) -> Path:
    d = capture_dir(data_dir) / uid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_meta(data_dir: Path, rec: CaptureRecord) -> None:
    d = _unit_dir(data_dir, rec.uuid)
    (d / "meta.json").write_text(
        json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_verdict(data_dir: Path, uid: str, claim: dict, verdict: dict) -> None:
    d = _unit_dir(data_dir, uid)
    (d / "verdict.json").write_text(
        json.dumps({"claim": claim, "verdict": verdict}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _write_source(data_dir: Path, uid: str, text: str) -> None:
    d = _unit_dir(data_dir, uid)
    (d / "source.txt").write_text(text, encoding="utf-8")


def _write_strategy_code(data_dir: Path, uid: str, code: str) -> None:
    d = _unit_dir(data_dir, uid)
    (d / "strategy.py").write_text(code, encoding="utf-8")


def _eval_claim(claim: dict) -> dict:
    """跑五道门, 返回 verdict dict (含 decision / reasons / checks)。"""
    from app.strategy.eval_protocol import Claim, evaluate

    c = Claim(**{k: v for k, v in claim.items() if k in Claim.__dataclass_fields__})
    return evaluate(c).to_dict()


def _publish_strategy(engine, strategy_id: str) -> None:
    """把 research_only 草稿翻转为公开 (research_only=False)。

    等价于 api/strategy.py:publish_ai_strategy 的核心, 但作为服务内动作直接执行。
    这是「五道门 PASS 取代人点发布」的落点, 动作显式记录在 verdict.provenance。
    """
    s = engine.get(strategy_id)
    if s.source != "ai":
        raise ValueError(f"仅 AI 策略可自动发布: {strategy_id}")
    if not s.meta.get("research_only"):
        return  # 已公开, 幂等
    path = s.file_path
    if path is None:
        raise ValueError(f"策略源文件路径无效: {strategy_id}")
    previous = path.read_text(encoding="utf-8")
    from app.api.strategy import _set_meta_bool_field
    path.write_text(_set_meta_bool_field(previous, "research_only", False), encoding="utf-8")
    try:
        engine.reload()
        loaded = engine.get(strategy_id)
        if loaded.meta.get("research_only"):
            raise ValueError("发布后策略仍为草稿态")
    except Exception as e:
        path.write_text(previous, encoding="utf-8")
        engine.reload()
        raise ValueError(f"策略发布失败: {e}") from e


def _extract_signals_from_trades(trades: list[dict]) -> pl.DataFrame:
    """从回测 trades 提取 (entry_signal_date, symbol) 信号集合。

    每笔交易的信号日 = entry_signal_date 或 entry_date (无信号日字段时)。
    去重 (同一天同一只可能多笔, 只算一次信号)。
    """
    rows = []
    for t in trades:
        sig_date = t.get("entry_signal_date") or t.get("entry_date")
        sym = t.get("symbol")
        if sig_date and sym:
            rows.append({"date": str(sig_date)[:10], "symbol": str(sym)})
    if not rows:
        return pl.DataFrame({"date": [], "symbol": []})
    return pl.DataFrame(rows).unique()


def _default_prompt_builder(source_text: str) -> str:
    """把社区战法文本包装成 AI 策略生成提示词。

    社区文本原样保留在 source.txt, 这里只做「生成提示词」的语义包装:
    把社区战法描述当作策略规则, 让 AI 生成可回测的 Polars 策略。

    硬约束: **只生成日线策略** (timeframes=["1d"], polars_expr 后端)。
    横截面 alpha 统计链 (cross_section_alpha) 用 open_t1→close_h 的日线口径,
    分钟策略 (minute_filter / 1m) 无法进入该统计链, 且分钟回测的 panel 缺
    consecutive_limit_ups 等日线列会直接 ColumnNotFoundError。
    """
    return (
        "请把下面这段来自股票社区的游资战法/选股思路, 转成一个可回测的 A 股**日线**策略代码。\n"
        "只提取其中可量化、可验证的规则 (涨跌幅/成交量/换手/均线/连板等), "
        "忽略主观判断与无法用日线数据表达的部分。\n"
        "若原文有「开盘后 N 分钟」「盘中」等分钟级描述, 请一律降级为日线近似 "
        "(如「开盘高开」用 next_open 相对昨收的涨跌幅, 「开盘后放量」用当日量比/换手), "
        "并在策略描述里注明这是日线近似。\n"
        "若原文没有给出明确的量化规则, 请基于其核心逻辑做最保守的量化近似, "
        "并在策略描述里注明这是近似。\n\n"
        "硬性约束 (不可违反):\n"
        "1. timeframes 必须为 [\"1d\"] (日线策略), 不得生成分钟/盘中策略。\n"
        "2. 执行后端用 polars_expr (定义 filter()/scoring), 不生成 minute_filter 或 matrix_native。\n\n"
        "策略名称：社区战法 (自动捕获)\n"
        "策略方向：long\n"
        "策略规则：\n"
        f"{source_text}\n"
    )


def run_capture(
    data_dir: Path,
    source_text: str,
    *,
    engine,
    repo,
    prompt_builder=None,
    entry: str = "open_t1",
    exit: str = "close_t2",
    event_type: str = "turnover_shock",
    uid: str | None = None,
) -> CaptureRecord:
    """执行一次完整采集 (同步, 供 API 在后台线程调用)。

    Args:
        prompt_builder: callable(source_text) -> user_prompt, 把社区文本转成生成提示词。
            缺省用 _default_prompt_builder。
        engine: StrategyEngine (app.state.strategy_engine)
        repo: KlineRepository (app.state.repo)
        uid: 预生成的采集单元 id (缺省自动生成)。API 层传 uuid 以便前端立即轮询。
    Returns:
        CaptureRecord (status = pass/reject/failed)
    """
    builder = prompt_builder or _default_prompt_builder
    uid = uid or uuid.uuid4().hex[:12]
    rec = CaptureRecord(
        uuid=uid, status="running", source_text=source_text,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    _write_meta(data_dir, rec)
    _write_source(data_dir, uid, source_text)

    try:
        # ── S2 生成 ──
        from app.strategy.ai_generator import AIStrategyGenerator

        gen = AIStrategyGenerator()
        import asyncio
        result = asyncio.run(gen.generate(builder(source_text)))
        if not result.get("valid"):
            raise ValueError(f"AI 策略生成失败: {result.get('error')}")
        code = result["code"]
        _write_strategy_code(data_dir, uid, code)

        # ── S2.5 口径校验: 自动捕获只支持日线策略 ──
        # 横截面 alpha 统计链是 open_t1→close_h 的日线口径; 分钟策略无法进入该链,
        # 且分钟回测 panel 缺 consecutive_limit_ups 等日线列会 ColumnNotFoundError。
        # 这里 fail-fast, 比等到回测失败再标 failed 更快、原因更清晰。
        meta0 = result.get("meta") or {}
        timeframes = meta0.get("timeframes") or ["1d"]
        if "1d" not in timeframes or "1m" in timeframes:
            raise ValueError(
                f"生成的策略是分钟/盘中策略 (timeframes={timeframes}), "
                "自动捕获仅支持日线策略 (timeframes=['1d'])"
            )

        # ── S3 落盘草稿 (复用 ai_iterator 的落盘逻辑) ──
        from app.strategy.ai_iterator import _rewrite_meta_id
        strategy_id = _alloc_draft_id(engine)
        out_dir = data_dir / "strategies" / "ai"
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {**result["meta"], "research_only": True}
        (out_dir / f"{strategy_id}.py").write_text(
            _rewrite_meta_id(code, strategy_id, meta), encoding="utf-8"
        )
        engine.reload()
        rec.strategy_id = strategy_id
        _write_meta(data_dir, rec)

        # ── S4 回测 → 信号集合 ──
        signals, capacity = _run_signal_extraction(data_dir, strategy_id, repo)
        if signals.height == 0:
            raise ValueError("策略在回测区间内无任何信号命中, 无法评估")

        # 信号集合落盘 (可回查)
        _write_signals(data_dir, uid, signals)

        # ── S4 横截面 alpha + Claim ──
        from app.services import cross_section_alpha as csa

        daily = _load_daily_for_alpha(data_dir, repo)
        n_stock_days = daily.height if daily.height else None
        alpha = csa.compute_signal_alpha(signals, daily, holding_days=(2,))
        claim = csa.build_claim(
            strategy_id, alpha,
            benchmark="board_bucket", h=2,
            event_type=event_type,
            entry=entry, exit=exit,
            n_stock_days=n_stock_days,
            capacity_annual_pct=capacity,
            # entry_unfillable_share 由 compute_signal_alpha 的 buyable 口径自动算出,
            # 不再硬传 0.0。
        )
        # 标记自动发布动作来源
        claim["provenance"]["auto_publish"] = "true"

        # ── S5 判分 ──
        verdict = _eval_claim(claim)
        _write_verdict(data_dir, uid, claim, verdict)
        rec.decision = verdict["decision"]
        rec.reasons = list(verdict["reasons"])
        rec.finished_at = datetime.now().isoformat(timespec="seconds")

        if verdict["decision"] == "PASS":
            # 自动发布 + 入池 (动作显式记录)
            _publish_strategy(engine, strategy_id)
            from app.services import strategy_pool

            strategy_pool.add(
                [strategy_id],
                action="capture_pass",
                reason=f"auto-capture {uid} PASS (五道门全绿)",
            )
            rec.status = "pass"
        else:
            # 归档 (不入池, 保留草稿)
            rec.status = "reject"
        _write_meta(data_dir, rec)
        return rec

    except Exception as e:  # noqa: BLE001 — 任一步失败标 failed, 不中断其它单元
        logger.exception("strategy capture %s failed", uid)
        rec.status = "failed"
        rec.error = str(e)
        rec.finished_at = datetime.now().isoformat(timespec="seconds")
        _write_meta(data_dir, rec)
        return rec


def _alloc_draft_id(engine) -> str:
    while True:
        sid = f"ai_{uuid.uuid4().hex[:8]}"
        if not engine.has(sid):
            return sid


def _run_signal_extraction(data_dir: Path, strategy_id: str, repo):
    """回测 (放宽 max_positions) → (信号集合, 容量曲线)。"""
    from app.services import cross_section_alpha as csa

    # 信号回测: 放宽容量逼近全信号
    signals = _extract_signals_from_trades(
        _run_backtest_trades(data_dir, strategy_id, max_positions=_SIGNAL_MAX_POSITIONS)
    )
    # 容量曲线 (供 G2)
    capacity = csa.compute_capacity_curve(
        data_dir, strategy_id, max_positions=_CAPACITY_POSITIONS,
    )
    return signals, capacity


def _run_backtest_trades(data_dir: Path, strategy_id: str, *, max_positions: int) -> list[dict]:
    """跑一次回测, 返回 trades (list[dict])。失败返回 []。

    用 mode="full": 不受 max_positions/资金约束, 反映策略能力本身 (全信号),
    正是横截面 alpha 需要的「T 日选中的股票集合」(而非受容量截断后的持仓)。
    """
    from datetime import date as _date

    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import make_worker_task, run_worker_task
    from app.market_time import cn_today
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    import datetime as _dt

    end_date = cn_today()
    start_date = end_date - _dt.timedelta(days=365)
    cfg = StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=start_date,
        end=end_date,
        mode="full",  # 全信号, 不受容量截断
        asset_type="stock",
    )
    try:
        with shared_heavy_job_limiter.slot("normal"):
            task = make_worker_task("backtest", data_dir, cfg)
            result = run_worker_task(task)
    except Exception as e:  # noqa: BLE001
        logger.warning("signal extraction backtest failed: %s", e)
        return []
    trades = result.get("trades") or []
    if not trades:
        # mode="full" 的 trades 可能为空(全仿真只给聚合统计), 回退用 stats 里的信号数
        logger.warning("mode=full backtest returned no trades for %s", strategy_id)
    return trades


def _load_daily_for_alpha(data_dir: Path, repo) -> pl.DataFrame:
    """加载全市场日线 (供 compute_signal_alpha 计算超额基准)。"""
    from app.tickflow.repository import enriched_dirname

    enriched_dir = data_dir / enriched_dirname("stock")
    if not enriched_dir.exists():
        return pl.DataFrame()
    from app.parquet import scan_enriched_parquet

    cols = ["symbol", "date", "open", "close", "raw_close"]
    try:
        lf = scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
        available = [c for c in cols if c in lf.collect_schema().names()]
        if "raw_close" not in available:
            return pl.DataFrame()
        return lf.select(available).sort(["symbol", "date"]).collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("load daily for alpha failed: %s", e)
        return pl.DataFrame()


def _write_signals(data_dir: Path, uid: str, signals: pl.DataFrame) -> None:
    d = _unit_dir(data_dir, uid)
    signals.write_parquet(d / "signals.parquet")


def list_captures(data_dir: Path | None = None, limit: int = 50) -> list[dict]:
    """列出采集记录 (新在前), 供回查。"""
    base = capture_dir(data_dir)
    records = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            records.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if len(records) >= limit:
            break
    return records
