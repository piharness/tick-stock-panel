"""Phase 2 审计 · 步骤 2：统计审计。

覆盖：
  A1 三种加权口径对账（事件层 / 消融层 / 逐信号）
  A3 多重检验（搜索次数、Bonferroni、网格 max-t 现实检查）
  A4 NW 滞后 15/20/30/60
  A5 日期 / 股票 / 双向聚类标准误
  A6 区块 Bootstrap 置信区间
  A7 阈值邻域 324 格稳定性
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / ".scratch" / "p2audit"
PANEL = DATA / ".scratch" / "footprint20d" / "replay_daily.parquet"

RNG = np.random.default_rng(20261123)
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")

GRID = {
    "cum5_hi": [0.02, 0.03, 0.04, 0.05],
    "mono_lo": [0.3, 0.5, 0.7],
    "eff_lo": [0.2, 0.3, 0.4],
    "dd_lo": [-0.8, -0.6, -0.4],
    "snr_hi": [1.5, 2.0, 2.5],
}
FROZEN = {"cum5_hi": 0.03, "mono_lo": 0.5, "eff_lo": 0.3, "dd_lo": -0.6, "snr_hi": 2.0}


def nw_t(x: np.ndarray, lag: int) -> tuple[float, float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    mu = x.mean()
    e = x - mu
    v = e @ e / n
    for l in range(1, lag + 1):
        v += 2 * (1 - l / (lag + 1)) * (e[l:] @ e[:-l] / n)
    se = np.sqrt(max(v, 0) / n)
    return float(mu), float(mu / se) if se > 0 else np.nan


def cluster_t(y: np.ndarray, g: np.ndarray, extra_var: float = 0.0) -> float:
    """对 y = mu + e 的均值做单变量聚类稳健 t。"""
    y = np.asarray(y, float)
    n = len(y)
    mu = y.mean()
    e = y - mu
    gi = pd.factorize(g)[0]
    sums = np.bincount(gi, weights=e)
    v = (sums ** 2).sum() / n ** 2 + extra_var
    return float(mu / np.sqrt(v)) if v > 0 else np.nan


def two_way_t(y: np.ndarray, g1: np.ndarray, g2: np.ndarray) -> tuple[float, float, float, float]:
    y = np.asarray(y, float)
    n = len(y)
    mu = y.mean()
    e = y - mu
    v_iid = (e ** 2).sum() / n ** 2
    v1 = (np.bincount(pd.factorize(g1)[0], weights=e) ** 2).sum() / n ** 2
    v2 = (np.bincount(pd.factorize(g2)[0], weights=e) ** 2).sum() / n ** 2
    v2w = v1 + v2 - v_iid
    return (float(mu / np.sqrt(v1)), float(mu / np.sqrt(v2)),
            float(mu / np.sqrt(v2w)) if v2w > 0 else np.nan,
            float(mu / np.sqrt(v_iid)))


def block_boot(x: np.ndarray, block: int, n_boot: int = 10000) -> dict:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    nb = int(np.ceil(n / block))
    means = np.empty(n_boot)
    for b in range(n_boot):
        starts = RNG.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]) % n
        means[b] = x[idx.ravel()[:n]].mean()
    obs = x.mean()
    return {
        "mean": float(obs),
        "ci_lo": float(np.quantile(means, 0.025)),
        "ci_hi": float(np.quantile(means, 0.975)),
        "p_one_sided": float((means <= 0).mean()),
        "p_two_sided": float(2 * min((means <= 0).mean(), (means >= 0).mean())),
    }


def grid_scan() -> pd.DataFrame:
    """324 格阈值邻域：只用于稳定性评估，不改正式规则。"""
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "close", "ret", "basic_ok"])
    df["date"] = pd.to_datetime(df["date"])
    df = df[df.symbol.str.startswith(MAIN)]
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index()
         for c in ["open", "close", "ret", "basic_ok"]}
    dates = list(w["open"].index)
    op = w["open"].to_numpy(float)
    cl = w["close"].to_numpy(float)
    R = w["ret"].to_numpy(float)
    B = w["basic_ok"].to_numpy(float) == 1
    mkt = np.array([np.nanmean(R[i, B[i]]) if B[i].sum() > 50 else 0.0 for i in range(len(dates))])

    keys = list(GRID)
    cells = [(a, b, c, d, e) for a in GRID["cum5_hi"] for b in GRID["mono_lo"]
             for c in GRID["eff_lo"] for d in GRID["dd_lo"] for e in GRID["snr_hi"]]
    acc = {k: [] for k in cells}
    for d in range(5, len(dates) - 15):
        if B[d].sum() < 300:
            continue
        ex = R[d - 4:d + 1] - mkt[d - 4:d + 1, None]
        cum = np.cumsum(ex, axis=0)
        cum5 = cum[4]
        day = np.arange(1, 6, dtype=float)
        xd = day - day.mean()
        yc = cum - cum.mean(axis=0)
        mono = (xd @ yc) / np.sqrt((xd @ xd) * (yc * yc).sum(axis=0) + 1e-12)
        eff = cum5 / (np.abs(ex).sum(axis=0) + 1e-12)
        run = np.maximum.accumulate(cum, axis=0)
        dd = ((cum - run) / (np.abs(cum5) + 1e-9)).min(axis=0)
        snr = cum5 / (ex.std(axis=0) * np.sqrt(5) + 1e-9)
        fwd = cl[d + 15] / op[d + 1] - 1
        okb = B[d] & np.isfinite(fwd) & np.isfinite(cum5)
        if okb.sum() < 100:
            continue
        bench = np.nanmean(fwd[okb])
        ex_r = fwd - bench
        f0 = np.where(np.isfinite(ex_r), ex_r, 0.0)
        valid = okb & (cum5 > 0)
        for cell in cells:
            m = (valid & (cum5 <= cell[0]) & (mono >= cell[1]) & (eff >= cell[2])
                 & (dd >= cell[3]) & (snr >= 1.0) & (snr < cell[4]) & np.isfinite(ex_r))
            n = int(m.sum())
            if n:
                acc[cell].append((dates[d], float(ex_r[m].mean()), n))
    rows = []
    for cell, rec in acc.items():
        if not rec:
            continue
        x = pd.DataFrame(rec, columns=["date", "ex", "n"])
        mu, t = nw_t(x.ex.to_numpy(), 15)
        rows.append({
            "cum5_hi": cell[0], "mono_lo": cell[1], "eff_lo": cell[2],
            "dd_lo": cell[3], "snr_hi": cell[4],
            "days": len(x), "n": int(x.n.sum()), "avg_n": float(x.n.mean()),
            "gross_bp": mu * 1e4, "t_nw15": t,
            "hit": float((x.ex > 0).mean()),
            "frozen": all(abs(a - b) < 1e-12 for a, b in zip(cell, [FROZEN[k] for k in keys])),
        })
    g = pd.DataFrame(rows)
    g.to_csv(OUT / "threshold_grid.csv", index=False)
    # 逐日序列矩形（无信号日 = 空仓 0 收益，与 15 cohort 资金安排一致）
    idx = pd.Index(sorted({d for rec in acc.values() for d, _, _ in rec}), name="date")
    keys = [c for c in acc if acc[c]]
    mat = np.full((len(idx), len(keys)), np.nan)
    pos = {d: i for i, d in enumerate(idx)}
    for j, cell in enumerate(keys):
        for d, v, _ in acc[cell]:
            mat[pos[d], j] = v
    X = pd.DataFrame(mat, index=idx, columns=np.arange(len(keys)))
    X.attrs["cells"] = keys
    X.to_parquet(OUT / "threshold_grid_daily.parquet")
    return g, X


def nw_t_nan(S: np.ndarray, lag: int = 15) -> np.ndarray:
    """按列计算 NW t，允许 NaN（成对剔除）。"""
    n = np.isfinite(S).sum(axis=0)
    mu = np.nanmean(S, axis=0)
    e = S - mu
    cnt = np.isfinite(e)
    v = np.nansum(e * e, axis=0) / cnt.sum(axis=0)
    for l in range(1, lag + 1):
        prod = e[:-l] * e[l:]
        m = np.isfinite(prod)
        num = np.nansum(prod, axis=0)
        c = m.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            v = v + 2 * (1 - l / (lag + 1)) * np.where(c > 0, num / c, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return mu / np.sqrt(v / n)


def reality_check(X: pd.DataFrame, g: pd.DataFrame, block: int = 15, n_boot: int = 400) -> dict:
    """网格族的 max-t 现实检查：零假设下联合区块重采样，统计量用 NW(15)。

    重采样保留格子之间的横截面相关与日序列自相关；每格独立计算 NW t，取 max。
    """
    A = X.to_numpy(float)
    T, C = A.shape
    t_obs = nw_t_nan(A, 15)
    Ac = A - np.nanmean(A, axis=0, keepdims=True)
    nb = int(np.ceil(T / block))
    ar = np.arange(block)
    maxt = np.empty(n_boot)
    for b in range(n_boot):
        st = RNG.integers(0, T, nb)
        idx = ((st[:, None] + ar[None, :]) % T).ravel()[:T]
        t = nw_t_nan(Ac[idx], 15)
        maxt[b] = np.nanmax(np.where(np.isfinite(t), t, 0.0))
    return {
        "max_t_nw_obs": float(np.nanmax(t_obs)),
        "frozen_t_nw": float(g[g.frozen].t_nw15.iloc[0]),
        "maxt_95pct_null": float(np.quantile(maxt, 0.95)),
        "maxt_99pct_null": float(np.quantile(maxt, 0.99)),
        "fwer_p": float((maxt >= np.nanmax(t_obs)).mean()),
        "cells": int(C), "boot": int(n_boot), "block": int(block),
    }


def main() -> None:
    s = pd.read_parquet(OUT / "signals_enriched.parquet")
    s["date"] = pd.to_datetime(s["date"])
    res: dict = {}

    # ---------- A1 三种口径 ----------
    daily_all = s.groupby("date").ex_orig.mean()
    daily_n5 = s[s.groupby("date").ex_orig.transform("size") >= 5].groupby("date").ex_orig.mean()
    daily_n10 = s[s.groupby("date").ex_orig.transform("size") >= 10].groupby("date").ex_orig.mean()
    w_sig = s.ex_orig.mean()
    res["A1"] = {
        "signal_weighted_bp": float(w_sig * 1e4),
        "date_weighted_all_bp": float(daily_all.mean() * 1e4),
        "date_weighted_n5_bp": float(daily_n5.mean() * 1e4),
        "date_weighted_n10_bp": float(daily_n10.mean() * 1e4),
        "n_days_all": int(daily_all.size), "n_days_n5": int(daily_n5.size),
        "n_days_n10": int(daily_n10.size),
        "signals": int(len(s)),
        "cohort_buckets": [],
    }
    s["n_today"] = s.groupby("date").ex_orig.transform("size")
    buckets = [(1, 1), (2, 4), (5, 9), (10, 19), (20, 999)]
    for lo, hi in buckets:
        m = (s.n_today >= lo) & (s.n_today <= hi)
        if m.sum() == 0:
            continue
        res["A1"]["cohort_buckets"].append({
            "bucket": f"{lo}-{hi}" if hi < 999 else f"{lo}+",
            "days": int(s[m].date.nunique()), "signals": int(m.sum()),
            "mean_bp": float(s[m].ex_orig.mean() * 1e4),
            "day_mean_bp": float(s[m].groupby("date").ex_orig.mean().mean() * 1e4),
            "share_of_days": float(s[m].date.nunique() / s.date.nunique()),
        })
    res["A1"]["year_table"] = []
    for y, g in s.groupby("year"):
        d5 = g[g.n_today >= 5].groupby("date").ex_orig.mean()
        res["A1"]["year_table"].append({
            "year": int(y), "days": int(g.date.nunique()),
            "n_all_bp": float(g.groupby("date").ex_orig.mean().mean() * 1e4),
            "n_ge5_bp": float(d5.mean() * 1e4) if len(d5) else np.nan,
            "days_lt5": int((g.groupby("date").size() < 5).sum()),
        })

    # ---------- A4 NW ----------
    x = daily_all.to_numpy(float)
    res["A4"] = {f"lag_{l}": {"mean_bp": float(m * 1e4), "t": t}
                 for l in [0, 5, 10, 15, 20, 30, 60]
                 for m, t in [nw_t(x, l)]}
    res["A4"]["n_days"] = int(len(x))
    res["A4"]["std_bp"] = float(np.nanstd(x) * 1e4)
    res["A4"]["sharpe_ann_daily"] = float(np.nanmean(x) / np.nanstd(x) * np.sqrt(252))

    # ---------- A5 聚类 ----------
    s_ok = s[np.isfinite(s.ex_orig)].copy()
    y_all = s_ok.ex_orig.to_numpy(float)
    t_date, t_stock, t_2w, t_iid = two_way_t(y_all, s_ok.date.to_numpy(), s_ok.symbol.to_numpy())
    res["A5"] = {
        "signal_iid_t": t_iid, "signal_date_cluster_t": t_date,
        "signal_stock_cluster_t": t_stock, "signal_two_way_t": t_2w,
        "n_obs": int(len(s_ok)), "n_dates": int(s_ok.date.nunique()),
        "n_stocks": int(s_ok.symbol.nunique()),
        "avg_signals_per_stock": float(len(s_ok) / s_ok.symbol.nunique()),
        "date_weighted_iid_t": float(np.nanmean(x) / (np.nanstd(x) / np.sqrt(np.isfinite(x).sum()))),
    }
    # 逐股票聚合后的 t（等价于只按股票聚类、忽略日期相关）
    per_stock = s_ok.groupby("symbol").ex_orig.agg(["mean", "size"])
    per_stock = per_stock[per_stock["size"] >= 2]
    res["A5"]["stock_mean_t"] = float(
        per_stock["mean"].mean() / (per_stock["mean"].std() / np.sqrt(len(per_stock))))

    # ---------- A6 区块 Bootstrap ----------
    res["A6"] = {f"block_{b}": block_boot(x, b, 5000) for b in [5, 10, 15, 20, 30]}


    # ---------- A7 阈值网格 ----------
    g, X = grid_scan()
    res["A7"] = {
        "cells": int(len(g)),
        "cells_days_ge500": int((g.days >= 500).sum()),
        "frozen": g[g.frozen].to_dict("records"),
        "grid_mean_bp": float(g.gross_bp.mean()),
        "grid_median_bp": float(g.gross_bp.median()),
        "grid_p10_bp": float(g.gross_bp.quantile(0.1)),
        "grid_p90_bp": float(g.gross_bp.quantile(0.9)),
        "share_positive": float((g.gross_bp > 0).mean()),
        "share_t_gt2": float((g.t_nw15 > 2).mean()),
        "max_cell": g.loc[g.t_nw15.idxmax()].to_dict(),
        "min_cell": g.loc[g.t_nw15.idxmin()].to_dict(),
        "frozen_rank_by_t": int((g.t_nw15 > g[g.frozen].t_nw15.iloc[0]).sum() + 1),
    }
    # 单轴邻域
    res["A7"]["axis"] = {}
    for k, vals in GRID.items():
        rows = []
        for v in vals:
            sub = g[g[k] == v]
            rows.append({"value": v, "mean_bp": float(sub.gross_bp.mean()),
                         "median_bp": float(sub.gross_bp.median()),
                         "mean_t": float(sub.t_nw15.mean()),
                         "share_pos": float((sub.gross_bp > 0).mean())})
        res["A7"]["axis"][k] = rows

    # ---------- A3 多重检验 ----------
    t_obs = res["A4"]["lag_15"]["t"]
    K = 384
    p_naive = float(math.erfc(abs(t_obs) / math.sqrt(2)))
    res["A3"] = {
        "t_obs_nw15": t_obs,
        "p_naive_two_sided": p_naive,
        "K_documented": K,
        "p_bonferroni": float(min(1.0, p_naive * K)),
        "expected_max_t_under_null_indep": float(np.sqrt(2 * np.log(K))),
        "expected_max_t_under_null_grid": float(np.sqrt(2 * np.log(len(g)))),
        "grid_max_t": float(g.t_nw15.max()),
        "grid_max_t_95pct_under_null": None,
    }
    # 网格族联合 Bootstrap：对每格日序列做共同区块重采样，取 max t 的零分布
    # 用 thresholds 网格的日序列重建（从 grid_scan 的 acc 不可得，改为直接用每日重采样近似）
    # 近似：以各格日均值序列的协方差结构做参数化模拟代价过高，这里用「有效独立检验数」
    # 由网格内 t 的方差反推：K_eff ≈ (max_t / 1.645)^2 不稳健，故只报告解析上界与经验分布。
    rc = reality_check(X, g, block=15, n_boot=500)
    res["A3"]["grid_reality_check"] = rc
    cells = X.attrs.get("cells", [])
    frozen_col = [j for j, c in enumerate(cells) if all(abs(a - b) < 1e-12 for a, b in zip(
        c, [FROZEN[k] for k in GRID]))]
    if frozen_col:
        xf = X[frozen_col[0]].to_numpy(float)
        res["A3"]["frozen_cell_bootstrap"] = block_boot(xf, 15, 5000)
    res["A3"]["note"] = ("p_bonferroni 假设 384 次检验相互独立，过于保守；"
                         "grid_reality_check 只覆盖 324 格网格族，是搜索强度的下界。"
                         "真实族错误率介于两者之间。")

    # ---------- 补充：逐年 leave-one-out 与子样本 ----------
    yrs = sorted(s.year.unique())
    loo = []
    for y in yrs:
        sub = s[s.year != y]
        d = sub.groupby("date").ex_orig.mean()
        loo.append({"excluded": int(y), "days": int(len(d)),
                    "mean_bp": float(d.mean() * 1e4), "t_nw15": nw_t(d.to_numpy(), 15)[1]})
    res["A1"]["leave_one_year_out"] = loo
    subs = []
    for lo, hi in [(2018, 2021), (2022, 2026), (2019, 2022), (2023, 2026)]:
        sub = s[(s.year >= lo) & (s.year <= hi)]
        if len(sub) == 0:
            continue
        d = sub.groupby("date").ex_orig.mean()
        d5 = sub[sub.n_today >= 5].groupby("date").ex_orig.mean()
        subs.append({"window": f"{lo}-{hi}", "days": int(len(d)),
                     "mean_bp": float(d.mean() * 1e4), "t_nw15": nw_t(d.to_numpy(), 15)[1],
                     "mean_bp_n5": float(d5.mean() * 1e4) if len(d5) else np.nan,
                     "t_nw15_n5": nw_t(d5.to_numpy(), 15)[1] if len(d5) else np.nan})
    res["A1"]["subperiods"] = subs

    json.dump(res, open(OUT / "stats.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
