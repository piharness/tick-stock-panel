"""现金腿下单计划（短融打底 + 国债锚）—— 唯一产地。

为什么单独一个脚本：页面、CLI、账户脚本三处都要回答「明天买多少份」，
如果各处各自取整一次，就会出现三个答案（本金取整、100 份整手、佣金最低值任何一处不同都算）。
本文件是唯一计算点，其余入口只读它的产出物 `data/etf_watch/cash_leg.json`。

规则（写死，不随标的漂移）：
  R1 国债市值 = bond_w × 权益（权益取账户快照，不用成本价）
  R2 短融市值 = 权益 − 腿1 − 国债 − 现金
  R3 缺钱顺序：先卖短融，短融不足 1 手才卖国债

标的可换：把 BOND / SHORT 换成别的代码与名称即可，流程不改（见 docs/structural-edge/account-runbook.md §7）。

运行：
  python analysis/etf_watch_cash_leg.py                     # 从 account.json 取权益/腿1
  python analysis/etf_watch_cash_leg.py --equity 600000 --leg1 0
  python analysis/etf_watch_cash_leg.py --bond-w 0.4 --no-write
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT = ROOT / "data/etf_watch" / "cash_leg.json"
ACCOUNT = ROOT / "data/etf_watch" / "account.json"

BOND = {"symbol": "511010.SH", "name": "511010 国债ETF国泰", "duration": 4.3}
SHORT = {"symbol": "511360.SH", "name": "511360 短融ETF"}
BOND_W = 0.40              # 国债占权益目标；改它等于改回撤预算（见 runbook §3）
REBALANCE_MIN = 3_000.0    # 偏离小于此不调仓：最低佣金 0.3 元在碎单上放大费率
FEE_RATE = 0.00005
FEE_MIN = 0.3
LOT = 100


def fee(amount: float) -> float:
    return round(max(amount * FEE_RATE, FEE_MIN), 2)


def plan(equity: float, leg1: float, bond_w: float,
         p_bond: float, p_short: float) -> dict:
    """纯函数：给定权益、腿1、价格，算出两份订单。所有取整只在这里发生一次。"""
    avail = max(equity - leg1, 0.0)
    bond_target = equity * bond_w
    lots = max(int(round(bond_target / p_bond / LOT)), 0) if p_bond > 0 else 0
    shares_bond = lots * LOT
    amount_bond = shares_bond * p_bond
    left = avail - amount_bond
    shares_short = max(int(left // p_short // LOT), 0) * LOT if p_short > 0 else 0
    amount_short = shares_short * p_short
    residual = left - amount_short
    deviation = amount_bond - bond_target
    orders = []
    if shares_bond:
        orders.append({"no": 1, "action": "BUY", "symbol": BOND["symbol"], "name": BOND["name"],
                       "shares": shares_bond, "price": round(p_bond, 3),
                       "amount": round(amount_bond, 0), "fee": fee(amount_bond),
                       "why": f"{bond_w:.0%} 权益锚（久期 {BOND['duration']:.1f}）"})
    if shares_short:
        orders.append({"no": len(orders) + 1, "action": "BUY", "symbol": SHORT["symbol"],
                       "name": SHORT["name"], "shares": shares_short, "price": round(p_short, 3),
                       "amount": round(amount_short, 0), "fee": fee(amount_short),
                       "why": "吸收剩余（R2）"})
    return {
        "equity": round(equity, 0), "leg1": round(leg1, 0), "available": round(avail, 0),
        "bond_w": round(bond_w, 4),
        "bond_target": round(bond_target, 0), "bond_amount": round(amount_bond, 0),
        "short_amount": round(amount_short, 0), "residual": round(residual, 0),
        "deviation": round(deviation, 0), "need_rebalance": abs(deviation) > REBALANCE_MIN,
        "rebalance_min": REBALANCE_MIN,
        "duration_years": round(amount_bond * BOND["duration"] / equity, 2) if equity else 0.0,
        "orders": orders,
    }


def latest_prices() -> tuple[dict, dict]:
    """最新收盘（与账户脚本同源：backfill 全量 + 更新的日分区）。"""
    import etf_watch  # noqa: E402  —— 延迟导入，纯函数被 API 复用时不加载数据层

    d, meta = etf_watch.fresh_etf()
    out, asof = {}, {}
    for k in (BOND, SHORT):
        s = d[d["symbol"] == k["symbol"]].sort_values("date")
        if s.empty:
            raise SystemExit(f"{k['symbol']} 不在 ETF 日线里，无法定价")
        out[k["symbol"]] = float(s["close"].iloc[-1])
        asof[k["symbol"]] = str(s["date"].iloc[-1].date())
    return out, {"etf_last": meta.get("last"), "asof": max(asof.values())}


def read_account() -> dict:
    if ACCOUNT.exists():
        return json.loads(ACCOUNT.read_text(encoding="utf-8"))
    return {}


def build(equity: float | None = None, leg1: float | None = None,
          bond_w: float = BOND_W, prices: dict | None = None,
          asof: str | None = None, write: bool = True) -> dict:
    acc = read_account()
    st = acc.get("state", {})
    if equity is None:
        equity = float(st.get("equity") or 0.0)
    if leg1 is None:
        leg1 = float(st.get("leg1", {}).get("value") or 0.0)
    if equity <= 0:
        raise SystemExit("没有可用权益：先跑 analysis/etf_watch_account.py，或用 --equity 指定")
    src = {"asof": asof, "prices": prices}
    if not prices:
        prices, extra = latest_prices()
        src.update(extra)
    p = plan(float(equity), float(leg1), float(bond_w),
             prices[BOND["symbol"]], prices[SHORT["symbol"]])
    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "asof": src.get("asof") or acc.get("asof"),
            "policy": "短融打底 + 国债锚（R1/R2/R3）",
            "engine": "analysis/etf_watch_cash_leg.py::plan（唯一计算点）",
            "bond": {**BOND, "weight": round(bond_w, 4)},
            "short": dict(SHORT),
            "rules": {
                "R1": "国债市值 = bond_w × 权益",
                "R2": "短融市值 = 权益 − 腿1 − 国债 − 现金",
                "R3": "缺钱先卖短融；短融不足 1 手才卖国债",
            },
            "prices": {k: {"close": round(v, 3), "asof": src.get("asof")} for k, v in prices.items()},
        },
        **p,
    }
    if write:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=None)
    ap.add_argument("--leg1", type=float, default=None)
    ap.add_argument("--bond-w", type=float, default=BOND_W)
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    r = build(a.equity, a.leg1, a.bond_w, write=not a.no_write)
    print(f"权益 {r['equity']:,.0f} | 腿1 {r['leg1']:,.0f} | 国债目标 {r['bond_target']:,.0f}"
          f"（{r['bond_w']:.0%}）| 久期暴露 {r['duration_years']} 年")
    for o in r["orders"]:
        print(f"  {o['no']}. {o['action']} {o['name']}  {o['shares']:,} 份 × {o['price']} "
              f"= {o['amount']:,.0f}  佣金 {o['fee']}")
    print(f"  留现金 {r['residual']:,.0f} | 偏离 {r['deviation']:+,.0f} | "
          f"{'需再平衡' if r['need_rebalance'] else '不调仓'}")
    if not a.no_write:
        print(f"写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
