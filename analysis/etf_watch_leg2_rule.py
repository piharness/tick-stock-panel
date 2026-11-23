"""EXP-LEG2-RULE-01：红利腿自己的入场/退出规则——不是买入持有。

前置结论（leg2-dividend.md）：中证红利全收益 9.26% > 现金 1.88%，但那只是
「跑赢现金的资产」。要成为策略腿，必须自己有入场/退出规则，并单独回测成本与换手。

════════════════════════════════════════════════════════════════════
预注册（写于看任何结果之前；跑完后不允许改阈值重跑）
════════════════════════════════════════════════════════════════════

R1（主规则，唯一允许进账户的规则）——MA200 趋势闸门：
    sig_t = 1 if TR_t > MA200(TR)_t else 0（t 收盘算出）
    执行：t+1 收盘调仓，t+2 起承担收益（与腿 1 完全同一时滞，pos = sig.shift(2)）
    成本：换手日扣 6bp/边

R2（稳健性，同一思想换频率）——12-1 月度动量：
    每月最后一个交易日收盘，若 TR_t / TR_{t-252} > 1 → 持有下月，否则现金
    执行：下月第一个交易日收盘调仓；该日的收益仍归旧仓位

基线：B0 买入持有（扣费）；CASH 现金
对照（不许事后挑）：
    C1 同暴露随机（block bootstrap × 1000，段长取自 R1 的真实 run-length 分布）：
       R1 的年化排在随机分位的第几 ⇒ 判断是「择时技能」还是只是「少在场」
    C2 同一规则套在沪深300（价格指数）：边际是红利特有还是普适的市场择时

成本与窗口（两段都必须报，不许只挑一段）：
    研究段 2005-01-04→2026-11-20：H00922 全收益 − 0.60%/年费率（按 510880 一代 ETF 假设）
    实施段 2019-12→2026-11：515180 逐笔分红还原净值（已含费，对官方指数 −2bp）
    现金：2012-12-27 起用 511990 真实万份收益；之前按 2%/年 假设（标注）
    调仓成本 6bp/边（与腿 1 相同口径）

账户级：腿 1（六格）优先，红利腿只用剩余现金；腿 1 进场缺钱时先减红利腿（记拥挤成本）。

统计纪律：月度收益做 t 值（月聚簇），不用日数当 N。
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
import etf_watch_portfolio as pf  # noqa: E402
import etf_watch_dividend as D  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

OUT = ROOT / "data/etf_watch/leg2_rule.json"
TR_FILE = ROOT / "data/etf_watch/_index_tr/H00922.parquet"

MA_N = 200            # R1
MOM_LB = 252          # R2
COST = pf.COST        # 6bp/边
FEE_LONG = 0.0060     # 研究段费率假设（510880 一代 ETF）
CASH_FALLBACK = 0.02  # 511990 之前的现金假设
SEED = 20261122
N_BOOT = 1000

ETF_WINDOW_START = "2019-12-02"    # 实施段（515180 上市后，与红利组合比较同窗）
LONG_WINDOW_START = "2005-01-04"   # 研究段


# ---------------------------------------------------------------- 信号
def sig_ma(tr: pd.Series, n: int = MA_N) -> pd.Series:
    """R1：TR_t > MA_n(TR)_t，t 收盘可知。"""
    ma = tr.rolling(n, min_periods=n).mean()
    return (tr > ma).astype(int)


def sig_mom_monthly(tr: pd.Series, lb: int = MOM_LB) -> pd.Series:
    """R2 的每日仓位序列（signal 月末收盘算出，下月首个交易日收盘调仓）。

    返回值直接是「当日应持仓位」：
      - 月内第 2 个交易日起 = 本月决策 d_j（在 j-1 月末算出）
      - 每月第 1 个交易日 = 上月决策 d_{j-1}（当日收盘才换仓，当日收益归旧仓）
      - 首个决策存在之前 = 0
    """
    s = tr.sort_index()
    keys = [(k, g.index[0], g.index[-1]) for k, g in s.groupby([s.index.year, s.index.month])]
    pos = pd.Series(0, index=s.index, dtype=int)

    def dec_at(month_end) -> int:
        base = s.shift(lb).loc[month_end]
        if not np.isfinite(base) or base <= 0:
            return 0
        return int(s.loc[month_end] / base > 1)

    for j in range(1, len(keys)):
        f0, l0 = keys[j][1], keys[j][2]
        d = dec_at(keys[j - 1][2])                    # 本月决策（上月末算出）
        d_prev = 0 if j < 2 else dec_at(keys[j - 2][2])
        month_idx = s.loc[f0:l0].index
        pos.loc[month_idx] = d
        pos.loc[f0] = d_prev                          # 首日收盘才调仓
    return pos


def strat_return(sig: pd.Series, asset_r: pd.Series, cash_r: pd.Series,
                 cost: float = COST) -> tuple[pd.Series, pd.Series, pd.Series]:
    """统一执行口径：pos_t = sig_{t-2}（t-2 收盘知道、t-1 收盘成交、t 当日承担收益）。

    r_t = pos_t*asset_r_t + (1-pos_t)*cash_r_t − cost*|Δpos_t|
    返回 (日收益, 仓位序列, 换手序列)。
    """
    pos = sig.shift(2).fillna(0.0)
    trade = (pos - pos.shift(1)).abs().fillna(0.0)
    r = pos * asset_r.fillna(0.0) + (1 - pos) * cash_r.reindex(asset_r.index).fillna(0.0)
    return r - cost * trade, pos, trade


# ---------------------------------------------------------------- 统计
def perf(r: pd.Series, label: str = "") -> dict:
    r = r.dropna()
    if len(r) < 60:
        return {"label": label, "note": "窗口太短"}
    eq = (1 + r).cumprod()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    mo = (1 + r).resample("ME").prod() - 1
    t = float(mo.mean() / (mo.std(ddof=1) / np.sqrt(len(mo)))) if len(mo) > 6 and mo.std(ddof=1) > 0 else None
    return {
        "label": label,
        "ann": round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4),
        "total": round(float(eq.iloc[-1] - 1), 4),
        "mdd": round(float(dd.min()), 4),
        "worst20": round(float(eq.pct_change(20).min()), 4),
        "vol": round(float(r.std() * np.sqrt(252)), 4),
        "months": int(len(mo)),
        "t_month": round(t, 2) if t is not None else None,
        "win_month": round(float((mo > 0).mean()), 3),
        "yearly": {str(k): round(float(v), 4)
                   for k, v in mo.groupby(mo.index.to_period("Y")).apply(lambda g: (1 + g).prod() - 1).items()},
    }


def run_lengths(pos: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    v = pos.to_numpy()
    runs, cur, val = [], 0, v[0]
    for x in v:
        if x == val:
            cur += 1
        else:
            runs.append((val, cur))
            val, cur = x, 1
    runs.append((val, cur))
    on = np.array([l for s, l in runs if s == 1], dtype=int)
    off = np.array([l for s, l in runs if s == 0], dtype=int)
    return on, off


def bootstrap_same_exposure(pos: pd.Series, asset_r: pd.Series, cash_r: pd.Series,
                            n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """C1：同暴露随机。生成与真实 pos 同样多 on 天的随机段序列，套同一执行/成本口径。"""
    rng = np.random.default_rng(seed)
    on_len, off_len = run_lengths(pos)
    if not len(on_len) or not len(off_len):
        return {"note": "run-length 不足"}
    n = len(pos)
    yrs = (pos.index[-1] - pos.index[0]).days / 365.25
    p_on = pos.mean()
    anns = []
    for _ in range(n_boot):
        pieces, tot = [], 0
        state = 1 if rng.random() < p_on else 0
        while tot < n:
            L = int(rng.choice(on_len if state else off_len))
            L = min(L, n - tot)
            pieces.append(np.full(L, state))
            tot += L
            state = 1 - state
        sig_r = pd.Series(np.concatenate(pieces), index=pos.index)
        r, _, _ = strat_return(sig_r, asset_r, cash_r)
        eq = (1 + r.dropna()).cumprod()
        anns.append(float(eq.iloc[-1] ** (1 / yrs) - 1))
    anns = np.array(anns)
    return {"n_boot": n_boot,
            "random_p05": round(float(np.quantile(anns, 0.05)), 4),
            "random_p50": round(float(np.median(anns)), 4),
            "random_p95": round(float(np.quantile(anns, 0.95)), 4)}


def turnover_stats(trade: pd.Series, pos: pd.Series) -> dict:
    yrs = (pos.index[-1] - pos.index[0]).days / 365.25
    on, _ = run_lengths(pos)
    return {
        "switches": int((trade > 0).sum()),
        "switches_per_year": round(float((trade > 0).sum()) / yrs, 1),
        "exposure_pct": round(float(pos.mean()) * 100, 1),
        "cost_drag_bp_per_year": round(float(trade.sum()) * COST * 1e4 / yrs, 1),
        "mean_hold_days": round(float(on.mean()), 1) if len(on) else None,
        "median_hold_days": float(np.median(on)) if len(on) else None,
    }


def cash_series(sessions, fallback: float = CASH_FALLBACK) -> pd.Series:
    """511990 真实万份收益；起点之前按 fallback/252。"""
    r = D.cash_daily(D.fetch("511990")["income"], sessions)
    first = D.fetch("511990")["income"].index[0]
    r = r.copy()
    r.loc[r.index < first] = fallback / 252
    return r


# ---------------------------------------------------------------- 账户级
def replay_joint(ev: pd.DataFrame, sessions, prices, sym_mat,
                 div_r: pd.Series, div_tgt: pd.Series, cash_r: pd.Series,
                 use_div: bool = True, start: pd.Timestamp | None = None,
                 allow_open: bool = False, init: float | None = None,
                 entry_mode: str = "close", entry_cap: float = 1.03) -> dict:
    """腿 1（六格，逐字复制 pf.replay 的 six_slots 逻辑）+ 防御腿吃剩余现金。

    div_tgt[t] = t-1 收盘算出的目标仓位（t 收盘调仓，t+1 起承担收益）。
    腿 1 优先：进场缺钱时先减防御腿（拥挤成本计入，**整格补足**）。
    use_div=False 时应逐日复现 pf.replay("six_slots")（回归测试钉住）。
    allow_open=True（运营账户用）：到期超出数据窗口的仓位照常建仓并保持开放
    （回测默认 False——到期不能测量的信号不进场）。
    init：初始资金（运营账户用；默认 pf.INIT）。
    entry_mode="close"：t+1 收盘市价（研究基线，保守）。
    entry_mode="open_limit"：t+1 开盘挂限价 = 信号收盘 ×entry_cap；开盘低于限价按开盘成交，
    盘中回落到限价按限价成交，否则**放弃该信号**。一个订单同时实现「不付尾盘溢价」与
    「不追已修复出区间的事件」；读数见 _probe_entry_timing.json。
    """
    pos: dict[str, dict] = {}
    cash = init if init is not None else pf.INIT
    div_v = 0.0
    eq_rows, exp_rows, div_exp_rows = [], [], []
    trades: list[dict] = []
    idx = {t: i for i, t in enumerate(sessions)}
    by_date: dict[pd.Timestamp, list[dict]] = {}
    for _, r in ev.iterrows():
        by_date.setdefault(r["date"], []).append(dict(r))
    start_i = min(idx[d] for d in by_date) if start is None else int(np.searchsorted(np.array(sessions), start))
    crowding = {"events": 0, "sold_value": 0.0, "extra_cost": 0.0}
    div_trades = 0

    cash_arr = cash_r.reindex(sessions).fillna(0.0).to_numpy()
    for i in range(start_i, len(sessions)):
        t = sessions[i]
        cash *= (1 + float(cash_arr[i]))
        if div_v > 0:
            dr = float(div_r.get(t, 0.0)) if np.isfinite(div_r.get(t, np.nan)) else 0.0
            div_v *= (1 + dr)
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
            cash += p["value"] * (1 - pf.COST)
            px = prices[p["sym"]]
            a = float(p.get("entry_px") or px.at[sessions[p["entry_i"]], "close_adj"]) if p["entry_i"] < i else None
            b = float(px.at[t, "close_adj"])
            trades.append({"industry": ind, "symbol": p["sym"],
                           "entry": str(sessions[p["entry_i"]].date()), "exit": str(t.date()),
                           "fill": p.get("fill_how", "close"),
                           "ret": round(b / a - 1, 4) if a and a > 0 else 0.0})
        for e in by_date.get(sessions[i - 1], []) if i > 0 else []:
            sym = _sym_for(e, sym_mat)
            if not sym or sym not in prices:
                continue
            if i + pf.HOLD >= len(sessions) and not allow_open:
                continue     # 回测口径：到期无法测量的信号不进场
            px = prices[sym]
            if t not in px.index:
                continue
            close_t = float(px.at[t, "close_adj"])
            if not np.isfinite(close_t) or close_t <= 0:
                continue
            entry_px, fill_how = close_t, "close"
            if entry_mode == "open_limit":
                sig_t = sessions[i - 1]
                if sig_t not in px.index:
                    continue
                sig_close = float(px.at[sig_t, "close_adj"])
                limit = round(sig_close * entry_cap, 3) if np.isfinite(sig_close) else np.nan
                o = float(px.at[t, "open_adj"]) if "open_adj" in px.columns and np.isfinite(px.at[t, "open_adj"]) else np.nan
                lo = float(px.at[t, "lo_adj"]) if "lo_adj" in px.columns and np.isfinite(px.at[t, "lo_adj"]) else np.nan
                if np.isfinite(o) and o <= limit:
                    entry_px, fill_how = o, "open"
                elif np.isfinite(lo) and lo <= limit:
                    entry_px, fill_how = limit, "limit"
                else:
                    continue      # 未成交：放弃该信号，不占用格子
            pos[e["industry"]] = {"sym": sym, "value": 0.0, "entry_i": i, "exit_i": i + pf.HOLD,
                                  "entry_px": entry_px, "fill_how": fill_how}

        # 六格填仓（现金不够时先减防御腿，按整格补足——半格下限会把腿 1 砍半，违背"腿 1 优先"）
        for k in list(pos):
            if pos[k]["value"] > 0:
                continue
            eq = cash + div_v + sum(x["value"] for x in pos.values())
            slot = eq / pf.SLOTS
            want = min(slot, cash)
            if want < slot and use_div and div_v > 0:
                shortfall = (slot - cash) / (1 - pf.COST)
                sell = min(max(shortfall, 0.0), div_v)
                if sell > 0:
                    div_v -= sell
                    cash += sell * (1 - pf.COST)
                    crowding["events"] += 1
                    crowding["sold_value"] += sell
                    crowding["extra_cost"] += sell * pf.COST
                    want = min(slot, cash)
            if want < 0.5 * slot:
                pos.pop(k)
                continue
            px = prices[pos[k]["sym"]]
            close_t = float(px.at[t, "close_adj"]) if t in px.index else pos[k]["entry_px"]
            ratio = close_t / pos[k]["entry_px"] if pos[k]["entry_px"] > 0 else 1.0
            pos[k]["value"] = want * (1 - pf.COST) * ratio
            cash -= want

        # 红利腿调仓（收盘，目标 = div_tgt[t]，t-1 收盘的信号）
        if use_div:
            tgt = float(div_tgt.get(t, 0.0))
            leg1_inv = sum(p["value"] for p in pos.values())
            eq = cash + div_v + leg1_inv
            desired = max(eq - leg1_inv, 0.0) * tgt
            delta = desired - div_v
            if abs(delta) > 1.0:
                div_trades += 1
                if delta > 0:
                    spend = min(delta, cash)
                    cash -= spend
                    div_v += spend * (1 - pf.COST)
                else:
                    sell = min(-delta, div_v)
                    div_v -= sell
                    cash += sell * (1 - pf.COST)

        leg1_inv = sum(p["value"] for p in pos.values())
        eq = cash + div_v + leg1_inv
        eq_rows.append(eq)
        exp_rows.append((leg1_inv + div_v) / eq if eq > 0 else 0.0)
        div_exp_rows.append(div_v / eq if eq > 0 else 0.0)

    eqs = pd.Series(eq_rows, index=sessions[start_i:start_i + len(eq_rows)])
    exs = pd.Series(exp_rows, index=eqs.index)
    dxs = pd.Series(div_exp_rows, index=eqs.index)
    out = pf.metrics(eqs, exs)
    out.update({"policy": "joint", "final_equity": round(float(eqs.iloc[-1]), 0),
                "leg1_trades": len(trades), "div_rebalances": div_trades,
                "avg_div_exposure": round(float(dxs.mean()), 3),
                "crowding": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in crowding.items()}})
    open_pos = [{"industry": k, "symbol": p["sym"], "exit_i": p["exit_i"],
                 "entry": str(sessions[p["entry_i"]].date()),
                 "fill": p.get("fill_how", "close"), "entry_px": p.get("entry_px"),
                 "exit": str(sessions[p["exit_i"]].date()) if p["exit_i"] < len(sessions) else None,
                 "value": round(float(p["value"]), 2)} for k, p in pos.items()]
    return {"metrics": out, "trades": trades, "equity": eqs, "exposure": exs, "div_exposure": dxs,
            "cash": round(float(cash), 2), "defensive_value": round(float(div_v), 2),
            "leg1_value": round(float(leg1_inv), 2), "open_positions": open_pos}


def _sym_for(e, sym_mat) -> str | None:
    try:
        s = sym_mat.at[e["date"], e["industry"]]
    except KeyError:
        return None
    return str(s) if isinstance(s, str) else None


def account_joint() -> dict:
    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = X.scan_history(d, idx)
    mats = X.daily_matrices(m)
    status, sym_mat = mats["status"], mats["symbol"]
    sessions_all = list(status.index)
    ev = X.build_events(mats["status"], mats["dd"])
    prices = X.price_frame(d, {str(s) for s in sym_mat.stack().dropna().unique()})

    start = max(pd.Timestamp(ETF_WINDOW_START), sessions_all[0])
    sessions = [t for t in sessions_all if t >= start]
    ev_w = ev[ev.date >= start].copy()

    f = D.fetch("515180")
    nav, ac = f["nav"], f["ac"]
    dv = D.dividends_from_ac(nav, ac)
    tr_level = D.tr_from_nav_div(nav, dv) if dv is not None else ac
    sess_idx = pd.DatetimeIndex(sessions)
    div_r = tr_level.reindex(sess_idx).ffill().pct_change()

    tr = pd.read_parquet(TR_FILE).set_index("date")["close"].sort_index()
    sig1 = sig_ma(tr)
    div_tgt = sig1.shift(1).fillna(0.0)            # t 收盘按 t-1 的信号调仓
    cash_r = cash_series(pd.DatetimeIndex(sessions))

    res = {}
    runs = {}
    for name, use_div, tgt in (("J0_leg1_cash", False, None),
                               ("J1_leg1_cash_div_R1", True, div_tgt),
                               ("J2_leg1_cash_div_bh", True, pd.Series(1.0, index=sess_idx))):
        r = replay_joint(ev_w, sessions, prices, sym_mat, div_r,
                         tgt if tgt is not None else div_tgt, cash_r,
                         use_div=use_div, start=start)
        runs[name] = r
        res[name] = {"metrics": r["metrics"]}

    # 互补性：腿 1 空仓日红利腿的实际贡献（逐日收益差，不是权益水平差——水平差会把累计缺口误当日收益）
    r0, r1 = runs["J0_leg1_cash"], runs["J1_leg1_cash_div_R1"]
    idle = r0["exposure"] < 1e-9
    contrib = r1["equity"].pct_change() - r0["equity"].pct_change()
    idle_c = contrib[idle]
    w20_start = r0["equity"].pct_change(20).idxmin()
    w20_end = r0["equity"].index[min(r0["equity"].index.get_loc(w20_start) + 20, len(r0["equity"]) - 1)]
    res["complementarity"] = {
        "idle_days": int(idle.sum()),
        "idle_total_contribution_pct": round(float((1 + idle_c).prod() - 1) * 100, 2),
        "idle_mean_daily_bp": round(float(idle_c.mean()) * 1e4, 2),
        "idle_worst_day_bp": round(float(idle_c.min()) * 1e4, 1),
        "leg1_worst20_window": [str(w20_start.date()), str(w20_end.date())],
        "leg1_worst20_J0": round(float(r0["equity"].pct_change(20).min()), 4),
        "leg1_worst20_J1": round(float(r1["equity"].pct_change(20).min()), 4),
        "note": "J1=腿1+红利(R1 规则)，J2=腿1+红利(买入持有)，J0=腿1+现金；三者同起点同引擎",
    }
    return res


# ---------------------------------------------------------------- 主流程
def main() -> None:
    tr = pd.read_parquet(TR_FILE).set_index("date")["close"].sort_index()
    idx_px = pd.read_parquet(core.IDX_FILE, columns=["symbol", "date", "close"])
    hs300 = idx_px[idx_px.symbol == "000300.SH"].set_index("date")["close"].sort_index()
    hs300.index = pd.to_datetime(hs300.index)

    out: dict = {"meta": {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rules": {"R1": f"TR > MA{MA_N}(TR)，t 收盘算出、t+1 收盘调仓、t+2 起承担收益",
                  "R2": f"月末 TR/TR_(-{MOM_LB}) > 1 → 持有下月，下月首日收盘调仓"},
        "cost_per_side": COST, "fee_long": FEE_LONG, "cash_fallback": CASH_FALLBACK,
        "seed": SEED, "n_boot": N_BOOT,
        "windows": {"long": LONG_WINDOW_START, "etf": ETF_WINDOW_START},
    }}

    for window, start in (("long", LONG_WINDOW_START), ("etf", ETF_WINDOW_START)):
        tr_w = tr.loc[tr.index >= pd.Timestamp(start)]
        if window == "long":
            asset_r = tr_w.pct_change().dropna() - FEE_LONG / 252
        else:
            f = D.fetch("515180")
            dv = D.dividends_from_ac(f["nav"], f["ac"])
            lvl = D.tr_from_nav_div(f["nav"], dv) if dv is not None else f["ac"]
            # 净值日期 +1 对齐后可能落在周末；先重索引到 H00922 的交易日历再算收益
            lvl = lvl.reindex(tr_w.index).ffill()
            asset_r = lvl.pct_change().dropna()
        cash_r = cash_series(asset_r.index)

        res = {"asset_bh": perf(asset_r, "红利买入持有(扣费)"),
               "cash": perf(cash_r.reindex(asset_r.index).fillna(0), "现金")}

        sig1 = sig_ma(tr.reindex(tr_w.index).ffill())
        r1, pos1, trade1 = strat_return(sig1, asset_r, cash_r)
        p1 = perf(r1, "R1 MA200 趋势闸门")
        p1["turnover"] = turnover_stats(trade1, pos1)
        p1["bootstrap"] = bootstrap_same_exposure(pos1.loc[r1.dropna().index], asset_r, cash_r)
        res["R1_ma200"] = p1

        sig2 = sig_mom_monthly(tr.reindex(tr_w.index).ffill())
        r2, pos2, trade2 = strat_return(sig2, asset_r, cash_r)
        p2 = perf(r2, "R2 12-1 月度动量")
        p2["turnover"] = turnover_stats(trade2, pos2)
        res["R2_mom12_1"] = p2

        # C2：同一规则套沪深300（价格指数，无费率假设，只比相对改善）
        hs = hs300.loc[hs300.index >= pd.Timestamp(start)]
        hs = hs.reindex(hs.index.union(tr_w.index)).sort_index().ffill()
        hs_r = hs.pct_change().dropna().loc[asset_r.index]
        sig_h = sig_ma(hs.reindex(hs_r.index).ffill())
        rh, posh, tradeh = strat_return(sig_h, hs_r, cash_r)
        bh = perf(hs_r, "HS300 买入持有")
        rl = perf(rh, "HS300 + MA200")
        res["C2_hs300"] = {"bh_ann": bh["ann"], "bh_mdd": bh["mdd"],
                           "rule_ann": rl["ann"], "rule_mdd": rl["mdd"],
                           "improve_bp": round((rl["ann"] - bh["ann"]) * 1e4),
                           "exposure_pct": turnover_stats(tradeh, posh)["exposure_pct"]}
        out[window] = res

    out["account"] = account_joint()

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}")
    for w in ("long", "etf"):
        print(f"\n=== {w} ===")
        for k, v in out[w].items():
            if k == "C2_hs300":
                print(f"  C2 hs300: bh {v['bh_ann']:+.2%}/{v['bh_mdd']:+.2%} → rule {v['rule_ann']:+.2%}/{v['rule_mdd']:+.2%}  改善 {v['improve_bp']:+.0f}bp")
                continue
            m = v
            line = f"  {k:10s} 年化 {m['ann']:+.2%}  MDD {m['mdd']:+.2%}  最差20日 {m['worst20']:+.2%}  月胜率 {m['win_month']:.0%}  t {m['t_month']}"
            if "turnover" in m:
                line += f"  暴露 {m['turnover']['exposure_pct']:.0f}%  换手 {m['turnover']['switches_per_year']}/年  成本 {m['turnover']['cost_drag_bp_per_year']:.0f}bp/年"
            if "bootstrap" in m:
                b = m["bootstrap"]
                line += f"  随机[p05 {b['random_p05']:+.2%}, p50 {b['random_p50']:+.2%}, p95 {b['random_p95']:+.2%}]"
            print(line)


if __name__ == "__main__":
    main()
