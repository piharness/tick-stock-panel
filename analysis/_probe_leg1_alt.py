"""ONE-OFF：腿 1 的风险收益结构 + 同窗口替代方案对照（不改规则、不产出结论表）。

问题：腿 1 不执行时 60 万该放哪；执行时腿 1 的风险与收益是否匹配。
方法：复用 account-runbook 的同一引擎 replay_joint，只改「闲置资金放什么」和「单笔仓位」，
      同一窗口、同一成本、同一执行时滞，逐日重放。

运行：python analysis/_probe_leg1_alt.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402
import etf_watch_account as ACC  # noqa: E402
import etf_watch_dividend as D  # noqa: E402
import etf_watch_leg2_rule as LR  # noqa: E402
import etf_watch_portfolio as pf  # noqa: E402
import etf_watch_leg2_bond as LB  # noqa: E402

START = pd.Timestamp("2019-12-02")
OUT = ROOT / "data/etf_watch/_probe_leg1_alt.json"


def short_bond_total() -> pd.Series:
    """511360 短融ETF 全收益：AC 即累计净值（恒等式不成立，用 AC 口径，需在结论里标注）。"""
    f = D.fetch("511360")
    ac = f["ac"]
    return ac / ac.iloc[0]


def run(world, *, defensive: str, slots: int, start=START, no_leg1: bool = False) -> dict:
    d, mats, sessions_all, ev, _ = world
    sessions = [t for t in sessions_all if t >= start]
    sym_mat = mats["symbol"]
    ev_w = ev[ev["date"] >= start].copy()
    if no_leg1:
        ev_w = ev_w.iloc[0:0]
    sess_idx = pd.DatetimeIndex(sessions)

    cal = pd.read_parquet(LR.TR_FILE).set_index("date")["close"].sort_index().index
    if defensive == "bond":
        lvl, _ = LB.bond_total_return()
    elif defensive == "short":
        lvl = short_bond_total()
    else:
        lvl = pd.Series(dtype=float)
    if len(lvl):
        dr = lvl.reindex(cal).ffill().reindex(sess_idx).ffill().pct_change()
        tgt = pd.Series(1.0, index=sess_idx)
        tgt.iloc[0] = 0.0
        use_div = True
    else:
        dr, tgt, use_div = pd.Series(dtype=float), pd.Series(dtype=float), False

    cash_r = LR.cash_series(sess_idx)
    prices = ACC.X.price_frame(d, {str(s) for s in sym_mat.stack().dropna().unique()}
                               | {"511010.SH", "511360.SH"})
    old = pf.SLOTS
    pf.SLOTS = slots
    try:
        r = LR.replay_joint(ev_w, sessions, prices, sym_mat, div_r=dr, div_tgt=tgt,
                            cash_r=cash_r, use_div=use_div, start=start,
                            allow_open=False, init=600_000.0)
    finally:
        pf.SLOTS = old
    flat = dict(r["metrics"])
    flat["_trades"] = r.get("trades", [])
    flat["_prices"] = prices
    flat["_sessions"] = sessions
    flat["_equity"] = r.get("equity")
    flat["leg1_trades"] = len(r.get("trades", []))
    return flat


def trade_stats(trades: list[dict], prices, sessions) -> dict:
    if not trades:
        return {}
    idx = {t: i for i, t in enumerate(sessions)}
    rows = []
    for tr in trades:
        px = prices.get(tr["symbol"])
        ret = tr["ret"]
        mae = np.nan
        if px is not None:
            a, b = pd.Timestamp(tr["entry"]), pd.Timestamp(tr["exit"])
            seg = px.loc[(px.index >= a) & (px.index <= b), "close_adj"]
            if len(seg) > 1:
                mae = float(seg.min() / seg.iloc[0] - 1)
        rows.append({"industry": tr["industry"], "entry": tr["entry"], "exit": tr["exit"], "ret": ret, "mae": mae})
    df = pd.DataFrame(rows)
    r = df["ret"]
    top = r.nlargest(5).sum()
    return {
        "n": int(len(df)),
        "mean": round(float(r.mean()), 4), "median": round(float(r.median()), 4),
        "win": round(float((r > 0).mean()), 3),
        "p10": round(float(r.quantile(0.10)), 4), "p90": round(float(r.quantile(0.90)), 4),
        "worst": round(float(r.min()), 4), "best": round(float(r.max()), 4),
        "sum": round(float(r.sum()), 4), "top5_share": round(float(top / r.sum()), 3),
        "mae_mean": round(float(df["mae"].mean()), 4), "mae_p10": round(float(df["mae"].quantile(0.1)), 4),
        "mae_worst": round(float(df["mae"].min()), 4),
        "yr": {str(k): int(v) for k, v in df.groupby(df["entry"].str[:4]).size().items()},
        "detail": df.round(4).to_dict("records"),
    }


def main() -> None:
    world = ACC.load_world()
    out: dict = {"window_start": str(START.date())}

    specs = [
        ("A_cash", dict(defensive="cash", slots=6)),
        ("B_bond", dict(defensive="bond", slots=6)),
        ("C_bond_half_leg1", dict(defensive="bond", slots=12)),
        ("D_cash_half_leg1", dict(defensive="cash", slots=12)),
        ("E_bond_only", dict(defensive="bond", slots=6, no_leg1=True)),
        ("F_short_only", dict(defensive="short", slots=6, no_leg1=True)),
        ("G_short_leg1", dict(defensive="short", slots=6)),
    ]
    for name, kw in specs:
        s = START
        if "short" in name:
            s = pd.Timestamp("2020-09-28")     # 511360 上市后
        r = run(world, start=s, **kw)
        if name == "E_bond_only":
            pass
        key = {"cagr": "cagr", "max_dd": "max_dd", "worst20": "worst20",
               "longest_underwater": "longest_underwater", "avg_exposure": "avg_exposure",
               "days_invested_pct": "days_invested_pct", "final_equity": "final_equity"}
        rec = {v: r.get(k) for k, v in key.items()}
        rec["yearly"] = r.get("yearly")
        rec["leg1_trades"] = r.get("leg1_trades") or len(r["_trades"])
        rec["start"] = str(s.date())
        rec["ratio"] = round(abs(rec["cagr"] / rec["max_dd"]), 2) if rec.get("max_dd") else None
        if name in ("B_bond", "C_bond_half_leg1", "A_cash"):
            rec["trades"] = trade_stats(r["_trades"], r["_prices"], r["_sessions"])
        out[name] = rec
        print(f"{name:18s} start {rec['start']}  n={int(rec["leg1_trades"]):>2}  "
              f"CAGR {rec['cagr']:+.2%}  MDD {rec['max_dd']:+.2%}  "
              f"worst20 {rec['worst20']:+.2%}  水下 {rec['longest_underwater']}d  "
              f"仓位 {rec['avg_exposure']:.1%}  ratio {rec['ratio']}")

    t = out["B_bond"]["trades"]
    print("\n腿1 逐笔（JB0 窗口）：")
    for k in ("n", "mean", "median", "win", "p10", "p90", "worst", "best", "sum",
              "top5_share", "mae_mean", "mae_p10", "mae_worst", "yr"):
        print(f"  {k:12s} {t[k]}")
    t2 = out["C_bond_half_leg1"]["trades"]
    print(f"\n半仓腿1 逐笔：n={t2['n']} mean {t2['mean']:+.2%} win {t2['win']:.0%} "
          f"mae_mean {t2['mae_mean']:+.2%} worst {t2['worst']:+.2%}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
