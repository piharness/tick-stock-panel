"""决策卡生产者 — 「策略 → 卡」的最后一公里（docs/decision-ledger.md §8 第 2 步）。

§6 诚实登记：账本建成时只有显式 API 建卡，没有自动生产者。本模块把
「当日跑策略」接上：盘后管道数据就绪后，结算到期卡 → 跑策略取 picks →
auto_pwin 反推 → 门禁 → 封卡。**每天自动积累前向证据**，账本才第一次
拥有它存在的意义（§1：回答"我下过的判断，到期后比常数预测强吗"）。

设计约定（全部沿用账本既有纪律，不新增第二套口径）：
- **门禁只跳过，不越权**：gate STOP 时不建卡、不 override——自动路径
  写 `allow_stop=true` 等于把「补登历史卡」的越权开关交给定时任务。
- **fail-closed 的两个出口都如实呈现**：没有证据支撑 p_win 时不编数字
  （不建卡，reason 记 `no_evidence`）；pick 缺 raw_close 时丢该 pick
  而不是拿复权价冒充限价。
- **限价 = as_of 日 raw_close**：结算的撮合口径是「开盘 ≤ 限价按开盘，
  否则最低 ≤ 限价按限价，否则 NOT_FILLED 留痕」——"看得见买不到"本身
  是策略的一部分（§4 口径）。用不复权价，与 paper/settle 一致。
- **幂等**：card_id 是承诺内容的哈希，同日重跑同配置得到同一张卡，
  `save_card` 返回 `created: false`。结算本身幂等（`test_settle_is_idempotent`）。
- 复用 API 层已验证的可见性口径（§9.8 `exit.date < as_of`）与 pwin
  收缩逻辑：原实现住在 HTTP 层，生产者与 API 共用本模块的域函数，
  不平行维护第二份 p_win 出处登记。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.decision import bars, ledger, pwin as pwin_mod, settle as settle_mod
from app.decision.gates import GateResult, evaluate as evaluate_gates

logger = logging.getLogger(__name__)


class PickValidationError(ValueError):
    """pick 自身非法（缺 p_win 且未开 auto）——HTTP 层映射 400。"""


class NoEvidenceError(ValueError):
    """账本里没有可见证据支撑 p_win——HTTP 层映射 409（fail-closed）。"""


def exit_visible(row: dict[str, Any], as_of: str) -> bool:
    """结算记录在 `as_of` 决策时点是否可见：出场日**严格早于**建卡日。

    口径来自 `docs/decision-ledger.md` §9.8（`exit.date < D`，当天出场仍不算
    可见）。缺 `exit.date` ⇒ 无法证明"当时已知" ⇒ 不可见：fail-closed，与账本
    「读不到即 UNKNOWN」同一哲学。日期均为 ISO 串，字典序即时间序。
    """
    exit_date = (row.get("exit") or {}).get("date")
    return exit_date is not None and str(exit_date) < as_of


def resolve_picks_with_pwin(
    data_dir: Path,
    raw_picks: list[dict[str, Any]],
    *,
    auto_pwin: bool,
    strategy_id: str | None,
    horizon: int,
    basis: str,
    allow_prior: bool,
    as_of: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把 picks 的 p_win 补齐，返回 (picks, p_win 出处)。

    `auto_pwin=False` 时每个 pick 必须自带 p_win；缺失抛
    `PickValidationError`（API 层映射 400），不猜。
    `auto_pwin=True` 时用账本历史反推；没有证据且 `allow_prior=False` 则抛
    `NoEvidenceError`（API 层映射 409）。

    `as_of` 是**建卡日**。反推只允许看见出场日早于它的结算：`load_settlements`
    会把账本里全部记录一起返回，补登历史卡时不过滤就是把未来函数写进 p_win
    （2026-11-06 独立评审 P1；    回归测试见
    `test_auto_pwin_backfill_ignores_settlements_after_as_of`）。

    纯函数纪律：**不修改调用方传入的 dicts**——p_win 补齐写进拷贝。原地
    改写会让"同一份输入第二次调用"看到已被补过的 p_win，出处登记与
    承诺哈希随之漂移（幂等性依赖每次都从原始输入重新推导）。
    """
    raw_picks = [dict(p) for p in raw_picks]
    missing = [p["symbol"] for p in raw_picks if p.get("p_win") is None]
    if not auto_pwin:
        if missing:
            raise PickValidationError(
                "以下 pick 缺少 p_win：" + ", ".join(missing))
        return raw_picks, None
    if not missing:
        # 全部自带 p_win：auto_pwin 没有可补的。照样不写 `source=ledger`
        # 的出处——人工填的数字记成账本算的，出处就在撒谎。
        return raw_picks, None

    rows = ledger.load_settlements(data_dir)
    rows = [r for r in rows if exit_visible(r, as_of)]
    try:
        est = pwin_mod.suggest(
            rows,
            strategy_id=strategy_id,
            horizon=horizon,
            basis=basis,
            # 交易日历：`n_eff` 的上限分母要按交易日算。发卡稀疏时
            # 决策日与交易日不是同一个单位（`docs/decision-ledger.md` §9.24）。
            sessions=bars.sessions(data_dir),
            allow_prior=allow_prior,
        )
    except ValueError as exc:
        raise NoEvidenceError(str(exc)) from exc

    p = float(est["p_win"])
    for item in raw_picks:
        if item.get("p_win") is None:
            item["p_win"] = p
    source = {
        "source": est["source"],
        "basis": est["basis"],
        "n": est["n"],
        "hits": est["hits"],
        "days": est["days"],
        "window": est["window"],
        "kappa": est["kappa"],
        # 这个 p_win 里先验占多大比重。两个分母：观测数（n）与等效独立样本数
        # （n_eff）。真实账本 386 条 / n_eff=1 时是 4.9% 对 95.2%——`source`
        # 只说是「账本算的」，这两个数才说「账本占多少」。只做标签，不做 gate。
        "prior_weight": est.get("prior_weight"),
        "prior_weight_effective": est.get("prior_weight_effective"),
        # 哪些 pick 的 p_win 是这次补出来的。人工填的那些不在里面。
        "filled": sorted(missing),
        "reliable": est["reliable"],
        "verdict": est["verdict"],
        # 三态结论：PASS / FAIL / UNKNOWN。`reliable` 只回答「lcb 符号」，
        # 这个字段还回答「这个 lcb 有没有分母」。进卡后随哈希封存，
        # 事后无法把「测不了」改写成「没通过」。
        "significance": est.get("significance"),
        "oos_skill": est.get("oos_skill"),
        "oos_days": est.get("oos_days"),
        # 可评估日 ≠ 独立样本：持有期让相邻决策日的结果重叠，n_eff 才是分母。
        "n_eff": est.get("n_eff"),
        # False ⇒ n_eff 是构造值（不重叠窗口数），不是 NW 估计值。
        "n_eff_reliable": est.get("n_eff_reliable"),
        # 增量检验那条序列的 n_eff。与上面的 n_eff 不是同一个数（残差不同），
        # 所以分开存，不共用一个键。
        "n_eff_incremental": est.get("n_eff_incremental"),
        "hac_lag": est.get("hac_lag"),
        # 增量序列的滞后阶数。**与 `hac_lag` 相等**（都是 horizon-1）。
        # 字段单独存，是因为 §9.18 曾让它取 window-1 并把功效打没——留一个位置，
        # 下次改错时在卡上看得见。
        "hac_lag_incremental": est.get("hac_lag_incremental"),
        # 第二个检验：p 相对「同样诚实的历史常数」有没有增量信息。
        # 与 verdict 并列返回，不替代它——两者回答的是不同问题。
        "verdict_incremental": est.get("verdict_incremental"),
        # 第二个检验的可辨识性三态（PASS / FAIL / UNKNOWN）。`verdict_incremental`
        # 读符号与量级，这个字段读「那个下界有没有分母」。
        "significance_incremental": est.get("significance_incremental"),
        # 这个检验实际比了几天。`truncated_days` 取决于每天的出场日，
        # `oos_days` 只数被评估的日子，推不出来，所以单独存。
        "truncated_days": est.get("truncated_days"),
        "skill_vs_clim": est.get("skill_vs_clim"),
        "skill_vs_clim_lcb": est.get("skill_vs_clim_lcb"),
        "reason_incremental": est.get("reason_incremental"),
        "reason": est["reason"],
    }
    return raw_picks, source


def picks_from_scan_rows(
    rows: list[dict[str, Any]], top_n: int
) -> tuple[list[dict[str, Any]], int]:
    """策略扫描结果 → 决策 picks：限价 = as_of 日 raw_close。

    引擎结果已按评分排序，取前 `top_n`。缺 `raw_close` 或 `symbol` 的行
    **丢弃并计数**，不用复权价冒充限价（不复权价是结算撮合口径，§4）。
    """
    picks: list[dict[str, Any]] = []
    dropped = 0
    for row in rows[:top_n]:
        symbol = row.get("symbol")
        limit = row.get("raw_close")
        if not symbol or limit is None:
            dropped += 1
            continue
        picks.append({"symbol": str(symbol), "limit_price": float(limit)})
    return picks, dropped


def produce_card(
    data_dir: Path,
    *,
    as_of: str,
    picks: list[dict[str, Any]],
    horizon_sessions: int,
    benchmark: str = "000300.SH",
    strategy_id: str | None = None,
    account_id: str = "default",
    pwin_basis: str = "median",
    allow_prior: bool = False,
    scan_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次自动发卡尝试。**任何一档不满足都返回 `status != "created"`**，
    绝不抛异常打断管道，也绝不降级造卡。

    返回字段：`status ∈ created | skipped_gate | skipped_no_picks |
    skipped_no_evidence | exists`，附 `gates` / `card_id` / `reason`。
    """
    gate: GateResult = evaluate_gates(data_dir, as_of=as_of, account_id=account_id)
    if gate.blocked:
        # 门禁 STOP：自动路径只跳过，不 override（越权是显式 API 调用的特权，
        # 且必须留 10 字以上理由）。触发即停是账本的地基，定时任务没有豁免权。
        logger.info("decision produce: gate %s, skip card (as_of=%s)",
                    gate.decision, as_of)
        return {"status": "skipped_gate", "gates": gate.to_dict(),
                "decision": gate.decision}

    if not picks:
        return {"status": "skipped_no_picks", "gates": gate.to_dict(),
                "reason": "策略当日 0 命中：没有判断就没有卡，不造空卡。"}

    try:
        picks, pwin_source = resolve_picks_with_pwin(
            data_dir, picks,
            auto_pwin=True,
            strategy_id=strategy_id,
            horizon=horizon_sessions,
            basis=pwin_basis,
            allow_prior=allow_prior,
            as_of=as_of,
        )
    except NoEvidenceError as exc:
        # 冷启动（可见样本为 0）：不编 p_win。出路是显式 API 建卡
        # （allow_prior=true 记 source=prior）或补登历史卡结算。
        logger.info("decision produce: no evidence for p_win, skip (as_of=%s)", as_of)
        return {"status": "skipped_no_evidence", "gates": gate.to_dict(),
                "reason": str(exc)}

    forecast: dict[str, Any] = dict(scan_meta or {})
    if pwin_source is not None:
        # 出处进 forecast ⇒ 进承诺哈希 ⇒ p_win 换了就是另一张卡。
        forecast["pwin"] = pwin_source

    card = ledger.build_card(
        as_of=as_of,
        picks=picks,
        horizon_sessions=horizon_sessions,
        benchmark=benchmark,
        strategy_id=strategy_id,
        account_id=account_id,
        forecast=forecast or None,
        gates=gate.to_dict(),
        override_reason=None,
    )
    _path, created = ledger.save_card(data_dir, card)
    return {
        "status": "created" if created else "exists",
        "card_id": card["card_id"],
        "gates": gate.to_dict(),
        "pwin": pwin_source,
        "picks": len(picks),
    }


def scan_picks(
    repo: Any,
    engine: Any,
    strategy_id: str,
    as_of: Any,
    *,
    top_n: int,
    asset_type: str = "stock",
    timeframe: str = "1d",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """跑一次策略扫描，返回 (picks, scan 元信息)。与 `/api/strategy/run` 同路径。

    复用 `ScreenerService.build_strategy_context` + `StrategyEngine.run`——
    这是全站唯一的策略执行入口（"回测 = 实盘同路径"），生产者不另起第二套。
    scan_meta 里放 as_of 与命中总数，进 forecast（承诺哈希的一部分），
    使「同日改了 top_n/数据重算」在卡上可追溯。
    """
    from app.services import strategy_config
    from app.services.screener import ScreenerService

    svc = ScreenerService(repo, asset_type=asset_type)
    if as_of is None:
        as_of = svc.latest_date()
    if as_of is None:
        raise ValueError("没有可用数据日期：请先跑盘后管道同步日K")
    overrides = strategy_config.load_override(repo.store.data_dir, strategy_id)
    params = dict(overrides.get("params") or {})
    context = svc.build_strategy_context(
        engine, as_of, [strategy_id],
        timeframe=timeframe,
        params_map={strategy_id: params},
        overrides_map={strategy_id: overrides or {}},
    )
    result = engine.run(
        strategy_id,
        context,
        params=params,
        overrides=overrides or None,
    )
    picks, dropped = picks_from_scan_rows(result.rows, top_n)
    meta = {
        "producer": "auto",
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        "total": result.total,
        "top_n": top_n,
        "dropped_missing_raw_close": dropped,
    }
    return picks, meta


def run_decision_stage(repo: Any, engine: Any | None = None,
                       *, today: Any = None) -> dict[str, Any]:
    """盘后管道阶段入口：先结算到期卡，再按配置自动发卡。软失败由调用方兜。

    顺序刻意是 **先结算后发卡**：今天结算落库的行里出场日早于今天的
    才对今天的卡可见（§9.8），先结算让 p_win 用上最新到期的证据。
    """
    from app.services import preferences

    cfg = preferences.get_decision_auto_config()
    if not cfg["enabled"]:
        return {"status": "disabled"}
    data_dir = Path(repo.store.data_dir)
    # settle 的 as_of 是 ISO 串；管道传进来的是 date，统一在这里折叠。
    as_of_str = (today.isoformat() if hasattr(today, "isoformat") else str(today)) \
        if today is not None else None

    settle_summary: dict[str, Any] = {}
    try:
        settle_summary = settle_mod.settle(data_dir, as_of=as_of_str)
    except ledger.LedgerCorruptError:
        # 残缺账本 fail-closed：不结算也不发卡，上抛给管道记 stage_error。
        raise

    if not cfg["strategy_id"]:
        return {"status": "no_strategy", "settle": settle_summary,
                "reason": "decision_auto_strategy_id 未配置：只结算，不发卡"}
    if engine is None:
        return {"status": "no_engine", "settle": settle_summary,
                "reason": "策略引擎未初始化：只结算，不发卡"}

    try:
        picks, scan_meta = scan_picks(
            repo, engine, cfg["strategy_id"], today, top_n=cfg["top_n"])
    except ValueError as exc:
        return {"status": "scan_failed", "settle": settle_summary,
                "reason": str(exc)}

    summary = produce_card(
        data_dir,
        as_of=scan_meta["as_of"],
        picks=picks,
        horizon_sessions=cfg["horizon"],
        strategy_id=cfg["strategy_id"],
        account_id=cfg["account_id"],
        pwin_basis=cfg["basis"],
        allow_prior=cfg["allow_prior"],
        scan_meta=scan_meta,
    )
    summary["settle"] = settle_summary
    summary["strategy_id"] = cfg["strategy_id"]
    logger.info("decision stage: %s (settle written=%s)", summary.get("status"),
                settle_summary.get("written"))
    return summary
