"""Phase 2 审计 · 步骤 3：真实交易仿真 + 组合审计 + 容量审计。

组合口径：15 个滚动 cohort，每 cohort 占 1/15 资金，cohort 内等权，d+1 开盘买入，持有 15 个交易日。
基准口径：同一 15 cohort 结构、同一持有期、同一成交假设，但 cohort = 当日全部 basic_ok。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / ".scratch" / "p2audit"
PANEL = DATA / ".scratch" / "footprint20d" / "replay_daily.parquet"
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
COST = {"base": 0.0015, "mid": 0.003, "high": 0.005, "stress": 0.010}


def load_matrices():
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "close", "ret", "basic_ok"])
    df["date"] = pd.to_datetime(df["date"])
    df = df[df.symbol.str.startswith(MAIN)]
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index()
         for c in ["open", "close", "ret", "basic_ok"]}
    return w


def tranche_portfolio(sig: pd.DataFrame, W: dict, cost: float, use_exec: bool = True,
                      benchmark: bool = False) -> dict:
    """15 个滚动 cohort 的组合。benchmark=True 时 cohort=全部 basic_ok。"""
    dates = list(W["open"].index)
    di = {d: i for i, d in enumerate(dates)}
    op = W["open"].to_numpy(float)
    cl = W["close"].to_numpy(float)
    R = W["ret"].to_numpy(float)
    B = W["basic_ok"].to_numpy(float) == 1
    syms = list(W["open"].columns)
    sidx = {s: i for i, s in enumerate(syms)}

    n = len(dates)
    port = np.zeros(n)
    slot_active = np.zeros(n)
    npos = np.zeros(n)
    invested = np.zeros(n)
    entry_notional = np.zeros(n)
    ind_w: dict[str, np.ndarray] = {}
    mcap_daily = np.full(n, np.nan)
    logmc = sig.set_index("symbol")["log_mcap"].groupby(level=0).first()

    if benchmark:
        groups = {d: g for d, g in sig.groupby("date")}
        cohorts = []
        for d, _ in groups.items():
            j = di.get(d)
            if j is None:
                continue
            cols = np.flatnonzero(B[j])
            if len(cols) == 0:
                continue
            cohorts.append((j, cols))
    else:
        cohorts = []
        for d, g in sig.groupby("date"):
            j = di.get(d)
            if j is None:
                continue
            if use_exec:
                g = g[g.entry_filled & ~g.entry_at_limit_up]
            cols = [sidx[s] for s in g.symbol if s in sidx]
            if not cols:
                continue
            cohorts.append((j, np.array(cols)))

    for j, cols in cohorts:
        if j + 15 >= n:
            continue
        # 首日：开盘买入
        r0 = np.nanmean(cl[j + 1, cols] / op[j + 1, cols] - 1)
        if not np.isfinite(r0):
            continue
        port[j + 1] += r0 / 15
        for k in range(1, 15):
            rr = np.nanmean(R[j + 1 + k, cols])
            if np.isfinite(rr):
                port[j + 1 + k] += rr / 15
        for k in range(15):
            port[j + 1 + k] -= cost / 2 / 15 if k == 0 else 0.0
            if k == 14:
                port[j + 1 + k] -= cost / 2 / 15
        slot_active[j + 1:j + 16] += 1
        # 持仓统计
        for k in range(15):
            t = j + 1 + k
            npos[t] += len(cols)
            invested[t] += 1.0
        entry_notional[j + 1] += 1.0 / 15
        # 行业暴露（按 cohort 权重 1/15 摊到 15 天）
        if not benchmark:
            for _, row in sig[sig.date == dates[j]].iterrows():
                if use_exec and (not row.entry_filled or row.entry_at_limit_up):
                    continue
                ind = row.industry if isinstance(row.industry, str) else "未知"
                ind_w.setdefault(ind, np.zeros(n))[j + 1:j + 16] += 1.0 / 15 / max(len(cols), 1)

    port_s = pd.Series(port, index=dates)
    nav = (1 + port_s).cumprod()
    return {"ret": port_s, "nav": nav, "slot": pd.Series(slot_active / 15, index=dates),
            "npos": pd.Series(npos, index=dates), "ind_w": ind_w,
            "entry_notional": pd.Series(entry_notional, index=dates), "cohorts": cohorts}


def perf(ret: pd.Series, bench: pd.Series | None = None, rf: float = 0.0) -> dict:
    ret = ret.dropna()
    n = len(ret)
    years = n / 252
    nav = (1 + ret).cumprod()
    cagr = nav.iloc[-1] ** (1 / years) - 1
    vol = ret.std() * np.sqrt(252)
    sharpe = (ret.mean() - rf / 252) / ret.std() * np.sqrt(252)
    dd = nav / nav.cummax() - 1
    mdd = dd.min()
    # 回撤持续：从峰值到收复
    peak = nav.cummax()
    under = nav < peak * (1 - 1e-9)
    longest, cur = 0, 0
    for u in under:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    out = {"days": n, "years": years, "cagr": float(cagr), "vol": float(vol),
           "sharpe": float(sharpe), "max_dd": float(mdd),
           "dd_duration_days": int(longest), "total": float(nav.iloc[-1] - 1)}
    if bench is not None:
        ex = ret - bench.reindex(ret.index).fillna(0)
        out["ann_excess_arith"] = float(ex.mean() * 252)
        out["sharpe_excess"] = float(ex.mean() / ex.std() * np.sqrt(252))
        out["hit_days"] = float((ex > 0).mean())
        bn = (1 + bench.reindex(ret.index).fillna(0)).cumprod()
        out["bench_cagr"] = float(bn.iloc[-1] ** (1 / years) - 1)
        # beta
        b = bench.reindex(ret.index).fillna(0)
        cov = np.cov(ret, b)
        out["beta"] = float(cov[0, 1] / cov[1, 1])
        out["corr"] = float(np.corrcoef(ret, b)[0, 1])
    return out


def monthly(ret: pd.Series) -> pd.DataFrame:
    m = (1 + ret).resample("ME").prod() - 1
    return pd.DataFrame({"month": m.index.strftime("%Y-%m"), "ret": m.to_numpy()})


def main() -> None:
    s = pd.read_parquet(OUT / "signals_enriched.parquet")
    s["date"] = pd.to_datetime(s["date"])
    res: dict = {}

    # ================= B 成交仿真 =================
    b = {}
    b["signals"] = int(len(s))
    b["entry_filled"] = int(s.entry_filled.sum())
    b["entry_unfilled"] = int((~s.entry_filled).sum())
    b["entry_delayed"] = int((s.entry_di > s.entry_di_planned).sum())
    b["entry_at_limit_up"] = int(s.entry_at_limit_up.sum())
    b["entry_oneword_up"] = int(s.entry_oneword_up.sum())
    b["fill_rate_strict"] = float((s.entry_filled & ~s.entry_at_limit_up).mean())
    b["exit_delayed"] = int((s.exit_delay_days > 0).sum())
    b["exit_missing"] = int((~s.exit_ok).sum())
    b["gap_mean_bp"] = float(s.entry_gap.mean() * 1e4)
    b["gap_median_bp"] = float(s.entry_gap.median() * 1e4)
    b["gap_p10_bp"] = float(s.entry_gap.quantile(0.10) * 1e4)
    b["gap_p90_bp"] = float(s.entry_gap.quantile(0.90) * 1e4)
    b["gap_pos_share"] = float((s.entry_gap > 0).mean())
    # 口径对比
    variants = {}
    for name, m in [("all", np.ones(len(s), bool)),
                    ("filled", s.entry_filled.to_numpy()),
                    ("strict", (s.entry_filled & ~s.entry_at_limit_up).to_numpy())]:
        x = s[m]
        variants[name] = {
            "n": int(m.sum()),
            "gross_bp_orig": float(x.ret_orig.mean() * 1e4),
            "gross_bp_exec": float(x.ret_exec.mean() * 1e4),
            "day_weighted_bp": float(x.groupby("date").ret_exec.mean().mean() * 1e4),
            "hit": float((x.ret_exec > 0).mean()),
        }
    b["variants"] = variants
    b["delta_exec_minus_orig_bp"] = float((s.ret_exec - s.ret_orig).mean() * 1e4)
    b["cost_curve"] = {}
    x0 = s[s.entry_filled & ~s.entry_at_limit_up]
    dA = float(x0.ret_exec.mean())
    dE = float(x0.groupby("date").ret_exec.mean().mean())
    xb = x0.groupby("date").ex_exec.mean().mean()
    b["levels"] = {"abs_signal_weighted_bp": dA * 1e4, "abs_day_weighted_bp": dE * 1e4,
                   "excess_signal_weighted_bp": float(x0.ex_exec.mean() * 1e4),
                   "excess_day_weighted_bp": float(xb * 1e4),
                   "bench_on_signal_days_bp": float((dE - xb) * 1e4)}
    for k, c in COST.items():
        b["cost_curve"][k] = {
            "cost_pct": c,
            "net_abs_bp_day_weighted": (dE - c) * 1e4,
            "net_excess_bp_day_weighted": (xb - c) * 1e4,
            "net_abs_ann_day_weighted": (dE - c) * 252 / 15,
            "net_excess_ann": (xb - c) * 252 / 15,
        }
    # 冲击成本（平方根律）：一买一卖
    vol = s.vol20.clip(0.005, 0.10)
    part_grid = [0.005, 0.01, 0.02, 0.05, 0.10]
    b["impact"] = {}
    for y in [0.5, 1.0]:
        b["impact"][f"Y_{y}"] = {
            f"{int(p*1000)/10}%": {
                "one_way_bp": float(np.nanmedian(y * vol * np.sqrt(p) * 1e4)),
                "round_trip_bp": float(2 * np.nanmedian(y * vol * np.sqrt(p) * 1e4)),
            } for p in part_grid}
    res["B"] = b

    # ================= C 组合审计 =================
    W = load_matrices()
    bench_sig = s[["date", "symbol"]].copy()
    bench_sig["log_mcap"] = s["log_mcap"]
    bench_sig["industry"] = s["industry"]
    bench_sig = bench_sig.drop_duplicates(["date", "symbol"])
    # 基准 cohort 需要全部 basic_ok：直接构造
    dates = list(W["open"].index)
    B = W["basic_ok"].to_numpy(float) == 1
    R = W["ret"].to_numpy(float)
    op = W["open"].to_numpy(float)
    cl = W["close"].to_numpy(float)
    bench_ret = np.zeros(len(dates))
    for j in range(len(dates) - 15):
        cols = np.flatnonzero(B[j])
        if len(cols) < 50:
            continue
        r0 = np.nanmean(cl[j + 1, cols] / op[j + 1, cols] - 1)
        bench_ret[j + 1] += r0 / 15
        for k in range(1, 15):
            rr = np.nanmean(R[j + 1 + k, cols])
            if np.isfinite(rr):
                bench_ret[j + 1 + k] += rr / 15
    bench = pd.Series(bench_ret, index=dates)

    port = tranche_portfolio(s, W, cost=COST["base"], use_exec=True)
    c = {"strategy": perf(port["ret"], bench), "benchmark": perf(bench)}
    c["strategy"]["cagr"] = c["strategy"].pop("cagr")
    c["benchmark"].pop("bench_cagr", None)
    c["excess_total"] = float((port["nav"].iloc[-1]) / ((1 + bench).cumprod().iloc[-1]) - 1)
    _b = bench.reindex(port["ret"].index).fillna(0)
    _beta = float(np.cov(port["ret"], _b)[0, 1] / np.var(_b, ddof=1))
    c["capm_alpha_ann"] = float((port["ret"].mean() - _beta * _b.mean()) * 252)
    c["ir"] = float((port["ret"] - _b).mean() / (port["ret"] - _b).std() * np.sqrt(252))
    c["capital_utilization"] = float(port["slot"].mean())
    c["capital_utilization_last3y"] = float(port["slot"].iloc[-750:].mean())
    c["concurrent_positions"] = {
        "mean": float(port["npos"].mean()), "median": float(port["npos"].median()),
        "p90": float(port["npos"].quantile(0.9)), "max": float(port["npos"].max()),
    }
    c["cohorts"] = int(len(port["cohorts"]))
    # 换手
    per_year = port["entry_notional"].groupby(port["entry_notional"].index.year).sum()
    navy = port["nav"].groupby(port["nav"].index.year).mean()
    c["turnover_oneway"] = {int(k): float(per_year[k] / navy[k]) for k in per_year.index}
    c["turnover_mean"] = float(np.mean(list(c["turnover_oneway"].values())))
    c["turnover_two_way_mean"] = 2 * c["turnover_mean"]
    m = monthly(port["ret"])
    m.to_csv(OUT / "portfolio_monthly.csv", index=False)
    c["monthly_mean"] = float(m.ret.mean())
    c["monthly_std"] = float(m.ret.std())
    c["monthly_positive"] = float((m.ret > 0).mean())
    c["monthly_best"] = float(m.ret.max())
    c["monthly_worst"] = float(m.ret.min())
    streak, cur = 0, 0
    for v in m.ret:
        cur = cur + 1 if v < 0 else 0
        streak = max(streak, cur)
    c["longest_losing_months"] = int(streak)
    # 行业暴露
    ind = port["ind_w"]
    tot = sum(v.mean() for v in ind.values()) if ind else 0
    c["industry"] = sorted(
        [{"industry": k, "weight": float(v.mean() / tot)} for k, v in ind.items()],
        key=lambda x: -x["weight"])[:12] if tot else []
    c["industry_hhi"] = float(sum((v.mean() / tot) ** 2 for v in ind.values())) if tot else np.nan
    c["industry_n"] = int(len(ind))
    # 市值暴露
    c["log_mcap"] = {"mean": float(s.log_mcap.mean()), "median": float(s.log_mcap.median()),
                     "p10": float(s.log_mcap.quantile(0.1)),
                     "p90": float(s.log_mcap.quantile(0.9))}
    # 成本敏感性
    c["cost_sensitivity"] = {}
    for k, cc in COST.items():
        p = tranche_portfolio(s, W, cost=cc, use_exec=True)
        c["cost_sensitivity"][k] = perf(p["ret"], bench)
    # 基准 vs 策略逐年
    yr = []
    for y in sorted(set(port["ret"].index.year)):
        a = port["ret"][port["ret"].index.year == y]
        bb = bench[bench.index.year == y]
        if len(a) < 20:
            continue
        yr.append({"year": int(y),
                   "strategy": float((1 + a).prod() - 1),
                   "benchmark": float((1 + bb).prod() - 1),
                   "excess": float((1 + a).prod() - (1 + bb).prod())})
    c["yearly"] = yr
    # NAV 与回撤定位
    navd = pd.DataFrame({"strategy": port["nav"], "benchmark": (1 + bench).cumprod()})
    navd.to_csv(OUT / "portfolio_nav.csv")
    _dd = navd.strategy / navd.strategy.cummax() - 1
    _tr = _dd.idxmin()
    _pk = navd.strategy.loc[:_tr].idxmax()
    _rec = navd.strategy.loc[_tr:][navd.strategy.loc[_tr:] >= navd.strategy.loc[_pk]]
    c["dd_detail"] = {"peak": str(_pk.date()), "trough": str(_tr.date()),
                      "recover": str(_rec.index[0].date()) if len(_rec) else None,
                      "calendar_days_underwater": int((navd.index > _pk).sum()),
                      "still_underwater": bool(len(_rec) == 0)}
    c["n_months"] = int(len(m))
    res["C"] = c

    # ================= D 容量审计 =================
    d = {}
    q = [0.10, 0.25, 0.50, 0.75, 0.90]
    for col in ["amount_d", "amount_entry", "adv20_entry"]:
        x = s[col].dropna() / 1e8
        d[col] = {f"p{int(p*100)}": float(x.quantile(p)) for p in q}
        d[col]["mean"] = float(x.mean())
        d[col]["max"] = float(x.max())
    # cohort 级：每 cohort 可容纳资金（等权，受最差一只约束）
    rows = []
    for dt, g in s[s.entry_filled & ~s.entry_at_limit_up].groupby("date"):
        adv = g.adv20_entry.dropna()
        if len(adv) < 3:
            continue
        rows.append({"date": dt, "n": len(adv), "adv_med": adv.median(),
                     "adv_p10": adv.quantile(0.1), "adv_min": adv.min(),
                     "adv_sum": adv.sum()})
    coh = pd.DataFrame(rows)
    coh.to_csv(OUT / "cohort_liquidity.csv", index=False)
    d["cohorts"] = len(coh)
    d["cohort_n"] = {"mean": float(coh.n.mean()), "p50": float(coh.n.median()),
                     "p90": float(coh.n.quantile(0.9)), "max": int(coh.n.max())}
    caps = {}
    for p in [0.01, 0.02, 0.05, 0.10]:
        caps[f"p{int(p*100)}"] = {
            "equal_weight_median_adv_yi": float(15 * p * coh.adv_med.median() * coh.n.median() / 1e8),
            "equal_weight_p10_adv_yi": float(15 * p * coh.adv_p10.median() * coh.n.median() / 1e8),
            "equal_weight_worst_cohort_yi": float(15 * p * coh.adv_p10.min() * coh.n.min() / 1e8),
            "liquidity_weighted_yi": float(15 * p * coh.adv_sum.median() / 1e8),
        }
    d["capacity"] = caps
    # 每日资金需求（相对 AUM 的占比）
    d["daily_demand"] = {
        "new_cohorts_days": int((coh.n > 0).sum()),
        "avg_names_per_day": float(coh.n.mean()),
        "p95_names_per_day": float(coh.n.quantile(0.95)),
        "daily_outlay_at": {f"{a}_yi": a / 15 for a in [1, 3, 5, 10, 30]},
    }
    d["cohort_capacity_yi_at_5pct"] = {
        "p10": float(0.05 * (coh.n * coh.adv_p10).quantile(0.10) / 1e8),
        "p50": float(0.05 * (coh.n * coh.adv_p10).quantile(0.50) / 1e8),
        "p90": float(0.05 * (coh.n * coh.adv_p10).quantile(0.90) / 1e8),
        "min": float(0.05 * (coh.n * coh.adv_p10).min() / 1e8),
        "worst_date": str(coh.loc[(coh.n * coh.adv_p10).idxmin(), "date"].date()),
    }
    # 冲击后净收益随 AUM 变化
    volm = s.vol20.clip(0.005, 0.10)
    edge = float((s[s.entry_filled & ~s.entry_at_limit_up]
                  .groupby("date").ret_exec.mean().mean() - COST["base"]))
    aum_tab = []
    for aum in [1, 2, 3, 5, 10, 20, 30]:
        aum_yuan = aum * 1e8
        pos = aum_yuan / (15 * coh.n.median())
        part = float((pos / coh.adv_med.median()))
        imp = float(2 * 1.0 * np.nanmedian(volm) * np.sqrt(min(part, 1.0)))
        aum_tab.append({"aum_yi": aum, "position_yi": pos / 1e8,
                        "participation_median": part,
                        "impact_round_trip_bp": imp * 1e4,
                        "net_bp": (edge - imp) * 1e4,
                        "ann_net": (edge - imp) * 252 / 15})
    d["aum_scenarios"] = aum_tab
    res["D"] = d

    json.dump(res, open(OUT / "exec_portfolio_capacity.json", "w"),
              ensure_ascii=False, indent=1, default=float)
    for k in ["B", "C", "D"]:
        print("=" * 20, k)
        print(json.dumps(res[k], ensure_ascii=False, indent=1, default=float)[:3000])


if __name__ == "__main__":
    main()
