"""决策卡 → 模拟盘订单（最后一米）。

## 它补的是哪一段

`paper.create_order` 有三个调用点：手动 API、自定义执行、监控规则。
**没有一个读决策账本**。所以账本能记十张卡两百行结算，纸面账户却八个
交易日零成交——不是没人盯盘，是没有代码能把「判断」变成「仓位」。

## 它不做什么（比它做什么更重要）

**不假装能日内平仓。** L1 的收益是 open→close 同一根 bar，而 T+1 禁止当日
买入当日卖出。真实做法是「持有底仓，买入的同时卖出等量老股」——那需要先有
库存。本模块只下**买入腿**，平仓由 `paper.settle_day` 的 `_close_rounds`
在处理日成交时配对（卖出的是 `lot.date < day` 的老股，合法）；账户没有底仓
时它记 `round_unfunded`，不成交。

所以这里产出的是**执行证据**（成交价、成交率、涨跌停拒绝、缺库存次数），
不是 PnL。PnL 由 `decision.settle` 的回合结算负责，两者不混在一个数里。

## 两条硬守卫

1. **历史卡不许执行。** 卡里带 `forecast.entry_date`；它早于今天就说明这是
   回填卡，拿今天的开盘价去执行一个月前的判断，等于用未来价成交。
2. **幂等。** `source = "card:{card_id}"`。同一张卡的同一标的重跑不会再下单，
   重跑只是把「已存在」数一遍。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.decision import bars, ledger
from app.market_time import cn_today
from app.strategy import paper

#: 订单来源前缀。`_close_rounds` 按它识别哪些买单需要配对平仓。
SOURCE_PREFIX = "card:"


def _entry_of(data_dir: Path, card: dict) -> str | None:
    """这张卡服务的是哪一个入场日。缺 `forecast.entry_date` 时按日历推。"""
    entry = (card.get("forecast") or {}).get("entry_date")
    if entry:
        return str(entry)
    after = [d for d in bars.sessions(data_dir) if d > str(card.get("as_of"))]
    return after[0] if after else None


def from_card(
    data_dir: Path,
    card_id: str,
    *,
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
    budget: float | None = None,
    equity_pct: float = 0.5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把一张卡变成一组 `next_open` 限价买单。

    `budget` 不给时按 `可用现金 × equity_pct`。每只均分，不足一手的跳过
    （`qty_from_amount` 向下取整到百股），这一点必须计数——否则「20 只里
    只下了 17 只」会看起来像全下了。
    """
    card = ledger.get_card(data_dir, card_id)
    if card is None:
        raise ValueError(f"账本里没有这张卡: {card_id}")

    today = cn_today().isoformat()
    entry = _entry_of(data_dir, card)
    if entry is not None and entry < today:
        return {
            "status": "stale_card",
            "card_id": card_id,
            "as_of": card.get("as_of"),
            "entry_date": entry,
            "reason": (
                f"入场日 {entry} 早于今天 {today}：历史卡不能执行，"
                "那会拿今天的开盘价去成交一个月前的判断"),
        }
    notes: list[str] = []
    if entry is None:
        # 周五/节前盘后建卡时，下一交易日的分区还不存在，`entry_date` 推不出来。
        # 这时不能拒——拒了等于「每周五不下单」。按「下一个交易日开盘成交」处理，
        # 但把不确定性写在产物里：成交与否由 `settle_day` 用真实 bar 决定。
        notes.append("entry_date 未落盘（下一交易日未知），按下一交易日开盘成交处理")

    tag = f"{SOURCE_PREFIX}{card_id}"
    orders = paper.load_orders(data_dir, account_id)
    already = {str(o.get("symbol")) for o in orders if str(o.get("source")) == tag}

    picks = card.get("picks") or []
    if not picks:
        return {"status": "no_picks", "card_id": card_id}

    acc = paper.get_account(data_dir, account_id)
    if acc is None:
        raise ValueError("尚未创建模拟账户")
    total = float(budget) if budget is not None else float(acc["cash"]) * float(equity_pct)
    per = total / max(1, len(picks))

    warnings: list[str] = list(notes)
    if str(card.get("fill_mode")) != "open_only":
        warnings.append(
            f"卡的 fill_mode={card.get('fill_mode')}，不是 open_only："
            "limit 口径会把「开盘没成交、盘中回落补上」的反向样本一起买进来")

    created: list[dict] = []
    skipped_dup: list[str] = []
    skipped_small: list[str] = []
    skipped_no_inv: list[str] = []
    failed: list[dict] = []

    for pick in picks:
        symbol = str(pick.get("symbol") or "")
        limit = pick.get("limit_price")
        if not symbol or limit is None:
            failed.append({"symbol": symbol, "reason": "缺 symbol 或 limit_price"})
            continue
        if symbol in already:
            skipped_dup.append(symbol)
            continue
        qty = paper.qty_from_amount(per, float(limit))
        if qty <= 0:
            skipped_small.append(symbol)
            continue
        # **买入量必须被库存约束**：回合靠「买入 + 卖出等量老股」闭合，
        # 买超过库存就有一截平不掉，那一截会变成隔夜持仓（负收益的那条腿）。
        # 预演实测：底仓 700 股、按预算买 800 股 ⇒ 平仓时 `avail < qty`，
        # 整笔不平，持仓从 700 变 1500。这不是"少赚"，是策略变成了另一个。
        cap = paper.sell_capacity(data_dir, symbol, account_id)
        avail = int(cap[0]) if cap else 0
        if avail <= 0:
            skipped_no_inv.append(symbol)
            continue
        qty = min(qty, avail)
        if qty < paper.LOT_SIZE:
            skipped_small.append(symbol)
            continue
        if dry_run:
            created.append({"symbol": symbol, "qty": qty, "limit_price": float(limit)})
            continue
        order, err = paper.create_order(
            data_dir, symbol, "buy",
            account_id=account_id,
            qty=qty,
            order_type="next_open",
            ref_price=float(limit),
            limit_price=float(limit),
            source=tag,
        )
        if order is None:
            failed.append({"symbol": symbol, "reason": err})
        else:
            created.append({"symbol": symbol, "qty": qty,
                            "limit_price": float(limit), "order_id": order["id"]})

    return {
        "status": "dry_run" if dry_run else "ok",
        "card_id": card_id,
        "as_of": card.get("as_of"),
        "entry_date": entry,
        "fill_mode": card.get("fill_mode"),
        "horizon_sessions": card.get("horizon_sessions"),
        "per_pick_budget": round(per, 2),
        "total_budget": round(total, 2),
        "n_picks": len(picks),
        "n_created": len(created),
        "n_skipped_dup": len(skipped_dup),
        "n_skipped_small": len(skipped_small),
        "n_skipped_no_inventory": len(skipped_no_inv),
        "n_failed": len(failed),
        "created": created,
        "skipped_dup": skipped_dup,
        "skipped_small": skipped_small,
        "skipped_no_inventory": skipped_no_inv,
        "failed": failed,
        "warnings": warnings,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def seed_inventory(
    data_dir: Path,
    symbols: list[str],
    *,
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
    per_symbol_amount: float = 25000.0,
    source: str = "seed:inventory",
) -> dict[str, Any]:
    """建底仓：为回合交易准备**可卖的老股**。

    它只下 `next_open` 买单，成交要等下一个交易日的 `settle_day`——
    和真实建仓一样，没有捷径。`source` 与决策卡前缀不同，所以
    `_close_rounds` 不会把这些买单当成需要当日平仓的回合。
    """
    latest = bars.latest_session(data_dir)
    if latest is None:
        return {"status": "no_data", "reason": "没有已落盘的日线分区，建不了底仓"}
    created, failed = [], []
    for symbol in symbols:
        # 用最近收盘换手数：`qty_from_amount` 需要正参考价。
        bar = paper.read_daily_bar(data_dir, symbol, "stock", latest)
        if bar is None:
            failed.append({"symbol": symbol, "reason": "读不到最近日线"})
            continue
        qty = paper.qty_from_amount(float(per_symbol_amount), float(bar["close"]))
        if qty <= 0:
            failed.append({"symbol": symbol, "reason": "金额不足一手"})
            continue
        order, err = paper.create_order(
            data_dir, symbol, "buy",
            account_id=account_id,
            qty=qty,
            order_type="next_open",
            ref_price=float(bar["close"]),
            source=source,
        )
        if order is None:
            failed.append({"symbol": symbol, "reason": err})
        else:
            created.append({"symbol": symbol, "qty": qty, "order_id": order["id"]})
    return {
        "status": "ok",
        "n_requested": len(symbols),
        "n_created": len(created),
        "n_failed": len(failed),
        "created": created,
        "failed": failed,
        "note": "买单在下一个交易日的 settle_day 成交；成交后才成为可卖的老股",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
