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

from app.decision import (
    bars,
    l1_cohort,
    ledger,
    lu_rebound,
    pwin as pwin_mod,
    settle as settle_mod,
    slate as slate_mod,
)
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
    fill_mode: str = ledger.FILL_MODE_LIMIT,
) -> dict[str, Any]:
    """一次自动发卡尝试。**任何一档不满足都返回 `status != "created"`**，
    绝不抛异常打断管道，也绝不降级造卡。

    返回字段：`status ∈ created | skipped_gate | skipped_no_picks |
    skipped_no_evidence | exists`，附 `gates` / `card_id` / `reason`。

    `fill_mode` 必须从这一层透传：`ledger.build_card` 支持它、HTTP 层支持它，
    中间缺了它，**自动路径就永远只能造 `limit` 卡**。而 `limit` 与 `open_only`
    不是一个开关的两档，是两个策略（`settle._fill` docstring）：实测同一批
    L1 pick，`limit` 成交 70 笔、`open_only` 成交 42 笔，多出的 28 笔全是
    「开盘没成交、盘中回落补上」的反向样本，均值因此被摊薄（逐位口径见
    `analysis/_diag_l1_loop_walk_findings.md`）。默认取 `limit` 只为向后兼容
    既有卡，**新口径必须在调用点显式传**。
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
        fill_mode=fill_mode,
    )
    _path, created = ledger.save_card(data_dir, card)
    return {
        "status": "created" if created else "exists",
        "card_id": card["card_id"],
        "gates": gate.to_dict(),
        "pwin": pwin_source,
        "picks": len(picks),
    }


def produce_flat_card(
    data_dir: Path,
    *,
    as_of: str,
    horizon_sessions: int,
    reason: str,
    benchmark: str = "000300.SH",
    strategy_id: str | None = None,
    account_id: str = "default",
    scan_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """显式记一张「今天决定不参与」的卡。**与 `produce_card` 的语义相反**。

    `produce_card` 在 0 命中时返回 `skipped_no_picks` 且不落盘，理由是
    「没有判断就没有卡，不造空卡」——那条规则针对的是**尝试持仓但扫不出标的**，
    它防止的是把「策略坏了」伪装成「策略决定空仓」。这里相反：调用方**已经**
    做了不参与的判断，理由必须显式给出（≥10 字），否则同样是在伪造决策。

    三条硬约束：
    - `horizon_sessions >= 1`：0 天持有期的空仓没有可评价窗口（entry==exit
      ⇒ 基准收益恒 0），结算出来全是 0，等于给自己发了一张「完美空仓」的奖状；
    - 理由 ≥ 10 字符：和 `override_reason` 同一标准，越权/反向决定必须可读；
    - 门禁 STOP 时不出卡：自动路径没有豁免权，空仓也不例外。
    """
    if int(horizon_sessions) < 1:
        raise ValueError(
            "空仓卡必须 horizon_sessions >= 1：0 天持有期的空仓没有可评价窗口"
        )
    text = (reason or "").strip()
    if len(text) < 10:
        raise ValueError("空仓理由至少 10 个字符：『不买』必须留下可读的依据")

    gate: GateResult = evaluate_gates(data_dir, as_of=as_of, account_id=account_id)
    if gate.blocked:
        logger.info("decision flat: gate %s, skip card (as_of=%s)", gate.decision, as_of)
        return {"status": "skipped_gate", "gates": gate.to_dict(),
                "decision": gate.decision}

    forecast: dict[str, Any] = dict(scan_meta or {})
    forecast["stance"] = ledger.STANCE_FLAT
    forecast["reason"] = text
    card = ledger.build_card(
        as_of=as_of,
        picks=[],
        horizon_sessions=int(horizon_sessions),
        benchmark=benchmark,
        strategy_id=strategy_id,
        account_id=account_id,
        forecast=forecast,
        gates=gate.to_dict(),
        stance=ledger.STANCE_FLAT,
    )
    _path, created = ledger.save_card(data_dir, card)
    return {
        "status": "created" if created else "exists",
        "card_id": card["card_id"],
        "gates": gate.to_dict(),
        "stance": ledger.STANCE_FLAT,
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


def _refresh_slate(data_dir: Path, as_of: str | None) -> dict[str, Any]:
    """盘后刷新「每日名单」——**与自动发卡开关无关**。

    名单回答「今天买哪几只」，发卡回答「敢不敢为它签名」。把前者绑在后者
    的开关上，会让关掉自动发卡的用户连名单都看不到（默认配置正是关闭）。
    所以这一步在 `cfg["enabled"]` 判断之前。

    软失败不打断管道：宁可这次没有名单，也不要整条盘后管道缺一天。
    `build_slate` 自己已把「数据未收盘 / 缺分区 / 口径非法」变成带 reason 的
    结构，这里兜的只是它没预料到的异常。
    """
    try:
        built = slate_mod.build_slate(data_dir, as_of)
        path = slate_mod.write_slate(data_dir, built)
    except Exception as exc:  # noqa: BLE001 - 软失败由调用方兜
        logger.warning("decision slate failed: %s", exc)
        return {"status": slate_mod.STATUS_FAILED,
                "reason": f"{type(exc).__name__}: {exc}", "written": False}
    out = slate_mod.summary(built)
    out["written"] = path is not None
    logger.info("decision slate: %s as_of=%s rows=%s",
                out.get("status"), out.get("as_of"), out.get("n_rows"))
    return out


def _l1_picks(data_dir: Path, as_of: str | None
              ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """L1 的 picks 从**刚落盘的名单**读回，不再自己算一遍。

    `(picks, scan_meta, error)`。三个理由必须走磁盘：

    1. `rows` 与 `card.picks` 的逐位相等从此是**构造性**的，不靠对拍测试；
    2. 名单没落盘（写失败/结构版本不认）时，卡也不该发——承诺不能指向一份
       读者拿不到的名单；
    3. `as_of` 用名单里的值，不用调用方传的 `today`：两者不一致时，
       卡必须记录它实际承诺的那一天。

    **只读 `as_of` 那一天，绝不回退到「最新一份」。** 回退看起来是兜底，
    实际是把「今天的名单没算出来」改写成「今天用昨天的名单发卡」：
    卡上会写昨天的日期、昨天的限价，而调用方以为它是今天的。目录里最新那份
    名单永远存在（除非首次运行），所以这个兜底永远生效、永远不报错。

    盘中数据仍要在这里再挡一次：`build_slate` 会返回 `skipped_not_closed`，
    这里把它原样上报，不降级造卡。
    """
    if not as_of:
        return [], {}, {
            "status": slate_mod.STATUS_FAILED,
            "reason": "本次构建没有 as_of（写出失败/无分区）：不回退到旧名单发卡",
        }
    slate = slate_mod.read_slate(Path(data_dir), str(as_of))
    if slate is None:
        return [], {}, {
            "status": "scan_failed",
            "reason": f"{as_of} 的每日名单未落盘：无法从同一份名单发卡"
                      "（先看管道 slate 日志）",
        }
    if slate.get("status") not in slate_mod.READY_STATES:
        return [], {}, {
            "status": str(slate.get("status") or "scan_failed"),
            "reason": str(slate.get("reason") or "名单未成立"),
        }

    picks = slate_mod.picks_from_slate(slate)
    rows = slate.get("rows") or []
    meta = {
        "source": "l1_cohort",
        #: `produce_card` 从 scan_meta 取 as_of；缺了它就是 KeyError。
        #: 取名单的值，不取 today——卡承诺的是名单那一天。
        "as_of": slate.get("as_of"),
        "entry_date": slate.get("entry_date"),
        "top_n": slate.get("top_n"),
        "gap_threshold_pct": rows[0].get("gap_threshold_pct") if rows else None,
        "n_candidates": len(picks),
        "caliber": slate.get("caliber"),
        "warnings": slate.get("warnings") or [],
        # 名单的身份进 forecast ⇒ 进承诺哈希。用**内容指纹**而不是 `built_at`：
        # 补跑一次（同一批单、新的构建时刻）必须落回同一个 card_id，
        # 否则同一天会被记两张卡，同一份证据在 Brier 里算两遍。
        # 「另一份名单」指的是 rows 变了，不是写盘时间变了。
        "slate": {
            "schema_revision": slate.get("schema_revision"),
            "status": slate.get("status"),
            "n_rows": slate.get("n_rows"),
            "stale_at_build": slate.get("stale_at_build"),
            "fingerprint": slate_mod.fingerprint(slate),
        },
    }
    return picks, meta, None


def _lu_picks(data_dir: Path, as_of: str | None
              ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """LU 腿的 picks：直接来自 `lu_rebound.candidates()`，**不经过每日名单**。

    每日名单（`slate`）是 L1 的唯一产地，它的 `top_n`、状态机、45 日面板窗口
    都与 L1 口径绑在一起。把第二条腿塞进去会让两份名单共享一个 `as_of` 状态，
    而它们的「今天该挂哪几张单」是两个问题。

    `forecast` 只放**内容**：`fingerprint` = sha256(as_of + strategy + rows)。
    刻意不放 `entry_date` 与 `warnings`——它们会随时间漂移（次日分区落盘后
    `entry_date` 从 None 变成日期、`stale` 警告会出现），而卡的身份必须是内容：
    周五盘后建的卡，周一重跑一次不该变成第二张卡（同一份证据在 Brier 里算两遍）。
    入场日由 `_settle_pick()` 按日历推导，不依赖卡上的这个字段。
    """
    if not as_of:
        return [], {}, {"status": "skipped_no_data",
                        "reason": "没有决策日：先同步日K"}
    cand = lu_rebound.candidates(data_dir, str(as_of))
    if cand.get("status") not in ("ready", "empty"):
        return [], {}, {"status": str(cand.get("status")),
                        "reason": str(cand.get("reason") or "候选未成立")}
    rows = cand.get("rows") or []
    if not rows:
        return [], {}, {"status": "empty", "reason": "当日没有被砸的换手板"}
    picks = [{"symbol": str(r["symbol"]), "limit_price": float(r["limit_price"])} for r in rows]
    meta = {
        "source": "lu_rebound",
        "as_of": cand["as_of"],
        "n_candidates": len(picks),
        "truncated": cand.get("truncated"),
        "drop_threshold": cand.get("drop_threshold"),
        "caliber": cand.get("caliber"),
        "lu": {
            "fingerprint": lu_rebound.fingerprint(cand),
            "top_n": cand.get("top_n"),
            "n_rows": cand.get("n_rows"),
        },
    }
    return picks, meta, None


def _lu_card(
    data_dir: Path,
    *,
    as_of: str | None,
    account_id: str = "default",
    basis: str = "median",
) -> dict[str, Any]:
    """LU 腿的一次封卡尝试。**任何一档不满足都返回 status != created**，不抛异常。"""
    try:
        day = as_of
        if not day:
            from app.decision import bars as _bars

            day = _bars.latest_session(data_dir, _bars.STOCK_ENRICHED)
        if not day:
            return {"status": "skipped_no_data", "reason": "没有已落盘的交易日"}
        picks, meta, why = _lu_picks(data_dir, day)
        if why is not None:
            return {"status": why["status"], "reason": why["reason"]}
        return produce_card(
            data_dir,
            as_of=str(meta["as_of"]),
            picks=picks,
            #: 三个固定口径（见 `run_decision_stage` 的注释）：配置不许覆盖。
            horizon_sessions=lu_rebound.HORIZON_SESSIONS,
            strategy_id=lu_rebound.STRATEGY_ID,
            account_id=account_id,
            pwin_basis=basis,
            allow_prior=True,
            scan_meta=meta,
            fill_mode=lu_rebound.FILL_MODE,
        )
    except Exception as exc:  # noqa: BLE001 - 软失败：一条腿坏了不能拖垮整条管道
        logger.warning("decision lu_rebound failed: %s", exc)
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def run_decision_stage(repo: Any, engine: Any | None = None,
                       *, today: Any = None) -> dict[str, Any]:
    """盘后管道阶段入口：先刷新名单，再结算到期卡，再跑第二条腿，最后发主腿的卡。

    顺序是几条独立的因果，不是可以互换的代码：

    1. **名单**不依赖账本，也不依赖发卡开关——它只依赖收盘数据。所以排第一，
       且 `disabled` 时照样执行。
    2. **结算先于发卡**：今天结算落库的行里出场日早于今天的才对今天的卡可见
       （§9.8），先结算让 p_win 用上最新到期的证据。
    3. **两条腿各走各的开关**：`decision_auto_enabled` 管主腿（策略引擎 / L1），
       `decision_lu_rebound_enabled` 管涨停次日砸盘反转。两腿都被关掉才算停摆。
    4. **LU 腿早于主腿的四个早退出口**（未配策略 / 无引擎 / 扫描失败 / 名单没
       落盘）：它与主腿没有因果关系，主腿的失败不该让另一条腿的承诺消失。

    软失败由调用方兜；残缺账本（fail-closed）上抛。
    """
    from app.services import preferences

    cfg = preferences.get_decision_auto_config()
    #: 第二条生产腿的开关（涨停次日砸盘反转）。独立于 `decision_auto_*`：
    #: 两腿的口径完全在策略层固定（L1 强制 `open_only`、LU 强制 `horizon=0`），
    #: 共用的只有账户、p_win 口径与门禁。
    lu_cfg = preferences.get_decision_lu_config()
    flat_cfg = preferences.get_decision_flat_config()
    data_dir = Path(repo.store.data_dir)
    # settle 的 as_of 是 ISO 串；管道传进来的是 date，统一在这里折叠。
    as_of_str = (today.isoformat() if hasattr(today, "isoformat") else str(today)) \
        if today is not None else None

    # Step 0: 每日名单（唯一产地）。关掉自动发卡也要有名单。
    slate_summary = _refresh_slate(data_dir, as_of_str)

    # 两条腿都被自己的开关关掉，才算「本阶段停摆」。只认主腿的话，
    # 「只开 LU」会走进下面那个 disabled 出口，而它连结算都不跑——
    # 开关是开的、卡是发的，然后永远不结算，前向证据一条也攒不到。
    if not cfg["enabled"] and not lu_cfg["enabled"]:
        return {"status": "disabled", "slate": slate_summary}

    settle_summary: dict[str, Any] = {}
    try:
        settle_summary = settle_mod.settle(data_dir, as_of=as_of_str)
        # 空仓卡走自己的流水与自己的窗口, 但**同一次结算调用**——分两次调用会
        # 让「今天结算了 N 条」的日志里看不见另一半, 而另一半决定了空仓成绩单
        # 是否在更新。
        settle_summary["flat"] = settle_mod.settle_flats(data_dir, as_of=as_of_str)
    except ledger.LedgerCorruptError:
        # 残缺账本 fail-closed：不结算也不发卡，上抛给管道记 stage_error。
        raise

    # 第二条腿：涨停次日砸盘反转。**在建卡顺序上先于主腿，不是因为更重要**：
    # 主腿的四个早退出口（未配策略 / 无引擎 / 扫描失败 / 名单没落盘）都会
    # return，而这条腿与它们毫无因果关系。放在后面，等于让「L1 名单今天没算
    # 出来」静默地把第二腿的卡一起吞掉——管道日志里只有 scan_failed 一个词，
    # 看不出还丢了另一条腿的承诺。
    #
    # 三条固定口径不接受配置覆盖（它们是策略定义，不是显示参数）：
    # `horizon_sessions=0`（入场日收盘出场）、`fill_mode=open_only`（只在竞价成交）、
    # `top_n`（容量约束，研究口径同值）。配置能改的只有账户与 p_win 口径。
    #
    # `allow_prior=True` 是**冷启动的唯一出路**：新策略在账本里必然没有可见证据
    # （§9.15 那个鸡生蛋），而 0.5 是 median 口径的零信息常数、不是编出来的胜率。
    # 出处随卡封存（`source=prior`、`prior_weight=1.0`），`by_source` 会把它与
    # 账本证据分开计分——读者不会把先验读成证据。
    lu_summary: dict[str, Any] | None = None
    if lu_cfg["enabled"]:
        lu_summary = _lu_card(
            data_dir,
            as_of=as_of_str,
            account_id=cfg["account_id"],
            basis=cfg["basis"],
        )

    def _finish(payload: dict[str, Any]) -> dict[str, Any]:
        """把与本腿无关的两块结论挂到**每一个**出口上。

        名单与 LU 腿是另外两条独立的因果。让某个 `status != created` 的早退
        把它们一并丢掉，读的人只会看到「今天没发卡」，而实际上名单算了、
        第二腿也发了卡。
        """
        payload.setdefault("slate", slate_summary)
        if settle_summary:
            payload.setdefault("settle", settle_summary)
        if lu_summary is not None:
            payload["lu_rebound"] = lu_summary
        return payload

    if not cfg["enabled"]:
        # 只开了 LU 腿：主腿的结论是「关闭」，但结算与第二腿都真的跑过了。
        return _finish({"status": "disabled",
                        "reason": "自动发卡未开启：只结算并跑 LU 腿"})

    if not cfg["strategy_id"]:
        return _finish({"status": "no_strategy",
                        "reason": "decision_auto_strategy_id 未配置：只结算，不发卡"})

    fill_mode = str(cfg.get("fill_mode") or ledger.FILL_MODE_LIMIT)
    if cfg["strategy_id"] == l1_cohort.L1_STRATEGY_ID:
        # L1 的候选不是策略引擎的产物：它来自刚落盘的每日名单。
        # 这条分支存在的唯一理由是让「证据最硬的那条腿」进同一个账本，
        # 而不是另建一套生产者——名单已经是唯一产地，这里只做读取。
        picks, scan_meta, why = _l1_picks(data_dir, slate_summary.get("as_of"))
        if why is not None:
            return _finish({"status": why["status"], "reason": why["reason"]})
        # L1 的成交口径是策略定义的一部分，不受配置影响：`candidates()` 输出的
        # 就是「开盘 ≤ 限价才买」。用 limit 口径结算等于换了策略（多出来的那批
        # 是「高开走弱」），所以这里不接受配置覆盖。
        fill_mode = ledger.FILL_MODE_OPEN_ONLY
    else:
        if engine is None:
            return _finish({"status": "no_engine",
                            "reason": "策略引擎未初始化：只结算，不发卡"})
        try:
            picks, scan_meta = scan_picks(
                repo, engine, cfg["strategy_id"], today, top_n=cfg["top_n"])
        except ValueError as exc:
            return _finish({"status": "scan_failed", "reason": str(exc)})

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
        fill_mode=fill_mode,
    )
    summary["strategy_id"] = cfg["strategy_id"]

    # 空仓决策：**只有「候选池正常但门槛把票全滤掉」才算决定不参与**。
    # 数据故障（名单未落盘 / 扫描失败 / 未配策略）在上面就走早退出口了，
    # 到不了这里——把故障日记成空仓，会让空仓成绩单被「读不到」稀释。
    # 目前只接 L1：其它策略的 0 命中可能只是参数没调好，语义不同。
    if (
        flat_cfg["enabled"]
        and summary.get("status") == "skipped_no_picks"
        and cfg["strategy_id"] == l1_cohort.L1_STRATEGY_ID
    ):
        summary["flat"] = produce_flat_card(
            data_dir,
            as_of=str(scan_meta["as_of"]),
            horizon_sessions=max(int(cfg["horizon"]), 1),
            reason=(
                f"{cfg['strategy_id']} 当日 0 命中：候选池正常、全部被门槛滤掉，"
                "决定不参与"
            ),
            strategy_id=cfg["strategy_id"],
            account_id=cfg["account_id"],
            scan_meta=scan_meta,
        )

    # 最后一米：建卡之后把它变成订单。默认关（`decision_auto_execute`），
    # 因为这是第一个动资金的步骤。软失败不打断管道——账单缺一天可以补，
    # 管道中断会让后面的阶段全部缺一天。
    #
    # **只作用于主腿的卡**：LU 腿的回合需要底仓配对（T+2 买、T+2 收盘卖），
    # 自动下单只会得到一串 `round_unfunded`。
    if cfg.get("execute") and summary.get("card_id"):
        try:
            from app.decision import execute as execute_mod

            summary["execute"] = execute_mod.from_card(
                data_dir, str(summary["card_id"]),
                account_id=cfg.get("account_id") or "default",
            )
        except Exception as exc:  # noqa: BLE001 - 软失败由调用方兜
            logger.warning("decision execute failed: %s", exc)
            summary["execute"] = {"status": "failed",
                                  "reason": f"{type(exc).__name__}: {exc}"}

    # 名单的身份跟着结果一起回传：同一 as_of 的卡有没有对应的名单，
    # 在管道日志里要一眼可判，而不是去翻目录。
    summary = _finish(summary)
    logger.info("decision stage: %s (settle written=%s, lu=%s)",
                summary.get("status"), settle_summary.get("written"),
                (lu_summary or {}).get("status"))
    return summary
