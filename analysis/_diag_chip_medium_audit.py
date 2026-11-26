"""卷二审计（A–E）：联合控制 / tc_close / 固定核宽 / 市值分层 / Theta 的增量。

读 `data/.scratch/chip_medium/panel.parquet`（由 `_diag_chip_medium_verify.py --step build` 生成）。
所有 t 值按日聚合：先算每日截面统计量，再对日序列求 t。

预注册判据（写在这里，避免事后改口）
------------------------------------
K1 联合控制：dens 对 11 个控制变量做横截面回归后的**残差**，Q1−Q5 ≥ +50bp 且 t ≥ 2 才算活着；
   < +30bp 或 t < 1 ⇒ 卷二的「四重控制」是单变量控制的假存活。
K2 tc_close：dens 与 tc15 的截面 Spearman ≥ 0.5 ⇒ 重命名；控制 tc15 后残差 Q1−Q5 < +50bp ⇒ 死亡。
K3 核宽：固定核宽后 Q1−Q5 保留 ≥ 70% ⇒ 不是 1/ivd 的伪影。
K4 P1：递推 dens 的市值 Q5 组 Q1−Q5 ≥ +80bp 且 t ≥ 1.5。
K5 Theta：同 cum5 桶内 Q5−Q1 ≥ +30bp 且 t ≥ 2 才算 P2 成立。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / ".scratch" / "chip_medium" / "panel.parquet"
OUT = ROOT / "data" / ".scratch" / "chip_medium" / "audit.json"

CONTROLS = ["ln_mcap", "to15", "absg", "vol20", "ret20", "ret5", "ivd15", "tc15",
            "amihud", "range15", "maxret20"]


# ---------------------------------------------------------------- 矩阵工具
def qcut(A: np.ndarray, k: int = 5) -> np.ndarray:
    T, N = A.shape
    out = np.full((T, N), -1, dtype=np.int8)
    for i in range(T):
        row = A[i]; m = np.isfinite(row); n = int(m.sum())
        if n < 50:
            continue
        r = np.argsort(np.argsort(row[m], kind="stable"), kind="stable")
        out[i, m] = np.minimum((r * k // n).astype(np.int8), k - 1)
    return out


def daily_stats(x: np.ndarray, min_n: int = 20):
    """x: T×N 的收益矩阵 + 选择掩码由调用方给。这里接收 T 维日序列。"""
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return float("nan"), float("nan"), n
    m = float(x.mean()); se = float(x.std(ddof=1) / math.sqrt(n))
    return m, (m / se if se > 0 else float("nan")), n


def bucket_daily_mean(sel: np.ndarray, exc: np.ndarray, min_n: int = 20) -> np.ndarray:
    cnt = sel.sum(axis=1).astype(float)
    s = np.where(sel, exc, 0.0).sum(axis=1)
    return np.where(cnt >= min_n, s / np.maximum(cnt, 1.0), np.nan)


def spread_daily(sel_hi: np.ndarray, sel_lo: np.ndarray, exc: np.ndarray,
                 min_n: int = 20) -> np.ndarray:
    a = bucket_daily_mean(sel_hi, exc, min_n)
    b = bucket_daily_mean(sel_lo, exc, min_n)
    return a - b


def zscore_date(A: np.ndarray) -> np.ndarray:
    m = np.nanmean(A, axis=1, keepdims=True)
    s = np.nanstd(A, axis=1, keepdims=True)
    return (A - m) / np.where(s > 0, s, np.nan)


def winsor_date(A: np.ndarray, p: float = 0.01) -> np.ndarray:
    lo = np.nanquantile(A, p, axis=1, keepdims=True)
    hi = np.nanquantile(A, 1 - p, axis=1, keepdims=True)
    return np.clip(A, lo, hi)


# ---------------------------------------------------------------- 主
def main() -> None:
    d = pl.read_parquet(PANEL).filter(
        pl.col("exc20").is_not_null() & (pl.col("ret1").abs() < 0.095)
        & (pl.col("age_days") >= 365) & (pl.col("is_limit_up").fill_null(0) == 0))
    dates = d["date"].unique().sort().to_list()
    syms = d["symbol"].unique().sort().to_list()
    di = {x: i for i, x in enumerate(dates)}; si = {s: i for i, s in enumerate(syms)}
    T, N = len(dates), len(syms)
    print(f"样本 {d.height:,} 股票-日 / {N} 只 / {T} 日 / {dates[0]}~{dates[-1]}")

    cols = ["dens", "dens_k", "dens_fix", "dens_rec", "dens_up", "dens_dn", "q15",
            "to15", "to5", "ivd15", "tc15", "tc_close", "absg", "g", "ret1", "ret5",
            "ret20", "vol20", "maxret20", "range15", "amihud", "ln_mcap", "mcap",
            "fwd20", "THETA", "D0", "PI", "cum5"]
    ii = d["date"].replace_strict(di, return_dtype=pl.Int32).to_numpy()
    jj = d["symbol"].replace_strict(si, return_dtype=pl.Int32).to_numpy()
    F = {}
    for c in cols:
        A = np.full((T, N), np.nan)
        A[ii, jj] = d[c].cast(pl.Float64).to_numpy()
        F[c] = A
    mask = np.isfinite(F["fwd20"])
    # 池内超额（用同一个 mask 重新算，避免口径漂移）
    fwd = F["fwd20"]
    F["exc"] = np.where(mask, fwd - np.nanmean(np.where(mask, fwd, np.nan), axis=1, keepdims=True), np.nan)
    exc = F["exc"]
    print(f"有效 {int(mask.sum()):,} 股票-日")

    out = {}

    def rec(key, obj):
        out[key] = obj
        return obj

    def show(name, res):
        print(f"\n{name}")
        if "buckets" in res:
            print("  " + "  ".join(f"Q{b['q']}={b['bp']:+.0f}(t{b['t']:.2f})"
                                   for b in res["buckets"]))
        if "spread" in res:
            print(f"  Δ={res['spread']:+.0f}bp  t={res['t']:.2f}  n={res['n']:,} days={res['nd']}")

    def simple_sort(var, label, k=5, sub=None):
        A = F[var] if sub is None else np.where(sub, F[var], np.nan)
        q = qcut(np.where(mask, A, np.nan), k)
        res = {"buckets": [], "n": int((q >= 0).sum())}
        for b in range(k):
            m, t, nd = daily_stats(bucket_daily_mean(q == b, exc))
            res["buckets"].append({"q": b + 1, "bp": m * 1e4, "t": t, "nd": nd})
        m, t, nd = daily_stats(spread_daily(q == 0, q == k - 1, exc))
        res.update({"spread": m * 1e4, "t": t, "nd": nd})
        rec(label, res); show(label, res)
        return res

    # ---------------- 0 基线
    print("\n" + "=" * 70)
    simple_sort("dens", "dens_band15")
    simple_sort("dens_k", "dens_kernel15")
    simple_sort("dens_fix", "dens_fixedwidth")
    simple_sort("dens_rec", "dens_recursive")
    simple_sort("q15", "q15_share")
    simple_sort("to15", "to15")
    simple_sort("absg", "absg")

    # ---------------- A 联合控制
    print("\n" + "=" * 70 + "\n[A] 联合控制")
    ctrl = [F[c] for c in CONTROLS]
    # 残差 dens：逐日 OLS dens ~ controls
    resid = np.full((T, N), np.nan)
    betas = []
    Xs = np.stack([zscore_date(winsor_date(c)) for c in ctrl], axis=2)  # T×N×k
    yd = zscore_date(F["dens"])
    for i in range(T):
        m = mask[i] & np.isfinite(yd[i]) & np.isfinite(Xs[i]).all(axis=1)
        if m.sum() < 200:
            continue
        X = Xs[i, m]; y = yd[i, m]
        X = np.column_stack([np.ones(len(X)), X])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        res_i = y - X @ beta
        resid[i, m] = res_i
        betas.append(beta)
    F["dens_resid"] = resid
    simple_sort("dens_resid", "dens_resid_11controls")

    # Fama-MacBeth：exc ~ dens_z + controls_z
    coefs = []
    for i in range(T):
        m = mask[i] & np.isfinite(yd[i]) & np.isfinite(Xs[i]).all(axis=1)
        if m.sum() < 200:
            continue
        X = np.column_stack([np.ones(m.sum()), yd[i, m], Xs[i, m]])
        y = winsor_date(exc)[i, m]
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        coefs.append(beta)
    C = np.array(coefs)
    names = ["const", "dens_z"] + CONTROLS
    fm = {}
    print("  Fama-MacBeth（bp/1sd，日聚合 t）")
    for j, nm in enumerate(names):
        x = C[:, j]
        t = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
        fm[nm] = {"coef_bp": float(x.mean() * 1e4), "t": float(t)}
        print(f"    {nm:10s} {x.mean()*1e4:+8.1f}bp  t={t:+6.2f}")
    rec("fama_macbeth", fm)

    # ---------------- B tc_close
    print("\n" + "=" * 70 + "\n[B] tc_close")
    # 截面 Spearman(dens, tc15)
    rhos = []
    for i in range(T):
        m = mask[i] & np.isfinite(F["dens"][i]) & np.isfinite(F["tc15"][i])
        if m.sum() < 200:
            continue
        a = F["dens"][i, m]; b = F["tc15"][i, m]
        ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b))
        rhos.append(float(np.corrcoef(ra, rb)[0, 1]))
    print(f"  Spearman(dens, tc15) = {np.mean(rhos):+.3f}  (sd {np.std(rhos):.3f})")
    rec("rho_dens_tc15", {"mean": float(np.mean(rhos)), "sd": float(np.std(rhos))})

    # 控制 tc15 后的残差
    resid_tc = np.full((T, N), np.nan)
    for i in range(T):
        m = mask[i] & np.isfinite(F["dens"][i]) & np.isfinite(F["tc15"][i])
        if m.sum() < 200:
            continue
        x = F["tc15"][i, m]; y = F["dens"][i, m]
        X = np.column_stack([np.ones(len(x)), x])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid_tc[i, m] = y - X @ beta
    F["dens_tc"] = resid_tc
    simple_sort("dens_tc", "dens_resid_tc15_only")

    # 在 tc15 五分位内做 dens 双排序
    qtc = qcut(np.where(mask, F["tc15"], np.nan), 5)
    rows = []
    for b in range(5):
        A = np.where(qtc == b, F["dens"], np.nan)
        q = qcut(np.where(mask & (qtc == b), A, np.nan), 5)
        m, t, nd = daily_stats(spread_daily(q == 0, q == 4, exc))
        rows.append({"tc_q": b + 1, "spread": m * 1e4, "t": t})
        print(f"  tc15 Q{b+1}: dens Q1-Q5 = {m*1e4:+.0f}bp t={t:.2f}")
    rec("dens_within_tc15", rows)

    # ---------------- A2 逐个控制（复现卷二的四重表）
    print("\n" + "=" * 70 + "\n[A2] 单变量控制（复现卷二的表）")
    ctrl_map = {"absg": "|g|", "vol20": "vol20", "ret20": "ret20", "to15": "TO15",
                "ivd15": "ivd", "ln_mcap": "logcap", "tc15": "tc15", "range15": "range15"}
    grid = []
    for c, lab in ctrl_map.items():
        row = []
        qc = qcut(np.where(mask, F[c], np.nan), 5)
        for b in range(5):
            A = np.where(qc == b, F["dens"], np.nan)
            q = qcut(np.where(mask & (qc == b), A, np.nan), 5)
            m, t, nd = daily_stats(spread_daily(q == 0, q == 4, exc))
            row.append({"ctrl_q": b + 1, "spread": m * 1e4, "t": t})
        grid.append({"ctrl": lab, "cells": row})
        print(f"  {lab:8s} " + "  ".join(f"{c['spread']:+6.0f}(t{c['t']:4.1f})" for c in row))
    rec("dual_sort_grid", grid)

    # ---------------- K3 核宽
    print("\n" + "=" * 70 + "\n[K3] 核宽伪影：dens_fix / dens 的保留率")
    b0 = out["dens_band15"]["spread"]; b1 = out["dens_fixedwidth"]["spread"]
    print(f"  {b0:+.0f} -> {b1:+.0f}bp  保留 {b1/b0*100:.0f}%")
    rec("kernel_retention", {"base": b0, "fixed": b1, "retention": b1 / b0})

    # ---------------- K4 市值分层与递推
    print("\n" + "=" * 70 + "\n[K4] 市值分层（P1）")
    qm = qcut(np.where(mask, F["ln_mcap"], np.nan), 5)
    for var in ["dens", "dens_rec"]:
        rows = []
        for b in range(5):
            A = np.where(qm == b, F[var], np.nan)
            q = qcut(np.where(mask & (qm == b), A, np.nan), 5)
            m, t, nd = daily_stats(spread_daily(q == 0, q == 4, exc))
            rows.append({"cap_q": b + 1, "spread": m * 1e4, "t": t, "nd": nd})
            print(f"  {var:8s} cap Q{b+1}: {m*1e4:+.0f}bp t={t:.2f}")
        rec(f"{var}_by_cap", rows)

    # ---------------- E Theta
    print("\n" + "=" * 70 + "\n[E] Theta")
    th = F["THETA"]; cu = F["cum5"]
    mth = mask & np.isfinite(th) & (th < 50) & np.isfinite(cu) & (cu > 0)
    print(f"  可算 {int(mth.sum()):,}")
    # Spearman
    def sp(a, b, m):
        x = a[m]; y = b[m]
        if len(x) < 1000:
            return float("nan")
        rx = np.argsort(np.argsort(x)); ry = np.argsort(np.argsort(y))
        return float(np.corrcoef(rx, ry)[0, 1])
    print(f"  Spearman(Theta, cum5) = {sp(th, cu, mth):+.3f}")
    print(f"  Spearman(Theta, to15) = {sp(th, F['to15'], mth):+.3f}")
    print(f"  Theta 分位: " + "  ".join(
        f"p{int(p*100)}={np.nanquantile(th[mth], p):.2f}" for p in (.1, .25, .5, .75, .9)))
    # 分桶
    edges = [(0, .01), (.01, .03), (.03, .06), (.06, .10), (.10, .20), (.20, .50)]
    tab = []
    for lo, hi in edges:
        s = mth & (cu >= lo) & (cu < hi)
        if s.sum() < 500:
            continue
        q = qcut(np.where(s, th, np.nan), 5)
        m, t, nd = daily_stats(spread_daily(q == 4, q == 0, exc))  # Q5-Q1：Theta 高更好
        med = float(np.nanmedian(th[s]))
        line = {"cum5": [lo, hi], "n": int(s.sum()), "theta_med": med,
                "q5_minus_q1": m * 1e4, "t": t}
        # 桶内每档均值
        line["buckets"] = [{"q": b + 1,
                            "bp": daily_stats(bucket_daily_mean(q == b, exc))[0] * 1e4}
                           for b in range(5)]
        tab.append(line)
        print(f"  cum5 {lo:.0%}-{hi:.0%} n={s.sum():>7,} Th={med:.2f} "
              f"Q5-Q1={m*1e4:+.0f}bp t={t:+.2f}  " +
              " ".join(f"{x['bp']:+.0f}" for x in line["buckets"]))
    rec("theta_by_cum5", tab)

    # Theta 在控制 to15 / cum5 后还有没有增量
    resid_th = np.full((T, N), np.nan)
    for i in range(T):
        m = mth[i] & np.isfinite(F["to15"][i])
        if m.sum() < 200:
            continue
        y = np.log(np.maximum(th[i, m], 1e-6))
        X = np.column_stack([np.ones(m.sum()), zscore_date(F["to15"])[i, m],
                             zscore_date(F["cum5"])[i, m]])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid_th[i, m] = y - X @ beta
    F["theta_resid"] = resid_th
    q = qcut(np.where(mth, resid_th, np.nan), 5)
    m, t, nd = daily_stats(spread_daily(q == 4, q == 0, exc))
    print(f"  Theta 残差（控 to15+cum5）Q5-Q1 = {m*1e4:+.0f}bp t={t:.2f}")
    rec("theta_resid", {"spread": m * 1e4, "t": t})

    # ---------------- P5 上下不对称
    print("\n" + "=" * 70 + "\n[P5] dens_up vs dens_dn")
    for v in ["dens_up", "dens_dn"]:
        simple_sort(v, v)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float),
                   encoding="utf-8")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
