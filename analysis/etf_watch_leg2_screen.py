"""第二条腿的准入筛查：先看它能不能填第一条腿的空仓，而不是先看它自己多能赚。

四条准入（用户 2026-11-22 定的）：
1. 信号独立——不同触发机制与收益来源；
2. 时间互补——在本策略空仓的 675 个交易日里有机会；
3. 尾部互补——本策略最差 20 日/最大回撤时它不一起亏；
4. 独立证明——自己的回测、成本、退出、账户级检验。

本脚本只做 1–3 的**描述性筛查**，不做 4，也不给入场规则：
候选资产一律按买入持有处理，看的是「它在第一条腿空仓时赚不赚钱、亏的时候是否同步」。

口径与偏差（必须一起读）：
- 第一条腿＝`six_slots`，含 2%/年现金收益（`portfolio.json` 的 `six_slots_cash`）；
- 指数/ETF 用**价格收益**：不含股息与票息。红利、国债被系统性低估，
  故另给一列「价格＋假设分红」（假设值写死在这里，不是数据）；
- 候选只做买入持有，不设计趋势/择时——那属于准入第 4 条，不在本轮。

用法：
    python analysis/etf_watch_leg2_screen.py
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
import etf_watch_experiments as X  # noqa: E402
import etf_watch_portfolio as pf  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

OUT = ROOT / "data/etf_watch/leg2_screen.json"
CASH = 0.02          # 与 portfolio.json 的 six_slots_cash 一致

# 假设分红/票息（年化）。不是数据，是为了不让「价格收益」把红利与国债判死。
CANDIDATES = [
    {"key": "hs300", "name": "沪深300", "symbol": "000300.SH", "src": "index", "yield": 0.020},
    {"key": "zz500", "name": "中证500", "symbol": "000905.SH", "src": "index", "yield": 0.015},
    {"key": "div", "name": "中证红利", "symbol": "000922.SH", "src": "index", "yield": 0.040},
    {"key": "gold", "name": "黄金ETF", "symbol": "518880.SH", "src": "etf", "yield": 0.000},
    {"key": "bond", "name": "国债ETF", "symbol": "511010.SH", "src": "etf", "yield": 0.030},
]


def load_series(src: str, symbol: str) -> pd.Series:
    """价格收益序列（未计分红/票息）。"""
    if src == "index":
        d = pd.read_parquet(core.IDX_FILE, columns=["symbol", "date", "close"])
        x = d[d.symbol == symbol].copy()
        x["date"] = pd.to_datetime(x["date"])
        s = x.set_index("date")["close"].sort_index()
        return s.pct_change()
    d = pd.read_parquet(core.ETF_FILE, columns=["symbol", "date", "close"])
    x = d[d.symbol == symbol].copy()
    if x.empty:
        raise SystemExit(f"{symbol} 不在 ETF 日线里")
    x["date"] = pd.to_datetime(x["date"])
    x = x.sort_values("date")
    px = core._dejump(x["close"].reset_index(drop=True))
    s = pd.Series(px.to_numpy(), index=pd.DatetimeIndex(x["date"]))
    return s.pct_change()


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 2:
        return {}
    eq = (1 + r).cumprod()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    return {
        "ann": round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4) if yrs > 0 else None,
        "mdd": round(float(dd.min()), 4),
        "worst20": round(float(eq.pct_change(20).min()), 4),
        "vol": round(float(r.std() * np.sqrt(252)), 4),
    }


def main() -> None:
    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = X.scan_history(d, idx)
    status, dd, sym_mat = X.daily_matrices(m)["status"], X.daily_matrices(m)["dd"], X.daily_matrices(m)["symbol"]
    sessions = list(status.index)
    ev = X.build_events(status, dd)
    prices = X.price_frame(d, {str(s) for s in sym_mat.stack().dropna().unique()})
    vols = {s: pf.vol20(p) for s, p in prices.items()}

    leg1 = pf.replay(ev, sessions, prices, vols, sym_mat, "six_slots", cash_yield=CASH)
    eq, expo = leg1["equity"], leg1["exposure"]
    r1 = eq.pct_change().dropna()
    idle = expo.reindex(r1.index) == 0
    active = ~idle
    print(f"腿1 {eq.index[0].date()}→{eq.index[-1].date()}  年化 "
          f"{(eq.iloc[-1] / eq.iloc[0]) ** (365.25 / (eq.index[-1] - eq.index[0]).days) - 1:.2%}  "
          f"空仓日 {int(idle.sum())}/{len(idle)}")

    # 最长空仓段
    best, cur, start = (0, None), 0, None
    for t, i in idle.items():
        if i:
            cur += 1
            start = start or t
            if cur > best[0]:
                best = (cur, (start, t))
        else:
            cur, start = 0, None
    gap_days, (gap_a, gap_b) = best
    print(f"最长空仓 {gap_days} 日（{gap_a.date()} → {gap_b.date()}）")

    # 腿1 最差 20 日窗口 / 最大回撤窗口
    w20 = eq.pct_change(20)
    t20 = w20.idxmin()
    i20 = eq.index.get_loc(t20)
    win20 = (eq.index[max(0, i20 - 20)], t20)
    peak = eq.cummax()
    trough_t = (eq / peak - 1).idxmin()
    peak_t = eq.loc[:trough_t].idxmax()

    rows = []
    for c in CANDIDATES:
        r = load_series(c["src"], c["symbol"]).reindex(r1.index)
        carry = c["yield"] / 252
        total = r + carry
        idle_ann = float((1 + total[idle].dropna()).prod() ** (252 / max(idle.sum(), 1)) - 1)
        active_ann = float((1 + total[active].dropna()).prod() ** (252 / max(active.sum(), 1)) - 1)
        corr = float(r1[active].corr(r[active]))
        # 腿1 最差 10% 的交易日里，候选的平均日收益
        thr = r1.quantile(0.10)
        down_avg = float(total[(r1 <= thr)].mean())
        rows.append({
            "key": c["key"], "name": c["name"], "symbol": c["symbol"],
            "price_ann": stats(r)["ann"], "total_ann": round(stats(total)["ann"], 4),
            "idle_ann": round(idle_ann, 4), "active_ann": round(active_ann, 4),
            "corr_active": round(corr, 3),
            "gap_ret": round(float((1 + total.loc[gap_a:gap_b]).prod() - 1), 4),
            "worst20_ret": round(float((1 + total.loc[win20[0]:win20[1]]).prod() - 1), 4),
            "mdd_window_ret": round(float((1 + total.loc[peak_t:trough_t]).prod() - 1), 4),
            "down_day_avg_bp": round(down_avg * 1e4, 1),
            "vol": stats(total)["vol"], "mdd": stats(total)["mdd"],
            # 50/50 示意（每日再平衡，未计额外成本）
            "combo": stats(0.5 * r1 + 0.5 * total.reindex(r1.index)),
        })
        print(f"\n{c['name']:8s} 年化 {rows[-1]['total_ann']:+.2%}（价格 {rows[-1]['price_ann']:+.2%}）"
              f"  空仓日年化 {rows[-1]['idle_ann']:+.2%}  持仓日 {rows[-1]['active_ann']:+.2%}"
              f"  相关 {rows[-1]['corr_active']:+.2f}")
        print(f"  空仓段 {rows[-1]['gap_ret']:+.2%}  腿1最差20日 {rows[-1]['worst20_ret']:+.2%}"
              f"  腿1回撤期 {rows[-1]['mdd_window_ret']:+.2%}  腿1最差10%日均 {rows[-1]['down_day_avg_bp']:+.1f}bp")
        print(f"  50/50 示意：年化 {rows[-1]['combo']['ann']:+.2%}  MDD {rows[-1]['combo']['mdd']:+.2%}"
              f"  最差20日 {rows[-1]['combo']['worst20']:+.2%}")

    leg1_stats = stats(r1)
    cash = pd.Series(CASH / 252, index=r1.index)
    print(f"\n腿1 单独：年化 {leg1_stats['ann']:+.2%}  MDD {leg1_stats['mdd']:+.2%}  最差20日 {leg1_stats['worst20']:+.2%}")
    print(f"纯现金 2%/年：年化 +2.00%")

    out = {
        "meta": {
            "leg1": "six_slots + 2%/年现金，2018-06-25→2026-11-20",
            "cash_yield": CASH,
            "screen_only": "候选一律买入持有，未设计任何入场规则；准入第 4 条未做",
            "bias": "指数/ETF 价格收益不含股息票息；yield 列是假设值，不是数据",
        },
        "leg1_stats": leg1_stats,
        "leg1_idle": {"days": int(idle.sum()), "sessions": int(len(idle)),
                      "longest_gap_days": int(gap_days),
                      "gap": [str(gap_a.date()), str(gap_b.date())]},
        "worst_windows": {"worst20": [str(win20[0].date()), str(win20[1].date())],
                          "mdd": [str(peak_t.date()), str(trough_t.date())]},
        "candidates": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"\n写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
