"""OS/FE → 未来 60 日被拉升概率：同日/同行业/同市值孪生匹配 + 块自助 + 容量裁决。

严格按 `docs/osfe-prereg.md`：主判据一格（pump30 × OS&FE 双 Q5 × 全匹配），
其余为探索性。所有口径数字由本脚本打印，不手工抄写。

用法：python analysis/_test_osfe_pump.py [--reps 2000]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_osfe"
ZCOLS = ["log_mcap", "r20", "r60", "to20", "amp20", "dist_hi250"]
OUTCOMES = {"pump30": 0.30, "pump20": 0.20, "pump50": 0.50}
EVAL_STEP = 5


# ---------------------------------------------------------------- 数据准备
def load_panel() -> pd.DataFrame:
    p = pl.read_parquet(str(OUT / "panel.parquet")).drop(["is_st", "st_known"])
    # ST 在读取时 join：拉取可断点续跑，面板不必重建
    st_path = ROOT / "data" / "_st_hist" / "st_days.parquet"
    cov_path = ROOT / "data" / "_st_hist" / "st_covered.parquet"
    if st_path.exists():
        st = pl.read_parquet(str(st_path)).with_columns(pl.lit(True).alias("is_st"))
        cov = pl.read_parquet(str(cov_path)).with_columns(pl.lit(True).alias("st_known"))
        p = p.join(st, on=["symbol", "date"], how="left").join(cov, on="symbol", how="left")
        p = p.with_columns(pl.col("is_st").fill_null(False),
                           pl.col("st_known").fill_null(False))
    else:
        p = p.with_columns(pl.lit(False).alias("is_st"), pl.lit(False).alias("st_known"))
    df = p.to_pandas()
    df["mcap_f"] = df["mcap_f"].clip(lower=1e5)
    df["log_mcap"] = np.log(df["mcap_f"])
    df["log_adv"] = np.log(df["adv20"].clip(lower=1e4))
    for name, thr in OUTCOMES.items():
        df[name] = np.where(df["fwd_max60"].notna(),
                            (df["fwd_max60"] >= thr).astype(float), np.nan)
    df["fast30"] = np.where(df["fwd_fast"].notna(),
                            (df["fwd_fast"] >= 0.30).astype(float), np.nan)
    df["industry"] = df["industry"].fillna("NA")
    return df


def base_sample(df: pd.DataFrame) -> pd.DataFrame:
    need = ["os", "fe", "r20", "r60", "to20", "amp20", "dist_hi250", "mcap_f", "adv20"]
    m = df["full_window"].fillna(False).to_numpy().astype(bool).copy()
    for c in need:
        m &= df[c].notna().to_numpy()
    m &= (df["to20"].to_numpy() > 0) & (df["mcap_f"].to_numpy() > 0)
    m &= df["pump30"].notna().to_numpy()
    out = df.loc[m].copy()
    # 日期压缩为连续整数，供块自助使用
    out["dpos"] = out["date"].rank(method="dense").astype(int) - 1
    out = out.sort_values(["dpos", "symbol"]).reset_index(drop=True)
    return out


def add_ranks(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("dpos", sort=False)
    for col in ("os", "fe", "adv20", "profit", "to20", "amp20"):
        df[f"{col}_q"] = g[col].rank(pct=True, method="average") * 5
        df[f"{col}_q"] = np.ceil(df[f"{col}_q"]).clip(1, 5).astype(int)
    for c in ZCOLS:
        x = df[c].to_numpy(dtype=float)
        # 按日标准化，切片内截尾 ±3
        s = df.groupby("dpos", sort=False)[c]
        mu = s.transform("mean").to_numpy()
        sd = s.transform("std").to_numpy()
        z = (x - mu) / np.where(sd > 0, sd, 1.0)
        df[f"z_{c}"] = np.clip(z, -3, 3)
    return df


# ---------------------------------------------------------------- 匹配
def build_pairs(df: pd.DataFrame, treat: np.ndarray, k: int = 3):
    """同日 × 同行业 内，按 6 维标准化特征取 k 近邻为孪生。

    返回 ev_row（处理组行号）、ctrl_row（n×k 对照行号，-1 表示不足）、
    以及每个事件用了哪个层级（1=同行业，0=同日兜底）。
    """
    Z = df[[f"z_{c}" for c in ZCOLS]].to_numpy(dtype=np.float32)
    ev, ctrl, lvl = [], [], []
    codes, _ = pd.factorize(df["industry"], sort=False)
    for d, idx in df.groupby("dpos", sort=False).indices.items():
        ti = idx[treat[idx]]
        if len(ti) == 0:
            continue
        ci = idx[~treat[idx]]
        for ind in np.unique(codes[ti]):
            t_sel = ti[codes[ti] == ind]
            c_sel = ci[codes[ci] == ind]
            if len(c_sel) < k:
                c_sel2 = ci  # 同日兜底
                level = 0
            else:
                c_sel2 = c_sel
                level = 1
            if len(c_sel2) == 0:
                continue
            A, B = Z[t_sel], Z[c_sel2]
            D = ((A[:, None, :] - B[None, :, :]) ** 2).sum(-1)
            kk = min(k, len(c_sel2))
            nn = np.argpartition(D, kk - 1, axis=1)[:, :kk]
            buf = np.full((len(t_sel), k), -1, dtype=np.int64)
            buf[:, :kk] = c_sel2[nn]
            ev.append(t_sel)
            ctrl.append(buf)
            lvl.append(np.full(len(t_sel), level, dtype=np.int8))
    if not ev:
        return None
    return (np.concatenate(ev), np.vstack(ctrl), np.concatenate(lvl))


def evaluate(df: pd.DataFrame, pairs, y: np.ndarray, treat: np.ndarray):
    """按事件求 (处理组 − 对照组均值)，再按日汇总。"""
    ev, ctrl, _ = pairs
    yc = y[ctrl]
    ok = (ctrl >= 0).all(axis=1)
    ev, yc = ev[ok], yc[ok]
    ycm = np.nanmean(yc, axis=1)
    d = y[ev] - ycm
    good = np.isfinite(d)
    ev, d = ev[ok][good], d[good]
    dpos = df["dpos"].to_numpy()[ev]
    nd = int(dpos.max()) + 1
    s = np.bincount(dpos, weights=d, minlength=nd)
    n = np.bincount(dpos, minlength=nd)
    return s, n, ev, d


def agg(dates_s: np.ndarray, dates_n: np.ndarray):
    ok = dates_n > 0
    if ok.sum() == 0:
        return float("nan"), 0
    return float(dates_s[ok].sum() / dates_n[ok].sum()), int(dates_n[ok].sum())


def boot_ci(dates_s, dates_n, L, reps=2000, seed=11):
    T = len(dates_n)
    L = max(1, int(round(L)))
    act = np.flatnonzero(dates_n > 0)
    if len(act) == 0:
        return (np.nan, np.nan, np.nan)
    rng = np.random.default_rng(seed)
    out = np.empty(reps)
    nb = int(np.ceil(len(act) / L))
    for r in range(reps):
        starts = rng.integers(0, len(act), nb)
        idx = np.concatenate([act[(s + np.arange(L)) % len(act)] for s in starts])[:len(act)]
        out[r] = dates_s[idx].sum() / dates_n[idx].sum()
    iid = np.empty(reps)
    for r in range(reps):
        idx = rng.choice(act, len(act), replace=True)
        iid[r] = dates_s[idx].sum() / dates_n[idx].sum()
    return ((float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))),
            (float(np.percentile(iid, 2.5)), float(np.percentile(iid, 97.5))),
            float(np.std(out)))


def run_length_days(df: pd.DataFrame, treat: np.ndarray) -> float:
    """treated 状态在真实数据里的 run-length（交易日）。观测每 5 日一格 ⇒ ×5。"""
    sub = df.loc[treat, ["symbol", "dpos"]]
    if sub.empty:
        return float(EVAL_STEP)
    runs = []
    for _, g in sub.groupby("symbol", sort=False):
        p = np.sort(g["dpos"].to_numpy())
        if len(p) == 0:
            continue
        brk = np.flatnonzero(np.diff(p) != 1)
        seg = np.split(p, brk + 1)
        runs.extend(len(s) for s in seg)
    if not runs:
        return float(EVAL_STEP)
    return float(max(EVAL_STEP, np.median(runs) * EVAL_STEP))


# ---------------------------------------------------------------- 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--perm", type=int, default=200)
    a = ap.parse_args()

    df0 = load_panel()
    print(f"panel rows={len(df0):,} symbols={df0['symbol'].nunique():,} "
          f"dates={df0['dpos'].nunique() if 'dpos' in df0 else df0['date'].nunique()}")
    df = add_ranks(base_sample(df0))
    print(f"base sample rows={len(df):,} symbols={df['symbol'].nunique():,} "
          f"dates={df['dpos'].nunique():,} span={df['date'].min()}~{df['date'].max()}")
    print("universe: gap(退市/缺口) rows share = "
          f"{(df['src'] == 'gap').mean():.3%}; ST rows share = {df['is_st'].mean():.3%}"
          f" (ST 已知覆盖 {df['st_known'].mean():.1%})")
    print(f"base rate pump30={df['pump30'].mean():.3%} pump20={df['pump20'].mean():.3%} "
          f"pump50={df['pump50'].mean():.3%} fast30={df['fast30'].mean():.3%}")
    print(df.groupby("date")["pump30"].mean().resample("YE").mean().round(4).to_string())

    y = df["pump30"].to_numpy()
    treat = ((df["os_q"] == 5) & (df["fe_q"] == 5)).to_numpy()
    print(f"\ntreated (OS&FE 双Q5): n={treat.sum():,} "
          f"({treat.mean():.2%} of cells), stocks={df.loc[treat,'symbol'].nunique():,}")
    print(f"treated 状态 run-length 中位数 = {run_length_days(df, treat):.0f} 交易日")

    res = {}
    pairs = build_pairs(df, treat)
    print(f"matched events={len(pairs[0]):,}; 同行业命中率="
          f"{(pairs[2] == 1).mean():.1%}")

    # naive：处理组均值 − 同日非处理组均值
    dpos = df["dpos"].to_numpy()
    nd = dpos.max() + 1
    tr_cnt = np.bincount(dpos[treat], minlength=nd)
    al_cnt = np.bincount(dpos, minlength=nd)
    tr_sum = np.bincount(dpos[treat], weights=y[treat], minlength=nd)
    al_sum = np.bincount(dpos, weights=y, minlength=nd)
    ctrl_cnt = al_cnt - tr_cnt
    ctrl_sum = al_sum - tr_sum
    with np.errstate(invalid="ignore", divide="ignore"):
        naive_d = tr_sum / tr_cnt - ctrl_sum / ctrl_cnt
    bad = ~np.isfinite(naive_d)
    naive_s = np.where(bad, 0.0, naive_d * tr_cnt)
    naive_n = np.where(bad, 0, tr_cnt)

    # 粗匹配：同日同行业（不含规模/动量），行业均值剔除自身
    codes, _ = pd.factorize(df["industry"], sort=False)
    key = dpos.astype(np.int64) * 10000 + codes
    ck = pd.Series(key)
    ind_cnt = ck.groupby(ck).transform("size").to_numpy()
    ind_sum = pd.Series(y).groupby(ck).transform("sum").to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        ind_mean_ex = (ind_sum - y) / np.maximum(ind_cnt - 1, 1)
    coarse_d = y - ind_mean_ex
    co_s = np.bincount(dpos[treat], weights=coarse_d[treat], minlength=nd)
    co_n = tr_cnt

    # 全匹配
    fs, fn, ev, dd = evaluate(df, pairs, y, treat)
    L_state = run_length_days(df, treat)
    # 60 日前向窗口重叠 ⇒ 日块至少要覆盖 12 个观测日（60/5）；取两者较大者作保守区间
    L_eff = max(L_state, int(np.ceil(60 / EVAL_STEP)))
    print(f"block 长度：状态 run-length={L_state:.0f} 日，标签重叠下限={L_eff:.0f} 日"
          f"（保守区间用后者）")
    L = L_eff
    r = {}
    for name, (ss, nn) in {"naive": (naive_s, naive_n), "coarse": (co_s, co_n),
                           "full": (fs, fn)}.items():
        m, n = agg(ss, nn)
        (lo, hi), (ilo, ihi), sd = boot_ci(ss, nn, L, reps=a.reps)
        (lo2, hi2), _, _ = boot_ci(ss, nn, L_state, reps=max(400, a.reps // 2))
        r[name] = {"delta_pp": 100 * m, "n_events": n,
                   "ci_block": [100 * lo, 100 * hi],
                   "ci_block_stateL": [100 * lo2, 100 * hi2],
                   "ci_iid": [100 * ilo, 100 * ihi],
                   "se_block_pp": 100 * sd}
        print(f"{name:7s} Δ={100*m:+.2f}pp  n={n:,}  "
              f"block95=[{100*lo:+.2f},{100*hi:+.2f}]  "
              f"(状态L=[{100*lo2:+.2f},{100*hi2:+.2f}])  iid95=[{100*ilo:+.2f},{100*ihi:+.2f}]")
    res["primary"] = r
    res["treated_n"] = int(treat.sum())
    res["run_length_days"] = L_state
    res["block_len_used"] = L_eff

    # 分量：只 OS / 只 FE
    print("\n-- 分解（探索性）--")
    dec = {}
    for name, t in {"os_only": (df["os_q"] == 5), "fe_only": (df["fe_q"] == 5),
                    "os_hi_fe_lo": (df["os_q"] == 5) & (df["fe_q"] <= 2),
                    "fe_hi_os_lo": (df["fe_q"] == 5) & (df["os_q"] <= 2)}.items():
        p = build_pairs(df, t.to_numpy())
        ss, nn, _, _ = evaluate(df, p, y, t.to_numpy())
        m, n = agg(ss, nn)
        (lo, hi), _, _ = boot_ci(ss, nn, L, reps=max(400, a.reps // 4))
        dec[name] = {"delta_pp": 100 * m, "n": n, "ci": [100 * lo, 100 * hi]}
        print(f"{name:12s} Δ={100*m:+.2f}pp n={n:,} block95=[{100*lo:+.2f},{100*hi:+.2f}]")
    res["decompose"] = dec

    # 其他标签口径
    print("\n-- 标签口径敏感性（探索性）--")
    lab = {}
    for name in ["pump20", "pump50", "fast30"]:
        yy = df[name].to_numpy()
        ss, nn, _, _ = evaluate(df, pairs, yy, treat)
        m, n = agg(ss, nn)
        (lo, hi), _, _ = boot_ci(ss, nn, L, reps=max(400, a.reps // 4))
        lab[name] = {"delta_pp": 100 * m, "n": n, "ci": [100 * lo, 100 * hi]}
        print(f"{name:8s} Δ={100*m:+.2f}pp n={n:,} block95=[{100*lo:+.2f},{100*hi:+.2f}]")
    # 连续口径：未来 60 日最大涨幅本身（不只是「是否≥30%」）
    yy = df["fwd_max60"].to_numpy()
    ss, nn, _, _ = evaluate(df, pairs, yy, treat)
    m, n = agg(ss, nn)
    (lo, hi), _, _ = boot_ci(ss, nn, L, reps=max(400, a.reps // 4))
    lab["fwd_max60_pp"] = {"delta_pp": 100 * m, "n": n, "ci": [100 * lo, 100 * hi]}
    print(f"fwd_max60 Δ={100*m:+.2f}pp n={n:,} block95=[{100*lo:+.2f},{100*hi:+.2f}]")
    res["labels"] = lab

    # 与既有特征的共线：判断 OS/FE 是否只是既有因子的重新表述
    print("\n-- 与既有特征的日内相关（去日效应）--")
    dmv = lambda v: v - pd.Series(v).groupby(df["dpos"]).transform("mean").to_numpy()
    for c in ("os", "fe"):
        av = dmv(df[c].to_numpy())
        cells = " ".join(f"{cc}={np.corrcoef(av, dmv(df[cc].to_numpy()))[0,1]:+.2f}"
                         for cc in ("to20", "amp20", "r20", "r60", "dist_hi250",
                                    "log_mcap", "profit"))
        print(f"{c:3s}: {cells}")

    # FE 正交化：扣掉换手/振幅/规模/动量后，还剩多少独立信息
    print("\n-- FE 正交化（扣 to20/amp20/mcap/r20/r60/dist_hi250）--")
    Xd = np.column_stack([np.ones(len(df))] + [df[f"z_{c}"] for c in ZCOLS])
    yv = df["fe"].to_numpy()
    beta_r, *_ = np.linalg.lstsq(Xd, yv, rcond=None)
    df["fe_res"] = yv - Xd @ beta_r
    df["fe_res_q"] = df.groupby("dpos", sort=False)["fe_res"].rank(pct=True) * 5
    df["fe_res_q"] = np.ceil(df["fe_res_q"]).clip(1, 5).astype(int)
    t2 = ((df["os_q"] == 5) & (df["fe_res_q"] == 5)).to_numpy()
    p2 = build_pairs(df, t2)
    ss2, nn2, _, _ = evaluate(df, p2, y, t2)
    m2, n2 = agg(ss2, nn2)
    (lo2, hi2), _, _ = boot_ci(ss2, nn2, L, reps=max(400, a.reps // 4))
    print(f"OS Q5 × FE残差 Q5: Δ={100*m2:+.2f}pp n={n2:,} "
          f"block95=[{100*lo2:+.2f},{100*hi2:+.2f}]")
    res["fe_orthogonal"] = {"delta_pp": 100 * m2, "n": n2, "ci": [100 * lo2, 100 * hi2]}

    # 容量：按 ADV 五分位
    print("\n-- 容量（探索性，但为决定性检验）--")
    cap = {}
    for q in range(1, 6):
        sub = (df["adv20_q"] == q).to_numpy()
        t = treat & sub
        if t.sum() < 50:
            continue
        p = build_pairs(df, t)
        ss, nn, _, _ = evaluate(df, p, y, t)
        m, n = agg(ss, nn)
        (lo, hi), _, _ = boot_ci(ss, nn, L, reps=max(400, a.reps // 4))
        adv = df.loc[t, "adv20"].median()
        cap[f"adv_q{q}"] = {"delta_pp": 100 * m, "n": n, "ci": [100 * lo, 100 * hi],
                            "median_adv_yi": float(adv / 1e8)}
        print(f"ADV Q{q}: Δ={100*m:+.2f}pp n={n:,} block95=[{100*lo:+.2f},{100*hi:+.2f}] "
              f"median ADV={adv/1e8:.2f}亿")
    res["capacity"] = cap

    # 组内（股票固定效应）
    print("\n-- 组内（同一只股票 treated vs 非 treated）--")
    g = df.groupby("symbol")
    rows = []
    for sym, gg in g:
        t = treat[gg.index.to_numpy()]
        if t.sum() < 3 or (~t).sum() < 3:
            continue
        rows.append((sym, gg["pump30"].to_numpy()[t].mean() -
                     gg["pump30"].to_numpy()[~t].mean(), int(t.sum())))
    if rows:
        dd = np.array([r[1] for r in rows])
        ww = np.array([r[2] for r in rows], dtype=float)
        m = np.average(dd, weights=ww)
        se = dd.std(ddof=1) / np.sqrt(len(dd))
        print(f"within-stock Δ={100*m:+.2f}pp  s.e.={100*se:.2f}  n_stocks={len(dd):,} "
              f"(股票层聚类，未做日块校正)")
        res["within_stock"] = {"delta_pp": 100 * m, "se_pp": 100 * se, "n": len(dd)}

    # 安慰剂：OS&FE 双 Q3
    print("\n-- 安慰剂（双 Q3，应≈0）--")
    plac = ((df["os_q"] == 3) & (df["fe_q"] == 3)).to_numpy()
    p = build_pairs(df, plac)
    ss, nn, _, _ = evaluate(df, p, y, plac)
    m, n = agg(ss, nn)
    (lo, hi), _, _ = boot_ci(ss, nn, L, reps=max(400, a.reps // 4))
    print(f"placebo Δ={100*m:+.2f}pp n={n:,} block95=[{100*lo:+.2f},{100*hi:+.2f}]")
    res["placebo"] = {"delta_pp": 100 * m, "n": n, "ci": [100 * lo, 100 * hi]}

    # 标签置换（同日内打乱）
    print("\n-- 标签置换零分布 --")
    rng = np.random.default_rng(3)
    perm = []
    for _ in range(a.perm):
        yp = y.copy()
        for _, idx in df.groupby("dpos", sort=False).indices.items():
            yp[idx] = yp[idx][rng.permutation(len(idx))]
        ss, nn, _, _ = evaluate(df, pairs, yp, treat)
        mm, _ = agg(ss, nn)
        perm.append(100 * mm)
    perm = np.array(perm)
    print(f"置换 Δ 均值={perm.mean():+.3f}pp  sd={perm.std():.3f}  "
          f"p95={np.percentile(perm,95):+.3f}  实测={r['full']['delta_pp']:+.2f}pp")
    res["permutation"] = {"mean_pp": float(perm.mean()), "sd_pp": float(perm.std()),
                          "p95_pp": float(np.percentile(perm, 95))}

    # 时间外样本
    print("\n-- 设计期 / 外样本 --")
    oos = {}
    ss_all, nn_all, _, _ = evaluate(df, pairs, y, treat)
    for name, msk in {"design_2016_2021": df["date"].dt.year <= 2021,
                      "oos_2022_2026": df["date"].dt.year >= 2022}.items():
        keep_d = np.unique(dpos[msk.to_numpy()])
        mask = np.zeros(len(ss_all), dtype=bool)
        mask[keep_d] = True
        ss2, nn2 = np.where(mask, ss_all, 0.0), np.where(mask, nn_all, 0)
        mm, n = agg(ss2, nn2)
        (lo, hi), _, _ = boot_ci(ss2, nn2, L, reps=max(400, a.reps // 4))
        oos[name] = {"delta_pp": 100 * mm, "n": n, "ci": [100 * lo, 100 * hi]}
        print(f"{name}: Δ={100*mm:+.2f}pp n={n:,} block95=[{100*lo:+.2f},{100*hi:+.2f}]")
    res["split"] = oos

    # 条件于「已启动」：状态变量真正该被检验的地方（探索性，但更接近"条件增量"本义）
    print("\n-- 条件于已启动（r20≥8%）--")
    trig = (df["r20"] >= 0.08).to_numpy()
    t_cond = treat & trig
    print(f"trigger cells={trig.sum():,} treated∩trigger={t_cond.sum():,}")
    if t_cond.sum() >= 500:
        p = build_pairs(df, t_cond)
        ss, nn, _, _ = evaluate(df, p, y, t_cond)
        m, n = agg(ss, nn)
        (lo, hi), _, _ = boot_ci(ss, nn, L, reps=max(400, a.reps // 4))
        print(f"pump30 | 已启动: Δ={100*m:+.2f}pp n={n:,} "
              f"block95=[{100*lo:+.2f},{100*hi:+.2f}]")
        res["trigger"] = {"delta_pp": 100 * m, "n": n, "ci": [100 * lo, 100 * hi]}
        # 同一触发样本里的「未启动→启动」对照：低 OS/FE 组
        t_low = ((df["os_q"] <= 2) & (df["fe_q"] <= 2) & trig).to_numpy()
        p2 = build_pairs(df, t_low)
        ss2, nn2, _, _ = evaluate(df, p2, y, t_low)
        m2, n2 = agg(ss2, nn2)
        print(f"pump30 | 已启动 & 双低: Δ={100*m2:+.2f}pp n={n2:,}（负控）")
        res["trigger_low"] = {"delta_pp": 100 * m2, "n": n2}

    # 嵌套双排序（决定性）：在竞品变量内部再看 OS/FE 还剩什么
    print("\n-- 嵌套双排序：控制竞品后 OS/FE 的残留增量 --")
    nested = {}
    for label, base_col, base_val, tgt in [
        ("profit_q5 内 OS", "profit_q", 5, "os"),
        ("to20_q1 内 FE", "to20_q", 1, "fe"),
        ("amp20_q5 内 OS", "amp20_q", 5, "os"),
    ]:
        key = f"{tgt}_in_{base_col}"
        rr = df.groupby(["dpos", base_col], sort=False)[tgt].rank(pct=True) * 5
        df[key] = np.ceil(rr).clip(1, 5).astype(int)
        tm = ((df[base_col] == base_val) & (df[key] == 5)).to_numpy()
        p3 = build_pairs(df, tm)
        ss3, nn3, _, _ = evaluate(df, p3, y, tm)
        m3, n3 = agg(ss3, nn3)
        (lo3, hi3), _, _ = boot_ci(ss3, nn3, L, reps=max(400, a.reps // 4))
        nested[label] = {"delta_pp": 100 * m3, "n": n3, "ci": [100 * lo3, 100 * hi3]}
        print(f"{label:14s} Δ={100*m3:+.2f}pp n={n3:,} "
              f"block95=[{100*lo3:+.2f},{100*hi3:+.2f}]")
    res["nested"] = nested

    # OS 对全部协变量正交化：最严格的「是否只是既有特征的再表述」检验
    print("\n-- OS 正交化（扣 profit/amp20/to20/mcap/r20/r60/dist_hi250）--")
    Xo = np.column_stack([np.ones(len(df)), df["profit"].to_numpy(),
                          df["z_amp20"], df["z_to20"], df["z_log_mcap"],
                          df["z_r20"], df["z_r60"], df["z_dist_hi250"]])
    yo = df["os"].to_numpy()
    beta_o, *_ = np.linalg.lstsq(Xo, yo, rcond=None)
    df["os_res"] = yo - Xo @ beta_o
    df["os_res_q"] = df.groupby("dpos", sort=False)["os_res"].rank(pct=True) * 5
    df["os_res_q"] = np.ceil(df["os_res_q"]).clip(1, 5).astype(int)
    t3 = (df["os_res_q"] == 5).to_numpy()
    p3 = build_pairs(df, t3)
    ss3, nn3, _, _ = evaluate(df, p3, y, t3)
    m3, n3 = agg(ss3, nn3)
    (lo3, hi3), _, _ = boot_ci(ss3, nn3, L, reps=max(400, a.reps // 4))
    print(f"OS残差 Q5: Δ={100*m3:+.2f}pp n={n3:,} block95=[{100*lo3:+.2f},{100*hi3:+.2f}]")
    res["os_residual"] = {"delta_pp": 100 * m3, "n": n3, "ci": [100 * lo3, 100 * hi3]}

    # 双残差同时取高位：若两个构念都不含新信息，这里必须≈0
    t5 = ((df["os_res_q"] == 5) & (df["fe_res_q"] == 5)).to_numpy()
    p5 = build_pairs(df, t5)
    ss5, nn5, _, _ = evaluate(df, p5, y, t5)
    m5, n5 = agg(ss5, nn5)
    (lo5, hi5), _, _ = boot_ci(ss5, nn5, L, reps=max(400, a.reps // 4))
    print(f"双残差 Q5: Δ={100*m5:+.2f}pp n={n5:,} block95=[{100*lo5:+.2f},{100*hi5:+.2f}]")
    res["joint_residual"] = {"delta_pp": 100 * m5, "n": n5, "ci": [100 * lo5, 100 * hi5]}

    # 退市股敏感性：剔除 src='gap' 后主判据是否改变
    print("\n-- 剔除退市/缺口股后（src=main）--")
    keep = (df["src"] == "main").to_numpy()
    t4 = treat & keep
    p4 = build_pairs(df, t4)
    ss4, nn4, _, _ = evaluate(df, p4, y, t4)
    m4, n4 = agg(ss4, nn4)
    (lo4, hi4), _, _ = boot_ci(ss4, nn4, L, reps=max(400, a.reps // 4))
    print(f"主判据-only-main: Δ={100*m4:+.2f}pp n={n4:,} "
          f"block95=[{100*lo4:+.2f},{100*hi4:+.2f}]")
    res["ex_gap"] = {"delta_pp": 100 * m4, "n": n4, "ci": [100 * lo4, 100 * hi4]}
    print(f"截断率：全窗口缺失 share={(~df['full_window'].astype(bool)).mean():.3%}，"
          f"其中 gap 股占 {(df.loc[~df['full_window'].astype(bool), 'src'] == 'gap').mean():.1%}")

    # 竞品对照（决定性）：OS/FE 是否只是「获利盘」「换手率」的另一种写法
    print("\n-- 竞品对照：既有单变量能否复现同样的增量 --")
    comp = {}
    for name, t in {
        "profit_q5(获利盘高)": (df["profit_q"] == 5),
        "profit_q1(获利盘低)": (df["profit_q"] == 1),
        "to20_q1(换手最低)": (df["to20_q"] == 1),
        "amp20_q1(振幅最低)": (df["amp20_q"] == 1),
        "to20_q5(换手最高)": (df["to20_q"] == 5),
    }.items():
        tm = t.to_numpy()
        p2 = build_pairs(df, tm)
        ss2, nn2, _, _ = evaluate(df, p2, y, tm)
        m2, n2 = agg(ss2, nn2)
        (lo2, hi2), _, _ = boot_ci(ss2, nn2, L, reps=max(400, a.reps // 4))
        comp[name] = {"delta_pp": 100 * m2, "n": n2, "ci": [100 * lo2, 100 * hi2]}
        print(f"{name:20s} Δ={100*m2:+.2f}pp n={n2:,} block95=[{100*lo2:+.2f},{100*hi2:+.2f}]")
    res["competitors"] = comp

    # 回归（同日去均值的线性概率模型，作对照而非主判据）
    print("\n-- 线性概率模型（同日 FE，仅对照）--")
    dm = lambda v: v - pd.Series(v).groupby(df["dpos"]).transform("mean").to_numpy()
    Xc = ["z_log_mcap", "z_r20", "z_r60", "z_to20", "z_amp20", "z_dist_hi250"]
    X = np.column_stack([dm(df["os"].to_numpy()), dm(df["fe"].to_numpy())] +
                        [df[c].to_numpy() for c in Xc])
    X = (X - X.mean(0)) / X.std(0)
    X = np.column_stack([np.ones(len(X)), X])
    yv = dm(y)
    beta, *_ = np.linalg.lstsq(X, yv, rcond=None)
    resid = yv - X @ beta
    XtX_inv = np.linalg.pinv(X.T @ X)
    # 按日聚类
    meat = np.zeros((X.shape[1], X.shape[1]))
    for d, idx in df.groupby("dpos", sort=False).indices.items():
        Xg = X[idx]
        u = resid[idx]
        s = Xg.T @ u
        meat += np.outer(s, s)
    V = XtX_inv @ meat @ XtX_inv
    se = np.sqrt(np.diag(V))
    names = ["os", "fe"] + Xc
    for i, nm in enumerate(names):
        print(f"  {nm:12s} {100*beta[i+1]:+.3f}pp  t={beta[i+1]/se[i+1]:.2f}")
    res["lpm"] = {nm: {"coef_pp": float(100 * beta[i + 1]),
                       "t": float(beta[i + 1] / se[i + 1])} for i, nm in enumerate(names)}

    (OUT / "result.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print(f"\n→ {OUT/'result.json'}")


if __name__ == "__main__":
    main()
