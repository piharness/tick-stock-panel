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

MIN_HORIZON, MAX_HORIZON = 0, 250
MAX_PICKS = 100

#: 撮合模式。它决定「成交了吗」这个问题的答案，所以属于承诺，必须进内容哈希。
#:
#: - `limit`   既有口径：开盘 ≤ 限价按开盘，盘中触及按限价，否则不成交；
#: - `open_only` 只认集合竞价：开盘高于限价就当日不成交，**不许盘中补票**。
#:
#: 为什么需要第二种：L1 缺口反转的 edge 全部落在「开盘 → 收盘」这一段路径上
#: （研究侧 121.5bp 对 6.4bp）。若允许盘中跌到限价再成交，实际买到的是
#: 「高开、盘中回落」的另一批标的——那不是同一个策略，而且它恰好是亏的那一边。
FILL_MODE_LIMIT = "limit"
FILL_MODE_OPEN_ONLY = "open_only"
FILL_MODES = (FILL_MODE_LIMIT, FILL_MODE_OPEN_ONLY)

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
    """承诺部分：只有这些字段进内容哈希。

    `fill_mode` 只在**非默认**时进哈希。原因不是"它不重要"——它决定成交判定，
    当然属于承诺——而是账本只追加：历史卡里没有这个键，无条件加进去会让每一张
    旧卡的 `verify_card` 重算失败，等于把既往承诺全部作废。默认档本来就等于旧
    行为，省略它才是同一个承诺。
    """
    out: dict[str, Any] = {
        "kind": CARD_KIND,
        "as_of": str(card["as_of"]),
        "strategy_id": card.get("strategy_id"),
        "account_id": card.get("account_id"),
        "horizon_sessions": int(card["horizon_sessions"]),
        "benchmark": str(card["benchmark"]),
        "forecast": card.get("forecast") or {},
        "picks": normalize_picks(card.get("picks") or []),
    }
    mode = card.get("fill_mode")
    if mode not in (None, FILL_MODE_LIMIT):
        out["fill_mode"] = str(mode)
    return out


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
    fill_mode: str = FILL_MODE_LIMIT,
) -> dict[str, Any]:
    """构造并封卡；不落盘。重复调用同样入参得到同样 card_id。

    `horizon_sessions=0` 是合法值，含义是「入场日收盘出场」：结算里
    `exit_i = entry_i + horizon`，所以 0 表示入场当天平仓（L1 的
    「T+1 开盘买、T+1 收盘卖」）。它不等于「持有 1 天」——那是
    `horizon_sessions=1`，出场在下一个交易日收盘，多隔一夜。
    """
    try:
        date.fromisoformat(str(as_of))
    except ValueError as exc:
        raise ValueError(f"as_of 不是合法日期: {as_of!r}") from exc
    if not MIN_HORIZON <= int(horizon_sessions) <= MAX_HORIZON:
        raise ValueError(f"horizon_sessions 必须落在 [{MIN_HORIZON}, {MAX_HORIZON}]")
    if str(fill_mode) not in FILL_MODES:
        raise ValueError(f"fill_mode 必须是 {FILL_MODES} 之一: {fill_mode!r}")
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
        "fill_mode": str(fill_mode),
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


def _scan_settlements(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """逐行读结算流水，返回 (可解析的记录, 问题清单)。**不抛异常**。

    问题清单每一条都带行号：只报「有几处坏」而不报「哪一行」，读者只能把
    整份文件重看一遍——而这份文件的每一行都是一次已经发生的结算。

    重复键计入问题：`settle()` 的幂等性保证同一 key 只写一次，出现两行同键
    说明要么写入中断后重试、要么多进程并发（§9.36 二 的已知边界）。
    这两种情形下 `score()` 都会把一笔样本计成两笔，静默收下等于让分数无声
    失真。
    """
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"第 {lineno} 行 JSON 解析失败（{exc.msg}）")
            continue
        if not isinstance(row, dict):
            problems.append(f"第 {lineno} 行不是对象")
            continue
        if row.get("kind") != SETTLEMENT_KIND:
            problems.append(f"第 {lineno} 行 kind 非结算记录")
            continue
        key = row.get("key")
        if not key:
            # `settle()` 的幂等性靠 key；缺 key 的行既不会被跳过、也不会被覆盖，
            # 它会在每次读到时被当成一条新样本。这不是「不完整」，是不可用。
            problems.append(f"第 {lineno} 行缺 key")
            continue
        key = str(key)
        if key in seen:
            problems.append(f"第 {lineno} 行 key 重复（同第 {seen[key]} 行）")
            continue
        seen[key] = lineno
        rows.append(row)
    return rows, problems


def load_settlements(data_dir: Path) -> list[dict[str, Any]]:
    """全部结算记录，按写入顺序（jsonl 只追加，顺序即写入时间序）。

    损坏行（解析失败 / 不是对象 / kind 非结算记录 / 同键重复）直接抛
    `LedgerCorruptError` 并列出问题行号——fail-closed，与 `load_cards` 同构。

    这条曾经是 `except: continue`。静默跳过一行的后果不是「少一个样本」，
    而是**两个下游看到两份不同的账本**：`settle()` 认为该 key 没写过，于是
    重算并再写一行（同键重复，从此永久双计）；`score()` 认为样本少了一条。
    同一份文件被两处以两种方式读错，而两处都不报错。

    目录不存在仍是空账本（从未写入），不是损坏——两者要靠
    `ledger_health()` 区分，不能都读作「空」。
    """
    path = settlements_path(data_dir)
    if not path.exists():
        return []
    rows, problems = _scan_settlements(path)
    if problems:
        more = "" if len(problems) <= 10 else f"；…共 {len(problems)} 处"
        raise LedgerCorruptError(
            f"结算流水有 {len(problems)} 处损坏，拒绝在残缺账本上继续"
            f"（fail-closed）：" + "；".join(problems[:10]) + more
        )
    return rows


#: `ledger_health()` 的四态。它不是 `load_*` 的返回值，是**解释**：
#: 同一句「0 条记录」至少有三种成因，而这三种的下一步动作完全不同。
HEALTH_NEVER_WRITTEN = "never_written"
HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"
HEALTH_CORRUPT = "corrupt"


def ledger_health(data_dir: Path) -> dict[str, Any]:
    """账本的可判定性快照。**从不抛异常**——它的职责是替你分辨「0 条」是哪一种。

    三种「0 条记录」：

    - `never_written`：`<data_dir>/user_data/decision` 不存在。账本从未被写过，
      所以「没有结算」等于「还没有开始计时」，不是「计时坏了」；
    - `corrupt`：目录在、文件在，但有行读不出来。此时 `load_cards` /
      `load_settlements` 会抛错，而**任何统计量都不该在这份账本上计算**；
    - `degraded`：全部能读，但存在**已被 `done` 键固化、永远不会重算**的残缺行
      （`outcome` 缺失却已成交、或 `outcome_med` 缺失）。它们不会自己好。

    第三种最容易被误读成第二种的轻症。`settle()` 按 `done` 集合永久跳过已写
    key，所以一条 `outcome_med=None` 的行是终态：`median_covered` 会一直小于
    `written`，而每一份读数都仍然「正常」。这里把它数出来，就是为了让
    「读数正常但分母在缩水」在事发当时可见。

    `status` 只看这四态；细项在 `cards` / `settlements` 两个块里，字段名即口径。
    """
    base = root(data_dir)
    cards = cards_dir(data_dir)
    spath = settlements_path(data_dir)

    cards_info: dict[str, Any] = {
        "dir": str(cards),
        "dir_exists": cards.is_dir(),
        "count": 0,
        "error": None,
    }
    try:
        cards_info["count"] = len(load_cards(data_dir))
    except LedgerCorruptError as exc:
        cards_info["error"] = str(exc)

    settle_info: dict[str, Any] = {
        "path": str(spath),
        "exists": spath.exists(),
        "lines": 0,
        "rows": 0,
        #: 每一处都带行号；空列表表示整份文件逐行可解析。
        "problems": [],
        #: 已成交却读不出读数（到期日缺价）——`done` 键已固化，重跑不会修。
        "filled_without_outcome": 0,
        #: 有绝对读数但没有中位数读数（当日全市场分区不足）——同上，永久缺。
        "median_missing": 0,
    }
    if spath.exists():
        rows, problems = _scan_settlements(spath)
        text = spath.read_text(encoding="utf-8")
        settle_info["lines"] = sum(1 for line in text.splitlines() if line.strip())
        settle_info["rows"] = len(rows)
        settle_info["problems"] = problems
        settle_info["filled_without_outcome"] = sum(
            1 for r in rows
            if (r.get("fill") or {}).get("status") == "FILLED" and r.get("outcome") is None)
        settle_info["median_missing"] = sum(
            1 for r in rows
            if r.get("outcome") is not None and r.get("outcome_med") is None)

    frozen = settle_info["filled_without_outcome"] + settle_info["median_missing"]
    if not base.exists():
        status = HEALTH_NEVER_WRITTEN
        detail = (f"{base} 不存在：账本从未被写过。「没有结算」在此只等于"
                  f"「还没开始计时」，不是「计时坏了」。")
    elif settle_info["problems"] or cards_info["error"]:
        status = HEALTH_CORRUPT
        detail = (f"账本可读性受损：结算流水 {len(settle_info['problems'])} 处问题、"
                  f"决策卡 {'有' if cards_info['error'] else '无'}问题。"
                  f"在此之上算出的任何统计量都不成立。")
    elif frozen:
        status = HEALTH_DEGRADED
        detail = (f"全部可解析，但有 {frozen} 条残缺结算行已被 `done` 键固化："
                  f"{settle_info['filled_without_outcome']} 条已成交无读数、"
                  f"{settle_info['median_missing']} 条缺中位数读数。"
                  f"重跑 `settle()` 不会修它们——它们已不在待结算集合里。")
    else:
        status = HEALTH_OK
        detail = "账本可读且无固化残缺行。"

    return {
        "status": status,
        #: 账本根目录。它是否存在，是「从未写入」与其余各态的唯一分界。
        "root": str(base),
        "root_exists": base.exists(),
        "cards": cards_info,
        "settlements": settle_info,
        "detail": detail,
    }


def settlement_key(card_id: str, symbol: str) -> str:
    return f"{card_id}:{symbol}"
