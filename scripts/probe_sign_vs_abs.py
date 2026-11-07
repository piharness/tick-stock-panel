"""诊断：|mom| 是不是动量溢价的主人？以及「t 随 N 上升」到底证明了什么。

两件独立的事：

【一】t(N) 是不是独立信息？
  N 取层内前 N 只（层 M≈862），「组合 − 层」的超额序列标准差有一个**机械**下界：
  sd ∝ g(N) = sqrt(1/N − 1/M)。若 t(N) ≈ μ(N)/g(N)，那么「t 的形状」只是 μ 的形状
  乘上一个已知的、与排名无关的权重，**跨 N 比较 t 不构成任何证据**。
  这条可以用相关系数与逐格比值直接证伪。

【二】abs 的 t=3.07 超过 18 格的族系误差吗？
  零假设用**日期块自助**（block=120）而不是日内置换：日内置换会把 fwd60 的
  时间持续性一起打散，零分布的方差只剩观测的 ~1/4，p 会被系统性做小。
  块自助保留持续性，并且同一次重采样同时施加到 18 格上，格间相关自动进入。

另附 B：|mom| 中性篮子（每个 |mom| 五分位内取 signed top-k, k=N/5）——
它等价于「逐五分位跑 hi-N 再等权平均」，但作为一个组合直接给出 se / MDE / 功效。

**本脚本是 `data/.scratch/sign_vs_abs.json` 的唯一产地。** `--json PATH` 决定写到哪里；
不给这个参数时只打印、不写文件（交互式重跑用）。它从 `data/.scratch/diag_sign_vs_abs.py`
迁进 `scripts/` 并进了封签：这份结论要被引用，生成它的代码就不能留在版本控制外，
也不能改一行而产物不变。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_decile_net as P  # noqa: E402

H = 60
FRAC = 0.2
NS = (10, 20, 50, 100, 200, 500)
K = 5
W_ABS = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
W_SIG = np.array([2.0, -1.0, -2.0, -1.0, 2.0])
BLOCK = 2 * H
REPS = 2000  # 默认块自助次数；`--reps` 覆盖，实际用的次数写进产物 `meta.reps`
TAGS = ("hi", "lo", "abs")


def qcodes(x: np.ndarray) -> np.ndarray:
    n = x.size
    r = np.argsort(np.argsort(x, kind="stable"), kind="stable")
    return np.minimum((r / n * K).astype(np.int64), K - 1)


def stat(x: np.ndarray, block: int = BLOCK) -> tuple[float, float, float, float]:
    """返回 (bp, t, se_bp, sd_daily_bp)。"""
    fin = x[np.isfinite(x)]
    mu = 1e4 * float(fin.mean())
    s = P.block_se(fin, block=block)
    sd = 1e4 * float(fin.std(ddof=1))
    return mu, mu / (1e4 * s), 1e4 * s, sd


def power(mu: float, se_bp: float, alpha: float = 0.05) -> float:
    z = 1.959963985
    if se_bp <= 0:
        return float("nan")
    return float(0.5 * (1 - math.erf((z * se_bp - abs(mu)) / (math.sqrt(2) * se_bp)))
                 + 0.5 * (1 - math.erf((z * se_bp + abs(mu)) / (math.sqrt(2) * se_bp))))


def max_t_null(mat: np.ndarray, se: np.ndarray, reps: int = REPS,
               seed: int = 0) -> np.ndarray:
    """与 probe_width_frontier.max_t_bootstrap 同语义：日期块自助，格间共用同一次重采样。"""
    x = mat - mat.mean(axis=1, keepdims=True)
    c, t = x.shape
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(t / BLOCK))
    out = np.empty((reps, c))
    ar = np.arange(BLOCK)
    for i in range(reps):
        starts = rng.integers(0, t, nb)
        idx = ((starts[:, None] + ar[None, :]) % t).ravel()[:t]
        out[i] = x[:, idx].mean(axis=1) / se
    return out


def main(reps: int = REPS, out: Path | None = None) -> None:
    df = P.load_panel()
    fwd = f"fwd{H}"
    d = (df.filter(pl.col("mom").is_not_null() & pl.col(fwd).is_not_null()
                   & (pl.col("turnover") > 0))
         .with_columns((((pl.col("turnover").rank("ordinal").over("date") - 1)
                        / pl.col("turnover").count().over("date")) < FRAC).alias("in_b"))
         .filter(pl.col("in_b")))
    d = d.sort("date")
    # 层规模逐日 —— g(N) 的 M 用逐日中位
    sizes = d.group_by("date").agg(pl.len().alias("n")).sort("date")
    M = float(sizes["n"].median())
    n_days = int(d["date"].n_unique())
    print(f"层内 {n_days} 个决策日，逐日规模中位 {M:.0f} 只\n", flush=True)
    rep: dict = {"meta": {
        "script": "scripts/probe_sign_vs_abs.py",
        "layer": "低成交额 20%（turnover 升序前 20%），层内再排序",
        "fwd": fwd, "H": H, "frac": FRAC, "ns": list(NS), "quintiles": K,
        "block": BLOCK, "reps": reps, "boot_seed_family": 0, "boot_seed_shape": 1,
        "layer_days": n_days, "layer_size_median": M,
        "panel_rows": int(df.height),
    }}

    # ---------- 逐日展开：mom 排序固定，只算一次 ----------
    o_hi_all, o_lo_all, o_ab_all, pos_a_all = [], [], [], []
    ga_all, gs_all, cnt_a_all, cnt_s_all, mean_all, y_all, m_all = ([] for _ in range(7))
    for sub in d.partition_by("date", maintain_order=True):
        y = sub[fwd].to_numpy().astype(np.float64)
        m = sub["mom"].to_numpy().astype(np.float64)
        am = np.abs(m)
        ga = qcodes(am)
        gs = qcodes(m)
        pa = np.empty(m.size, dtype=np.int64)
        for b in range(K):
            idx = np.flatnonzero(ga == b)
            pa[idx[np.argsort(-m[idx], kind="stable")]] = np.arange(idx.size)
        y_all.append(y)
        m_all.append(m)
        ga_all.append(ga)
        gs_all.append(gs)
        mean_all.append(y.mean())
        cnt_a_all.append(np.bincount(ga, minlength=K).astype(np.float64))
        cnt_s_all.append(np.bincount(gs, minlength=K).astype(np.float64))
        o_hi_all.append(np.argsort(-m, kind="stable"))
        o_lo_all.append(np.argsort(m, kind="stable"))
        o_ab_all.append(np.argsort(-am, kind="stable"))
        pos_a_all.append(pa)
    nd = len(y_all)

    # ---------- 1. 三条曲线 ----------
    series: dict[tuple[str, int], np.ndarray] = {}
    for j in range(nd):
        y, base = y_all[j], mean_all[j]
        for tag, o in zip(TAGS, (o_hi_all[j], o_lo_all[j], o_ab_all[j])):
            cs = np.cumsum(y[o])
            for n in NS:
                k = min(n, y.size)
                series.setdefault((tag, n), np.zeros(nd))[j] = cs[k - 1] / k - base

    print("=== 三条宽度曲线（应与 diag_width_direction 逐位一致）===")
    print("   N      hi      t       lo      t      abs     t")
    obs = {}
    for n in NS:
        cells = []
        for tag in TAGS:
            mu, t, se_bp, sd = stat(series[(tag, n)])
            obs[(tag, n)] = (mu, t, se_bp, sd)
            cells.append(f"{mu:+8.1f} {t:+6.2f}")
        print(f"  {n:4d} " + " ".join(cells))
    rep["curves"] = {tag: {str(n): {"bp": obs[(tag, n)][0], "t": obs[(tag, n)][1],
                                    "se_bp": obs[(tag, n)][2],
                                    "sd_daily_bp": obs[(tag, n)][3]}
                           for n in NS} for tag in TAGS}

    # ---------- 2. t(N) 是不是机械的 ----------
    print("\n=== 机械性检验：g(N)=sqrt(1/N−1/M) 能否解释 t(N) ===")
    g = {n: math.sqrt(1.0 / n - 1.0 / M) for n in NS}
    print("   N     g(N)    hi: t   μ/g    比值 | abs: t   μ/g    比值")
    for n in NS:
        a, b = obs[("abs", n)], obs[("hi", n)]
        print(f"  {n:4d}  {g[n]:.4f}   {b[1]:+6.2f} {b[0]/g[n]:7.0f} {b[1]/(b[0]/g[n]):5.3f} "
              f"| {a[1]:+6.2f} {a[0]/g[n]:7.0f} {a[1]/(a[0]/g[n]):5.3f}")
    for tag in TAGS:
        tvec = np.array([obs[(tag, n)][1] for n in NS])
        rvec = np.array([obs[(tag, n)][0] / g[n] for n in NS])
        print(f"   corr(t, μ/g) [{tag}] = {np.corrcoef(tvec, rvec)[0,1]:.4f}")
    print("   sd 的实际衰减 vs sqrt(1/N−1/M)：")
    for tag in TAGS:
        sds = np.array([obs[(tag, n)][3] for n in NS])
        print(f"   {tag}: sd(10)/sd(500) = {sds[0]/sds[-1]:5.2f}   "
              f"g(10)/g(500) = {g[10]/g[500]:5.2f}")

    def lam(ns, vals, max_n=100):
        xs = [np.log(n) for n in ns if n <= max_n and vals[NS.index(n)] > 0]
        ys = [np.log(vals[NS.index(n)]) for n in ns if n <= max_n and vals[NS.index(n)] > 0]
        return float(-np.polyfit(xs, ys, 1)[0]) if len(xs) > 1 else float("nan")

    print("\n   α(N) 的衰减指数 λ（α ∝ N^-λ）")
    for tag in TAGS:
        vals = [obs[(tag, n)][0] for n in NS]
        print(f"   {tag:4s}  N≤100: λ={lam(NS, vals):.3f}    全区间: λ={lam(NS, vals, 1e9):.3f}")
    rep["mechanical"] = {
        "g": {str(n): g[n] for n in NS},
        "corr_t_over_mu_g": {
            tag: float(np.corrcoef(
                np.array([obs[(tag, n)][1] for n in NS]),
                np.array([obs[(tag, n)][0] / g[n] for n in NS]))[0, 1])
            for tag in TAGS},
        "sd_ratio_first_last": {
            tag: float(np.array([obs[(tag, n)][3] for n in NS])[0]
                       / np.array([obs[(tag, n)][3] for n in NS])[-1])
            for tag in TAGS},
        "g_ratio_first_last": float(g[NS[0]] / g[NS[-1]]),
        "lambda": {tag: {"n_le_100": lam(NS, [obs[(tag, n)][0] for n in NS]),
                         "all": lam(NS, [obs[(tag, n)][0] for n in NS], 1e9)}
                   for tag in TAGS},
    }

    # ---------- 3. 族系零分布 ----------
    print(f"\n=== 块自助零分布（block={BLOCK}，{reps} 次，18 格共用重采样）===", flush=True)
    mat = np.vstack([series[(t, n)] for t in TAGS for n in NS])
    sev = np.array([obs[(t, n)][2] / 1e4 for t in TAGS for n in NS])
    obs_t = np.array([obs[(t, n)][1] for t in TAGS for n in NS])
    null = max_t_null(mat, sev, reps=reps)          # (reps, 18)
    nmax = np.abs(null).max(axis=1)
    mt = float(np.abs(obs_t).max())
    p_fw = (1 + int((nmax >= mt).sum())) / (1 + reps)
    best_i = int(np.argmax(np.abs(obs_t)))
    print(f"   观测 max|t| = {mt:.2f}（{TAGS[best_i//6]}/N{NS[best_i%6]}）")
    print(f"   零分布：中位 {np.median(nmax):.2f}  95% {np.quantile(nmax,0.95):.2f}  "
          f"99% {np.quantile(nmax,0.99):.2f}")
    print(f"   p_FWER(18 格) = {p_fw:.4f}")
    fam: dict = {"observed_max_abs_t": mt,
                 "observed_max_abs_t_cell": f"{TAGS[best_i//6]}/N{NS[best_i%6]}",
                 "null_median": float(np.median(nmax)),
                 "null_q95": float(np.quantile(nmax, 0.95)),
                 "null_q99": float(np.quantile(nmax, 0.99)),
                 "p_fwer_18": p_fw, "subfamilies": {}, "marginal_p": {},
                 "shape_all_positive": {}}
    for lab, cols in (("hi×6", list(range(0, 6))), ("lo×6", list(range(6, 12))),
                      ("abs×6", list(range(12, 18))),
                      ("hi+lo×6", list(range(0, 12))), ("hi+abs×6", list(range(0, 6)) + list(range(12, 18)))):
        sub = np.abs(null[:, cols]).max(axis=1)
        best = float(np.abs(obs_t[cols]).max())
        pp = (1 + int((sub >= best).sum())) / (1 + reps)
        print(f"   子族 {lab:8s} 6 格量级的 max|t|={best:.2f}  "
              f"q95={np.quantile(sub,0.95):.2f}  p={pp:.4f}")
        fam["subfamilies"][lab] = {"n_cells": len(cols), "max_abs_t": best,
                                   "q95": float(np.quantile(sub, 0.95)), "p": pp}
    for t in TAGS:
        ti = TAGS.index(t)
        for i, n in enumerate(NS):
            p = (1 + int((np.abs(null[:, ti*6+i]) >= abs(obs_t[ti*6+i])).sum())) / (1 + reps)
            print(f"   边际 p  {t}{n:<4d} t={obs_t[ti*6+i]:+5.2f}  p={p:.4f}")
            fam["marginal_p"][f"{t}/N{n}"] = {"t": float(obs_t[ti*6+i]), "p": p}

    print("\n   形状一致性：6 个 N 全为正，在零分布下的概率")
    for t in TAGS:
        ti = TAGS.index(t)
        blk = np.sign(null[:, ti*6:ti*6+6])
        allpos = bool((np.array([obs[(t, n)][0] for n in NS]) > 0).all())
        prob = float((blk > 0).all(axis=1).mean())
        print(f"   {t:4s} 观测 6/6 全正={allpos}"
              f"   零分布全正概率={prob:.3f}")
        fam["shape_all_positive"][t] = {"observed_all_positive": allpos,
                                        "null_prob_all_positive": prob}
    rep["family"] = fam

    # ---------- 4. 形状对比统计量 ----------
    print("\n=== A1/A2 无自由参数的形状统计量（单次检验，无族系）===")
    qa = np.empty((K, nd))
    qs = np.empty((K, nd))
    for j in range(nd):
        qa[:, j] = np.bincount(ga_all[j], weights=y_all[j], minlength=K) / cnt_a_all[j] - mean_all[j]
        qs[:, j] = np.bincount(gs_all[j], weights=y_all[j], minlength=K) / cnt_s_all[j] - mean_all[j]
    print("   |mom| 五分位超额  " + "  ".join(f"{1e4*qa[b].mean():+7.1f}" for b in range(K)))
    print("   signed 五分位超额 " + "  ".join(f"{1e4*qs[b].mean():+7.1f}" for b in range(K)))
    shp: dict = {
        "quintile_abs_bp": [float(1e4 * qa[b].mean()) for b in range(K)],
        "quintile_signed_bp": [float(1e4 * qs[b].mean()) for b in range(K)],
        "contrasts": {},
    }
    for name, ser in (("|mom| 线性对比", W_ABS @ qa), ("signed U 型对比", W_SIG @ qs),
                      ("signed s4-s0", qs[K-1] - qs[0])):
        mu, t, se_bp, sd = stat(ser)
        print(f"   {name:16s} {mu:+8.1f}bp  t={t:+6.2f}  se={se_bp:5.0f}bp  "
              f"MDE80={2.802*se_bp:5.0f}bp")
        shp["contrasts"][name] = {"bp": mu, "t": t, "se_bp": se_bp,
                                  "mde80_bp": 2.802 * se_bp}

    # ---------- 5. |mom| 中性篮子 ----------
    print("\n=== B  |mom| 中性篮子（= 逐 |mom| 五分位 hi-N 再等权平均）===")
    print("    N    超额      t    se_bp   MDE80bp   功效@观测效应")
    neu: dict = {}
    for n in NS:
        k = max(1, n // K)
        ser = np.array([y_all[j][pos_a_all[j] < k].mean() - mean_all[j] for j in range(nd)])
        mu, t, se_bp, sd = stat(ser)
        print(f"  {n:4d} {mu:+8.1f} {t:+6.2f}  {se_bp:6.0f}  {2.802*se_bp:8.0f}   "
              f"{power(mu, se_bp):6.1%}")
        neu[str(n)] = {"bp": mu, "t": t, "se_bp": se_bp, "mde80_bp": 2.802 * se_bp,
                       "k_per_quintile": k, "power_at_observed": power(mu, se_bp)}

    print("\n   逐 |mom| 五分位的「signed top-10」（相对本格均值）")
    cell10 = []
    c10: dict = {}
    for b in range(K):
        ser = []
        for j in range(nd):
            idx = np.flatnonzero(ga_all[j] == b)
            if idx.size < 10:
                continue
            order = idx[np.argsort(-m_all[j][idx], kind="stable")][:10]
            ser.append(float(y_all[j][order].mean() - y_all[j][idx].mean()))
        cell10.append(np.asarray(ser))
        mu, t, se_bp, sd = stat(np.asarray(ser))
        print(f"   a{b}  n={len(ser):5d}  {mu:+8.1f}bp  t={t:+6.2f}  "
              f"MDE80={2.802*se_bp:5.0f}bp  功效 {power(mu, se_bp):5.1%}")
        c10[f"a{b}"] = {"n_days": len(ser), "bp": mu, "t": t, "se_bp": se_bp,
                        "mde80_bp": 2.802 * se_bp, "power_at_observed": power(mu, se_bp)}
    # 交互作用的单次检验：权重固定 (b−2)，Σ(b−2)²=10，所以对比/10 就是「每档增量」
    inter = sum(W_ABS[b] * cell10[b] for b in range(K)) / float((W_ABS ** 2).sum())
    mu, t, se_bp, sd = stat(inter)
    print(f"   交互斜率（signed top10 效应每升一个 |mom| 五分位）{mu:+8.1f}bp/档  "
          f"t={t:+6.2f}  se={se_bp:5.0f}bp  MDE80={2.802*se_bp:5.0f}bp")
    shp["interaction_slope"] = {"bp": mu, "t": t, "se_bp": se_bp,
                                "mde80_bp": 2.802 * se_bp}
    rep["neutral"] = neu
    rep["cell_signed_top10"] = c10

    # 三个形状统计量各自的块自助零分布（单次检验，不构成族系）
    cser = np.vstack([W_ABS @ qa, W_SIG @ qs, qs[K-1] - qs[0], inter])
    cnames = ["|mom| 线性对比", "signed U 型对比", "signed s4-s0", "交互斜率"]
    cse = np.array([P.block_se(cser[i], block=BLOCK) for i in range(4)])
    cobst = np.array([1e4 * cser[i].mean() / (1e4 * cse[i]) for i in range(4)])
    cnull = max_t_null(cser, cse, reps=reps, seed=1)
    print("\n   形状统计量的块自助边际 p（各自单次检验）")
    for i, nm in enumerate(cnames):
        p = (1 + int((np.abs(cnull[:, i]) >= abs(cobst[i])).sum())) / (1 + reps)
        print(f"   {nm:14s} t={cobst[i]:+6.2f}  p={p:.4f}")
        shp["contrasts"].setdefault(nm, {})["p_block_bootstrap"] = p

    # ---------- 6. 中性篮子的年度稳定性 ----------
    print("\n   中性篮子 N=100 的年度超额（bp）")
    ser100 = np.array([y_all[j][pos_a_all[j] < 20].mean() - mean_all[j] for j in range(nd)])
    yrs = np.array([int(str(x)[:4]) for x in d["date"].unique().sort().to_list()])
    yearly: dict = {}
    for y in sorted(set(yrs.tolist())):
        msk = yrs == y
        if msk.sum() >= 20:
            print(f"     {y}  {1e4*ser100[msk].mean():+8.1f}  (n={msk.sum()})")
            yearly[str(y)] = {"bp": float(1e4 * ser100[msk].mean()),
                              "n_days": int(msk.sum())}
    rep["yearly_neutral_100"] = yearly
    rep["shape"] = shp
    rep["definitions"] = {
        "bp": "「组合 − 层均值」的逐日超额，对日期取算术均值（bp）",
        "t": "bp 除以 `probe_decile_net.block_se` 的块自助 se（block=120）",
        "hi": "层内按 mom 降序取前 N",
        "lo": "层内按 mom 升序取前 N",
        "abs": "层内按 |mom| 降序取前 N（与 hi 不是同一个排序）",
        "quintile": "层内按 |mom| 或 mom 的 ordinal 排名均分 5 格，格内超额对日期取均值",
        "contrasts": "|mom| 线性对比 = W·q，W=(-2,-1,0,1,2)，是五分位加权差，不是可持有组合",
        "p_fwer_18": "18 格共用同一次日期块重采样得到的 max|t| 零分布尾概率",
        "reps": "块自助次数；p 的分辨率下限是 1/(1+reps)",
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"\n写入 {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="|mom| 与符号的分解；产物唯一产地")
    ap.add_argument("--reps", type=int, default=REPS, help="块自助次数（默认 2000）")
    ap.add_argument("--json", type=Path, default=None, help="产物路径；不给就只打印")
    args = ap.parse_args()
    main(reps=args.reps, out=args.json)
