"""Kill criteria 接线 — 把"该停就停"写成一次可复算的检查。

## 三种状态，不是两种

    PASS       规则已评估，未触发
    TRIGGERED  规则已评估，触发 ⇒ 按规则停止新决策
    UNKNOWN    规则**无法评估**（缺数据 / 缺账户 / 口径不成立）

没有 UNKNOWN，"读不到"就会悄悄变成"没触发"——这正是 Capital-Console 反复
记载的失败模式（K4 的 `asym2` 缺依赖时返回 PASS，等于给了一个好看的绿灯）。
本模块 `strict=False` 时 UNKNOWN 只登记、不阻断；`strict=True` 时 UNKNOWN 也阻断
（"无法证明没有违规" ≠ "证明没有违规"）。

## 四条规则在本仓库的落点

| ID | 规则 | 数据源 | 与 Capital-Console 的差异 |
|---|---|---|---|
| K1_LOSS | 纸面账户回撤 ≥ 阈值 | `strategy.paper.overview` | 用 paper 账户而非 `capital_cny` 真钱账户 |
| K2_ASSUMPTION | 持仓出现 ST / 长停 | `instruments.parquet` + 日K缺口 | 无退市/立案数据源，故不假装能查 |
| K3_NO_RECORD | 决策卡字段完整且哈希自洽 | `decision/ledger` | 空账本不触发（未声称决策 ≠ 记录造假） |
| K4_DATA_STALE | as_of 当天没有数据 | 日K分区目录 | 用落盘分区而非外部新鲜度服务 |

**没有接线的规则不写进来。** 本文件里出现的每一条都真的读了数据；读不到就 UNKNOWN。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.decision import bars
from app.decision.ledger import LedgerCorruptError, load_cards, verify_card
from app.strategy import paper

PASS = "PASS"
TRIGGERED = "TRIGGERED"
UNKNOWN = "UNKNOWN"

GO = "GO"
GO_WITH_UNKNOWNS = "GO_WITH_UNKNOWNS"
STOP_NEW = "STOP_NEW"
STOP_NEW_AND_REVIEW = "STOP_NEW_AND_REVIEW"

DEFAULT_LOSS_LIMIT = 0.15
DEFAULT_SUSPENSION_SESSIONS = 10

#: 名称里出现这些片段即视为"假设已破坏"（ST / 退市整理）。只做包含匹配，
#: 不做拼音/简称猜测——误报会让用户永久停摆，漏报会被 K2 的 UNKNOWN 兜住。
_NAME_TOKENS = ("ST", "退")


@dataclass
class Check:
    id: str
    rule: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": self.rule,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class GateResult:
    as_of: str
    decision: str
    checks: list[Check]
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision.startswith("STOP")

    def by_id(self, cid: str) -> Check | None:
        return next((c for c in self.checks if c.id == cid), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "decision": self.decision,
            "blocked": self.blocked,
            "checks": [c.to_dict() for c in self.checks],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# K1 亏损
# ---------------------------------------------------------------------------


def check_k1_loss(data_dir: Path, as_of: str, *, account_id: str = "default",
                  loss_limit: float = DEFAULT_LOSS_LIMIT) -> Check:
    rule = f"纸面账户回撤 >= {loss_limit:.0%}"
    try:
        acc = paper.get_account(Path(data_dir), account_id)
    except Exception as exc:  # 读不出账户 = 无法评估，不是"没有亏损"
        return Check("K1_LOSS", rule, UNKNOWN, f"读取纸面账户失败：{exc}", {})
    if acc is None:
        return Check("K1_LOSS", rule, PASS,
                     "未开通纸面账户：没有部署中的资金，回撤为 0。",
                     {"account_id": account_id, "initialized": False})

    holdings = [s for s, pos in paper.load_positions(Path(data_dir), account_id).items()
                if int(pos.get("qty") or 0) > 0]
    price_map: dict[str, float] = {}
    if holdings:
        rows = bars.bar_map(Path(data_dir), as_of, holdings)
        for symbol, row in rows.items():
            raw = row.get("raw_close")
            value = raw if raw is not None else row.get("close")
            if value is not None:
                price_map[symbol] = float(value)

    overview = paper.overview(Path(data_dir), price_map or None, account_id)
    initial = float(overview.get("initial_cash") or 0.0)
    total = float(overview.get("total") or 0.0)
    if initial <= 0:
        return Check("K1_LOSS", rule, UNKNOWN,
                     "账户初始资金为 0 ⇒ 无法以本金为分母计算回撤。",
                     {"account_id": account_id, "initial_cash": initial})

    loss = max(0.0, (initial - total) / initial)
    unpriced = sorted(set(holdings) - set(price_map))
    if unpriced and loss < loss_limit:
        # 缺价时 overview 用成本价顶替 ⇒ 回撤被系统性低估。已经超限的仍判 TRIGGERED
        # （宁可误停，不可漏停）；未超限的不能判 PASS，只能判 UNKNOWN。
        return Check("K1_LOSS", rule, UNKNOWN,
                     f"{len(unpriced)} 个持仓取不到收盘价（{', '.join(unpriced[:5])}）⇒ "
                     f"回撤 {loss:.2%} 只是下界，不能据此放行。",
                     {"account_id": account_id, "loss_pct": round(loss, 6),
                      "unpriced": unpriced, "nav": round(total, 2)})

    status = TRIGGERED if loss >= loss_limit else PASS
    detail = (f"回撤 {loss:.2%}（本金 ¥{initial:,.0f} → 净值 ¥{total:,.0f}）"
              + (f"；{len(unpriced)} 个持仓无价，按成本计价" if unpriced else ""))
    return Check("K1_LOSS", rule, status, detail,
                 {"account_id": account_id, "loss_pct": round(loss, 6),
                  "initial_cash": round(initial, 2), "nav": round(total, 2),
                  "unpriced": unpriced or None})


# ---------------------------------------------------------------------------
# K2 假设破坏
# ---------------------------------------------------------------------------


def _instruments(data_dir: Path) -> dict[str, dict[str, Any]]:
    """`{symbol: {name, listing_date}}`；表不可读时返回空 dict（调用方判 UNKNOWN）。"""
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return {}
    import polars as pl

    try:
        df = pl.read_parquet(path)
    except Exception:
        return {}
    cols = set(df.columns)
    if "symbol" not in cols or "name" not in cols:
        return {}
    out: dict[str, dict[str, Any]] = {}
    listing = df["listing_date"].to_list() if "listing_date" in cols else [None] * df.height
    for symbol, name, listed in zip(df["symbol"].to_list(), df["name"].to_list(), listing):
        out[str(symbol)] = {
            "name": str(name),
            "listing_date": str(listed) if listed is not None else None,
        }
    return out


def _suspended(data_dir: Path, symbols: list[str], as_of: str, sessions: int,
               listed: dict[str, str | None] | None = None) -> tuple[list[str], list[str]]:
    """返回 (连续 N 日无行情的标的, 无法判断的标的)。

    "无行情"只说明这段时间没有 bar；停牌、退市、数据缺口在盘面上同形。所以它是
    假设破坏的**证据**，不是"已停牌"的结论——判据文案如实这么写。

    窗口内新上市的标的**排除在外**：没有 bar 是因为还没上市，不是停牌。
    这一条不写，任何次新股建仓都会触发 K2 全停。
    """
    calendar = [d for d in bars.sessions(Path(data_dir)) if d <= as_of]
    if len(calendar) < sessions:
        return [], list(symbols)
    window = calendar[-sessions:]
    seen: dict[str, set[str]] = {s: set() for s in symbols}
    for day in window:
        rows = bars.day_rows(Path(data_dir), day, symbols)
        if rows.is_empty():
            continue
        for symbol in rows["symbol"].to_list():
            seen.setdefault(str(symbol), set()).add(day)
    listed = listed or {}
    suspended: list[str] = []
    for symbol in symbols:
        if seen.get(symbol):
            continue
        listing = listed.get(symbol)
        if listing and listing > window[0]:
            continue                       # 窗口内上市：没 bar 属正常
        suspended.append(symbol)
    return suspended, []


def check_k2_assumption(data_dir: Path, as_of: str, *, account_id: str = "default",
                        suspension_sessions: int = DEFAULT_SUSPENSION_SESSIONS) -> Check:
    rule = "持仓出现假设破坏（ST / 退市整理 / 长期停牌）"
    held = sorted({s for s, pos in paper.load_positions(Path(data_dir), account_id).items()
                   if int(pos.get("qty") or 0) > 0})
    if not held:
        return Check("K2_ASSUMPTION", rule, PASS, "无持仓：无需复核。", {"held": []})

    names = _instruments(Path(data_dir))
    if not names:
        return Check("K2_ASSUMPTION", rule, UNKNOWN,
                     "instruments.parquet 不可读 ⇒ 无法核验 ST/退市状态。",
                     {"held": held})
    unknown = [s for s in held if s not in names]
    hits = {s: names[s]["name"] for s in held
            if s in names and any(t in names[s]["name"] for t in _NAME_TOKENS)}
    suspended, undecidable = _suspended(
        Path(data_dir), [s for s in held if s in names], as_of, suspension_sessions,
        listed={s: v.get("listing_date") for s, v in names.items()},
    )

    if hits:
        return Check("K2_ASSUMPTION", rule, TRIGGERED,
                     f"{len(hits)} 个持仓名称命中假设破坏词："
                     + "; ".join(f"{k} {v}" for k, v in sorted(hits.items())),
                     {"hits": hits, "held": held, "unknown": unknown or None})
    if suspended:
        return Check("K2_ASSUMPTION", rule, TRIGGERED,
                     f"{len(suspended)} 个持仓连续 {suspension_sessions} 个交易日无行情"
                     f"（{', '.join(suspended[:5])}）⇒ 流动性假设不再成立。",
                     {"suspended": suspended, "held": held, "window": suspension_sessions})
    if unknown or undecidable:
        return Check("K2_ASSUMPTION", rule, UNKNOWN,
                     f"{len(unknown + undecidable)} 个持仓无法核验"
                     f"（{', '.join((unknown + undecidable)[:5])}）⇒ 不构成放行理由。",
                     {"unknown": unknown, "undecidable": undecidable, "held": held})
    return Check("K2_ASSUMPTION", rule, PASS,
                 f"{len(held)} 个持仓均未命中假设破坏证据。", {"held": held})


# ---------------------------------------------------------------------------
# K3 记录完整
# ---------------------------------------------------------------------------


def check_k3_record(data_dir: Path, as_of: str) -> Check:
    rule = "决策卡字段完整且哈希自洽"
    try:
        cards = load_cards(Path(data_dir))
    except LedgerCorruptError as exc:
        # 损坏卡是「记录不完整」的极端形态：门必须转 TRIGGERED 阻断放行，
        # 而不是让门自己崩溃（否则 evaluate 整体失效）或静默视作空账本放行。
        return Check("K3_NO_RECORD", rule, TRIGGERED,
                     f"账本文件损坏（fail-closed）：{exc}", {"cards": None})
    if not cards:
        return Check("K3_NO_RECORD", rule, PASS,
                     "账本为空：尚未声称任何决策，不构成记录缺失。", {"cards": 0})
    broken: list[dict[str, str]] = []
    for card in cards:
        ok, why = verify_card(card)
        if not ok:
            broken.append({"card_id": str(card.get("card_id")), "why": why})
    if broken:
        return Check("K3_NO_RECORD", rule, TRIGGERED,
                     f"{len(broken)} 张卡哈希不自洽（被改写过）："
                     + "; ".join(f"{b['card_id']}←{b['why']}" for b in broken[:3]),
                     {"cards": len(cards), "broken": broken})
    return Check("K3_NO_RECORD", rule, PASS,
                 f"{len(cards)} 张卡字段完整、哈希自洽。", {"cards": len(cards)})


# ---------------------------------------------------------------------------
# K4 数据新鲜
# ---------------------------------------------------------------------------


def check_k4_fresh(data_dir: Path, as_of: str) -> Check:
    rule = "as_of 当日行情已落盘"
    latest = bars.latest_session(Path(data_dir))
    if latest is None:
        return Check("K4_DATA_STALE", rule, UNKNOWN,
                     "日K分区为空 ⇒ 无法判断新鲜度（不是新鲜）。", {})
    if latest < as_of:
        return Check("K4_DATA_STALE", rule, TRIGGERED,
                     f"数据止于 {latest}，决策日 {as_of} 尚无行情 ⇒ 决策用的是过期信息。",
                     {"latest": latest, "as_of": as_of})
    return Check("K4_DATA_STALE", rule, PASS,
                 f"数据已覆盖至 {latest}（决策日 {as_of}）。",
                 {"latest": latest, "as_of": as_of})


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

_ACTION_OF = {
    "K1_LOSS": STOP_NEW,
    "K2_ASSUMPTION": STOP_NEW_AND_REVIEW,
    "K3_NO_RECORD": STOP_NEW,
    "K4_DATA_STALE": STOP_NEW,
}


def evaluate(
    data_dir: Path,
    *,
    as_of: str | None = None,
    account_id: str = "default",
    loss_limit: float = DEFAULT_LOSS_LIMIT,
    suspension_sessions: int = DEFAULT_SUSPENSION_SESSIONS,
    strict: bool = False,
) -> GateResult:
    """四条规则一起算。`as_of` 缺省取最新交易日。"""
    day = as_of or bars.latest_session(Path(data_dir)) or date.today().isoformat()
    checks = [
        check_k1_loss(Path(data_dir), day, account_id=account_id, loss_limit=loss_limit),
        check_k2_assumption(Path(data_dir), day, account_id=account_id,
                            suspension_sessions=suspension_sessions),
        check_k3_record(Path(data_dir), day),
        check_k4_fresh(Path(data_dir), day),
    ]
    triggered = [c for c in checks if c.status == TRIGGERED]
    unknowns = [c for c in checks if c.status == UNKNOWN]

    decision = GO
    if triggered:
        acts = {_ACTION_OF.get(c.id, STOP_NEW) for c in triggered}
        decision = STOP_NEW_AND_REVIEW if STOP_NEW_AND_REVIEW in acts else STOP_NEW
    elif unknowns:
        decision = STOP_NEW if strict else GO_WITH_UNKNOWNS

    notes: list[str] = []
    if triggered:
        notes.append("触发：" + ", ".join(f"{c.id}({c.status})" for c in triggered))
    if unknowns:
        notes.append("无法评估：" + ", ".join(c.id for c in unknowns)
                     + ("（strict ⇒ 阻断）" if strict else "（未阻断）"))
    return GateResult(as_of=day, decision=decision, checks=checks, notes=notes)
