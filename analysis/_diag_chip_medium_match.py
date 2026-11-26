"""卷二审计（K6/K7）：匹配孪生（IPW + 最近邻）+ 条件效应的泛函。

预注册（先写死，再看数）
------------------------
上一轮用线性联合控制把 dens 的主效应打到 −4bp，那只证明「线性可解释部分」被控制掉。
这一轮做两件非参数的事。

K6 匹配孪生
  全样本与条件样本（TO15 Q5 ∩ vol20 Q5，约 12%）各跑同一个估计量。
  主估计量：每日 logit 倾向得分 + ATT 逆概率加权（对照权重 p/(1−p)，2% 截尾），
            协变量 11 个（市值/换手/距离/波动/动量/反转/宽度/尾盘/流动性/区间/最大单日），
            收益先按 date×行业 去均值。
  稳健性：1:1 最近邻（无放回，卡尺 2.0，逐维 0.5 sd），报告匹配率。
  并列报相邻分位阶梯 Q1→Q2 / Q2→Q3 / Q3→Q4 / Q4→Q5，看单调性。
  通过线：条件样本 Q5 比 Q1 低 ≥60bp、|t| ≥ 2、加权后 max|SMD| < 0.1；
         全样本应显著缩小（若与单变量 +136bp 差不多，说明匹配没起作用）。
  负控：把分位标签在日内打乱 20 次，看真实值落在安慰剂分布的哪个位置。

K7 泛函
  条件样本内把 S(p) 除以 ΣTO 归一，取 6 个形状泛函：
    L  中心对数密度   SL 对数斜率   CV 对数曲率
    MD 到众数距离     W  局部宽度   AS 上下不对称（±10%）
  先看相关矩阵，再联合回归（6 个 + log TO + log vol），
  最后对每个泛函做 IPW 匹配（控制其余 5 个 + TO/vol/cap）。
  通过线：联合回归 |t| ≥ 2 且匹配后同号、|Δ| ≥ 40bp。

口径：fwd20=close[t+21]/open[t+1]−1，超额减当日池内等权再减 date×行业 均值，t 按日聚合。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PANEL = DATA / ".scratch" / "chip_medium" / "panel.parquet"
ENT = DATA / ".scratch" / "chip_entropy_local" / "part.parquet"
INDUSTRY = DATA / "ext_data" / "ext_hy_ths" / "part.parquet"
OUT = DATA / ".scratch" / "chip_medium" / "match.json"

CONTROLS = ["ln_mcap", "to15", "absg", "vol20", "ret20", "ret5", "ivd15", "tc15",
            "amihud", "range15", "maxret20"]
FUNCS = ["L", "SL", "CV", "MD", "W", "AS"]
SQRT2PI = math.sqrt(2 * math.pi)


# ---------------------------------------------------------------- 基础
def qcut_vec(x: np.ndarray, k: int, min_n: int = 40) -> np.ndarray:
    out = np.full(len(x), -1, dtype=np.int8)
    m = np.isfinite(x); n = int(m.sum())
    if n < min_n:
        return out
    r = np.argsort(np.argsort(x[m], kind="stable"), kind="stable")
    out[m] = np.minimum((r * k // n).astype(np.int8), k - 1)
    return out


def qcut_mat(A: np.ndarray, k: int, min_n: int = 40) -> np.ndarray:
    out = np.full(A.shape, -1, dtype=np.int8)
    for i in range(A.shape[0]):
        out[i] = qcut_vec(A[i], k, min_n)
    return out


def nw_t(x: np.ndarray, lag: int = 20) -> float:
    """Newey-West t（Bartlett 核）。重叠 20 日收益窗必须用它，日聚合 t 会高估约 √20 倍。"""
    x = x[np.isfinite(x)]; n = len(x)
    if n < 3 * lag:
        return float("nan")
    mu = x.mean(); e = x - mu
    g0 = (e @ e) / n
    s = g0
    for l in range(1, lag + 1):
        gl = (e[l:] @ e[:-l]) / n
        s += 2 * (1 - l / (lag + 1)) * gl
    se = math.sqrt(max(s, 0) / n)
    return float(mu / se) if se > 0 else float("nan")


def dstats(x: np.ndarray) -> tuple[float, float, int]:
    x = x[np.isfinite(x)]; n = len(x)
    if n < 8:
        return float("nan"), float("nan"), n
    m = float(x.mean()); se = float(x.std(ddof=1) / math.sqrt(n))
    return m, (m / se if se > 0 else float("nan")), n


def zrow(x: np.ndarray) -> np.ndarray:
    mu = np.nanmean(x); sd = np.nanstd(x)
    return (x - mu) / sd if sd > 0 else np.zeros_like(x)


def zcols(X: np.ndarray, m: np.ndarray) -> np.ndarray:
    mu = np.nanmean(X[m], axis=0); sd = np.nanstd(X[m], axis=0)
    return (X - mu) / np.where(sd > 0, sd, np.nan)


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 1e-2, iters: int = 30) -> np.ndarray:
    """牛顿法，X 已标准化；L2 防止完全分离。"""
    X1 = np.column_stack([np.ones(len(X)), X])
    b = np.zeros(X1.shape[1])
    I = np.eye(X1.shape[1]); I[0, 0] = 0
    for _ in range(iters):
        p = sigmoid(X1 @ b)
        W = np.maximum(p * (1 - p), 1e-6)
        g = X1.T @ (y - p) - l2 * I @ b
        H = (X1 * W[:, None]).T @ X1 + l2 * I
        step = np.linalg.solve(H, g)
        b += step
        if np.abs(step).max() < 1e-8:
            break
    return b


def match_pairs(Xt, Xc, caliper=2.0, dim_cal=0.5):
    if len(Xt) == 0 or len(Xc) == 0:
        return np.array([], int), np.array([], int)
    diff = Xt[:, None, :] - Xc[None, :, :]
    d = np.sqrt((diff ** 2).sum(-1))
    feas = (np.abs(diff).max(-1) <= dim_cal) & (d <= caliper)
    order = np.argsort(feas.sum(1))
    used = np.zeros(len(Xc), bool)
    ti, ci = [], []
    for k in order:
        row = np.where(feas[k] & ~used, d[k], np.inf)
        j = int(np.argmin(row))
        if np.isfinite(row[j]):
            used[j] = True; ti.append(int(k)); ci.append(j)
    return np.array(ti, int), np.array(ci, int)


# ---------------------------------------------------------------- 两种估计量
def ipw_att(F, exc, mask, cell, q, hi, lo, cols=CONTROLS, trim=0.02, dsel=None):
    T = exc.shape[0]
    atts, smds, didx = [], {c: [] for c in cols}, []
    n_used = 0
    for i in range(T):
        if dsel is not None and not dsel[i]:
            continue
        m = mask[i] & cell[i] & np.isfinite(exc[i])
        if m.sum() < 80:
            continue
        Z = zcols(np.column_stack([F[c][i] for c in cols]), m)
        ok = np.isfinite(Z).all(1) & m
        t = ok & (q[i] == hi); c = ok & (q[i] == lo)
        if t.sum() < 30 or c.sum() < 30:
            continue
        Zt = Z[t]; Zc = Z[c]
        b = fit_logit(np.vstack([Zt, Zc]),
                      np.r_[np.ones(len(Zt)), np.zeros(len(Zc))])
        pc = sigmoid(np.column_stack([np.ones(len(Zc)), Zc]) @ b)
        w = pc / np.maximum(1 - pc, 1e-6)
        keep = (pc > trim) & (pc < 1 - trim)
        if keep.sum() < 20:
            continue
        Zc2, w2 = Zc[keep], w[keep]
        yt = exc[i, t].mean()
        yc = float((exc[i, c][keep] * w2).sum() / w2.sum())
        atts.append(yt - yc); didx.append(i)
        n_used += int(t.sum() + keep.sum())
        for k, cc in enumerate(cols):
            smds[cc].append(float(Zt[:, k].mean()
                                  - (Zc2[:, k] * w2).sum() / w2.sum()))
    m_, t_, nd = dstats(np.array(atts))
    return (m_, t_, nd, {c: float(np.mean(v)) for c, v in smds.items() if v},
            n_used, np.array(atts), np.array(didx))


def nn_att(F, exc, mask, cell, q, hi, lo, cols=CONTROLS, caliper=2.0, dim_cal=0.5, dsel=None):
    T = exc.shape[0]
    atts, n_t, n_m = [], 0, 0
    for i in range(T):
        if dsel is not None and not dsel[i]:
            continue
        m = mask[i] & cell[i] & np.isfinite(exc[i])
        if m.sum() < 80:
            continue
        Z = zcols(np.column_stack([F[c][i] for c in cols]), m)
        ok = np.isfinite(Z).all(1)
        ti_ = np.flatnonzero(m & (q[i] == hi) & ok)
        ci_ = np.flatnonzero(m & (q[i] == lo) & ok)
        if len(ti_) < 10 or len(ci_) < 10:
            continue
        ti, ci = match_pairs(Z[ti_], Z[ci_], caliper, dim_cal)
        n_t += len(ti_); n_m += len(ti)
        if len(ti) == 0:
            continue
        atts.append(float(np.mean(exc[i, ti_[ti]] - exc[i, ci_[ci]])))
    m_, t_, nd = dstats(np.array(atts))
    return m_, t_, nd, (n_m / n_t if n_t else float("nan")), n_m


def ladder(F, exc, mask, cell, q, label, mode="ipw", cols=CONTROLS, quiet=False):
    rows = []
    T = exc.shape[0]
    series = np.zeros(T); cnt = np.zeros(T)
    for a in range(4):
        hi, lo = a + 1, a
        if mode == "ipw":
            m_, t_, nd, smd, n_used, atts, didx = ipw_att(F, exc, mask, cell, q, hi, lo, cols)
            mr, npair = float("nan"), n_used
            series[didx] += atts; cnt[didx] += 1
        else:
            m_, t_, nd, mr, npair = nn_att(F, exc, mask, cell, q, hi, lo, cols)
            smd = None
        rows.append({"pair": f"Q{lo+1}->Q{hi+1}", "bp": m_ * 1e4, "t": t_,
                     "days": nd, "match_rate": mr, "n": npair, "smd": smd})
        if not quiet:
            extra = (f"匹配率={mr:.0%}" if mode == "nn"
                     else f"maxSMD={max(abs(v) for v in smd.values()):.2f}")
            print(f"  {label:10s} {rows[-1]['pair']:7s} {m_*1e4:+7.0f}bp  "
                  f"t={t_:+5.2f}  n={npair:>7,}  {extra}")
    tot = float(np.nansum([r["bp"] for r in rows]))
    full = cnt == 4
    ttot = float("nan"); tnw = float("nan")
    if full.sum() >= 60:
        x = series[full]
        ttot = float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))
        tnw = nw_t(x, 20)
    if not quiet:
        print(f"  {label:10s} 合计(Q5−Q1) {tot:+.0f}bp  t={ttot:+.2f}  "
              f"NW(20) t={tnw:+.2f}")
    return rows, tot, ttot, tnw


# ---------------------------------------------------------------- 泛函
def functionals(P, V, S, TOs, delta=0.12, npt=41):
    ok = np.isfinite(V) & np.isfinite(S) & np.isfinite(TOs) & (S > 0)
    V = np.where(ok, V, 0.0)
    S = np.where(ok, S, 1.0)
    TOs = np.where(ok, TOs, 0.0)
    n = len(P)
    g = np.linspace(-delta, delta, npt)
    h = g[1] - g[0]
    x = np.log(P)[:, None] + g[None, :]
    dens = np.zeros_like(x)
    for k in range(V.shape[1]):
        dens += (TOs[:, k:k+1] / S[:, k:k+1] / SQRT2PI
                 * np.exp(-0.5 * ((x - V[:, k:k+1]) / S[:, k:k+1]) ** 2))
    tw = np.maximum(TOs.sum(1), 1e-12)[:, None]
    dens = dens / tw
    out = {}
    L = np.log(np.maximum(dens, 1e-12))
    j0 = npt // 2
    out["L"] = L[:, j0]
    out["SL"] = (L[:, j0 + 1] - L[:, j0 - 1]) / (2 * h)
    out["CV"] = (L[:, j0 + 1] - 2 * L[:, j0] + L[:, j0 - 1]) / (h * h)
    out["MD"] = np.abs(g[np.argmax(dens, axis=1)])
    mass = np.maximum(dens.sum(1), 1e-12)
    mean = (dens * g[None, :]).sum(1) / mass
    var = (dens * (g[None, :] - mean[:, None]) ** 2).sum(1) / mass
    out["W"] = np.sqrt(np.maximum(var, 0))
    jm = g <= 0
    out["AS"] = np.log(np.maximum(dens[:, ~jm].sum(1), 1e-12)
                       / np.maximum(dens[:, jm].sum(1), 1e-12))
    return out


def spearman(x, y):
    rx = np.argsort(np.argsort(x)); ry = np.argsort(np.argsort(y))
    return float(np.corrcoef(rx, ry)[0, 1])


# ---------------------------------------------------------------- 主
def main() -> None:
    d = pl.read_parquet(PANEL).filter(
        pl.col("exc20").is_not_null() & (pl.col("ret1").abs() < 0.095)
        & (pl.col("age_days") >= 365) & (pl.col("is_limit_up").fill_null(0) == 0)
        & (pl.col("vol20") > 0) & (pl.col("to15") > 0) & (pl.col("dens") > 0))
    ind = (pl.read_parquet(INDUSTRY).select(["symbol", "所属同花顺行业"])
           .with_columns(pl.col("所属同花顺行业").str.split("-").list.first().alias("ind"))
           .select(["symbol", "ind"]).unique("symbol"))
    d = d.join(ind, on="symbol", how="left").with_columns(pl.col("ind").fill_null("NA"))
    dates = d["date"].unique().sort().to_list()
    syms = d["symbol"].unique().sort().to_list()
    di = {x: i for i, x in enumerate(dates)}; si = {s: i for i, s in enumerate(syms)}
    T, N = len(dates), len(syms)
    print(f"样本 {d.height:,} / {N} 只 / {T} 日")

    ii = d["date"].replace_strict(di, return_dtype=pl.Int32).to_numpy()
    jj = d["symbol"].replace_strict(si, return_dtype=pl.Int32).to_numpy()
    F = {}
    for c in CONTROLS + ["dens", "dens_fix", "exc20", "to"]:
        A = np.full((T, N), np.nan); A[ii, jj] = d[c].cast(pl.Float64).to_numpy(); F[c] = A
    inds = sorted(d["ind"].unique().to_list()); indc = {x: i for i, x in enumerate(inds)}
    F["ind"] = np.full((T, N), -1, np.int16)
    F["ind"][ii, jj] = d["ind"].replace_strict(indc, return_dtype=pl.Int16).to_numpy()

    mask = np.isfinite(F["exc20"])
    exc = F["exc20"].copy()
    for i in range(T):
        m = mask[i]
        if m.sum() < 50:
            continue
        codes = F["ind"][i, m]; vals = exc[i, m]; outv = vals.copy()
        for c in np.unique(codes):
            s = codes == c
            outv[s] = vals[s] - vals[s].mean()
        exc[i, m] = outv
    F["exc"] = exc

    qt = qcut_mat(np.where(mask, F["to15"], np.nan), 5)
    qv = qcut_mat(np.where(mask, F["vol20"], np.nan), 5)
    cell = mask & (qt == 4) & (qv == 4)
    print(f"条件样本 {int(cell.sum()):,}（{cell.sum()/mask.sum()*100:.1f}%）")
    out = {}

    res = np.full((T, N), np.nan)
    for i in range(T):
        m = cell[i]
        if m.sum() < 60:
            continue
        X = np.column_stack([np.ones(m.sum()), np.log(F["to15"][i, m]),
                             np.log(F["vol20"][i, m])])
        y = np.log(np.maximum(F["dens"][i, m], 1e-12))
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        res[i, m] = y - X @ b
    F["dens_res"] = res
    qr = qcut_mat(np.where(cell, res, np.nan), 5)
    qd = qcut_mat(np.where(mask, F["dens"], np.nan), 5)

    print("\n[K6] IPW 匹配 · 全样本（dens 原始分位）")
    out["all_ipw"], tot_all, t_all, tnw_all = ladder(F, exc, mask, mask, qd, "全样本", "ipw")
    out["all_total"] = {"bp": tot_all, "t": t_all, "nw": tnw_all}
    print("\n[K6] IPW 匹配 · 条件样本（dens 残差分位）")
    out["cell_ipw"], tot_cell, t_cell, tnw_cell = ladder(F, exc, mask, cell, qr, "条件样本", "ipw")
    out["cell_total"] = {"bp": tot_cell, "t": t_cell, "nw": tnw_cell}
    m_, t_, nd, smd, n_, atts, _ = ipw_att(F, exc, mask, cell, qr, 4, 0)
    print(f"  直接 Q5−Q1（单次 PS）：{m_*1e4:+.0f}bp  t={t_:+.2f}  n={n_:,}  "
          f"maxSMD={max(abs(v) for v in smd.values()):.2f}")
    out["cell_direct"] = {"bp": m_ * 1e4, "t": t_, "n": n_}
    yrs = np.array([x.year for x in dates])
    yr_rows = {}
    for y in sorted(set(yrs)):
        my, ty, ny, _, _, _, _ = ipw_att(F, exc, mask, cell, qr, 4, 0, dsel=(yrs == y))
        yr_rows[int(y)] = {"bp": my * 1e4, "t": ty, "days": ny}
        print(f"    {y}: {my*1e4:+7.0f}bp  t={ty:+5.2f}  days={ny}")
    out["cell_by_year"] = yr_rows

    print("\n[K6] 最近邻稳健性（条件样本）")
    out["cell_nn"], tot_nn, t_nn, tnw_nn = ladder(F, exc, mask, cell, qr, "条件样本", "nn")
    out["nn_total"] = {"bp": tot_nn, "t": t_nn, "nw": tnw_nn}

    print("\n  安慰剂：日内打乱分位标签 ×20（条件样本 Q5−Q1，IPW）")
    rng = np.random.default_rng(20261126)
    placebo = []
    for rep in range(20):
        qp = qr.copy()
        for i in range(T):
            idx = np.flatnonzero(cell[i] & (qr[i] >= 0))
            if len(idx) > 20:
                qp[i, idx] = rng.permutation(qr[i, idx])
        _, tot_p, _, _ = ladder(F, exc, mask, cell, qp, f"perm{rep}", "ipw", quiet=True)
        placebo.append(float(tot_p))
    placebo = np.array(placebo)
    actual = float(tot_cell)
    print(f"  真实 {actual:+.0f}bp；安慰剂 均值 {placebo.mean():+.0f} "
          f"sd {placebo.std(ddof=1):.0f}  p05 {np.quantile(placebo,.05):+.0f} "
          f"p95 {np.quantile(placebo,.95):+.0f} | 分位 {np.mean(placebo<=actual):.2f}")
    out["placebo"] = {"actual": actual, "mean": float(placebo.mean()),
                      "sd": float(placebo.std(ddof=1)), "draws": placebo.tolist()}

    # ---------------- K7
    print("\n[K7] 条件样本内的泛函")
    ent = pl.read_parquet(ENT, columns=["symbol", "date", "vwap", "ivd_lo"])
    ent = ent.filter((pl.col("ivd_lo") > 0) & (pl.col("ivd_lo") < 0.15))
    ei = ent["date"].replace_strict(di, return_dtype=pl.Int32, default=-1).to_numpy()
    ej = ent["symbol"].replace_strict(si, return_dtype=pl.Int32, default=-1).to_numpy()
    okrow = (ei >= 0) & (ej >= 0)
    VW = np.full((T, N), np.nan); IV = np.full((T, N), np.nan)
    VW[ei[okrow], ej[okrow]] = ent["vwap"].to_numpy()[okrow]
    IV[ei[okrow], ej[okrow]] = ent["ivd_lo"].to_numpy()[okrow]
    LV = np.log(np.where(VW > 0, VW, np.nan))

    fn = {k: np.full((T, N), np.nan) for k in FUNCS}
    used = 0
    for i in range(15, T):
        m = cell[i] & np.isfinite(LV[i]) & np.isfinite(F["to"][i])
        idx = np.flatnonzero(m)
        if len(idx) < 40:
            continue
        VV = np.stack([LV[i - k] for k in range(15)], axis=1)[idx]
        SS = np.stack([IV[i - k] for k in range(15)], axis=1)[idx]
        TT = np.stack([F["to"][i - k] for k in range(15)], axis=1)[idx]
        good = (np.isfinite(VV) & np.isfinite(SS) & np.isfinite(TT)).sum(1) >= 10
        if good.sum() < 30:
            continue
        idx = idx[good]
        f_ = functionals(np.exp(LV[i, idx]), VV[good], SS[good], TT[good])
        for k in FUNCS:
            fn[k][i, idx] = f_[k]
        used += len(idx)
    print(f"  可算泛函 {used:,} 股票-日（条件样本 {int(cell.sum()):,}）")
    F.update(fn)

    print("\n  相关矩阵（Spearman）")
    print("        " + "".join(f"{x:>7s}" for x in FUNCS))
    cors = {}
    for a in FUNCS:
        row = []
        for b in FUNCS:
            m = cell & np.isfinite(F[a]) & np.isfinite(F[b])
            v = spearman(F[a][m], F[b][m]) if m.sum() > 1000 else float("nan")
            row.append(v)
        cors[a] = row
        print(f"  {a:6s}" + "".join(f"{v:+7.2f}" for v in row))
    out["corr"] = {"funcs": FUNCS, "matrix": cors}

    print("\n  单变量五分位（条件样本内，Q1−Q5）")
    uni = {}
    for k in FUNCS:
        q = qcut_mat(np.where(cell, F[k], np.nan), 5, 60)
        dd = []
        for i in range(T):
            a = np.flatnonzero(q[i] == 0); b = np.flatnonzero(q[i] == 4)
            if len(a) > 15 and len(b) > 15:
                dd.append(np.nanmean(exc[i, a]) - np.nanmean(exc[i, b]))
        m_, t_, nd = dstats(np.array(dd))
        uni[k] = {"bp": m_ * 1e4, "t": t_}
        print(f"    {k:3s} {m_*1e4:+7.0f}bp  t={t_:+5.2f}  days={nd}")
    out["univariate"] = uni

    print("\n  联合回归（6 泛函 + log TO + log vol，bp/1sd）")
    coefs = []
    for i in range(T):
        m = cell[i].copy()
        for k in FUNCS + ["to15", "vol20"]:
            m = m & np.isfinite(F[k][i]) & np.isfinite(exc[i])
        if m.sum() < 80:
            continue
        cols = [np.ones(N)]
        for k in FUNCS:
            cols.append(zrow(np.where(m, F[k][i], np.nan)))
        cols.append(zrow(np.where(m, np.log(F["to15"][i]), np.nan)))
        cols.append(zrow(np.where(m, np.log(F["vol20"][i]), np.nan)))
        X = np.column_stack(cols)[m]
        y = exc[i, m]
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        coefs.append(b)
    C = np.array(coefs)
    names = ["const"] + FUNCS + ["logTO", "logVOL"]
    joint = {}
    for j, nm in enumerate(names):
        x = C[:, j]; x = x[np.isfinite(x)]
        tt = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
        joint[nm] = {"bp": float(x.mean() * 1e4), "t": float(tt)}
        if nm != "const":
            print(f"    {nm:7s} {x.mean()*1e4:+7.1f}bp  t={tt:+6.2f}")
    out["joint"] = joint

    print("\n  泛函 IPW 匹配（控制其余 5 个 + logTO + logVOL + logcap）")
    fm = {}
    for k in FUNCS:
        ctrl = [f"__{o}" for o in FUNCS if o != k] + ["__to15", "__vol20", "__cap"]
        G = dict(F)
        G["__to15"] = np.log(F["to15"]); G["__vol20"] = np.log(F["vol20"])
        G["__cap"] = F["ln_mcap"]
        for o in FUNCS:
            G[f"__{o}"] = F[o]
        q = qcut_mat(np.where(cell, F[k], np.nan), 5, 60)
        rows, tot, tot_t, tot_nw = ladder(G, exc, mask, cell, q, k, "ipw", cols=ctrl, quiet=True)
        last = rows[-1] if rows else {"t": float("nan"), "smd": {}}
        mx = max((abs(v) for v in (last.get("smd") or {}).values()), default=float("nan"))
        fm[k] = {"bp": tot, "t": tot_t, "nw": tot_nw, "t_last": last["t"], "max_smd": mx}
        print(f"    {k:3s} Q5−Q1 {tot:+7.0f}bp  t={tot_t:+5.2f}  NW={tot_nw:+5.2f}  maxSMD={mx:.2f}")
    out["func_match"] = fm

    # ---- 泛函的最终裁决：L 在完整 11 控下能不能活
    print("\n  [K6b] L / q15 在完整 11 控下的直接 ATT")
    out["L_full_ctrl"] = {}
    for k in ["L", "CV", "AS"]:
        q = qcut_mat(np.where(cell, F[k], np.nan), 5, 60)
        m_, t_, nd, smd, n_, atts, _ = ipw_att(F, exc, mask, cell, q, 4, 0)
        mx = max(abs(v) for v in smd.values())
        print(f"    {k:3s} Q5−Q1 {m_*1e4:+7.0f}bp  t={t_:+5.2f}  maxSMD={mx:.2f}  n={n_:,}")
        out["L_full_ctrl"][k] = {"bp": m_ * 1e4, "t": t_, "max_smd": mx}
        if k == "L":
            yr = {}
            for y in sorted(set(yrs)):
                my, ty, ny, _, _, _, _ = ipw_att(F, exc, mask, cell, q, 4, 0, dsel=(yrs == y))
                yr[int(y)] = {"bp": my * 1e4, "t": ty}
            out["L_by_year"] = yr
            print("        分年: " + "  ".join(f"{y}:{v['bp']:+.0f}" for y, v in yr.items()))

    ii2, jj2 = np.nonzero(cell & np.isfinite(F["L"]))
    df = pl.DataFrame({"date": [dates[i] for i in ii2], "symbol": [syms[j] for j in jj2],
                       **{k: F[k][ii2, jj2] for k in FUNCS + ["dens", "dens_res", "to15",
                                                              "vol20", "ln_mcap", "exc"]},
                       "q_dens_res": qr[ii2, jj2]})
    df.write_parquet(DATA / ".scratch" / "chip_medium" / "cell_funcs.parquet")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float),
                   encoding="utf-8")
    print(f"\n写出 {OUT}")


if __name__ == "__main__":
    main()
