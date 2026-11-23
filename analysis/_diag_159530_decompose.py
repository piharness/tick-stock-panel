"""159530 下跌归因：股票下跌、ETF 赎回，还是重新估值？

问题（用户 2026-11-22）：
  1. 159530 下跌是成分股下跌还是 ETF 赎回造成的？
  2. 是错杀还是重新估值？
  3. 如果是重新估值，是否存在让 F1/F2/F3/F6 彻底失效的情形？

口径与纪律：
  - 价格：本仓库 kline_etf_daily（含 11/20 增量分区），close_adj 已摊平份额折算。
  - 净值：天天基金 pingzhongdata（159530），东财净值日期 +1 天对齐交易日（同 leg2 脚本）。
  - 指数：国证机器人产业 980022.SZ（本地 index_daily_full + 增量分区）。
  - 份额：季度「规模 / 期末净值」反推；当日份额取东财 f84（与净值同日）。
  - 估值：当前 PE-TTM 取腾讯行情（字段 39）；2026-07-01 / 07-31 的 PE 为外部估值表快照，
    仅用于「价格 vs 倍数」的两点分解，不参与任何规则判定。
  - 事件层：复用 etf_watch 的判定与 build_events，不复制 F1/F2/F3/F6。

用法：
    python analysis/_diag_159530_decompose.py
    python analysis/_diag_159530_decompose.py --json   # 输出机器可读
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch as ew  # noqa: E402
import etf_watch_dividend as dv  # noqa: E402
import etf_watch_experiments as ex  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402

SYMBOL = "159530.SZ"
INDEX = "980022.SZ"
PE_TENCENT_URL = "https://qt.gtimg.cn/q=sz980022"
# 外部估值快照（2026-07-01）：PE-TTM 66.74；07-31: 54.53。
# 来源：公开估值表（sz980022 日度 PE/PB），本仓库无历史 PE 序列，故只作两点分解。
PE_0701 = 66.74
PE_0731 = 54.53


def _index_series() -> pd.Series:
    idx = pd.read_parquet(core.IDX_FILE, columns=["symbol", "date", "close"])
    idx["date"] = pd.to_datetime(idx["date"])
    parts = [(d, p) for d, p in ew._partition_days(ew.IDX_PART) if d > idx["date"].max() and p.exists()]
    for _, p in parts:
        y = pd.read_parquet(p)
        y["date"] = pd.to_datetime(y["date"])
        idx = pd.concat([idx, y[["symbol", "date", "close"]]], ignore_index=True)
    s = idx[idx.symbol == INDEX].drop_duplicates("date", keep="last").set_index("date").sort_index()["close"]
    return s


def _current_pe() -> float | None:
    try:
        txt = urllib.request.urlopen(PE_TENCENT_URL, timeout=15).read().decode("utf-8", "ignore")
        body = txt.split('="', 1)[1].rstrip('";\n')
        f = body.split("~")
        return float(f[39])
    except Exception:
        return None


def _current_aum() -> float | None:
    """当日基金规模（亿元）：腾讯行情字段 44（东财 push2 偶尔断连，腾讯稳定）。"""
    try:
        txt = urllib.request.urlopen("https://qt.gtimg.cn/q=sz159530", timeout=15).read().decode("utf-8", "ignore")
        f = txt.split('="', 1)[1].rstrip('";\n').split("~")
        v = float(f[44])
        return v if v > 0 else None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # ---------------- 价格 / 净值 / 指数 ----------------
    raw, _ = ew.fresh_etf()
    d, _ = core.load(etf_raw=raw)
    x = d[d.symbol == SYMBOL].set_index("date").sort_index()
    idx = _index_series()
    nav = dv.fetch("159530")["nav"]
    if nav is None:
        raise SystemExit("拿不到 159530 净值序列")

    cal = pd.DataFrame({"px": x["close"], "nav": nav, "idx": idx}).dropna(subset=["px", "nav"])
    cal["idx"] = cal["idx"].ffill()
    peak = x["close_adj"].idxmax()
    win = cal[cal.index >= peak]
    nav_win = nav[nav.index >= peak]
    idx_win = idx[idx.index >= peak]

    def ret(s: pd.Series) -> float:
        return float(s.iloc[-1] / s.iloc[0] - 1)

    prem = win["px"] / win["nav"] - 1
    r = win[["nav", "idx"]].pct_change().dropna()
    track_diff = float((1 + r["nav"]).prod() / (1 + r["idx"]).prod() - 1)
    te = float(r["nav"].sub(r["idx"]).std() * np.sqrt(252))

    # ---------------- 份额 / 资金流 ----------------
    # 季度规模（亿元）来自 pingzhongdata Data_fluctuationScale；份额 = 规模 / 期末净值。
    q_scale = {"2026-06-30": 159.99, "2026-09-30": 154.47}
    q_rows = []
    for day, scale in q_scale.items():
        n = float(nav[nav.index <= day].iloc[-1])
        q_rows.append({"date": day, "scale_yi": scale, "nav": n, "shares_yi": round(scale / n, 2)})
    aum_now = _current_aum()
    if aum_now:
        q_rows.append({"date": str(x.index[-1].date()), "scale_yi": round(aum_now, 2),
                       "nav": float(nav.iloc[-1]), "shares_yi": round(aum_now / float(nav.iloc[-1]), 2)})

    # 规模变动分解（6/30 → 11/20）：ΔAUM = ΔNAV×S0 + ΔS×NAV1（近似）
    s0, s1 = q_rows[0]["shares_yi"], q_rows[-1]["shares_yi"]
    n0, n1 = q_rows[0]["nav"], q_rows[-1]["nav"]
    aum0, aum1 = q_rows[0]["scale_yi"], q_rows[-1]["scale_yi"]
    perf_part = (n1 - n0) * s0
    flow_part = (s1 - s0) * n1
    flow_part = aum1 - aum0 - perf_part  # 残差对齐，避免四舍五入漂移

    # ---------------- 估值 ----------------
    pe_now = _current_pe()
    px_0701 = float(idx.asof(pd.Timestamp("2026-07-01")))
    px_now = float(idx.iloc[-1])
    implied_e = (px_now / px_0701) / (pe_now / PE_0701) if pe_now else None

    # ---------------- 事件层：机械行业是否首次 ----------------
    m = pd.read_parquet(ex.CACHE)
    m["date"] = pd.to_datetime(m["date"])
    status = m.pivot(index="date", columns="industry", values="status").sort_index()
    ddp = m.pivot(index="date", columns="industry", values="dd").sort_index()
    sym = m.pivot(index="date", columns="industry", values="best_symbol").sort_index()
    st = m.pivot(index="date", columns="industry", values="state").sort_index()
    ev = ex.build_events(status, ddp)
    mech = m[m.industry == "机械"]
    # ---------------- 连跌诊断（用户 2026-11-22 追问） ----------------
    # 事件当日的载体连跌天数与 7 日累计跌幅；只做描述，不改判定。
    prices = ex.price_frame(d, set(m.best_symbol.dropna().astype(str)))
    sessions = list(status.index)
    rows = []
    for _, r in ev.iterrows():
        sym_ = sym.at[r.date, r.industry] if r.date in sym.index else None
        if not isinstance(sym_, str) or sym_ not in prices:
            continue
        px = prices[sym_]
        i = int(r["i"])
        if i + 21 >= len(sessions):
            continue
        entry, exit_ = sessions[i + 1], sessions[i + 21]
        try:
            ret20_ = px.at[exit_, "close_adj"] / px.at[entry, "close_adj"] - 1
        except KeyError:
            continue
        rr = px["close_adj"].pct_change()
        st_ = (rr < 0).astype(int).groupby((rr >= 0).cumsum()).cumsum()
        base = px["close_adj"].asof(sessions[max(0, i - 7)])
        r7 = px.at[r.date, "close_adj"] / base - 1
        rows.append({"date": r.date, "industry": r.industry, "state": st.at[r.date, r.industry],
                     "streak": int(st_.get(r.date, 0)), "r7": float(r7), "ret20": float(ret20_)})
    eq = pd.DataFrame(rows)
    xr = x["close_adj"].pct_change()
    xst = (xr < 0).astype(int).groupby((xr >= 0).cumsum()).cumsum()
    streak_now = int(xst.iloc[-1])
    r7_now = float(x["close_adj"].iloc[-1] / x["close_adj"].iloc[-8] - 1)
    if len(eq):
        q = pd.qcut(eq.r7, 4, labels=["Q1 worst", "Q2", "Q3", "Q4 best"])
        g = eq.groupby(q, observed=True).ret20.agg(["count", "mean", "median"])

    out = {
        "asof": str(x.index[-1].date()),
        "peak": str(peak.date()),
        "ret_price": ret(x["close_adj"][x.index >= peak]),
        "ret_nav": ret(nav_win),
        "ret_index": ret(idx_win),
        "premium_mean": float(prem.mean()),
        "premium_min": float(prem.min()),
        "premium_max": float(prem.max()),
        "premium_last": float(prem.iloc[-1]),
        "tracking_diff": track_diff,
        "tracking_te": te,
        "shares": q_rows,
        "aum_perf_part": float(perf_part),
        "aum_flow_part": float(flow_part),
        "pe_now": pe_now,
        "pe_0701": PE_0701,
        "pe_0731": PE_0731,
        "implied_e_change": float(implied_e - 1) if implied_e else None,
        "events_total": int(len(ev)),
        "events_mech": int((ev.industry == "机械").sum()),
        "mech_inband_days": int((mech.dd <= -0.35).sum()),
        "mech_dd_min_before": float(mech[mech.date < pd.Timestamp("2026-01-01")].dd.min()),
        "streak_now": streak_now,
        "r7_now": r7_now,
        "event_by_state": {str(k): {"n": int(v["count"]), "mean": float(v["mean"])}
                           for k, v in eq.groupby("state").ret20.agg(["count", "mean"]).iterrows()} if len(eq) else None,
        "event_streak_max": int(eq.streak.max()) if len(eq) else None,
        "event_n_streak7": int((eq.streak >= 7).sum()) if len(eq) else None,
        "r7_quartiles": {str(k): {"n": int(v["count"]), "mean": float(v["mean"])}
                         for k, v in g.iterrows()} if len(eq) else None,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    print(f"== 159530 下跌归因（asof {out['asof']}，峰值 {out['peak']}）==")
    print(f"价格 {out['ret_price']:+.2%} | 净值 {out['ret_nav']:+.2%} | 指数 {out['ret_index']:+.2%}")
    print(f"溢价：均值 {out['premium_mean']:+.3%}，区间 [{out['premium_min']:+.3%}, {out['premium_max']:+.3%}]，末日 {out['premium_last']:+.3%}")
    print(f"跟踪差 {out['tracking_diff']:+.2%}，年化 TE {out['tracking_te']:.2%}")
    print("\n份额（季度规模/净值 反推）：")
    for row in q_rows:
        print(f"  {row['date']}  规模 {row['scale_yi']:.2f}亿  净值 {row['nav']:.4f}  份额 {row['shares_yi']:.2f}亿份")
    print(f"规模变动分解：净值 {out['aum_perf_part']:+.1f}亿 + 份额 {out['aum_flow_part']:+.1f}亿")
    print(f"\n估值：PE {PE_0701:.2f}(07-01) → {pe_now}(11-20)；价格比 {px_now/px_0701:.3f}，隐含盈利变化 {out['implied_e_change']:+.1%}")
    print(f"\n机械行业：历史在区间天数 {out['mech_inband_days']}，2026 前最深 {out['mech_dd_min_before']:.1%}，"
          f"事件数 {out['events_mech']}/{out['events_total']}")
    print(f"\n连跌：本次 {streak_now} 连跌、7 日 {r7_now:+.1%}；"
          f"34 个事件里最长 {out['event_streak_max']} 连跌，7 连跌 {out['event_n_streak7']} 次")
    if out["event_by_state"]:
        print("  事件按市场状态 → 20 日均值：")
        for k, v in out["event_by_state"].items():
            print(f"    {k:6s} n={v['n']:2d}  {v['mean']:+.2%}")
    if out["r7_quartiles"]:
        print("  事件当日 7 日跌幅分组 → 20 日均值：")
        for k, v in out["r7_quartiles"].items():
            print(f"    {k:9s} n={v['n']:2d}  {v['mean']:+.2%}")


if __name__ == "__main__":
    main()
