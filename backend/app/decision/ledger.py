"""决策卡：冻结、哈希、只追加。

## 为什么是"内容哈希"而不是"签名"

Capital-Console 区分两类对象：**承诺**（S/P，Principal 签名，改动即换策略）与
**观测**（M/A，只记 version + sha256 + provenance）。TSP 没有 Principal 私钥，
也不需要冒充有——所以本模块保留这个区分，但把"签名"降级为**内容哈希**：

    承诺 = 决策内容（as_of / picks / 限价 / 预测 / horizon）→ sha256 → card_id
    观测 = 封卡时的门状态（gates）→ 单独 sha256，随卡存档

承诺变了 ⇒ card_id 变了 ⇒ 它是**另一张卡**，不是"改过的旧卡"。这就是预登记的
全部效力：你无法在结果出来之后，把当初的判断悄悄改一个数字。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.services.fs_utils import atomic_write_text

CARD_KIND = "TSP-DECISION-CARD-v1"
SETTLEMENT_KIND = "TSP-DECISION-SETTLEMENT-v1"

SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
CARD_ID_RE = re.compile(r"^dc-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$")

MIN_HORIZON, MAX_HORIZON = 1, 250
MAX_PICKS = 100

#: K3 要求每张卡必须齐全的字段（缺一 ⇒ 记录不完整 ⇒ 实验不成立）
REQUIRED_PICK_FIELDS = ("symbol", "limit_price", "p_win")


class LedgerCorruptError(ValueError):
    """账本文件损坏（fail-closed）。

    残缺账本不得当作完整账本继续使用：K3 门靠 load_cards 检测坏记录，
    结算统计靠它保证样本完整——静默跳过损坏卡等于把这两个环节一起弄瞎
    （K3 缺口，2026-11-07 修复）。修复方式由调用方决定：K3 门转 TRIGGERED
    阻断放行；API 层把错误映射为 409 并给出损坏文件清单。
    """


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "decision"


def cards_dir(data_dir: Path) -> Path:
    return root(data_dir) / "cards"


def settlements_path(data_dir: Path) -> Path:
    return root(data_dir) / "settlements.jsonl"


# ---------------------------------------------------------------------------
# 规范化 + 哈希
# ---------------------------------------------------------------------------


def _norm_price(value: Any) -> float:
    price = round(float(value), 4)
    if price <= 0:
        raise ValueError("limit_price 必须为正数")
    return price


def _norm_prob(value: Any) -> float:
    p = round(float(value), 6)
    if not 0.0 < p < 1.0:
        raise ValueError("p_win 必须落在 (0, 1) 开区间")
    return p


def normalize_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验并规范化 picks；同 symbol 重复直接拒绝（一笔决策一个价，不做隐含加权）。"""
    if not isinstance(picks, list) or not picks:
        raise ValueError("picks 不能为空：没有标的的决策卡不是决策")
    if len(picks) > MAX_PICKS:
        raise ValueError(f"picks 上限 {MAX_PICKS} 个，实际 {len(picks)}")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in picks:
        if not isinstance(raw, dict):
            raise ValueError("picks 的每一项必须是对象")
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not SYMBOL_RE.match(symbol):
            raise ValueError(f"symbol 格式非法: {symbol!r}（应形如 600519.SH）")
        if symbol in seen:
            raise ValueError(f"symbol 重复: {symbol}")
        seen.add(symbol)
        item = {
            "symbol": symbol,
            "limit_price": _norm_price(raw.get("limit_price")),
            "p_win": _norm_prob(raw.get("p_win")),
        }
        weight = raw.get("target_weight")
        if weight is not None:
            w = round(float(weight), 6)
            if not 0.0 < w <= 1.0:
                raise ValueError("target_weight 必须落在 (0, 1]")
            item["target_weight"] = w
        out.append(item)
    return sorted(out, key=lambda x: x["symbol"])


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def commitment(card: dict[str, Any]) -> dict[str, Any]:
    """承诺部分：只有这些字段进内容哈希。"""
    return {
        "kind": CARD_KIND,
        "as_of": str(card["as_of"]),
        "strategy_id": card.get("strategy_id"),
        "account_id": card.get("account_id"),
        "horizon_sessions": int(card["horizon_sessions"]),
        "benchmark": str(card["benchmark"]),
        "forecast": card.get("forecast") or {},
        "picks": normalize_picks(card.get("picks") or []),
    }


def card_digest(card: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(commitment(card))).hexdigest()


def gates_digest(gates: dict[str, Any] | None) -> str | None:
    if gates is None:
        return None
    return hashlib.sha256(_canonical(gates)).hexdigest()


def _new_card_id(as_of: str, digest: str) -> str:
    return f"dc-{as_of}-{digest[:10]}"


def build_card(
    *,
    as_of: str,
    picks: list[dict[str, Any]],
    horizon_sessions: int = 20,
    benchmark: str = "000300.SH",
    strategy_id: str | None = None,
    account_id: str = "default",
    forecast: dict[str, Any] | None = None,
    gates: dict[str, Any] | None = None,
    override_reason: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """构造并封卡；不落盘。重复调用同样入参得到同样 card_id。"""
    try:
        date.fromisoformat(str(as_of))
    except ValueError as exc:
        raise ValueError(f"as_of 不是合法日期: {as_of!r}") from exc
    if not MIN_HORIZON <= int(horizon_sessions) <= MAX_HORIZON:
        raise ValueError(f"horizon_sessions 必须落在 [{MIN_HORIZON}, {MAX_HORIZON}]")
    if not SYMBOL_RE.match(str(benchmark)):
        raise ValueError(f"benchmark 格式非法: {benchmark!r}")
    if override_reason is not None and len(override_reason.strip()) < 10:
        raise ValueError("override_reason 至少 10 个字符：越权必须留下可读的理由")

    norm = normalize_picks(picks)
    card: dict[str, Any] = {
        "kind": CARD_KIND,
        "as_of": str(as_of),
        "strategy_id": strategy_id,
        "account_id": account_id,
        "horizon_sessions": int(horizon_sessions),
        "benchmark": benchmark,
        "forecast": dict(forecast or {}),
        "picks": norm,
    }
    digest = card_digest(card)
    card["card_id"] = _new_card_id(str(as_of), digest)
    card["created_at"] = created_at or datetime.now().astimezone().isoformat(timespec="seconds")
    card["gates"] = gates
    card["override"] = {"reason": override_reason.strip()} if override_reason else None
    card["sealed"] = {
        "algorithm": "sha256",
        "sha256": digest,
        "gates_sha256": gates_digest(gates),
        "sealed_at": card["created_at"],
    }
    return card


def verify_card(card: dict[str, Any]) -> tuple[bool, str]:
    """重算内容哈希与门哈希。任何一处对不上都返回 (False, 原因)。"""
    sealed = card.get("sealed") or {}
    expected = sealed.get("sha256")
    if not expected:
        return False, "缺少 sealed.sha256（不是封过的卡）"
    try:
        actual = card_digest(card)
    except ValueError as exc:
        return False, f"卡内容非法: {exc}"
    if actual != expected:
        return False, f"内容哈希不匹配: sealed={expected[:12]}… 实算={actual[:12]}…"
    if card.get("card_id") != _new_card_id(str(card.get("as_of")), actual):
        return False, "card_id 与内容哈希不一致（卡被改写过）"
    gate_expected = sealed.get("gates_sha256")
    if gate_expected and gates_digest(card.get("gates")) != gate_expected:
        return False, "门状态哈希不匹配（gates 在封卡后被改过）"
    return True, "ok"


# ---------------------------------------------------------------------------
# 存储（只追加：卡文件不覆盖，结算走 jsonl）
# ---------------------------------------------------------------------------


def save_card(data_dir: Path, card: dict[str, Any]) -> tuple[Path, bool]:
    """写入卡文件。已存在同 id 时返回 (路径, False) 且**不覆盖**。"""
    directory = cards_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{card['card_id']}.json"
    if path.exists():
        return path, False
    atomic_write_text(path, json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True))
    return path, True


def get_card(data_dir: Path, card_id: str) -> dict[str, Any] | None:
    if not CARD_ID_RE.match(str(card_id)):
        return None
    path = cards_dir(data_dir) / f"{card_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_cards(data_dir: Path) -> list[dict[str, Any]]:
    """全部决策卡，按 as_of 升序（同一天按 created_at）。

    损坏文件（JSON 解析失败 / 非对象 / kind 非决策卡）直接抛
    `LedgerCorruptError` 并列出全部问题文件——fail-closed，不静默跳过。
    cards 目录只有 `save_card` 一个写入方，`dc-*.json` 里出现非决策卡
    就是损坏或外部污染，不存在合法的跳过理由。
    """
    directory = cards_dir(data_dir)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    bad: list[str] = []
    for path in sorted(directory.glob("dc-*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            bad.append(f"{path.name}: {type(exc).__name__}")
            continue
        if not isinstance(card, dict) or card.get("kind") != CARD_KIND:
            bad.append(f"{path.name}: kind 非决策卡")
            continue
        out.append(card)
    if bad:
        raise LedgerCorruptError(
            f"决策卡账本有 {len(bad)} 个损坏文件，拒绝在残缺账本上继续"
            f"（fail-closed）：{'; '.join(bad)}"
        )
    out.sort(key=lambda c: (str(c.get("as_of")), str(c.get("created_at"))))
    return out


def append_settlements(data_dir: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    path = settlements_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def load_settlements(data_dir: Path) -> list[dict[str, Any]]:
    path = settlements_path(data_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def settlement_key(card_id: str, symbol: str) -> str:
    return f"{card_id}:{symbol}"
