"""把事件研究放进一个真实账户：六格、逆波动、重叠信号、现金约束。

前面所有测量都是「每笔独立、满仓一笔」的事件收益。真实账户不是这样：
信号会重叠、资金会不够、旧仓没到期就不能重复使用。本脚本只做一件事——
**同一批信号，按两种仓位政策跑一遍账户，看还剩多少非对称性。**

两种政策（先注册，不做事后挑选）：

- A `inv_vol_full`：满仓逆波动。持仓集合一变（进场/到期）就按 1/vol20 归一化到 100%。
  这是 `theme-etf-entry.md` §8 与页面上那六个权重（w=0.107~0.206）的算法。
- B `six_slots`：六格。每笔进场用「当时权益/6」，不重配，其余现金。到期释放。

共同口径：
- 信号＝候选段首日（`etf_watch.run_starts`），同行业 60 交易日冷却；
- t+1 收盘进场，持有 20 个交易日，收盘退出；
- 载体＝信号日 F6 选出的同行业最大 ETF；
- 成本 6bp/边（进场、出场、重配的成交额都算）；
- 不用未来数据：某日收盘知道信号，下一日收盘才成交。

用法：
    python analysis/etf_watch_portfolio.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402
import etf_watch_experiments as X  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

OUT = ROOT / "data/etf_watch/portfolio.json"
HOLD = 20
COST = 0.0006          # 每边 6bp
SLOTS = 6
INIT = 1_000_000.0


def vol20(px: pd.DataFrame) -> pd.Series:
    return px["close_adj"].pct_change().rolling(20, min_periods=15).std() * np.sqrt(252)


def metrics(eq: pd.Series, exposure: pd.Series) -> dict:
    eq = eq.dropna()
    if len(eq) < 2:
        return {}
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    r20 = eq.pct_change(20)
    longest, cur = 0, 0
    for u in (dd < 0):
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    li, ci, ii, cidle = 0, 0, 0, 0
    for e in exposure.to_numpy():
        if e > 0:
            ci += 1; ii = 0
        else:
            ii += 1; ci = 0
        li, cidle = max(li, ci), max(cidle, ii)
    per_year = eq.resample("YE").last()
    yearly = per_year.pct_change()
    yearly.iloc[0] = per_year.iloc[0] / eq.iloc[0] - 1
    return {
        "start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
        "years": round(yrs, 2),
        "total": round(float(eq.iloc[-1] / eq.iloc[0] - 1), 4),
        "cagr": round(float((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1), 4) if yrs > 0 else None,
        "max_dd": round(float(dd.min()), 4),
        "worst20": round(float(r20.min()), 4),
        "longest_underwater": int(longest),
        "longest_invested": int(li),
        "longest_idle": int(cidle),
        "avg_exposure": round(float(exposure.mean()), 3),
        "days_invested_pct": round(float((exposure > 0).mean()) * 100, 1),
        "yearly": {str(k.year): round(float(v), 4) for k, v in yearly.dropna().items()},
    }


def _alloc_full(pos: dict, cash: float, vols: dict, t) -> tuple[dict, float]:
    """满仓逆波动：按 1/vol 归一化到总权益，重配成交额计成本。"""
    tot = cash + sum(p["value"] for p in pos.values())
    inv = {}
    for k, p in pos.items():
        v = vols.get(p["sym"])
        vol = float(v.get(t, np.nan)) if v is not None else np.nan
        inv[k] = 1.0 / max(vol, 1e-6) if np.isfinite(vol) and vol > 0 else np.nan
    if all(np.isnan(v) for v in inv.values()):
        inv = {k: 1.0 for k in pos}
    else:  # 缺 vol 的那只用已知均值补，避免整仓塌成现金
        good = [v for v in inv.values() if np.isfinite(v)]
        fill = float(np.mean(good))
        inv = {k: (v if np.isfinite(v) else fill) for k, v in inv.items()}
    s = sum(inv.values())
    raw = {k: tot * w / s for k, w in inv.items()}
    turnover = sum(abs(raw[k] - pos[k]["value"]) for k in pos)   # 买卖总额
    cost = turnover * COST
    scale = (tot - cost) / tot if tot > 0 else 1.0
    target = {k: v * scale for k, v in raw.items()}
    return target, 0.0        # 成本已经通过 scale 从权益里扣掉，不留成现金


def replay(ev: pd.DataFrame, sessions, prices, vols, sym_mat, policy: str,
           cash_yield: float | pd.Series = 0.0) -> dict:
    pos: dict[str, dict] = {}
    cash = INIT
    eq_rows, exp_rows = [], []
    trades: list[dict] = []
    idx = {t: i for i, t in enumerate(sessions)}
    by_date: dict[pd.Timestamp, list[dict]] = {}
    for _, r in ev.iterrows():
        by_date.setdefault(r["date"], []).append(dict(r))

    start_i = min(idx[d] for d in by_date)
    pending: list[dict] = []
    dirty = False            # 只有持仓集合变化时才重配，否则权重自然漂移

    # cash_yield 可以是年化常数，也可以是逐日实现的收益序列（货币基金真实收益）
    if isinstance(cash_yield, pd.Series):
        cy = cash_yield.reindex(sessions).fillna(0.0)
        daily_cash = cy.to_numpy()
    else:
        daily_cash = np.full(len(sessions), cash_yield / 252)
    for i in range(start_i, len(sessions)):
        t = sessions[i]
        cash *= (1 + float(daily_cash[i]))   # 空仓资金计息（默认 0）
        for p in pos.values():
            px = prices[p["sym"]]
            if i > p["entry_i"] and t in px.index:
                prev = sessions[i - 1]
                if prev in px.index:
                    a, b = float(px.at[prev, "close_adj"]), float(px.at[t, "close_adj"])
                    if np.isfinite(a) and np.isfinite(b) and a > 0:
                        p["value"] *= b / a
        for ind in [k for k, p in pos.items() if p["exit_i"] == i]:
            p = pos.pop(ind)
            proceeds = p["value"] * (1 - COST)
            cash += proceeds
            dirty = True
            if p["entry_i"] < i:
                px = prices[p["sym"]]
                a, b = float(px.at[sessions[p["entry_i"]], "close_adj"]), float(px.at[t, "close_adj"])
                trades.append({"industry": ind, "symbol": p["sym"],
                               "entry": str(sessions[p["entry_i"]].date()), "exit": str(t.date()),
                               "ret": round(b / a - 1, 4) if a > 0 else None})
            else:
                trades.append({"industry": ind, "symbol": p["sym"],
                               "entry": str(sessions[p["entry_i"]].date()), "exit": str(t.date()), "ret": 0.0})

        for e in pending:
            sym = e["sym"]
            if not sym or sym not in prices or i + HOLD >= len(sessions):
                continue
            px = prices[sym]
            if t not in px.index:
                continue
            v = float(px.at[t, "close_adj"])
            if not np.isfinite(v) or v <= 0:
                continue
            pos[e["industry"]] = {"sym": sym, "value": 0.0, "entry_i": i, "exit_i": i + HOLD}
            dirty = True
        pending = []

        if pos and policy == "inv_vol_full" and dirty:
            target, cash = _alloc_full(pos, cash, vols, t)
            for k in pos:
                pos[k]["value"] = target[k]
            dirty = False
        elif pos:
            for k in list(pos):
                if pos[k]["value"] > 0:
                    continue
                eq = cash + sum(x["value"] for x in pos.values())
                slot = eq / SLOTS
                want = min(slot, cash)
                if want < 0.5 * slot:   # 不足半格不建仓，避免 250 元的碎仓
                    pos.pop(k)
                    continue
                pos[k]["value"] = want * (1 - COST)
                cash -= want

        eq = cash + sum(p["value"] for p in pos.values())
        eq_rows.append(eq)
        exp_rows.append(sum(p["value"] for p in pos.values()) / eq if eq > 0 else 0.0)

        for e in by_date.get(t, []):
            pending.append({"industry": e["industry"], "sym": _sym_for(e, sym_mat)})

    eqs = pd.Series(eq_rows, index=sessions[start_i:start_i + len(eq_rows)])
    exs = pd.Series(exp_rows, index=eqs.index)
    out = metrics(eqs, exs)
    out.update({"policy": policy, "final_equity": round(float(eqs.iloc[-1]), 0),
                "trades": len(trades)})
    return {"metrics": out, "trades": trades, "equity": eqs, "exposure": exs}


def _sym_for(e, sym_mat) -> str | None:
    try:
        s = sym_mat.at[e["date"], e["industry"]]
    except KeyError:
        return None
    return str(s) if isinstance(s, str) else None


def main() -> None:
    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = X.scan_history(d, idx)
    status, dd, sym_mat = X.daily_matrices(m)["status"], X.daily_matrices(m)["dd"], X.daily_matrices(m)["symbol"]
    sessions = list(status.index)
    ev = X.build_events(status, dd)
    prices = X.price_frame(d, {str(s) for s in sym_mat.stack().dropna().unique()})
    vols = {s: vol20(p) for s, p in prices.items()}

    nav = etf_watch.build_context(d, idx, sessions=10 ** 6).nav
    bench = nav.mean(axis=1)

    res = {}
    for policy, cy in (("inv_vol_full", 0.0), ("six_slots", 0.0), ("six_slots_cash", 0.02)):
        r = replay(ev, sessions, prices, vols, sym_mat, policy.split("_cash")[0], cash_yield=cy)
        res[policy] = r
        print(f"\n=== {policy} ===")
        for k in ("start", "end", "years", "total", "cagr", "max_dd", "worst20",
                  "longest_underwater", "longest_invested", "longest_idle",
                  "avg_exposure", "days_invested_pct", "trades"):
            print(f"  {k:22s} {r['metrics'][k]}")
        print("  yearly", r["metrics"]["yearly"])

    start = pd.Timestamp(res["six_slots"]["metrics"]["start"])
    b = bench.loc[bench.index >= start]
    bm = metrics(b / b.iloc[0] * INIT, pd.Series(1.0, index=b.index))
    print("\n=== 基准：全行业等权买入持有 ===")
    for k in ("total", "cagr", "max_dd", "worst20", "longest_underwater"):
        print(f"  {k:22s} {bm[k]}")

    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "hold": HOLD, "cost_per_side": COST, "slots": SLOTS, "init": INIT,
            "definitions": {
                "entry": "t+1 收盘进场（信号日收盘已知）",
                "exit": "持有 20 个交易日收盘退出",
                "inv_vol_full": "持仓集合变化时按 1/vol20 归一化到 100%，重配计成本",
                "six_slots": "每笔用当时权益/6，不重配",
                "cost": "6bp/边",
            },
        },
        "policies": {k: v["metrics"] for k, v in res.items()},
        "cash_yield_assumption": 0.02,
        "benchmark": bm,
        "events": int(len(ev)),
        "trade_counts": {k: len(v["trades"]) for k, v in res.items()},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"\n写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
