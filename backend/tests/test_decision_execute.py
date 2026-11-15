"""决策卡 → 订单桥的契约。

三件事必须钉住，否则「能下单」会变成另一种静默：
1. **限价是条件成交，不是报价上限**：开盘高于限价就不该进这个回合；
2. **回合平仓卖的是老股**：T+1 只允许卖 `lot.date < day` 的批次，没有底仓
   就不许造一个不可实现的日内交易；
3. **历史卡不许执行**：拿今天的开盘价去成交一个月前的判断，是用未来价成交。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.decision import execute, ledger, settle
from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
D0 = date(2026, 1, 5)
D1 = D0 + timedelta(days=1)
D2 = D0 + timedelta(days=2)
D3 = D0 + timedelta(days=3)


def _seed(tmp_path: Path, prices: dict[date, tuple[float, float]]) -> None:
    """prices: {日期: (开盘, 收盘)}。high/low 由两者包住，够撮合用。"""
    rows = []
    for day in sorted(prices):
        o, c = prices[day]
        rows.append({
            "symbol": SYM, "date": day,
            "open": o, "high": max(o, c), "low": min(o, c), "close": c,
            "volume": 1e6, "amount": 1e8,
        })
    KlineRepository(DataStore(tmp_path)).append_daily(pl.DataFrame(rows))


def _card(tmp_path: Path, *, as_of: date, entry: date, limit: float) -> str:
    card = ledger.build_card(
        as_of=as_of.isoformat(),
        picks=[{"symbol": SYM, "limit_price": limit, "p_win": 0.5}],
        horizon_sessions=0,
        strategy_id="l1_gap_reversal",
        forecast={"entry_date": entry.isoformat(), "backfill": True},
        fill_mode="open_only",
    )
    ledger.save_card(tmp_path, card)
    return card["card_id"]


def _account(tmp_path: Path, cash: float = 1_000_000.0) -> None:
    paper.create_account(tmp_path, cash, account_id=paper.DEFAULT_ACCOUNT_ID)


def _today(monkeypatch, day: date) -> None:
    monkeypatch.setattr(execute, "cn_today", lambda: day)


def _seed_inventory(tmp_path: Path, qty: int = 200, on: date = D1) -> None:
    """在 `on` 日开盘建底仓：先下单，再由 settle_day 成交成 `lot.date = on`。"""
    order, err = paper.create_order(
        tmp_path, SYM, "buy", qty=qty, order_type="next_open",
        ref_price=100.0, source="seed:inventory")
    assert order is not None, err
    assert paper.settle_day(tmp_path, on.isoformat())["filled"] == 1


def _seed_inventory(tmp_path: Path, qty: int = 200, on: date = D1) -> None:
    """在 `on` 日开盘建底仓：先下单，再由 settle_day 成交成 `lot.date = on`。"""
    order, err = paper.create_order(
        tmp_path, SYM, "buy", qty=qty, order_type="next_open",
        ref_price=100.0, source="seed:inventory")
    assert order is not None, err
    assert paper.settle_day(tmp_path, on.isoformat())["filled"] == 1


def test_limit_price_blocks_high_open(tmp_path: Path, monkeypatch):
    """开盘 110 > 限价 100 ⇒ 不成交，理由可读。"""
    _seed(tmp_path, {D1: (100.0, 100.0), D2: (110.0, 112.0)})
    _account(tmp_path)
    _seed_inventory(tmp_path)
    card_id = _card(tmp_path, as_of=D1, entry=D2, limit=100.0)
    _today(monkeypatch, D1)

    out = execute.from_card(tmp_path, card_id)
    assert out["status"] == "ok", out
    assert out["n_created"] == 1

    summary = paper.settle_day(tmp_path, D2.isoformat())
    assert summary["filled"] == 0
    assert summary["expired"] == 1
    # 按来源找卡单：底仓单也在这个目录里，按索引取会拿到别人。
    card_orders = [o for o in paper.load_orders(tmp_path)
                   if str(o.get("source") or "").startswith(execute.SOURCE_PREFIX)]
    assert len(card_orders) == 1
    assert card_orders[0]["limit_price"] == 100.0        # 限价必须随单落盘
    assert card_orders[0]["status"] == "expired"
    assert "高于限价" in (card_orders[0]["reason"] or "")


def test_round_trip_sells_old_shares(tmp_path: Path, monkeypatch):
    """有底仓时：开盘买入 + 当日收盘卖出等量老股，卖的是 `lot.date < day` 那批。"""
    _seed(tmp_path, {
        D1: (100.0, 100.0),   # 底仓在建仓日之后成交
        D2: (98.0, 99.0),     # 卡买单开盘成交，收盘平仓
        D3: (99.0, 99.0),
    })
    _account(tmp_path)
    # 底仓：D0 下单，D1 开盘成交 → D1 的 lot
    order, err = paper.create_order(
        tmp_path, SYM, "buy", qty=200, order_type="next_open",
        ref_price=100.0, source="seed:inventory")
    assert order is not None, err
    assert paper.settle_day(tmp_path, D1.isoformat())["filled"] == 1

    card_id = _card(tmp_path, as_of=D1, entry=D2, limit=100.0)
    _today(monkeypatch, D1)
    out = execute.from_card(tmp_path, card_id, budget=20_000.0)
    assert out["n_created"] == 1

    summary = paper.settle_day(tmp_path, D2.isoformat())
    assert summary["filled"] == 1
    assert summary["round_closed"] == 1
    assert summary["round_unfunded"] == 0

    fills = [f for f in paper.load_fills(tmp_path) if f.get("date") == D2.isoformat()]
    sides = sorted(f["side"] for f in fills)
    assert sides == ["buy", "sell"]
    buy = next(f for f in fills if f["side"] == "buy")
    sell = next(f for f in fills if f["side"] == "sell")
    assert buy["qty"] == sell["qty"]          # 等量，净持仓不变
    assert buy["price"] < sell["price"]       # 这一回合低买高卖
    pos = paper.load_positions(tmp_path)[SYM]
    assert pos["qty"] == 200                  # 底仓没被吃掉


def test_no_inventory_means_no_order(tmp_path: Path, monkeypatch):
    """没有底仓 ⇒ 连买单都不下。

    回合靠「买入 + 卖出等量老股」闭合。买了平不掉的那一截会变成隔夜持仓，
    而隔夜腿实测是负的——那不是"少赚一点"，是策略变成了另一个。
    """
    _seed(tmp_path, {D1: (100.0, 100.0), D2: (98.0, 99.0)})
    _account(tmp_path)
    card_id = _card(tmp_path, as_of=D1, entry=D2, limit=100.0)
    _today(monkeypatch, D1)

    out = execute.from_card(tmp_path, card_id, budget=20_000.0)
    assert out["n_created"] == 0
    assert out["n_skipped_no_inventory"] == 1
    assert paper.load_orders(tmp_path) == []


def test_order_qty_is_capped_by_inventory(tmp_path: Path, monkeypatch):
    """买入量不能超过可卖老股：底仓 100 股、预算够买 200 股 ⇒ 只买 100 股。"""
    _seed(tmp_path, {
        D1: (100.0, 100.0),
        D2: (98.0, 99.0),
    })
    _account(tmp_path)
    order, err = paper.create_order(
        tmp_path, SYM, "buy", qty=100, order_type="next_open",
        ref_price=100.0, source="seed:inventory")
    assert order is not None, err
    assert paper.settle_day(tmp_path, D1.isoformat())["filled"] == 1

    card_id = _card(tmp_path, as_of=D1, entry=D2, limit=100.0)
    _today(monkeypatch, D1)
    out = execute.from_card(tmp_path, card_id, budget=20_000.0)  # 预算够 200 股
    assert out["n_created"] == 1
    assert out["created"][0]["qty"] == 100        # 被库存压到 100

    summary = paper.settle_day(tmp_path, D2.isoformat())
    assert summary["round_closed"] == 1
    assert paper.load_positions(tmp_path)[SYM]["qty"] == 100   # 净持仓不变


def test_from_card_is_idempotent(tmp_path: Path, monkeypatch):
    _seed(tmp_path, {D1: (100.0, 100.0), D2: (98.0, 99.0)})
    _account(tmp_path)
    _seed_inventory(tmp_path)
    card_id = _card(tmp_path, as_of=D1, entry=D2, limit=100.0)
    _today(monkeypatch, D1)

    first = execute.from_card(tmp_path, card_id, budget=20_000.0)
    assert first["n_created"] == 1
    again = execute.from_card(tmp_path, card_id, budget=20_000.0)
    assert again["n_created"] == 0
    assert again["n_skipped_dup"] == 1
    assert len(paper.load_orders(tmp_path)) == 2      # 底仓 1 + 卡单 1


def test_from_card_rejects_stale_card(tmp_path: Path, monkeypatch):
    """入场日早于今天 ⇒ 拒绝：那会拿今天的开盘价去成交一个月前的判断。"""
    _seed(tmp_path, {D1: (100.0, 100.0), D2: (98.0, 99.0)})
    _account(tmp_path)
    card_id = _card(tmp_path, as_of=D1, entry=D2, limit=100.0)
    _today(monkeypatch, D3)          # 今天已经过了入场日

    out = execute.from_card(tmp_path, card_id)
    assert out["status"] == "stale_card"
    assert out["entry_date"] == D2.isoformat()
    assert paper.load_orders(tmp_path) == []
