"""EXP-LEG2-BOND-01：国债防御腿的最小规则研究——闲置资金去 511010 还是留在货币基金。

前置结论（leg2-dividend.md）：511010 国债ETF国泰 真实全收益 3.08%（2019-12→2026-11，
AC 口径；本轮用逐笔还原重算），与腿 1 条件相关 −0.10，腿 1 两个压力窗口均为正。
它是筛查中唯一改善风险调整的资产（50/50 静态：0.93→1.47）。但静态 50/50 把腿 1 也砍半了；
正确的结构是**防御叠加**：腿 1 满配不变，债券只接手闲置现金。

════════════════════════════════════════════════════════════════════
预注册（写于看任何结果之前；跑完后不允许改阈值重跑）
════════════════════════════════════════════════════════════════════

资产：511010 逐笔分红还原净值（D_t = Δ(AC−NAV/NAV₀)，恒等式先验证：单调不减、跳变 ≤40、
     无负跳；已验证 7 笔分红、2025 年起改季度分红）。净值已含费，**无需费率假设**。
执行：与红利轮完全同一套（复用 etf_watch_leg2_rule 已测函数）：
     R1 = NAV > MA200(NAV)，t 收盘算出、t+1 收盘调仓、t+2 起承担收益，换手 6bp/边；
     R2 = 月末 NAV/NAV₍₋₂₅₂₎ > 1 → 持有下月。
基线：B0 债券买入持有；CASH 现金（511990 真实万份收益，2012-12 起全覆盖）。
对照：C1 同暴露随机 ×1000（段长取自 R1 真实 run-length）；
     C2 同一规则套沪深300（价格指数）——边际是债券特有还是普适择时。
窗口：研究段 2013-03→2026-11（511010 上市起）；实施段 2019-12→2026-11（= 账户窗口）。

账户级（复用 replay_joint，腿 1 六格优先、债券腿吃剩余现金、缺钱先减债券）：
     J0  = 腿 1 + 现金（现行基准）
     JB0 = 腿 1 + 债券买入持有
     JB1 = 腿 1 + 债券 R1
     JB2 = 腿 1 + 债券 R2

**进账户门槛（预注册，机械判定）**：某 JBx 同时满足
  (a) CAGR/|MDD| > J0 的 0.93；
  (b) 最差 20 日 ≥ −6.8%（J0 −5.78% + 1pct 缓冲）；
才准入，否则维持现金。不许事后挑最好的那个 JBx 放宽另一条的门槛。
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
import etf_watch_leg2_rule as LR  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

OUT = ROOT / "data/etf_watch/leg2_bond.json"
BOND_WINDOW_START = "2013-03-05"    # 研究段（511010 上市）
ETF_WINDOW_START = LR.ETF_WINDOW_START  # 实施段 = 账户窗口（2019-12-02）

J0_RATIO = 0.93          # 预注册门槛 (a)
J0_WORST20_FLOOR = -0.068  # 预注册门槛 (b)


def bond_total_return() -> tuple[pd.Series, dict]:
    """511010 逐笔分红还原净值。恒等式不成立时抛错（不许静默退回 AC 口径）。"""
    f = D.fetch("511010")
    nav, ac = f["nav"], f["ac"]
    nav_s = nav / nav.iloc[0]                     # 缩放到 AC 量纲
    dv = D.dividends_from_ac(nav_s, ac)
    if dv is None:
        raise SystemExit("511010 恒等式不成立，逐笔还原不可用——终止，不许退回 AC 口径")
    tr = D.tr_from_nav_div(nav_s, dv)
    meta = {"n_div": int((dv > 0).sum()), "div_per_unit": round(float(dv.sum()), 4),
            "window": [str(nav.index[0].date()), str(nav.index[-1].date())],
            "method": "D_t = Δ(AC−NAV/NAV₀)；r_t = (NAV_t−NAV_{t−1}+D_t)/NAV_{t−1}"}
    return tr, meta


def main() -> None:
    tr_level, bond_meta = bond_total_return()
    # 净值日期 +1 对齐后有 8 个周末日期：重索引到 H00922 的干净交易日历
    cal = pd.read_parquet(LR.TR_FILE).set_index("date")["close"].sort_index().index
    lvl = tr_level.reindex(cal).ffill().dropna()

    out: dict = {"meta": {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bond": bond_meta,
        "rules": {"R1": "NAV > MA200(NAV)，t 收盘算出、t+1 收盘调仓、t+2 起承担收益",
                  "R2": "月末 NAV/NAV_(-252) > 1 → 持有下月，下月首日收盘调仓"},
        "cost_per_side": LR.COST, "fee_assumption": "无——511010 净值已含费",
        "gate": {"a": f"CAGR/|MDD| > {J0_RATIO}", "b": f"最差20日 ≥ {J0_WORST20_FLOOR}"},
    }}
    hs300_raw = pd.read_parquet(core.IDX_FILE, columns=["symbol", "date", "close"])
    hs300 = hs300_raw[hs300_raw.symbol == "000300.SH"].set_index("date")["close"].sort_index()
    hs300.index = pd.to_datetime(hs300.index)

    for window, start in (("research", BOND_WINDOW_START), ("etf", ETF_WINDOW_START)):
        lvl_w = lvl.loc[lvl.index >= pd.Timestamp(start)]
        asset_r = lvl_w.pct_change().dropna()
        cash_r = LR.cash_series(asset_r.index)

        res = {"bond_bh": LR.perf(asset_r, "债券买入持有"),
               "cash": LR.perf(cash_r.reindex(asset_r.index).fillna(0), "现金")}

        sig1 = LR.sig_ma(lvl.reindex(lvl_w.index).ffill())
        r1, pos1, trade1 = LR.strat_return(sig1, asset_r, cash_r)
        p1 = LR.perf(r1, "R1 MA200 闸门")
        p1["turnover"] = LR.turnover_stats(trade1, pos1)
        p1["bootstrap"] = LR.bootstrap_same_exposure(pos1.loc[r1.dropna().index], asset_r, cash_r)
        res["R1_ma200"] = p1

        sig2 = LR.sig_mom_monthly(lvl.reindex(lvl_w.index).ffill())
        r2, pos2, trade2 = LR.strat_return(sig2, asset_r, cash_r)
        p2 = LR.perf(r2, "R2 月动量")
        p2["turnover"] = LR.turnover_stats(trade2, pos2)
        res["R2_mom12_1"] = p2

        # C2：同一规则套沪深300（价格指数）
        hs = hs300.reindex(asset_r.index).ffill()
        hs_r = hs.pct_change()
        sig_h = LR.sig_ma(hs)
        rh, posh, tradeh = LR.strat_return(sig_h, hs_r, cash_r)
        bh = LR.perf(hs_r, "HS300 买入持有")
        rl = LR.perf(rh, "HS300 + MA200")
        res["C2_hs300"] = {"bh_ann": bh["ann"], "bh_mdd": bh["mdd"],
                           "rule_ann": rl["ann"], "rule_mdd": rl["mdd"],
                           "improve_bp": round((rl["ann"] - bh["ann"]) * 1e4),
                           "exposure_pct": LR.turnover_stats(tradeh, posh)["exposure_pct"]}
        out[window] = res

    out["account"] = account_bond()

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}")
    for w in ("research", "etf"):
        print(f"\n=== {w} ===")
        for k, m in out[w].items():
            if k == "C2_hs300":
                print(f"  C2 hs300: bh {m['bh_ann']:+.2%}/{m['bh_mdd']:+.2%} → rule {m['rule_ann']:+.2%}/{m['rule_mdd']:+.2%}  改善 {m['improve_bp']:+.0f}bp")
                continue
            line = f"  {k:9s} 年化 {m['ann']:+.2%}  MDD {m['mdd']:+.2%}  最差20日 {m['worst20']:+.2%}  月胜率 {m['win_month']:.0%}  t {m['t_month']}"
            if "turnover" in m:
                line += f"  暴露 {m['turnover']['exposure_pct']:.0f}%  换手 {m['turnover']['switches_per_year']}/年"
            if "bootstrap" in m:
                b = m["bootstrap"]
                line += f"  随机[p05 {b['random_p05']:+.2%}, p50 {b['random_p50']:+.2%}, p95 {b['random_p95']:+.2%}]"
            print(line)


def account_bond() -> dict:
    """J0/JB0/JB1/JB2 同引擎同起点；债券腿吃剩余现金，腿 1 缺钱先减债券。"""
    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = X.scan_history(d, idx)
    mats = X.daily_matrices(m)
    sessions_all = list(mats["status"].index)
    ev = X.build_events(mats["status"], mats["dd"])
    prices = X.price_frame(d, {str(s) for s in mats["symbol"].stack().dropna().unique()})

    start = max(pd.Timestamp(ETF_WINDOW_START), sessions_all[0])
    sessions = [t for t in sessions_all if t >= start]
    ev_w = ev[ev.date >= start].copy()
    sess_idx = pd.DatetimeIndex(sessions)

    tr_level, _ = bond_total_return()
    cal = pd.read_parquet(LR.TR_FILE).set_index("date")["close"].sort_index().index
    bond_r = tr_level.reindex(cal).ffill().reindex(sess_idx).ffill().pct_change()
    sig1 = LR.sig_ma(tr_level.reindex(cal).ffill())
    bond_tgt_r1 = sig1.shift(1).fillna(0.0).reindex(sess_idx).fillna(0.0)
    cash_r = LR.cash_series(sess_idx)

    res, runs = {}, {}
    for name, use_div, tgt in (("J0_leg1_cash", False, None),
                               ("JB0_leg1_bond_bh", True, pd.Series(1.0, index=sess_idx)),
                               ("JB1_leg1_bond_R1", True, bond_tgt_r1)):
        r = LR.replay_joint(ev_w, sessions, prices, mats["symbol"],
                            div_r=bond_r, div_tgt=tgt, cash_r=cash_r,
                            use_div=use_div, start=start)
        runs[name] = r
        res[name] = {"metrics": r["metrics"]}

    # 预注册门槛的机械判定
    j0 = res["J0_leg1_cash"]["metrics"]
    for name in ("JB0_leg1_bond_bh", "JB1_leg1_bond_R1"):
        mm = res[name]["metrics"]
        ratio = mm["cagr"] / abs(mm["max_dd"])
        res[name]["gate"] = {"ratio": round(ratio, 3), "pass_a": bool(ratio > J0_RATIO),
                             "pass_b": bool(mm["worst20"] >= J0_WORST20_FLOOR),
                             "admit": bool(ratio > J0_RATIO and mm["worst20"] >= J0_WORST20_FLOOR)}

    # 互补性：腿 1 空仓日债券腿的贡献（逐日收益差）
    r0, rb = runs["J0_leg1_cash"], runs["JB0_leg1_bond_bh"]
    idle = r0["exposure"] < 1e-9
    contrib = rb["equity"].pct_change() - r0["equity"].pct_change()
    idle_c = contrib[idle]
    res["complementarity"] = {
        "idle_days": int(idle.sum()),
        "idle_total_contribution_pct": round(float((1 + idle_c).prod() - 1) * 100, 2),
        "idle_mean_daily_bp": round(float(idle_c.mean()) * 1e4, 2),
        "idle_worst_day_bp": round(float(idle_c.min()) * 1e4, 1),
        "note": "JB0=腿1+债券(B&H)；腿1 最差20日窗口对照见 J0/JB0 的 worst20",
    }
    return res


if __name__ == "__main__":
    main()
