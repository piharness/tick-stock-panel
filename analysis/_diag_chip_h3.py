"""H3：进入方向 × 速度 → 吸收 / 雪崩。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
对象
  「密集区」= 递归成本分布的核心带。用换手加权的递归矩估计：
      m_t = (1 - to_t) * m_{t-1} + to_t          # 活质量，收敛到 1
      c_t = ((1-to)*m*c + to*P) / m_t            # 成本中枢
      v_t = ((1-to)*m*(v + (c_prev-c)^2) + to*(P-c)^2) / m_t
  带 = c_{t-1} ± K * sd_{t-1}，K=0.5（主），0.25/0.75/1.0/1.5 为敏感性。
  **带用 t-1 的参数**：t 日收盘只与昨日已知的带比较，否则带的漂移会伪造穿越。

事件
  上穿：t-1 收盘在带下方，t 收盘 >= 带下沿（价格从下往上进入密集区）。
  下穿：t-1 收盘在带上方，t 收盘 <= 带上沿。
  速度：|cum5| >= 5% 为快，否则慢；且 cum5 符号必须与方向一致。
  冷却：同一股票 20 个交易日内只取第一个事件（避免一次行情算多次）。
  剔除：|ret_t| >= 9.5%（涨跌停收盘价不可交易）、成交额=0、北交所。
  入场 t+1 开盘，出场 t+21 收盘（20 个交易日），与 H1/H2 同口径。

三层判据（逐层更强）
  L1 预注册表：四格符号 = 上慢 +、上快 −、下慢 +、下快 −，极差 >= 80bp。
     注意：这张表四个格子的符号只由「速度」决定，方向不进入预测——
     所以 L1 通过只等于确认「短期反转」，**不能**证明吸收/雪崩。
  L2 方向主效应：固定速度下，上穿 < 下穿（阻力 > 支撑）。这是筹码特有的方向预测。
  L3 密集区依赖：把 c/sd 在同波动分位内跨股票置换（价格路径不动、带变随机），
     重跑 L1/L2 的对比。若置换后效应仍在，则「密集区」不做事，只是反转。
     另加带宽剂量反应：K 越大（带越不密），效应应越弱。

控制
  月度截面回归：fwd20_bp ~ cell dummies + ret20 + vol20 + turn20 + logamt20。
  月度聚合后 NW(6) t 值；事件在月内重叠不算独立样本。

杀死
  L1 符号不符或极差 < 80bp；或 L2 方向与预测相反；或 L3 置换后效应保留 >70%。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h3"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "data" / "kline_daily_enriched"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"

HORIZON = 20
COOLDOWN = 20
K_BAND = 0.5
K_LIST = [0.25, 0.5, 0.75, 1.0, 1.5]
FAST = 0.05
LIMIT = 0.095
NW_LAG = 6
N_PLACEBO = 20


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
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.9)
    op = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="to", aggfunc="last").sort_index()
    cl = cl.reindex(op.index)
    am = am.reindex(op.index)
    to = to.reindex(op.index)
    return op, cl, am, to


def cost_moments(cl: np.ndarray, to: np.ndarray):
    """递归成本中枢与离散度（换手加权）。返回 c, sd，形状同日线。"""
    n, k = cl.shape
    m = np.zeros(k)
    c = np.zeros(k)
    v = np.zeros(k)
    C = np.full((n, k), np.nan, dtype=np.float32)
    S = np.full((n, k), np.nan, dtype=np.float32)
    for i in range(n):
        p = cl[i].astype(np.float64)
        tt = np.nan_to_num(to[i].astype(np.float64), nan=0.0)
        ok = np.isfinite(p) & (p > 0) & (tt > 0)
        keep = 1.0 - tt
        m_new = keep * m + tt
        den = np.maximum(m_new, 1e-12)
        c_new = (keep * m * c + tt * np.where(ok, p, 0.0)) / den
        dev = np.where(ok, p, c_new) - c_new
        v_new = (keep * m * (v + (c - c_new) ** 2) + tt * dev ** 2) / den
        good = np.isfinite(c_new) & (m_new > 1e-9)
        m = m_new
        c = np.where(good, c_new, c)
        v = np.where(good, np.maximum(v_new, 0.0), v)
        C[i] = np.where(good, c, np.nan)
        S[i] = np.where(good, np.sqrt(np.maximum(v, 0.0)), np.nan)
    return C, S


def build_events(C: np.ndarray, c: np.ndarray, sd: np.ndarray, k: float, cum5: np.ndarray):
    """返回 (ti, ci, dir, fast) 四个数组。dir: +1 上穿 / -1 下穿。"""
    lo = c - k * sd
    hi = c + k * sd
    nanrow = np.full((1, C.shape[1]), np.nan, dtype=np.float32)
    lo_lag = np.vstack([nanrow, lo[:-1]])
    hi_lag = np.vstack([nanrow, hi[:-1]])

    st_own = np.where(C < lo, 0.0, np.where(C > hi, 2.0, 1.0)).astype(np.float32)
    st_own[~np.isfinite(C) | ~np.isfinite(lo)] = np.nan
    st_lag = np.where(C < lo_lag, 0.0, np.where(C > hi_lag, 2.0, 1.0)).astype(np.float32)
    st_lag[~np.isfinite(C) | ~np.isfinite(lo_lag)] = np.nan
    prev = np.vstack([nanrow, st_own[:-1]])

    up = (prev == 0) & (st_lag >= 1) & (st_lag <= 2)
    dn = (prev == 2) & (st_lag <= 1) & (st_lag >= 0)
    ret = np.vstack([nanrow, C[1:] / C[:-1] - 1.0])
    tradable = np.isfinite(ret) & (np.abs(ret) < LIMIT)
    up &= tradable
    dn &= tradable

    out = []
    for sign, mask in ((1, up), (-1, dn)):
        mask = mask & np.isfinite(cum5)
        ti, ci = np.nonzero(mask)
        x = cum5[ti, ci]
        good = np.where(sign > 0, x > 0, x < 0)
        ti, ci, x = ti[good], ci[good], x[good]
        fast = np.abs(x) >= FAST
        # 冷却：每只股票 20 日内只留第一个
        order = np.argsort(ti, kind="stable")
        ti, ci, x, fast = ti[order], ci[order], x[order], fast[order]
        last = np.full(C.shape[1], -10**9)
        keep = np.zeros(len(ti), dtype=bool)
        for j in range(len(ti)):
            t, i = ti[j], ci[j]
            if t - last[i] >= COOLDOWN:
                keep[j] = True
                last[i] = t
        out.append((ti[keep], ci[keep], np.full(keep.sum(), sign, dtype=np.int8), fast[keep], x[keep]))
    ti = np.concatenate([o[0] for o in out])
    ci = np.concatenate([o[1] for o in out])
    dr = np.concatenate([o[2] for o in out])
    fa = np.concatenate([o[3] for o in out])
    cu = np.concatenate([o[4] for o in out])
    return ti, ci, dr, fa, cu


def outcomes(O: np.ndarray, C: np.ndarray, ti: np.ndarray, ci: np.ndarray):
    """事件的前瞻 20 日：fwd / mdd / skew。入场 t+1 开盘，出场 t+21 收盘。"""
    n = len(ti)
    fwd = np.full(n, np.nan)
    mdd = np.full(n, np.nan)
    skew = np.full(n, np.nan)
    worst5 = np.full(n, np.nan)
    for t in np.unique(ti):
        sel = np.nonzero(ti == t)[0]
        idx = ci[sel]
        if t + HORIZON + 2 > len(C):
            continue
        p0 = O[t + 1, idx].astype(np.float64)
        p = C[t + 1:t + HORIZON + 2, idx].astype(np.float64)
        if p.shape[0] < HORIZON + 1:
            continue
        ok = np.isfinite(p0) & (p0 > 0) & np.isfinite(p).all(axis=0)
        if not ok.any():
            continue
        p0 = p0[ok]
        p = p[:, ok]
        r1 = p[0] / p0 - 1.0
        r = np.vstack([r1, p[1:] / p[:-1] - 1.0])
        r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
        path = np.vstack([p0, p])
        peak = np.maximum.accumulate(path, axis=0)
        dd = path / peak - 1.0
        mu = p.mean(axis=0)
        d = p - mu
        m2 = (d ** 2).mean(axis=0)
        m3 = (d ** 3).mean(axis=0)
        sk = m3 / np.maximum(m2, 1e-18) ** 1.5
        sw = np.lib.stride_tricks.sliding_window_view(r, 5, axis=0)
        w5 = np.prod(1.0 + sw, axis=-1) - 1.0
        fwd[sel[ok]] = p[-1] / p0 - 1.0
        mdd[sel[ok]] = dd.min(axis=0)
        skew[sel[ok]] = sk
        worst5[sel[ok]] = w5.min(axis=0)
    return fwd, mdd, skew, worst5


def nw_t(x, lag: int = NW_LAG) -> float:
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


def zscore(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def cell_monthly(dates, ti, dr, fa, fwd, mdd, skew):
    """按月聚合四格均值与三条对比。"""
    df = pd.DataFrame({
        "date": dates[ti], "dir": dr, "fast": fa,
        "fwd": fwd, "mdd": mdd, "skew": skew,
    }).dropna(subset=["fwd"])
    df["cell"] = np.where(df["dir"] > 0, "up", "dn") + np.where(df["fast"], "_fast", "_slow")
    df["ym"] = df["date"].values.astype("datetime64[M]")
    g = df.groupby(["ym", "cell"]).agg(fwd=("fwd", "mean"), mdd=("mdd", "mean"),
                                       skew=("skew", "mean"), n=("fwd", "size")).reset_index()
    wide = g.pivot(index="ym", columns="cell", values="fwd")
    widem = g.pivot(index="ym", columns="cell", values="mdd")
    wides = g.pivot(index="ym", columns="cell", values="skew")
    n = g.pivot(index="ym", columns="cell", values="n")
    for col in ["up_slow", "up_fast", "dn_slow", "dn_fast"]:
        if col not in wide.columns:
            wide[col] = np.nan
            widem[col] = np.nan
            wides[col] = np.nan
            n[col] = np.nan
    out = pd.DataFrame(index=wide.index)
    out["n_events"] = n.sum(axis=1)
    for col in ["up_slow", "up_fast", "dn_slow", "dn_fast"]:
        out[f"fwd_{col}"] = wide[col]
        out[f"mdd_{col}"] = widem[col]
        out[f"skew_{col}"] = wides[col]
    out["slow_minus_fast"] = 0.5 * ((out["fwd_up_slow"] - out["fwd_up_fast"]) +
                                    (out["fwd_dn_slow"] - out["fwd_dn_fast"]))
    out["up_minus_dn"] = 0.5 * ((out["fwd_up_slow"] - out["fwd_dn_slow"]) +
                                (out["fwd_up_fast"] - out["fwd_dn_fast"]))
    out["interaction"] = ((out["fwd_up_slow"] - out["fwd_up_fast"]) -
                          (out["fwd_dn_slow"] - out["fwd_dn_fast"]))
    _c4 = out[["fwd_up_slow", "fwd_up_fast", "fwd_dn_slow", "fwd_dn_fast"]]
    out["spread"] = _c4.max(axis=1) - _c4.min(axis=1)
    out.loc[_c4.isna().any(axis=1), "spread"] = np.nan
    return out.reset_index()


def summarize(mon: pd.DataFrame) -> dict:
    def s(col):
        x = mon[col].dropna().to_numpy()
        return {"mean_bp": round(float(np.mean(x) * 1e4), 1), "nw_t": round(float(nw_t(x)), 2), "n": int(len(x))}

    cells = {c: s(f"fwd_{c}") for c in ["up_slow", "up_fast", "dn_slow", "dn_fast"]}
    spread = mon["spread"].dropna().to_numpy()
    return {
        "cells": cells,
        "spread_bp": round(float(np.mean(spread) * 1e4), 1),
        "spread_t": round(float(nw_t(spread)), 2),
        "slow_minus_fast_bp": round(float(mon["slow_minus_fast"].mean() * 1e4), 1),
        "slow_minus_fast_t": round(float(nw_t(mon["slow_minus_fast"])), 2),
        "up_minus_dn_bp": round(float(mon["up_minus_dn"].mean() * 1e4), 1),
        "up_minus_dn_t": round(float(nw_t(mon["up_minus_dn"])), 2),
        "interaction_bp": round(float(mon["interaction"].mean() * 1e4), 1),
        "interaction_t": round(float(nw_t(mon["interaction"])), 2),
        "months": int(mon["fwd_up_slow"].notna().sum()),
        "events": int(mon["n_events"].sum()),
    }


def main() -> None:
    print("[1/6] load ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(dtype=np.float32)
    C = cl_df.to_numpy(dtype=np.float32)
    A = am_df.to_numpy(dtype=np.float32)
    TO = to_df.to_numpy(dtype=np.float32)
    n, k = C.shape
    print(f"      {n} x {k}", flush=True)

    print("[2/6] turnover-recursive cost moments ...", flush=True)
    cst, sd = cost_moments(C, TO)
    print(f"      median sd/cost = {np.nanmedian(sd / cst):.3f}", flush=True)

    print("[3/6] cum5 ...", flush=True)
    cum5 = np.full((n, k), np.nan, dtype=np.float32)
    cum5[5:] = C[5:] / C[:-5] - 1.0

    print("[4/6] events + outcomes (primary K=0.5) ...", flush=True)
    ti, ci, dr, fa, cu = build_events(C, cst, sd, K_BAND, cum5)
    print(f"      events={len(ti):,}  up={int((dr>0).sum()):,}  fast={int(fa.sum()):,}", flush=True)
    fwd, mdd, skew, worst5 = outcomes(O, C, ti, ci)
    ok = np.isfinite(fwd)
    ti, ci, dr, fa, cu = ti[ok], ci[ok], dr[ok], fa[ok], cu[ok]
    fwd, mdd, skew, worst5 = fwd[ok], mdd[ok], skew[ok], worst5[ok]
    print(f"      outcomes={len(ti):,}", flush=True)

    mon = cell_monthly(dates, ti, dr, fa, fwd, mdd, skew)
    mon.to_parquet(OUT / "monthly.parquet", index=False)
    summary = {"sample": {
        "rows": int(len(ti)), "dates": int(len(np.unique(dates[ti]))),
        "symbols": int(len(np.unique(ci))), "start": str(dates[ti].min().date()),
        "end": str(dates[ti].max().date()), "k_band": K_BAND, "fast": FAST,
    }}
    summary["primary"] = summarize(mon)
    print(json.dumps(summary["primary"], ensure_ascii=False, indent=2), flush=True)

    print("[5/6] controls + dose response ...", flush=True)
    # 事件层控制：逐月截面回归（cell dummies）
    r = np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]).astype(np.float64)
    r = np.clip(np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0), -0.2, 0.2)
    S1 = np.vstack([np.zeros((1, k)), np.cumsum(r, axis=0)])
    S2 = np.vstack([np.zeros((1, k)), np.cumsum(r * r, axis=0)])
    TOc = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(TO, nan=0.0), axis=0)])
    Ac = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(A, nan=0.0), axis=0)])
    good_t = ti >= 25
    s1 = S1[ti[good_t]] - S1[ti[good_t] - 20]
    s2 = S2[ti[good_t]] - S2[ti[good_t] - 20]
    vol = np.sqrt(np.maximum(s2 / 20 - (s1 / 20) ** 2, 0.0))
    ev = pd.DataFrame({
        "date": dates[ti[good_t]], "dir": dr[good_t], "fast": fa[good_t],
        "fwd": fwd[good_t], "mdd": mdd[good_t], "skew": skew[good_t],
        "ret20": C[ti[good_t], ci[good_t]] / C[ti[good_t] - 20, ci[good_t]] - 1.0,
        "vol20": vol[np.arange(int(good_t.sum())), ci[good_t]],
        "turn20": (TOc[ti[good_t], ci[good_t]] - TOc[ti[good_t] - 20, ci[good_t]]) / 20,
        "logamt20": np.log(np.maximum((Ac[ti[good_t], ci[good_t]] - Ac[ti[good_t] - 20, ci[good_t]]) / 20, 1.0)),
    })
    ev = ev.replace([np.inf, -np.inf], np.nan).dropna()
    reg = []
    for dt, g in ev.groupby("date"):
        if len(g) < 40:
            continue
        y = g["fwd"].to_numpy() * 1e4
        up = (g["dir"].to_numpy() > 0).astype(float)
        fast = g["fast"].to_numpy().astype(float)
        X = np.column_stack([
            up, fast, up * fast,
            zscore(g["ret20"]), zscore(g["vol20"]), zscore(g["turn20"]), zscore(g["logamt20"]),
            np.ones(len(g)),
        ])
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        reg.append({"date": dt, "b_up": b[0], "b_fast": b[1], "b_int": b[2], "n": len(g)})
    reg = pd.DataFrame(reg)
    reg.to_parquet(OUT / "regression.parquet", index=False)
    summary["regression"] = {
        "b_up_bp": round(float(reg["b_up"].mean()), 1), "t_up": round(float(nw_t(reg["b_up"])), 2),
        "b_fast_bp": round(float(reg["b_fast"].mean()), 1), "t_fast": round(float(nw_t(reg["b_fast"])), 2),
        "b_int_bp": round(float(reg["b_int"].mean()), 1), "t_int": round(float(nw_t(reg["b_int"])), 2),
        "n_months": int(len(reg)),
    }

    dose = {}
    for kk in K_LIST:
        if kk == K_BAND:
            dose[f"{kk}"] = summary["primary"]
            continue
        t2, c2, d2, f2, _ = build_events(C, cst, sd, kk, cum5)
        fw2, md2, sk2, _ = outcomes(O, C, t2, c2)
        ok2 = np.isfinite(fw2)
        m2 = cell_monthly(dates, t2[ok2], d2[ok2], f2[ok2], fw2[ok2], md2[ok2], sk2[ok2])
        dose[f"{kk}"] = summarize(m2)
        print(f"      K={kk} n={int(ok2.sum()):,} up-dn={dose[f'{kk}']['up_minus_dn_bp']}bp "
              f"slow-fast={dose[f'{kk}']['slow_minus_fast_bp']}bp", flush=True)
    summary["dose_response"] = dose

    print("[6/6] shuffled-zone placebo ...", flush=True)
    # 同波动分位内跨股票置换 c/sd：价格路径不动，带变随机
    import numpy as _np
    vol_med = np.nanmedian(sd / cst, axis=0)
    q = pd.qcut(pd.Series(vol_med).rank(method="first"), 5, labels=False).to_numpy()
    rng = np.random.default_rng(20261126)
    plac = []
    for b in range(N_PLACEBO):
        perm = np.arange(k)
        for gq in range(5):
            idx = np.nonzero(q == gq)[0]
            perm[idx] = rng.permutation(idx)
        t3, c3, d3, f3, _ = build_events(C, cst[:, perm], sd[:, perm], K_BAND, cum5)
        fw3, md3, sk3, _ = outcomes(O, C, t3, c3)
        ok3 = np.isfinite(fw3)
        m3 = cell_monthly(dates, t3[ok3], d3[ok3], f3[ok3], fw3[ok3], md3[ok3], sk3[ok3])
        plac.append(summarize(m3))
        if (b + 1) % 5 == 0:
            print(f"      placebo {b+1}/{N_PLACEBO}", flush=True)
    psum = {
        "up_minus_dn_bp": round(float(np.mean([p["up_minus_dn_bp"] for p in plac])), 1),
        "up_minus_dn_t": round(float(np.mean([p["up_minus_dn_t"] for p in plac])), 2),
        "slow_minus_fast_bp": round(float(np.mean([p["slow_minus_fast_bp"] for p in plac])), 1),
        "slow_minus_fast_t": round(float(np.mean([p["slow_minus_fast_t"] for p in plac])), 2),
        "spread_bp": round(float(np.mean([p["spread_bp"] for p in plac])), 1),
    }
    true_ud = summary["primary"]["up_minus_dn_bp"]
    true_sf = summary["primary"]["slow_minus_fast_bp"]
    psum["ud_percentile"] = round(float(np.mean([p["up_minus_dn_bp"] < true_ud for p in plac]) * 100), 1)
    psum["sf_percentile"] = round(float(np.mean([p["slow_minus_fast_bp"] < true_sf for p in plac]) * 100), 1)
    summary["placebo"] = psum

    pr = summary["primary"]
    c_ = pr["cells"]
    l1_signs = (c_["up_slow"]["mean_bp"] > 0 and c_["up_fast"]["mean_bp"] < 0 and
                c_["dn_slow"]["mean_bp"] > 0 and c_["dn_fast"]["mean_bp"] < 0)
    summary["verdict"] = {
        "L1_signs": bool(l1_signs),
        "L1_spread_ok": bool(pr["spread_bp"] >= 80),
        "L1_pass": bool(l1_signs and pr["spread_bp"] >= 80),
        "L2_pass": bool(pr["up_minus_dn_bp"] < 0 and pr["up_minus_dn_t"] <= -2),
        "L2_direction": "up<dn" if pr["up_minus_dn_bp"] < 0 else "up>=dn",
        "L3_placebo_retention_ud": round(float((psum["up_minus_dn_bp"] / true_ud) if true_ud != 0 else np.nan), 2),
        "L3_placebo_retention_sf": round(float((psum["slow_minus_fast_bp"] / true_sf) if true_sf != 0 else np.nan), 2),
        "L3_zone_real": bool(abs(psum["up_minus_dn_bp"]) < abs(true_ud) * 0.3),
    }
    (OUT / "h3.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({kk: summary[kk] for kk in ("sample", "regression", "placebo", "verdict")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
