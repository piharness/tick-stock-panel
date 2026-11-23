"""20 日超额收益的前视统计：IC / 残差 IC / 分层 / FDR / 子区间 / 组合外推。"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "footprint20d"
PANEL = OUT / "panel.parquet"
INTRA = OUT / "intraday_daily.parquet"

STATIC = ["log_mcap", "log_price", "age_years", "is_sh"]
DYNAMIC = [
    "ret20", "ret5", "vol20", "skew20", "max_ret20", "ar1_20",
    "turn20", "turn_chg", "turn_cv20", "log_amt20", "log_amihud", "amt_chg",
    "upvol_ratio", "corr_pv", "range20", "range_vol_ratio", "limit_up_cnt20",
    "quiet_cnt20", "overnight20", "intraday20", "dist_high250", "beta60", "idio_vol60",
]
INTRA_BASE = [
    "sf", "tail_share", "open_share", "vwap_dev", "clv", "hhi", "top_share",
    "up_vol_share", "ret_ac1", "vol_ac1", "corr_rv", "signed_vol_ac1", "vol_cv",
    "big_share", "first30_ret", "last30_ret", "day_range", "illiq", "entropy",
]
INTRA_F = [f"i_{b}_20" for b in INTRA_BASE] + [
    f"i_{b}_trend" for b in ["sf", "tail_share", "vol_cv", "illiq"]
]
CONTROLS = ["log_mcap", "turn20", "vol20", "ret20", "log_amihud"]


def _zrank(s: pd.Series) -> pd.Series:
    r = s.rank(pct=True)
    return (r - 0.5) * 2.0


def _zarr(s: pd.Series) -> np.ndarray:
    return _zrank(s).to_numpy(dtype=float, copy=True)


def _nw_t(x, lag: int = 20) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 5:
        return float("nan")
    mu = x.mean()
    e = x - mu
    s = e @ e / n
    for l in range(1, min(lag, n - 1) + 1):
        g = e[l:] @ e[:-l] / n
        s += 2 * (1 - l / (lag + 1)) * g
    if s <= 0:
        return float("nan")
    return float(mu / math.sqrt(s / n))


def _block_p(x, L: int = 20, B: int = 2000, seed: int = 7) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 30:
        return float("nan")
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(n / L))
    starts = rng.integers(0, n, size=(B, nb))
    idx = (starts[:, :, None] + np.arange(L)[None, None, :]) % n
    means = x[idx.reshape(B, -1)[:, :n]].mean(axis=1)
    lo = (means <= 0).mean()
    hi = (means >= 0).mean()
    return float(min(1.0, 2 * min(lo, hi)))


def _bh(p) -> np.ndarray:
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(p)
    q = np.full(p.shape, np.nan)
    pp = p[ok]
    if len(pp) == 0:
        return q
    order = np.argsort(pp)
    m = len(pp)
    ranked = pp[order] * m / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(ranked, 0, 1)
    q[ok] = out
    return q


def _auc(v: np.ndarray, win: np.ndarray) -> float:
    m = np.isfinite(v)
    if m.sum() < 100:
        return float("nan")
    w = win[m]
    npos = int(w.sum())
    nneg = int((~w).sum())
    if npos < 5 or nneg < 5:
        return float("nan")
    r = pd.Series(v[m]).rank().to_numpy()
    return float((r[w].sum() - npos * (npos + 1) / 2) / (npos * nneg))


def load() -> pd.DataFrame:
    p = pd.read_parquet(PANEL)
    p = p[p["ok"] == 1].copy()
    p["date"] = pd.to_datetime(p["date"])

    it = pd.read_parquet(INTRA).sort_values(["symbol", "date"])
    it["date"] = pd.to_datetime(it["date"])
    g = it.groupby("symbol", sort=False)
    for f in ["sf", "tail_share", "open_share", "vwap_dev", "clv", "hhi", "top_share",
              "up_vol_share", "ret_ac1", "vol_ac1", "corr_rv", "signed_vol_ac1", "vol_cv",
              "big_share", "first30_ret", "last30_ret", "day_range", "illiq", "entropy"]:
        it[f + "_20"] = g[f].transform(lambda s: s.rolling(20, min_periods=15).mean())
    for f in ["sf", "tail_share", "vol_cv", "illiq"]:
        it[f + "_trend"] = (
            it.groupby("symbol", sort=False)[f].transform(lambda s: s.rolling(5, min_periods=4).mean())
            - it[f + "_20"]
        )
    cols = [c for c in it.columns if c.endswith("_20") or c.endswith("_trend")]
    it = it[["symbol", "date"] + cols].rename(columns={c: "i_" + c for c in cols})
    p = p.merge(it, on=["symbol", "date"], how="left")
    p["i_sf_trend"] = p["i_sf_trend"]
    return p


def run() -> dict:
    df = load()
    print("rows", len(df), "dates", df["date"].nunique())
    # 目标：主板内部等权超额；按日 winsorize 0.5/99.5
    q = df.groupby("date")["ex20_mb"].quantile([0.005, 0.995]).unstack()
    df = df.join(q.rename(columns={0.005: "lo", 0.995: "hi"}), on="date")
    df["ex_w"] = df["ex20_mb"].clip(df["lo"], df["hi"])

    inds = STATIC + DYNAMIC + INTRA_F
    inds = [c for c in inds if c in df.columns]
    print("indicators", len(inds))

    dates = sorted(df["date"].unique())
    rec = {c: {"ic": [], "ic_res": [], "ls": [], "top": [], "bot": [], "mono": [],
               "n": [], "cov": [], "date": [], "auc": [], "lift": [], "di": []} for c in inds}
    winner_prof = {c: {} for c in inds}
    during = []

    for d in dates:
        sub = df[df["date"] == d]
        n = len(sub)
        if n < 300:
            continue
        ex = sub["ex_w"].to_numpy(float)
        exz = _zrank(sub["ex_w"]).to_numpy(float)
        # 控制变量
        C = pd.DataFrame({c: _zrank(sub[c]) for c in CONTROLS})
        C = C.fillna(0.0)
        ind = sub["industry"].fillna("未分类")
        vc = ind.value_counts()
        keep_ind = vc[vc >= 20].index
        ind2 = ind.where(ind.isin(keep_ind), "OTHER")
        D = pd.get_dummies(ind2, drop_first=True).astype(float)
        X = np.column_stack([
            np.ones(n), C.to_numpy(float), sub["is_sh"].fillna(0).to_numpy(float),
            D.to_numpy(float),
        ])
        # 残差化：ex + 全部指标（缺失填 0）
        Z = []
        for c in inds:
            v = _zarr(sub[c])
            v[~np.isfinite(v)] = 0.0
            Z.append(v)
        Y = np.column_stack([exz] + Z)
        beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
        R = Y - X @ beta
        ex_r = R[:, 0]
        # 控制变量自身：从 X 中删掉自己再残差化（否则残差恒为 0）
        R_ctrl = {}
        for ci, cc in enumerate(CONTROLS):
            if cc not in inds:
                continue
            Xm = np.delete(X, ci + 1, axis=1)
            vv = _zarr(sub[cc])
            vv[~np.isfinite(vv)] = 0.0
            Y2 = np.column_stack([exz, vv])
            b2, *_ = np.linalg.lstsq(Xm, Y2, rcond=None)
            R2 = Y2 - Xm @ b2
            R_ctrl[cc] = (R2[:, 0], R2[:, 1])
        win = ex >= np.nanquantile(ex, 0.9)
        base = win.mean()
        # 目标排序（用于 IC）
        rank_ex = pd.Series(exz).rank().to_numpy()
        for j, c in enumerate(inds):
            v = _zarr(sub[c])
            m = np.isfinite(v)
            if m.sum() < 200:
                continue
            # rank IC
            rv = pd.Series(v[m]).rank().to_numpy()
            re = pd.Series(rank_ex[m]).rank().to_numpy()
            ic = np.corrcoef(rv, re)[0, 1]
            if c in R_ctrl:
                ex_c, res_c = R_ctrl[c]
            else:
                ex_c, res_c = ex_r, R[:, j + 1]
            ic_res = np.corrcoef(res_c[m], ex_c[m])[0, 1]
            auc = _auc(v, win)
            thr = np.nanquantile(v, 0.9)
            topm = m & (v >= thr)
            lift = float(win[topm].mean() / base) if topm.sum() > 20 and base > 0 else np.nan
            # 十分位
            try:
                b = pd.qcut(pd.Series(v[m]).rank(method="first"), 10, labels=False)
            except ValueError:
                continue
            e = ex[m]
            gm = np.bincount(b, weights=e, minlength=10) / np.maximum(
                np.bincount(b, minlength=10), 1
            )
            gv = gm[np.isfinite(gm)]
            ls = gv[-1] - gv[0] if len(gv) >= 8 else np.nan
            mono = np.corrcoef(np.arange(len(gv)), gv)[0, 1] if len(gv) >= 5 else np.nan
            rec[c]["ic"].append(ic)
            rec[c]["ic_res"].append(ic_res)
            rec[c]["ls"].append(ls)
            rec[c]["top"].append(gv[-1] if len(gv) else np.nan)
            rec[c]["bot"].append(gv[0] if len(gv) else np.nan)
            rec[c]["mono"].append(mono)
            rec[c]["n"].append(int(m.sum()))
            rec[c]["cov"].append(float(m.mean()))
            rec[c]["date"].append(d)
            rec[c]["di"].append(int(sub["di"].iloc[0]))
            rec[c]["auc"].append(auc)
            rec[c]["lift"].append(lift)
        # 赢家画像：同市值×波动格内的 z 差
        sz = pd.qcut(_zrank(sub["log_mcap"]), 5, labels=False, duplicates="drop")
        vl = pd.qcut(_zrank(sub["vol20"]), 5, labels=False, duplicates="drop")
        cell = pd.Series(list(zip(sz, vl)))
        for c in inds:
            v = _zarr(sub[c])
            diffs, ws = [], []
            for _, idx in cell.groupby(cell).groups.items():
                idx = np.asarray(idx, dtype=int)
                w = win[idx]
                if w.sum() < 5 or (~w).sum() < 5:
                    continue
                vv = v[idx]
                if np.isfinite(vv).sum() < 10:
                    continue
                diffs.append(np.nanmean(vv[w]) - np.nanmean(vv[~w]))
                ws.append(w.sum())
            if diffs:
                winner_prof[c][d] = float(np.average(diffs, weights=ws))
        # 窗口内 vs 窗口前（仅换手/波动）
        during.append({
            "date": d, "n": n,
            "turn_pre": float(np.nanmean(_zrank(sub["turn20"]))),
            "turn_dur": float(np.nanmean(_zrank(sub["turn20_fwd"]))),
            "vol_pre": float(np.nanmean(_zrank(sub["vol20"]))),
            "vol_dur": float(np.nanmean(_zrank(sub["vol20_fwd"]))),
            "win_turn_pre": float(np.nanmean(_zrank(sub.loc[win, "turn20"]))),
            "win_turn_dur": float(np.nanmean(_zrank(sub.loc[win, "turn20_fwd"]))),
            "win_vol_pre": float(np.nanmean(_zrank(sub.loc[win, "vol20"]))),
            "win_vol_dur": float(np.nanmean(_zrank(sub.loc[win, "vol20_fwd"]))),
        })

    # 时间序列汇总
    rows = []
    for c in inds:
        ic = np.array(rec[c]["ic"], float)
        icr = np.array(rec[c]["ic_res"], float)
        ls = np.array(rec[c]["ls"], float)
        dts = pd.to_datetime(rec[c]["date"])
        di = np.array(rec[c]["di"], int)
        wp = np.array([winner_prof[c].get(d, np.nan) for d in dts], float)
        if c.startswith("i_"):
            m = dts >= "2024-09-01"
            ic, icr, ls, wp, dts, di = ic[m], icr[m], ls[m], wp[m], dts[m], di[m]
        no = (di % 20) == 0
        ic_no = ic[no]
        ls_no = ls[no]
        rows.append({
            "indicator": c,
            "family": "static" if c in STATIC else ("intraday" if c.startswith("i_") else "dynamic"),
            "n_obs": int(np.nansum(rec[c]["n"])),
            "coverage": float(np.nanmean(rec[c]["cov"])),
            "ic": float(np.nanmean(ic)),
            "ic_t": _nw_t(ic),
            "ic_p_boot": _block_p(ic),
            "ic_hit": float(np.nanmean(ic > 0)),
            "icir": float(np.nanmean(ic) / np.nanstd(ic)) if np.nanstd(ic) > 0 else np.nan,
            "ic_res": float(np.nanmean(icr)),
            "ic_res_t": _nw_t(icr),
            "ic_res_hit": float(np.nanmean(icr > 0)),
            "ls_bp": float(np.nanmean(ls) * 1e4),
            "ls_t": _nw_t(ls),
            "ls_hit": float(np.nanmean(ls > 0)),
            "top_bp": float(np.nanmean(rec[c]["top"]) * 1e4),
            "top_net_bp": float(np.nanmean(rec[c]["top"]) * 1e4 - 15.0),
            "bot_bp": float(np.nanmean(rec[c]["bot"]) * 1e4),
            "ic_no": float(np.nanmean(ic_no)) if len(ic_no) else np.nan,
            "ic_no_t": _nw_t(ic_no, lag=1),
            "ls_no_bp": float(np.nanmean(ls_no) * 1e4) if len(ls_no) else np.nan,
            "n_no": int(len(ic_no)),
            "mono": float(np.nanmean(rec[c]["mono"])),
            "winner_z": float(np.nanmean(wp)),
            "winner_z_t": _nw_t(wp),
            "auc": float(np.nanmean(rec[c]["auc"])),
            "auc_t": _nw_t(np.array(rec[c]["auc"], float)),
            "lift": float(np.nanmean(rec[c]["lift"])),
            "lift_t": _nw_t(np.array(rec[c]["lift"], float)),
            "ic_2010_14": float(np.nanmean(ic[(dts >= "2010-01-01") & (dts < "2015-01-01")]))
            if np.any((dts >= "2010-01-01") & (dts < "2015-01-01")) else np.nan,
            "ic_2015_19": float(np.nanmean(ic[(dts >= "2015-01-01") & (dts < "2020-01-01")]))
            if np.any((dts >= "2015-01-01") & (dts < "2020-01-01")) else np.nan,
            "ic_2020_26": float(np.nanmean(ic[dts >= "2020-01-01"]))
            if np.any(dts >= "2020-01-01") else np.nan,
            "ic_intra": float(np.nanmean(ic[dts >= "2024-09-01"]))
            if np.any(dts >= "2024-09-01") else np.nan,
        })
    tab = pd.DataFrame(rows)
    tab["q_all"] = _bh(tab["ic_p_boot"])
    dm = ~tab["indicator"].str.startswith("i_")
    tab["q_daily"] = np.nan
    tab.loc[dm, "q_daily"] = _bh(tab.loc[dm, "ic_p_boot"])
    tab["p_auc"] = [
        2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))) if np.isfinite(t) else np.nan
        for t in tab["auc_t"]
    ]
    tab["q_auc"] = _bh(tab["p_auc"])
    tab = tab.sort_values("ic_t", key=lambda s: -s.abs())
    tab.to_csv(OUT / "indicator_stats.csv", index=False)

    # 年份 IC 明细
    yearly = {}
    for c in inds:
        ic = np.array(rec[c]["ic"], float)
        dts = pd.to_datetime(rec[c]["date"])
        s = pd.Series(ic, index=dts).groupby(dts.year).mean()
        yearly[c] = {int(k): float(v) for k, v in s.items()}
    json.dump(yearly, open(OUT / "yearly_ic.json", "w"), ensure_ascii=False, indent=1)

    dw = pd.DataFrame(during)
    dw.to_csv(OUT / "during_vs_pre.csv", index=False)

    # 相关性矩阵（指标间）
    samp = df.sample(min(200000, len(df)), random_state=1)
    corr = samp[inds].corr(method="spearman")
    corr.to_csv(OUT / "indicator_corr.csv")

    print(tab[["indicator", "ic", "ic_t", "ic_res_t", "ls_bp", "ls_t", "winner_z_t", "q_all"]].head(25).to_string(index=False))
    return {"n": len(df), "dates": len(dates), "table": tab.to_dict("records")}


if __name__ == "__main__":
    res = run()
    json.dump(res, open(OUT / "stats.json", "w"), ensure_ascii=False, indent=1, default=float)
    print("saved", OUT / "stats.json")
