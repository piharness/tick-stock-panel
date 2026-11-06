"""决策账本 API — HTTP 壳：校验 → 调域模块 → 返回。不含业务口径。

设计约定（与 paper / lots 一致）：
- 域模块自带文件级原子写与幂等；API 层不加第二把锁；
- 错误一律 400/404/409 + 可读 detail，不用 500 掩盖校验失败；
- 没有删除端点：账本只追加（要作废一张卡，用新卡覆盖判断，而不是抹掉历史）。

`POST /cards` 的 `auto_pwin=true` 会调 `app.decision.pwin.suggest` 用**已结算的
历史卡**反推 p_win——且只允许看见**出场日早于建卡日**的结算
（`docs/decision-ledger.md` §9.8 可见性口径）：补登过去某日的卡时，决策日之后
才产生的结算是未来信息，混进来 `p_win` 就是事后诸葛亮。`allow_prior=false`
（默认）时，若可见样本为空（只有先验、没有证据），直接 409 —— 拒绝的是
「用 prior 冒充证据」，不是「历史胜率不好看」。后者只写进卡上的
`verdict` / `significance`，不拦请求：那个 gate 的输入由被 gate 的行为产生，
永远解不开（`docs/decision-ledger.md` §9.15）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.decision import bars
from app.decision import gates as gates_mod
from app.decision import ledger, pwin as pwin_mod, settle as settle_mod

router = APIRouter(prefix="/api/decision", tags=["decision"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


class PickModel(BaseModel):
    symbol: str
    limit_price: float
    #: `auto_pwin=true` 时可以不填，由账本历史反推。
    p_win: float | None = None
    target_weight: float | None = None


class CardModel(BaseModel):
    as_of: str | None = None
    picks: list[PickModel] = Field(default_factory=list)
    horizon_sessions: int = 20
    benchmark: str = "000300.SH"
    strategy_id: str | None = None
    account_id: str = "default"
    forecast: dict[str, Any] | None = None
    allow_stop: bool = False
    override_reason: str | None = None
    auto_pwin: bool = False
    #: 三态，**不能**写成 `bool = False`。`bool` 分不清「没传」和「传了 false」，
    #: 而旧客户端只传 `require_reliable=false` 时，Pydantic 默认把未知字段
    #: **静默丢弃**（`extra="ignore"`，不报错）⇒ 这里的默认 `False` 胜出 ⇒
    #: 行为在同一处静默反转，只是换了个入口。
    allow_prior: bool | None = None
    #: 旧字段，保留只为兼容，不要在新代码里用。映射方向只有一个：
    #: `false` ⇒ `allow_prior=true`；`true`（含默认）⇒ `allow_prior=false`。
    #: 注意这**不是等价映射**：旧的 `true` 连 unreliable 账本一起拒，
    #: 新的 `false` 只拒「没有证据」。那是 §9.14 一的意图。
    require_reliable: bool = True
    pwin_basis: str = "median"


def _resolve_allow_prior(req: CardModel) -> bool:
    """把两个开关折叠成一个。**这两个字段只在这里读**，别处一律用返回值。

    单点解析是为了让「矛盾」变成 400，而不是让某个字段悄悄胜出。
    """
    if req.allow_prior is not None:
        if req.allow_prior is False and req.require_reliable is False:
            raise HTTPException(status_code=400, detail={
                "message": "allow_prior=false 与 require_reliable=false 相互矛盾",
                "hint": "require_reliable 是 allow_prior 的旧名（取值相反）。两者只传一个。",
            })
        return req.allow_prior
    return not req.require_reliable


def _card_summary(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_id": card.get("card_id"),
        "as_of": card.get("as_of"),
        "created_at": card.get("created_at"),
        "strategy_id": card.get("strategy_id"),
        "account_id": card.get("account_id"),
        "horizon_sessions": card.get("horizon_sessions"),
        "benchmark": card.get("benchmark"),
        "picks": len(card.get("picks") or []),
        "gates_decision": (card.get("gates") or {}).get("decision"),
        "override": card.get("override"),
        "sealed_sha256": (card.get("sealed") or {}).get("sha256"),
    }


@router.get("/overview")
def overview(request: Request, as_of: str | None = Query(default=None)) -> dict[str, Any]:
    data_dir = _data_dir(request)
    gate = gates_mod.evaluate(data_dir, as_of=as_of)
    cards = ledger.load_cards(data_dir)
    rows = ledger.load_settlements(data_dir)
    pending = settle_mod.outstanding(data_dir)
    return {
        "as_of": gate.as_of,
        "gates": gate.to_dict(),
        "cards": len(cards),
        "latest_cards": [_card_summary(c) for c in cards[-5:][::-1]],
        "settlements": len(rows),
        # 两个口径并列返回，不做取舍：`abs` 回答"赚没赚"，`median` 回答"选得准不准"。
        # 它们的样本量含义完全不同，混在一起看会得出相反结论。
        "score": settle_mod.score(rows, pending=pending, basis=settle_mod.BASIS_ABS),
        "score_median": settle_mod.score(rows, pending=pending,
                                         basis=settle_mod.BASIS_MEDIAN),
    }


@router.get("/cards")
def list_cards(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    cards = ledger.load_cards(_data_dir(request))
    return {"total": len(cards), "cards": [_card_summary(c) for c in cards[-limit:][::-1]]}


@router.get("/cards/{card_id}")
def get_card(card_id: str, request: Request) -> dict[str, Any]:
    card = ledger.get_card(_data_dir(request), card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="决策卡不存在")
    ok, why = ledger.verify_card(card)
    return {"card": card, "verified": ok, "reason": why}


def _exit_visible(row: dict[str, Any], as_of: str) -> bool:
    """结算记录在 `as_of` 决策时点是否可见：出场日**严格早于**建卡日。

    口径来自 `docs/decision-ledger.md` §9.8（`exit.date < D`，当天出场仍不算
    可见）。缺 `exit.date` ⇒ 无法证明"当时已知" ⇒ 不可见：fail-closed，与账本
    「读不到即 UNKNOWN」同一哲学。日期均为 ISO 串，字典序即时间序。
    """
    exit_date = (row.get("exit") or {}).get("date")
    return exit_date is not None and str(exit_date) < as_of


def _resolve_picks(req: CardModel, data_dir: Path, *, allow_prior: bool,
                   as_of: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把 picks 的 p_win 补齐，返回 (picks, p_win 出处)。

    `auto_pwin=False` 时每个 pick 必须自带 p_win；缺失就是 400，不猜。
    `auto_pwin=True` 时用账本历史反推；没有证据且 `allow_prior=False` 则 409。

    `as_of` 是**建卡日**（调用方已解析缺省值）。反推只允许看见出场日早于它的
    结算：`load_settlements` 会把账本里全部记录一起返回，补登历史卡时不过滤
    就是把未来函数写进 p_win（2026-11-06 独立评审 P1；回归测试见
    `test_auto_pwin_backfill_ignores_settlements_after_as_of`）。
    """
    raw = [p.model_dump() for p in req.picks]
    missing = [p["symbol"] for p in raw if p.get("p_win") is None]
    if not req.auto_pwin:
        if missing:
            raise HTTPException(
                status_code=400,
                detail={"message": "以下 pick 缺少 p_win：" + ", ".join(missing),
                        "hint": "传 auto_pwin=true 由账本历史反推，或显式填 p_win。"})
        return raw, None
    if not missing:
        # 全部自带 p_win：auto_pwin 没有可补的。旧实现照样写一块
        # `source=ledger` 的出处，等于把人工填的数字记成账本算的——出处
        # 一旦会撒谎，`by_source` 的分组就失去意义。
        return raw, None

    rows = ledger.load_settlements(data_dir)
    rows = [r for r in rows if _exit_visible(r, as_of)]
    try:
        est = pwin_mod.suggest(
            rows,
            strategy_id=req.strategy_id,
            horizon=req.horizon_sessions,
            basis=req.pwin_basis,
            # 交易日历：`n_eff` 的上限分母要按交易日算。发卡稀疏时
            # 决策日与交易日不是同一个单位（`docs/decision-ledger.md` §9.24）。
            sessions=bars.sessions(data_dir),
            allow_prior=allow_prior,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc),
            "hint": ("这是失败关闭：账本里没有证据支撑一个 p_win。"
                     "先补登历史卡并结算，或传 allow_prior=true "
                     "显式用先验常数建卡（卡上记 source=prior）。"),
        }) from exc

    p = float(est["p_win"])
    for item in raw:
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
        # 增量序列的滞后阶数。**与 `hac_lag` 相等**（都是 horizon-1）：`window`
        # 造的是估计量共用样本，它不进 `d_i` 的自协方差（§9.20）。字段仍然
        # 单独存，是因为 §9.18 曾让它取 window-1 并把功效打没——留一个位置，
        # 下次改错时在卡上看得见。
        "hac_lag_incremental": est.get("hac_lag_incremental"),
        # 第二个检验：p 相对「同样诚实的历史常数」有没有增量信息。
        # 与 verdict 并列返回，不替代它——两者回答的是不同问题。
        "verdict_incremental": est.get("verdict_incremental"),
        # 第二个检验的可辨识性三态（PASS / FAIL / UNKNOWN）。`verdict_incremental`
        # 读符号与量级，这个字段读「那个下界有没有分母」——`INCREMENTAL` 配
        # `UNKNOWN` 是「改善过门槛，但有效样本不足 30」，不是「有增量」。
        # 封存进卡，是因为它编码了当时的 `MIN_NEFF` 判据：事后改门槛，
        # 旧卡上的结论会跟着变，而卡的意义正是「当时的判断不可改」。
        "significance_incremental": est.get("significance_incremental"),
        # 这个检验实际比了几天。`truncated_any` 故意不进卡（它能由
        # window/days 推出，见 docs §9.16），但 `truncated_days` 推不出来：
        # 它取决于每天的出场日，而 `oos_days` 只数被评估的日子。
        # 不记这个数，事后只能看到「测过」，看不到它建立在几天上。
        "truncated_days": est.get("truncated_days"),
        "skill_vs_clim": est.get("skill_vs_clim"),
        "skill_vs_clim_lcb": est.get("skill_vs_clim_lcb"),
        "reason_incremental": est.get("reason_incremental"),
        "reason": est["reason"],
    }
    return raw, source


@router.post("/cards")
def create_card(req: CardModel, request: Request) -> dict[str, Any]:
    data_dir = _data_dir(request)
    allow_prior = _resolve_allow_prior(req)
    as_of = req.as_of or bars.latest_session(data_dir)
    if not as_of:
        raise HTTPException(status_code=400, detail="没有可用交易日：请先同步日K数据")

    gate = gates_mod.evaluate(data_dir, as_of=as_of, account_id=req.account_id)
    if gate.blocked and not req.allow_stop:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"门禁判决 {gate.decision}：触发即停，不新开决策。",
                "gates": gate.to_dict(),
                "hint": "确需补登历史卡，传 allow_stop=true 并写明 override_reason（会随卡存档）。",
            },
        )
    if gate.blocked and not (req.override_reason or "").strip():
        raise HTTPException(status_code=400,
                            detail="allow_stop=true 时必须提供 override_reason")

    picks, pwin_source = _resolve_picks(req, data_dir, allow_prior=allow_prior,
                                        as_of=as_of)
    forecast = dict(req.forecast or {})
    if pwin_source is not None:
        # 出处进 forecast ⇒ 进承诺哈希 ⇒ p_win 换了就是另一张卡。
        forecast["pwin"] = pwin_source

    try:
        card = ledger.build_card(
            as_of=as_of,
            picks=picks,
            horizon_sessions=req.horizon_sessions,
            benchmark=req.benchmark,
            strategy_id=req.strategy_id,
            account_id=req.account_id,
            forecast=forecast or None,
            gates=gate.to_dict(),
            override_reason=req.override_reason if gate.blocked else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    path, created = ledger.save_card(data_dir, card)
    return {
        "card": card,
        "created": created,
        "path": path.name,
        "gates": gate.to_dict(),
        "pwin": pwin_source,
        "note": None if created else "同内容的卡已存在，未重复写入（幂等）。",
    }


@router.post("/settle")
def run_settle(request: Request, as_of: str | None = Query(default=None)) -> dict[str, Any]:
    return settle_mod.settle(_data_dir(request), as_of=as_of)


@router.get("/score")
def get_score(request: Request, strategy_id: str | None = Query(default=None),
              include_prior: bool = Query(default=False),
              basis: str = Query(default=settle_mod.BASIS_ABS)) -> dict[str, Any]:
    data_dir = _data_dir(request)
    rows = ledger.load_settlements(data_dir)
    try:
        result: dict[str, Any] = {
            "score": settle_mod.score(rows, strategy_id=strategy_id,
                                      pending=settle_mod.outstanding(data_dir),
                                      basis=basis)
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if include_prior:
        result["prior"] = settle_mod.prior(data_dir)
    return result


@router.get("/pwin")
def get_pwin(request: Request, strategy_id: str | None = Query(default=None),
             horizon: int | None = Query(default=None, ge=1, le=250),
             basis: str = Query(default=settle_mod.BASIS_MEDIAN),
             window: int = Query(default=pwin_mod.DEFAULT_WINDOW, ge=20, le=2000),
             kappa: float = Query(default=pwin_mod.DEFAULT_KAPPA, gt=0, le=1000)) -> dict[str, Any]:
    """某个策略（或全部手工卡）当前的 p_win 估计，及其样本外证据。

    这是建卡前该看的东西：`reliable=false` 意味着历史胜率对未来没有预测力，
    此时 `p_win` 这个数字不该被当作信息使用。
    """
    rows = ledger.load_settlements(_data_dir(request))
    try:
        return pwin_mod.estimate(rows, strategy_id=strategy_id, horizon=horizon,
                                 basis=basis, window=window, kappa=kappa,
                                 # 同一份日历，否则 `/pwin` 与建卡会按两种单位
                                 # 报同一个 `n_eff`（§9.24）。
                                 sessions=bars.sessions(_data_dir(request)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/settlements")
def list_settlements(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    rows = ledger.load_settlements(_data_dir(request))
    return {"total": len(rows), "settlements": rows[-limit:][::-1]}
