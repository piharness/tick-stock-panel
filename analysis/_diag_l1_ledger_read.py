"""账本读数 —— 把「我下过的判断，到期后比常数预测强吗」变成一个可重复的命令。

## 它为什么必须存在

回填（`scripts/backfill_l1_cards.py`）让账本从空变成有内容，但「有内容」不等于
「有信息」。`settle.score()` 早就会算 Brier skill，只是从来没有人跑过它——
因为账本是空的，跑了也是 `n=0`。

这个脚本把那次计算固定成一个可重复的动作：每天（或每周）跑一次，看 `skill`
与 `n_eff` 怎么变。**它是这套系统里唯一一个不能由研究脚本替代的读数**——
研究侧算的是策略的历史收益，这里算的是「当时的预测」准不准。

## 三个必须一起看的量

| 量 | 回答 | 为什么不能单独看 |
|---|---|---|
| `skill` | 预测比常数强多少 | 小样本下正负都能出现，看不出显著性 |
| `n_eff` | 真正独立的样本数 | 持有期让相邻决策日重叠，`n` 会高估几十倍 |
| `significance` | 那个 `skill` 有没有分母 | UNKNOWN 不等于 FAIL，也不等于 PASS |

`n_eff < MIN_NEFF` 时结论固定为「还看不出来」。这不是保守，是 `n_eff` 的定义：
十来个决策日的日间相关让有效样本数远小于行数。

## 用法

    backend/.venv/Scripts/python.exe analysis/_diag_l1_ledger_read.py
    backend/.venv/Scripts/python.exe analysis/_diag_l1_ledger_read.py --strategy l1_gap_reversal

产物：`analysis/_diag_l1_ledger_read_result.json`
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import ledger, settle  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_l1_ledger_read_result.json"


def _pwin_evolution(cards: list[dict]) -> list[dict]:
    """逐卡的预测值与出处。看它是否在「学」，以及先验占多大比重。"""
    out = []
    for card in sorted(cards, key=lambda c: str(c.get("as_of"))):
        picks = card.get("picks") or []
        meta = ((card.get("forecast") or {}).get("pwin")) or {}
        out.append({
            "as_of": card.get("as_of"),
            "horizon": card.get("horizon_sessions"),
            "fill_mode": card.get("fill_mode"),
            "backfill": (card.get("forecast") or {}).get("backfill"),
            "p_win": None if not picks else picks[0].get("p_win"),
            "source": meta.get("source"),
            "n": meta.get("n"),
            "prior_weight": meta.get("prior_weight"),
        })
    return out


def _execution(account_id: str | None) -> dict:
    """账户侧的执行证据：订单按来源分组、现金、持仓。

    账本告诉「判断对不对」，这里告诉「有没有真的下单」。两者必须一起看——
    一张卡可以完美封存，而订单是 0（底仓没成交时 `from_card` 会整批跳过）。
    """
    if not account_id:
        return {"skipped": True}
    from app.strategy import paper

    acc = paper.get_account(DATA, account_id)
    if acc is None:
        return {"error": f"账户不存在: {account_id}"}
    orders = paper.load_orders(DATA, account_id)
    by_source: dict[str, int] = {}
    for o in orders:
        src = str(o.get("source") or "?")
        key = ("card:..." if src.startswith("card:") and src.endswith(":close")
               else "card:..." if src.startswith("card:") else src)
        by_source[key] = by_source.get(key, 0) + 1
    positions = paper.load_positions(DATA, account_id)
    return {
        "account_id": account_id,
        "cash": round(float(acc["cash"]), 2),
        "orders_total": len(orders),
        "orders_by_source": by_source,
        "orders_by_status": {
            s: sum(1 for o in orders if o.get("status") == s)
            for s in sorted({str(o.get("status")) for o in orders})
        },
        "positions": len([p for p in positions.values() if p.get("qty")]),
    }


def read(strategy_id: str | None, account_id: str | None = None) -> dict:
    cards = ledger.load_cards(DATA)
    rows = ledger.load_settlements(DATA)
    if strategy_id is not None:
        rows = [r for r in rows if r.get("strategy_id") == strategy_id]

    filled = [r for r in rows if (r.get("fill") or {}).get("status") == "FILLED"]
    scores = {}
    for basis in ("median", "abs"):
        try:
            scores[basis] = settle.score(rows, strategy_id=None, basis=basis)
        except ValueError as exc:
            scores[basis] = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "_meta": {
            "script": "analysis/_diag_l1_ledger_read.py",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "strategy_id": strategy_id,
            "ledger_dir": str(ledger.root(DATA)),
        },
        "counts": {
            "cards": len(cards),
            "settlement_rows": len(rows),
            "filled": len(filled),
            "not_filled": len(rows) - len(filled),
            "as_of_first": min((str(c.get("as_of")) for c in cards), default=None),
            "as_of_last": max((str(c.get("as_of")) for c in cards), default=None),
        },
        "pwin_evolution": _pwin_evolution(cards),
        "execution": _execution(account_id),
        "score": scores,
        "verdict_note": (
            "n_eff < MIN_NEFF 时 significance 必为 UNKNOWN：那不是失败，是还没有分母"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--account", default=None, help="同时报该账户的下单情况")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out = read(args.strategy, args.account)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")

    c = out["counts"]
    print(f"账本 {c['cards']} 张卡 / {c['settlement_rows']} 行结算 "
          f"（成交 {c['filled']} / 未成交 {c['not_filled']}）")
    print(f"as_of {c['as_of_first']} .. {c['as_of_last']}")
    print("-- p_win 演化 --")
    for r in out["pwin_evolution"]:
        pw = "None" if r["p_win"] is None else f"{r['p_win']:.4f}"
        print(f"  {r['as_of']} p_win={pw} src={r['source']} n={r['n']} "
              f"prior_w={r['prior_weight']}")
    print("-- score --")
    for basis, s in out["score"].items():
        if "error" in s:
            print(f"  {basis}: {s['error']}")
            continue
        print(f"  {basis}: n={s.get('n')} hit={s.get('hit_rate')} "
              f"brier={s.get('brier')} const={s.get('brier_const')} "
              f"skill={s.get('skill')} lcb={s.get('skill_lcb')} "
              f"n_eff={s.get('n_eff')} verdict={s.get('verdict')} "
              f"significance={s.get('significance')}")
    ex = out.get("execution") or {}
    if ex.get("account_id"):
        print("-- 执行 --")
        print(f"  账户 {ex['account_id']} 现金 {ex['cash']} 持仓 {ex['positions']} 只")
        print(f"  订单 {ex['orders_total']} 张 / 来源 {ex['orders_by_source']} / "
              f"状态 {ex['orders_by_status']}")
    elif ex.get("error"):
        print("  执行检查失败:", ex["error"])
    print(f"产物 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
