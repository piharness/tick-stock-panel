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

from app.decision import bars, pwin as pwin_mod
from app.decision import gates as gates_mod
from app.decision import ledger, producer as producer_mod, settle as settle_mod

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
    try:
        cards = ledger.load_cards(data_dir)
        pending = settle_mod.outstanding(data_dir)
    except ledger.LedgerCorruptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    rows = ledger.load_settlements(data_dir)
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
    try:
        cards = ledger.load_cards(_data_dir(request))
    except ledger.LedgerCorruptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"total": len(cards), "cards": [_card_summary(c) for c in cards[-limit:][::-1]]}


@router.get("/cards/{card_id}")
def get_card(card_id: str, request: Request) -> dict[str, Any]:
    card = ledger.get_card(_data_dir(request), card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="决策卡不存在")
    ok, why = ledger.verify_card(card)
    return {"card": card, "verified": ok, "reason": why}


def _exit_visible(row: dict[str, Any], as_of: str) -> bool:
    """可见性口径已下沉到域层（`producer.exit_visible`，§9.8）。

    这里保留薄委托，向后兼容既有引用；新增调用方一律直接用域函数。
    """
    return producer_mod.exit_visible(row, as_of)


def _resolve_picks(req: CardModel, data_dir: Path, *, allow_prior: bool,
                   as_of: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把 picks 的 p_win 补齐，返回 (picks, p_win 出处)。

    口径全部住在域层（`producer.resolve_picks_with_pwin`）：可见性、收缩、
    出处登记与 API/盘后生产者共用一份实现，不再平行维护。HTTP 层只负责
    把两类域异常映射成 400（pick 非法）/ 409（没有证据，fail-closed）。
    """
    raw = [p.model_dump() for p in req.picks]
    try:
        return producer_mod.resolve_picks_with_pwin(
            data_dir, raw,
            auto_pwin=req.auto_pwin,
            strategy_id=req.strategy_id,
            horizon=req.horizon_sessions,
            basis=req.pwin_basis,
            allow_prior=allow_prior,
            as_of=as_of,
        )
    except producer_mod.PickValidationError as exc:
        raise HTTPException(status_code=400,
                            detail={"message": str(exc),
                                    "hint": "传 auto_pwin=true 由账本历史反推，或显式填 p_win。"}) from exc
    except producer_mod.NoEvidenceError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc),
            "hint": ("这是失败关闭：账本里没有证据支撑一个 p_win。"
                     "先补登历史卡并结算，或传 allow_prior=true "
                     "显式用先验常数建卡（卡上记 source=prior）。"),
        }) from exc


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
    try:
        return settle_mod.settle(_data_dir(request), as_of=as_of)
    except ledger.LedgerCorruptError as exc:
        # 残缺账本拒绝结算（fail-closed）；修复或移除损坏文件后重试。
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/produce")
def produce(request: Request, as_of: str | None = Query(default=None)) -> dict[str, Any]:
    """手动触发一次「结算 + 自动发卡」，与盘后管道 decision 阶段同一入口。

    配置来自决策自动发卡偏好（`decision_auto_*`）；门禁 STOP 时只跳过，
    永不 override。`as_of` 缺省时用偏好外的默认路径（数据最新交易日）。
    """
    from datetime import date as _date

    from app.decision.producer import run_decision_stage

    state = request.app.state
    today = None
    if as_of:
        try:
            today = _date.fromisoformat(as_of)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="as_of 需为 ISO 日期") from exc
    try:
        return run_decision_stage(state.repo,
                                  getattr(state, "strategy_engine", None),
                                  today=today)
    except ledger.LedgerCorruptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/score")
def get_score(request: Request, strategy_id: str | None = Query(default=None),
              include_prior: bool = Query(default=False),
              basis: str = Query(default=settle_mod.BASIS_ABS)) -> dict[str, Any]:
    data_dir = _data_dir(request)
    rows = ledger.load_settlements(data_dir)
    try:
        pending = settle_mod.outstanding(data_dir)
    except ledger.LedgerCorruptError as exc:
        # 不能落进下面的 except ValueError：损坏账本是要用户修文件的状态冲突（409），
        # 不是参数错误（400）。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        result: dict[str, Any] = {
            "score": settle_mod.score(rows, strategy_id=strategy_id,
                                      pending=pending,
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
