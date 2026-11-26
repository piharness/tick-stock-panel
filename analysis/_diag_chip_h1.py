"""H1：久期错配 → 脆弱性（不是收益）。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
对象
  月度快照 2019-01 ~ 2026-11。月末收盘后建仓，次日开盘入场，20 个交易日后收盘出场。
  D = age_mean + old_share * 500：500 日以上老钱按 500 日下限补，是截断久期的保守估计。
  T = 过去 500 个交易日、周频、市场调整后收益的 AR(1) 半衰期，单位交易日。
      T 高 = 价格偏离衰减慢（市场重估这只股票需要更久）；T 低 = 快速均值回归。
  M = T / D：M 高 = 短钱持有慢故事（错配）；M 低 = 长钱持有快故事（反错配）。

主判据
  M 与 fragility 复合的月度 Spearman 均值 |ρ| ≥ 0.15，NW(6) |t| ≥ 2，
  且 2023-2026 分段同号。fragility = 月度截面 z(-mdd20)、z(-skew20)、z(-worst5)、z(+dvol20) 的均值。

次判据（风险 vs 收益）
  M 对 fwd20 不显著为负。正 = 风险溢价（与脆弱性一致）；零 = 纯风险指标；
  负 = 脆弱股同时跑输（也是空头信号，不算 H1 通过）。

负控
  月内打乱 M 200 次，看真实 ρ 在安慰剂分布中的百分位。

杀死条件
  主判据不过；或 2×2 的「错配格（D 低 × T 高）− 反错配格（D 高 × T 低）」< 0；
  或 corr(T, D) > 0.8（M 只是 D 的另一种写法）。

口径边界
  - 年龄面板是当前在市股票回溯，含生存偏差；2019 年的绝对水平不代表当年全市场。
  - T 是市场隐含的重估速度，不是基本面半衰期；基本面 T 需要分析师/盈利修正数据，当前没有。
  - top_float 不参与 H1；H1 只用日线 + 年龄面板。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h1"
OUT.mkdir(parents=True, exist_ok=True)
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"
SRC = ROOT / "data" / "kline_daily_enriched"

HORIZON = 20
RECOVERY = 60
T_WINDOW = 500
NW_LAG = 6
N_PLACEBO = 200


def load_daily(start: str = "2017-01-01"):
    dataset = ds.dataset(SRC, format="parquet")
    table = dataset.to_table(
        columns=["symbol", "date", "open", "close", "amount", "turnover_rate"],
        filter=ds.field("date") >= pd.Timestamp(start).date(),
    )
    df = table.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[df["close"] > 0]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.99)
    # 矩阵：date x symbol
    op = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="to", aggfunc="last").sort_index()
    cl = cl.reindex(op.index)
    am = am.reindex(op.index)
    to = to.reindex(op.index)
    return op, cl, am, to


def nw_t(x: np.ndarray, lag: int = NW_LAG) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return np.nan
    e = x - x.mean()
    s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def skewness(a: np.ndarray, axis: int = 0) -> np.ndarray:
    mu = np.nanmean(a, axis=axis, keepdims=True)
    d = a - mu
    m2 = np.nanmean(d ** 2, axis=axis)
    m3 = np.nanmean(d ** 3, axis=axis)
    return m3 / np.maximum(m2, 1e-18) ** 1.5


def spearman(x, y) -> float:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 30:
        return np.nan
    xr = pd.Series(x[mask]).rank().to_numpy()
    yr = pd.Series(y[mask]).rank().to_numpy()
    return float(np.corrcoef(xr, yr)[0, 1])


def rolling_t(cl: np.ndarray, targets: list[int], window: int = T_WINDOW, ma_window: int = 120) -> pd.DataFrame:
    """T = 价格相对 120 日均线的缺口、日频 AR(1) 的半衰期（交易日）。

    与「周收益自相关」不同：这里估的是**缺口**的回复速度。b<0 才有回复；
    b>=0（趋势/随机游走）截断在 250 日。T 高 = 市场重估慢；T 低 = 快速均值回归。
    """
    cl = pd.DataFrame(cl).ffill().to_numpy(dtype=np.float64)
    rows = {}
    for ti in targets:
        if ti < window:
            continue
        win = cl[ti - window:ti]
        cs = np.cumsum(win, axis=0)
        ma = np.full_like(win, np.nan)
        ma[ma_window - 1:] = (cs[ma_window - 1:] - np.vstack([np.zeros((1, win.shape[1])), cs[:-ma_window]])) / ma_window
        with np.errstate(divide="ignore", invalid="ignore"):
            x = np.log(win / ma)
        x = x[ma_window - 1:]
        dx = np.diff(x, axis=0)
        xlag = x[:-1]
        mask = np.isfinite(dx) & np.isfinite(xlag)
        n = mask.sum(axis=0)
        xm = np.nansum(np.where(mask, xlag, 0.0), axis=0) / np.maximum(n, 1)
        ym = np.nansum(np.where(mask, dx, 0.0), axis=0) / np.maximum(n, 1)
        cov = np.nansum(np.where(mask, (xlag - xm) * (dx - ym), 0.0), axis=0) / np.maximum(n, 1)
        var = np.nansum(np.where(mask, (xlag - xm) ** 2, 0.0), axis=0) / np.maximum(n, 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            b = cov / var
            t = np.where(b < -0.001, -np.log(2.0) / np.log1p(np.clip(b, -0.99, -0.001)), 250.0)
        t = np.clip(t, 1.0, 250.0)
        t[~np.isfinite(t) | (n < 100)] = np.nan
        rows[ti] = t
    return pd.DataFrame(rows).T


def future_metrics(op: np.ndarray, cl: np.ndarray, dates: pd.DatetimeIndex, syms: np.ndarray, targets: list[int]) -> pd.DataFrame:
    rows = []
    for ti in targets:
        if ti + HORIZON + 2 > len(cl):
            continue
        p0 = op[ti + 1]
        p = cl[ti + 1:ti + HORIZON + 2]  # 21 个收盘：与旧页 close[t+21]/open[t+1] 对齐
        if p.shape[0] < HORIZON + 1:
            continue
        r1 = p[0] / p0 - 1.0
        r = np.vstack([r1, p[1:] / p[:-1] - 1.0])
        r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
        fwd = p[-1] / p0 - 1.0
        path = np.vstack([p0, p])
        peak = np.maximum.accumulate(path, axis=0)
        dd = path / peak - 1.0
        mdd = dd.min(axis=0)
        skew = skewness(r, axis=0)
        # 5 日最差
        sw = np.lib.stride_tricks.sliding_window_view(r, 5, axis=0)
        w5 = np.prod(1.0 + sw, axis=-1) - 1.0
        worst5 = w5.min(axis=0)
        dvol = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2, axis=0))
        # 恢复：先有 5% 回撤，再看 60 日内是否回到冲击前收盘
        cl_t = cl[ti]
        horizon = cl[ti + 1:ti + RECOVERY + 1]
        hit = horizon >= cl_t[None, :]
        any_hit = hit.any(axis=0)
        first = np.where(any_hit, hit.argmax(axis=0) + 1, RECOVERY)
        recovered = (first < RECOVERY) & (mdd < -0.05)
        rows.append(pd.DataFrame({
            "date": dates[ti],
            "symbol": syms,
            "fwd20": fwd,
            "mdd20": mdd,
            "skew20": skew,
            "worst5": worst5,
            "dvol20": dvol,
            "recovery_days": np.where(mdd < -0.05, first, np.nan),
            "recovered": np.where(mdd < -0.05, recovered.astype(float), np.nan),
        }))
    return pd.concat(rows, ignore_index=True)


def zscore(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def main() -> None:
    print("[1/6] load daily ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    op = op_df.to_numpy(dtype=np.float32)
    cl = cl_df.to_numpy(dtype=np.float32)
    am = am_df.to_numpy(dtype=np.float32)
    to = to_df.to_numpy(dtype=np.float32)
    print(f"      matrix={op.shape[0]} x {op.shape[1]}", flush=True)

    print("[2/6] market-adjusted returns + rolling T ...", flush=True)
    r = np.vstack([np.zeros((1, cl.shape[1])), cl[1:] / cl[:-1] - 1.0])
    r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
    r = np.clip(r, -0.2, 0.2)
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"])
    targets = [int(dates.get_loc(d)) for d in sorted(age["date"].unique()) if d in dates]
    T = rolling_t(cl, targets)
    # long format
    T_long = T.stack().rename("T").reset_index()
    T_long.columns = ["ti", "col", "T"]
    T_long["date"] = dates[T_long["ti"].astype(int)]
    T_long["symbol"] = syms[T_long["col"].astype(int)]
    T_long = T_long[["date", "symbol", "T"]]
    print(f"      T rows={len(T_long):,} median={T_long['T'].median():.1f}", flush=True)

    print("[3/6] future fragility ...", flush=True)
    fut_long = future_metrics(op, cl, dates, syms, targets)
    print(f"      future rows={len(fut_long):,}", flush=True)

    print("[4/6] controls + merge ...", flush=True)
    ctrl_parts = []
    for ti in targets:
        if ti < 25 or ti + HORIZON + 2 > len(cl):
            continue
        ret20 = cl[ti] / cl[ti - 20] - 1.0
        vol20 = np.std(r[ti - 20:ti], axis=0)
        turn20 = np.mean(to[ti - 20:ti], axis=0)
        amt20 = np.mean(am[ti - 20:ti], axis=0)
        ctrl_parts.append(pd.DataFrame({
            "date": dates[ti], "symbol": syms,
            "ret20": ret20, "vol20": vol20, "turn20": turn20,
            "logamt20": np.log(np.maximum(amt20, 1.0)),
        }))
    ctrl = pd.concat(ctrl_parts, ignore_index=True)

    df = age.merge(T_long, on=["date", "symbol"], how="inner")
    df = df.merge(fut_long, on=["date", "symbol"], how="inner")
    df = df.merge(ctrl, on=["date", "symbol"], how="inner")
    df["D"] = df["age_mean"].astype(float) + df["old_share"].astype(float) * 500.0
    df["M"] = df["T"] / df["D"].clip(lower=1.0)
    df = df[(df["mass"] > 0.10) & df["D"].notna() & df["T"].notna() & df["M"].notna()]
    df = df[np.isfinite(df["M"]) & (df["M"] > 0)]
    print(f"      merged rows={len(df):,} dates={df['date'].nunique()} symbols={df['symbol'].nunique()}", flush=True)

    # 月度截面 z：fragility 复合
    zparts = []
    for dt, g in df.groupby("date"):
        z = pd.DataFrame(index=g.index)
        z["z_mdd"] = zscore(-g["mdd20"].to_numpy())
        z["z_skew"] = zscore(-g["skew20"].to_numpy())
        z["z_worst5"] = zscore(-g["worst5"].to_numpy())
        z["z_dvol"] = zscore(g["dvol20"].to_numpy())
        z["fragility"] = z[["z_mdd", "z_skew", "z_worst5", "z_dvol"]].mean(axis=1)
        zparts.append(z)
    df = pd.concat([df, pd.concat(zparts)], axis=0) if False else df.join(pd.concat(zparts))
    df = df.dropna(subset=["fragility"])
    df.to_parquet(OUT / "panel.parquet", index=False)

    print("[5/6] tests ...", flush=True)
    monthly = []
    for dt, g in df.groupby("date"):
        if len(g) < 100:
            continue
        row = {"date": dt, "n": len(g)}
        for col in ["fragility", "mdd20", "skew20", "worst5", "dvol20", "fwd20", "recovery_days"]:
            x = g[["M", col]].dropna()
            row[f"rho_{col}"] = spearman(x["M"], x[col]) if len(x) > 30 else np.nan
        # 五档
        try:
            g = g.copy()
            g["q"] = pd.qcut(g["M"], 5, labels=False, duplicates="drop")
            for col in ["fragility", "mdd20", "skew20", "fwd20"]:
                q = g.groupby("q")[col].mean()
                for qi in range(5):
                    row[f"q{qi+1}_{col}"] = float(q.loc[qi]) if qi in q.index else np.nan
        except Exception:
            pass
        # 2x2：D 低/高 × T 高/低
        dmed, tmed = g["D"].median(), g["T"].median()
        for dname, dmask in (("dlo", g["D"] <= dmed), ("dhi", g["D"] > dmed)):
            for tname, tmask in (("tlo", g["T"] <= tmed), ("thi", g["T"] > tmed)):
                cell = g[dmask & tmask]
                row[f"cell_{dname}_{tname}_frag"] = cell["fragility"].mean() if len(cell) else np.nan
                row[f"cell_{dname}_{tname}_fwd"] = cell["fwd20"].mean() if len(cell) else np.nan
        monthly.append(row)
    mon = pd.DataFrame(monthly).sort_values("date")
    mon.to_parquet(OUT / "monthly.parquet", index=False)

    def sumstat(col):
        x = mon[col].dropna().to_numpy()
        return {"mean": round(float(np.mean(x)), 4), "nw_t": round(float(nw_t(x)), 2), "n": int(len(x))}

    summary = {
        "sample": {
            "rows": int(len(df)),
            "dates": int(df["date"].nunique()),
            "symbols": int(df["symbol"].nunique()),
            "start": str(df["date"].min().date()),
            "end": str(df["date"].max().date()),
        },
        "corr_T_D": round(float(df[["T", "D"]].corr(method="spearman").iloc[0, 1]), 3),
        "median_T": round(float(df["T"].median()), 1),
        "median_D": round(float(df["D"].median()), 1),
        "primary_fragility": sumstat("rho_fragility"),
        "rho_mdd": sumstat("rho_mdd20"),
        "rho_skew": sumstat("rho_skew20"),
        "rho_worst5": sumstat("rho_worst5"),
        "rho_dvol": sumstat("rho_dvol20"),
        "rho_fwd20": sumstat("rho_fwd20"),
        "rho_recovery": sumstat("rho_recovery_days"),
        "q_fragility": {
            "q1": round(float(mon["q1_fragility"].mean()), 3),
            "q5": round(float(mon["q5_fragility"].mean()), 3),
            "spread": round(float((mon["q5_fragility"] - mon["q1_fragility"]).mean()), 3),
            "nw_t": round(float(nw_t((mon["q5_fragility"] - mon["q1_fragility"]).dropna())), 2),
        },
        "q_mdd": {
            "q1": round(float(mon["q1_mdd20"].mean()), 4),
            "q5": round(float(mon["q5_mdd20"].mean()), 4),
            "spread": round(float((mon["q5_mdd20"] - mon["q1_mdd20"]).mean()), 4),
            "nw_t": round(float(nw_t((mon["q5_mdd20"] - mon["q1_mdd20"]).dropna())), 2),
        },
        "q_fwd20": {
            "q1": round(float(mon["q1_fwd20"].mean()), 4),
            "q5": round(float(mon["q5_fwd20"].mean()), 4),
            "spread": round(float((mon["q5_fwd20"] - mon["q1_fwd20"]).mean()), 4),
            "nw_t": round(float(nw_t((mon["q5_fwd20"] - mon["q1_fwd20"]).dropna())), 2),
        },
        "mismatch_cell": {
            "mismatch_frag": round(float(mon["cell_dlo_thi_frag"].mean()), 3),
            "opposite_frag": round(float(mon["cell_dhi_tlo_frag"].mean()), 3),
            "diff_frag": round(float((mon["cell_dlo_thi_frag"] - mon["cell_dhi_tlo_frag"]).mean()), 3),
            "nw_t_frag": round(float(nw_t((mon["cell_dlo_thi_frag"] - mon["cell_dhi_tlo_frag"]).dropna())), 2),
            "mismatch_fwd": round(float(mon["cell_dlo_thi_fwd"].mean()), 4),
            "opposite_fwd": round(float(mon["cell_dhi_tlo_fwd"].mean()), 4),
            "diff_fwd": round(float((mon["cell_dlo_thi_fwd"] - mon["cell_dhi_tlo_fwd"]).mean()), 4),
            "nw_t_fwd": round(float(nw_t((mon["cell_dlo_thi_fwd"] - mon["cell_dhi_tlo_fwd"]).dropna())), 2),
        },
        "dist": {
            "T": {k: round(float(df["T"].quantile(q)), 1) for k, q in [("p10", .1), ("p50", .5), ("p90", .9)]},
            "D": {k: round(float(df["D"].quantile(q)), 1) for k, q in [("p10", .1), ("p50", .5), ("p90", .9)]},
            "M": {k: round(float(df["M"].quantile(q)), 2) for k, q in [("p10", .1), ("p50", .5), ("p90", .9)]},
        },
        "corr_M": {
            "D": round(float(df[["M", "D"]].corr(method="spearman").iloc[0, 1]), 3),
            "T": round(float(df[["M", "T"]].corr(method="spearman").iloc[0, 1]), 3),
            "turn20": round(float(df[["M", "turn20"]].corr(method="spearman").iloc[0, 1]), 3),
            "vol20": round(float(df[["M", "vol20"]].corr(method="spearman").iloc[0, 1]), 3),
            "ret20": round(float(df[["M", "ret20"]].corr(method="spearman").iloc[0, 1]), 3),
            "logamt20": round(float(df[["M", "logamt20"]].corr(method="spearman").iloc[0, 1]), 3),
        },
        "months": {
            "n": int(len(mon)),
            "rho_min": round(float(mon["rho_fragility"].min()), 3),
            "rho_max": round(float(mon["rho_fragility"].max()), 3),
            "positive_share": round(float((mon["rho_fragility"] > 0).mean()), 3),
        },
        "quintiles": {
            col: [round(float(mon[f"q{i}_{col}"].mean()), 4) for i in range(1, 6)]
            for col in ["fragility", "mdd20", "fwd20", "skew20"]
        },
        "oos": {},
    }
    for name, mask in (("2019-2022", mon["date"] < "2023-01-01"), ("2023-2026", mon["date"] >= "2023-01-01")):
        x = mon.loc[mask, "rho_fragility"].dropna()
        y = (mon.loc[mask, "q5_fragility"] - mon.loc[mask, "q1_fragility"]).dropna()
        summary["oos"][name] = {
            "rho": round(float(x.mean()), 4), "nw_t": round(float(nw_t(x)), 2),
            "spread": round(float(y.mean()), 3), "n": int(len(x)),
        }

    print("[6/6] placebo + regression ...", flush=True)
    # 月内打乱 M 200 次，真值百分位
    rng = np.random.default_rng(20261126)
    true_rho = float(mon["rho_fragility"].mean())
    placebo = []
    for _ in range(N_PLACEBO):
        vals = []
        for dt, g in df.groupby("date"):
            if len(g) < 100:
                continue
            m_shuf = rng.permutation(g["M"].to_numpy())
            vals.append(spearman(m_shuf, g["fragility"]))
        placebo.append(float(np.nanmean(vals)))
    placebo = np.asarray(placebo)
    summary["placebo"] = {
        "true_rho": round(true_rho, 4),
        "placebo_mean": round(float(placebo.mean()), 4),
        "placebo_sd": round(float(placebo.std()), 4),
        "percentile": round(float((placebo < true_rho).mean() * 100), 1),
    }

    # 月度截面回归（标准化）：fragility / fwd20 ~ M + D + controls
    reg_rows = []
    for dt, g in df.groupby("date"):
        if len(g) < 200:
            continue
        g = g.dropna(subset=["M", "D", "logamt20", "vol20", "turn20", "ret20", "fragility", "fwd20"])
        if len(g) < 200:
            continue
        X = np.column_stack([
            zscore(g["M"]), zscore(g["D"]), zscore(g["logamt20"]),
            zscore(g["vol20"]), zscore(g["turn20"]), zscore(g["ret20"]),
            np.ones(len(g)),
        ])
        for yname in ("fragility", "fwd20"):
            y = zscore(g[yname])
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            reg_rows.append({"date": dt, "y": yname, "b_M": beta[0], "b_D": beta[1], "n": len(g)})
    reg = pd.DataFrame(reg_rows)
    reg.to_parquet(OUT / "regression.parquet", index=False)
    summary["regression"] = {}
    for yname, g in reg.groupby("y"):
        summary["regression"][yname] = {
            "b_M": round(float(g["b_M"].mean()), 4),
            "t_M": round(float(nw_t(g["b_M"])), 2),
            "b_D": round(float(g["b_D"].mean()), 4),
            "t_D": round(float(nw_t(g["b_D"])), 2),
            "n": int(len(g)),
        }

    # 判据
    prim = summary["primary_fragility"]
    oos = summary["oos"]
    pass_frag = prim["mean"] >= 0.15 and prim["nw_t"] >= 2 and (oos["2019-2022"]["rho"] > 0 and oos["2023-2026"]["rho"] > 0)
    pass_diff = summary["mismatch_cell"]["diff_frag"] > 0 and summary["mismatch_cell"]["nw_t_frag"] >= 2
    fwd = summary["rho_fwd20"]
    corr_td = summary["corr_T_D"]
    invalid = abs(corr_td) > 0.8
    summary["verdict"] = {
        "corr_T_D": corr_td,
        "invalid_collinear": bool(invalid),
        "primary_pass": bool(pass_frag),
        "mismatch_cell_pass": bool(pass_diff),
        "fwd20_not_negative": bool(not (fwd["mean"] < -0.05 and fwd["nw_t"] < -2)),
        "status": "INVALID" if invalid else ("PASS" if (pass_frag and pass_diff) else ("PARTIAL" if (pass_frag or pass_diff) else "FAIL")),
    }
    (OUT / "h1.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
