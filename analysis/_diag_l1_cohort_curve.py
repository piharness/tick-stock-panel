"""cohort 自己的 c(k)/Δ(k)：把 k* 从「反演」变成「读数」，并把判据从 c 轴移到 Δ 轴。

为什么需要它
------------
`_diag_l1_kcurve` 的 k*（45.1–81.3）不是 cohort 的：它把**参照类**的归一化曲线形状
映射到 cohort 的两个端点（Δ(1)=14.64、Δ(240)=109.29）上，再解 `c(k*)=判据`。
`_diag_l1_kstar_slope_provenance` 进一步证明，这条链的斜率（`dΔ/dk=−0.06949`、
`dc/dk=0.00383`）来自 `kline_minute` 的 40 只 000xxx.SZ，与 cohort 的 (date,symbol)
交集为 0，而且「切线/割线」比不可迁移（弯曲方向相反）。

于是三个量从来没有产地：cohort 的 `dΔ/dk`、cohort 在 k≈52 的局部斜率、cohort 的
`c(k)` 形状。它们全在同一个文件里——`curves.parquet`（2,752 格 × 240 根，含逐根
amount/volume）。有了它，k* 不再需要反演：直接在 Δ(k) 上解 `Δ=打平线`，在 c(k) 上
读同一位置的份额。

口径（与参照类逐字相同，否则又是跨口径）
----------------------------------------
    VWAP_k = Σ_{i≤k} amount_i / Σ_{i≤k} (volume_i × 100)
    Δ(k)   = (VWAP_k / open_0 − 1) × 1e4        （open_0 = 09:31 那根的开盘）
    c(k)   = Σ_{i≤k} amount_i / Σ_all amount_i

k 从 0 起（0 = 只有 09:31 那一根）。`Δ(0)` 因此等于 kcurve 的 `d1_bp`（首根 VWAP 对
开盘），`Δ(239)` 对应全日的 `d240_bp`——这两处是本脚本与既有产物的**唯一接缝**，
用它们做端点复现，单位错（volume 手/股差 100 倍）会立刻把它顶红。

判据（可被本产物直接否证）
--------------------------
    D1 接缝。 Δ(0)、Δ(239) 的 cell 均值与 kcurve 已发布的 14.64 / 109.29 差 ≤1.5bp
        ⇒ 分母口径一致（含 volume×100 的单位）。**只在样本天数 ≥ 基准天数时生效**：
        子样本均值本来就该不同，那时的差是抽样，不是口径；此时 `applicable=false`，
        差值照报但不进判定（否则前哨产物会被一个恒假的红钉死）。
    D2 打平可达（本脚本唯一的载荷判据）。 margin = Δ(239)_dayeq − 打平线。
        k* 存在 ⟺ Δ(239) ≥ 打平线（240 根内单调段上的等价），所以不需要解 k*，
        也不需要 c 轴。按**天**重采样整条曲线，报 `P(均值 margin ≥ 0)`；|t|≥2 才判
        可/不可达，否则 `undetermined`。
    D3 可辨性（解析，不依赖数据量）。 n_days_for_2sigma = (2·sd_day/|margin|)²。
        若它远大于可用天数，D2 的 `undetermined` 是**结构性的**，再抓多少天都不变。

为什么 c 轴判据被删掉
---------------------
旧版 C4 用 `se_c = se_Δ(52)·|dc/dk|/|dΔ/dk| ≤ gap/2` 判「k* 可辨」。两处错位：
① 阈值 `gap/2` 是**参照类两种读数的 c 间隙**（0.122/2），不是 cohort 的判据余量；
② se 取自 k=52。实测 cohort 的 c(k) 在 k=28.7 就到 `c_required=0.4011`、k=52 是
   0.5115、k=239 是 1.0 ⇒ **c 在 k* 附近早已饱和**，任何形如 `c(k*) ≥ 0.4011` 的
   判据都恒真。c 轴只留作描述量（`k_at_c_required`），`load_bearing=false`。

只读本地 parquet，不联网。缺 `curves.parquet` 时直接退出，不造替代品。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _artifact_guard import assert_clean, render_reading  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "_diag_l1_cohort_curve_result.json"
CURVES = ROOT / "data" / "research" / "l1_open_depth" / "curves.parquet"
KCJ = HERE / "_diag_l1_kcurve_result.json"
SP = HERE / "_diag_l1_kstar_slope_provenance_result.json"

BARS = 240
CKPTS = (0, 1, 5, 20, 52, 139, 239)
SLOPE_WINDOWS = ((40, 70), (30, 90), (45, 82), (0, 239))   # 与 kstar_condition 同一组
N_BOOT = 2000
BOOT_SEED = 7
T_DECIDE = 2.0          # |t| 达到它才判可达/不可达，否则 undetermined


def load_curves() -> pl.DataFrame:
    if not CURVES.exists():
        raise SystemExit(f"缺 {CURVES}：先跑 fetch_l1_open_depth.py --mode curves")
    return pl.read_parquet(CURVES)


def cell_curves(cv: pl.DataFrame) -> pl.DataFrame:
    """逐 cell 的 (k, Δ, c)。定义与 `_diag_l1_kcurve.load_reference` 逐字相同。"""
    cv = cv.sort(["date", "symbol", "k"]).filter(pl.col("volume") > 0)
    out = cv.with_columns(
        pl.col("amount").cum_sum().over(["date", "symbol"]).alias("ca"),
        (pl.col("volume").cum_sum().over(["date", "symbol"]) * 100.0).alias("cv100"),
        pl.col("amount").sum().over(["date", "symbol"]).alias("ta"),
        pl.col("open").first().over(["date", "symbol"]).alias("o0"),
    ).filter(pl.col("cv100") > 0)
    return out.with_columns(
        (((pl.col("ca") / pl.col("cv100")) / pl.col("o0") - 1.0) * 1e4).alias("delta_bp"),
        (pl.col("ca") / pl.col("ta")).alias("c"),
    ).select("date", "symbol", "k", "delta_bp", "c")


def curve_mean(cells: pl.DataFrame) -> pl.DataFrame:
    """k 上的 cell 等权曲线 + 跨 cell 的 se（诊断用，不用于日级推断）。"""
    return cells.group_by("k").agg(
        pl.col("delta_bp").mean().alias("delta_bp"),
        pl.col("c").mean().alias("c"),
        pl.col("delta_bp").std().alias("sd_cell"),
        pl.len().alias("n_cell"),
    ).sort("k")


def day_se(cells: pl.DataFrame, col: str) -> tuple[float, float, int]:
    """样本单位是**天**：先按日取 cell 截面均值，再在日序列上算 se。"""
    per = cells.group_by("date").agg(pl.col(col).mean().alias("m"))
    x = np.asarray(per["m"], float)
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("nan"), float("nan"), int(x.size)
    return float(x.mean()), float(x.std(ddof=1) / np.sqrt(x.size)), int(x.size)


def slope(kk: np.ndarray, y: np.ndarray, lo: int, hi: int) -> float:
    m = (kk >= lo) & (kk <= hi)
    A = np.column_stack([np.ones(int(m.sum())), kk[m]])
    b, *_ = np.linalg.lstsq(A, y[m], rcond=None)
    return float(b[1])


def crossings(kk: np.ndarray, y: np.ndarray, target: float) -> list[float]:
    """单调段上解 y(k)=target 的**全部**解；曲线不单调时可能 0 个、也可能 2 个。

    旧版 `crossing` 只取第一个，于是「曲线先破线再回落」与「只破一次」在产物里
    长得一样。K 曲线实测在 k≈200 见顶后回落，这个区别不是学术细节。
    """
    out: list[float] = []
    for i in range(len(kk) - 1):
        a, b = y[i], y[i + 1]
        if (a - target) * (b - target) <= 0 and a != b:
            out.append(float(kk[i] + (target - a) / (b - a) * (kk[i + 1] - kk[i])))
    return out


def crossing(kk: np.ndarray, y: np.ndarray, target: float) -> float:
    """兼容旧签名：返回第一个解，无解返回 nan。"""
    c = crossings(kk, y, target)
    return c[0] if c else float("nan")


def first_reach(kk: np.ndarray, y: np.ndarray, target: float) -> float:
    """k* 的定义式：**首次**达到 target 的 k。

    与 `crossings` 分开是必须的，否则两件事会被同一个空表混为一谈：
      · 起点已在线之上（Δ(0) ≥ target）⇒ k*=0，是「开盘就够」，不是「无解」；
      · 全程在下方 ⇒ nan，才是真的打不到。
    `crossings` 只管「破了又回落再破」有几个交点，那是形状描述。
    """
    if y.size and y[0] >= target:
        return 0.0
    for i in range(len(kk) - 1):
        if y[i] < target <= y[i + 1]:
            return float(kk[i] + (target - y[i]) / (y[i + 1] - y[i]) * (kk[i + 1] - kk[i]))
    return float("nan")


def decide(t: float, thr: float = T_DECIDE) -> str:
    """三档判定。单独成函数是为了能被单测钉住：判据自己的红/绿必须可构造。

    `undetermined` 不是「失败」，是「这个样本回答不了」——它必须与 `not_reachable`
    分开，否则「样本不够」会被读成「打平不成立」。
    """
    if not np.isfinite(t):
        return "undetermined"
    if t >= thr:
        return "breakeven_reachable"
    if t <= -thr:
        return "breakeven_not_reachable"
    return "undetermined"


def curve_matrix(cells: pl.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """把 90 万行长表压成 (n_cell, 240) 的 Δ 矩阵，供按天重采样整条曲线。

    重采样**必须按天**：同一天的 8.7 只票不是 8.7 个独立观测，按 cell 抽会把 se
    缩小 √8.7。D2 的 `P` 全部出自这里。
    """
    piv = cells.pivot(values="delta_bp", index=["date", "symbol"], on="k").sort(["date", "symbol"])
    kcols = sorted(c for c in piv.columns if c not in ("date", "symbol"))
    M = piv.select([str(c) for c in kcols]).to_numpy().astype(float)
    M = np.nan_to_num(M, nan=0.0)
    days = piv["date"].to_list()
    uniq = sorted(set(days))
    day_pos = {d: i for i, d in enumerate(uniq)}
    cell_day = np.array([day_pos[d] for d in days], dtype=np.int64)
    return M, cell_day, np.array(kcols, dtype=float), uniq


def bootstrap_curves(M: np.ndarray, cell_day: np.ndarray, n_days: int,
                     kk: np.ndarray, target: float,
                     n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict:
    """按天有放回抽 `n_days` 天，重建整条 240 点曲线。

    权重法而不是拼接法：某天被抽 k 次，它的 cell 权重就是 k，均值的分子分母一起
    放大，等价于把那天复制 k 份。O(n_cell×240)，2,000 次约 2 秒。
    """
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, float)
    n_sol = np.zeros(n_boot, dtype=np.int64)
    kstar = np.full(n_boot, np.nan)
    for b in range(n_boot):
        pick = rng.integers(0, n_days, n_days)
        w = np.bincount(pick, minlength=n_days).astype(float)[cell_day]
        cur = (M * w[:, None]).sum(0) / w.sum()
        means[b] = cur[-1]
        n_sol[b] = len(crossings(kk, cur, target))
        kstar[b] = first_reach(kk, cur, target)
    return {"means": means, "n_sol": n_sol, "kstar": kstar}


def main() -> None:
    t0 = time.time()
    kc = json.loads(KCJ.read_text(encoding="utf-8"))
    sp = json.loads(SP.read_text(encoding="utf-8"))

    breakeven = float(kc["breakeven"]["delta_breakeven_bp"])
    d1_pub = float(kc["endpoints"]["d1"]["cell_mean_bp"])
    d240_pub = float(kc["endpoints"]["d240"]["cell_mean_bp"])
    bench_days = int(kc["endpoints"]["d240"]["days"])
    c_req = float(kc["kstar"]["c_required"])
    ref_span = float(sp["level_bracket"]["ref_span_bp"])
    mixed_dc = float(sp["recompute"]["coh"]["dc_per_bp"])
    se52_pub = float(sp["inputs"]["se_delta_k52_316d_bp"])

    cv = load_curves()
    cells = cell_curves(cv)
    cur = curve_mean(cells)
    kk = np.asarray(cur["k"], float)
    dk = np.asarray(cur["delta_bp"], float)
    ck = np.asarray(cur["c"], float)
    n_cell = cells["date", "symbol"].n_unique()
    n_day = cells["date"].n_unique()

    # ---- D1 接缝：与既有产物的唯一可比处
    cell_d0 = float(cells.filter(pl.col("k") == 0)["delta_bp"].mean())
    cell_d239 = float(cells.filter(pl.col("k") == kk.max())["delta_bp"].mean())
    d0_gap, d239_gap = abs(cell_d0 - d1_pub), abs(cell_d239 - d240_pub)
    seam_applicable = bool(n_day >= bench_days)
    seam_ok = bool(d0_gap <= 1.5 and d239_gap <= 1.5)

    # ---- 形状描述（不再是判据）
    diffs = np.diff(dk)
    viol = int((diffs < 0).sum())
    span = float(dk.max() - dk.min())

    # ---- D2 打平可达：k* 存在 ⟺ Δ(239) ≥ 打平线
    per_day = cells.filter(pl.col("k") == int(kk.max()))
    d_mean_dayeq, d_se_day, d_days = day_se(per_day, "delta_bp")
    d_sd_day = d_se_day * np.sqrt(d_days) if d_days > 1 else float("nan")
    margin = d_mean_dayeq - breakeven
    t_margin = margin / d_se_day if d_se_day else float("nan")

    M, cell_day, kmat, uniq_days = curve_matrix(cells)
    boot = bootstrap_curves(M, np.asarray(cell_day), len(uniq_days), kk, breakeven)
    p_mean_ge = float(np.mean(boot["means"] >= breakeven))
    p_exists = float(np.mean(np.isfinite(boot["kstar"])))
    ci_margin = np.percentile(boot["means"], [2.5, 97.5]) - breakeven

    # ---- k*：首次达到打平线的位置（起点已过线 ⇒ 0；全程未达 ⇒ 无定义）
    sols = crossings(kk, dk, breakeven)
    k_point = first_reach(kk, dk, breakeven)
    k_star = float(k_point) if np.isfinite(k_point) else None
    c_at_star = float(np.interp(k_star, kk, ck)) if k_star is not None else None
    k_at_c_req = float(np.interp(c_req, ck, kk)) if ck.min() <= c_req <= ck.max() else None
    ks_valid = boot["kstar"][np.isfinite(boot["kstar"])]
    k_ci = (np.percentile(ks_valid, [2.5, 97.5]) if ks_valid.size >= 50
            else np.array([np.nan, np.nan]))

    # ---- D3 可辨性：解析，与再抓多少天无关
    n_2s = float((2.0 * d_sd_day / abs(margin)) ** 2) if margin else float("inf")

    # ---- 斜率描述
    s_win = {f"{lo}-{hi}": slope(kk, dk, lo, hi) for lo, hi in SLOPE_WINDOWS}
    c_win = {f"{lo}-{hi}": slope(kk, ck, lo, hi) for lo, hi in SLOPE_WINDOWS}
    main_key, full_key = "40-70", "0-239"
    d_delta = s_win[main_key]
    dratio = abs(s_win[full_key] / d_delta) if d_delta else float("nan")

    _, se52, _ = day_se(cells.filter(pl.col("k") == 52), "delta_bp")

    # ---- 判定：三档，不是 all_pass
    decision = decide(t_margin)

    checkpoints = {
        f"k{k}": {
            "delta_bp": round(float(dk[int(np.where(kk == k)[0][0])]), 2),
            "c": round(float(ck[int(np.where(kk == k)[0][0])]), 4),
        }
        for k in CKPTS if (kk == k).any()
    }

    out = {
        "purpose": "cohort 自己的 c(k)/Δ(k)：k* 从跨宇宙反演改成读数；判据从 c 轴移到 Δ 轴",
        "caliber": (
            "VWAP_k=Σamount/Σ(volume×100)；Δ(k)=(VWAP_k/open_0−1)·1e4；"
            "c(k)=Σamount_k/Σamount_all；k 自 0（09:31）起；"
            "点估计 cell 等权，推断单位是天（日级 se），重采样按天"
        ),
        "coverage": {
            "cells": n_cell,
            "days": n_day,
            "rows": cells.height,
            "days_bench": bench_days,
            "is_full_sample": bool(n_day >= bench_days),
            "bars_min": int(cv.group_by(["date", "symbol"]).len()["len"].min()),
            "bars_max": int(cv.group_by(["date", "symbol"]).len()["len"].max()),
        },
        "reconcile": {
            "d0_curve_bp": round(cell_d0, 2),
            "d0_published_bp": d1_pub,
            "d0_gap_bp": round(d0_gap, 2),
            "d239_curve_bp": round(cell_d239, 2),
            "d239_published_bp": d240_pub,
            "d239_gap_bp": round(d239_gap, 2),
            "applicable": seam_applicable,
            "within_tol": seam_ok,
            "tol_bp": 1.5,
        },
        "curve": {
            "k_max": int(kk.max()),
            "span_bp": round(span, 2),
            "ref_span_bp": round(ref_span, 2),
            "span_ratio": round(span / ref_span, 2),
            "monotone_violations": viol,
            "checkpoints": checkpoints,
            # 240 点全量：产物本身就是锚，660k 行的曲线缓存不必进版本控制
            "delta_bp_by_k": [round(float(x), 2) for x in dk],
            "c_by_k": [round(float(x), 4) for x in ck],
            "n_cell_by_k": [int(x) for x in cur["n_cell"].to_list()],
        },
        "slopes": {
            "d_delta_dk": {k: round(v, 5) for k, v in s_win.items()},
            "d_c_dk": {k: round(v, 5) for k, v in c_win.items()},
            "dc_per_bp_main": round(abs(c_win[main_key] / d_delta), 5) if d_delta else None,
            "dc_per_bp_mixed_published": round(mixed_dc, 5),
            "slope_window_ratio": round(dratio, 2),
        },
        "breakeven": {
            "line_bp": round(breakeven, 2),
            "delta_239_cell_bp": round(cell_d239, 2),
            "delta_239_dayeq_bp": round(d_mean_dayeq, 2),
            "margin_bp": round(margin, 2),
            "se_day_bp": round(d_se_day, 2),
            "sd_day_bp": round(d_sd_day, 2),
            "t": round(t_margin, 2) if np.isfinite(t_margin) else None,
            "n_days": int(d_days),
            "p_mean_ge": round(p_mean_ge, 3),
            "p_k_star_defined": round(p_exists, 3),
            "ci95_margin_bp": [round(float(x), 2) for x in ci_margin],
            "ci95_margin_lo_bp": round(float(ci_margin[0]), 2),
            "ci95_margin_hi_bp": round(float(ci_margin[1]), 2),
            "n_boot": N_BOOT,
            "boot_unit": "day",
            "frac_days_ge": round(float(np.mean(
                np.asarray(per_day.group_by("date").agg(pl.col("delta_bp").mean())["delta_bp"], float)
                >= breakeven)), 3),
        },
        "kstar": {
            "n_crossings_point": len(sols),
            "k_star": round(k_star, 1) if k_star is not None else None,
            "k_ci95": ([round(float(k_ci[0]), 1), round(float(k_ci[1]), 1)]
                       if np.isfinite(k_ci).all() else None),
            "p_defined": round(p_exists, 3),
            "c_at_k_star": round(c_at_star, 4) if c_at_star is not None else None,
            "c_required": round(c_req, 4),
            "k_at_c_required": round(k_at_c_req, 1) if k_at_c_req is not None else None,
            "c_at_k_max": round(float(ck[-1]), 4),
            "load_bearing": False,
            "note": "k_ci95 只在「k* 有定义」的重采样上取分位（此处 p_defined=0.179），"
                    "是有条件区间；c 轴恒真（c 在 k≈29 就到 0.4011、k=239 是 1.0），只作描述",
        },
        "power": {
            "n_days_available": int(d_days),
            "n_days_for_2sigma": int(n_2s) if np.isfinite(n_2s) else None,
            "shortfall_ratio": round(n_2s / d_days, 1) if np.isfinite(n_2s) and d_days else None,
            "identifiable": bool(np.isfinite(n_2s) and n_2s <= d_days),
            "note": "n_days_for_2sigma=(2·sd_day/|margin|)²，分母是本样本的余量；"
                    "余量本身随样本变化，抓完后若趋近 0，所需天数按平方发散 —— "
                    "不是「再抓够这个天数就能判」",
        },
        "verdict": {
            "decision": decision,
            "D1_seam_applicable": seam_applicable,
            "D1_seam_ok": seam_ok,
            "D2_t_abs_ge2": bool(np.isfinite(t_margin) and abs(t_margin) >= T_DECIDE),
            "D3_identifiable": bool(np.isfinite(n_2s) and n_2s <= d_days),
        },
        "noise": {
            "se_delta_k52_bp": round(se52, 2),
            "se_delta_k52_published_bp": se52_pub,
        },
    }

    tpl = (
        "cohort 自己曲线：{nc} 格 / {nd} 天（基准 {nb} 天，{full}）。"
        "端点复现：Δ(0)={d0}bp、Δ(239)={d239}bp，对既有 {d0p}/{d240p} 差 "
        "{d0g}/{d239g}bp（容差 {tol}bp，{seam}）。\n"
        "**判据在 Δ 轴**：打平线 {be}bp，Δ(239) 日等权 {dm}bp（se {se}，t={t}）"
        "⇒ 余量 {margin}bp。按天重采样整条曲线 {nboot} 次："
        "P(均值破线)={p}，P(k* 有定义)={pe}，余量 95% 区间 [{cil},{cih}]bp。\n"
        "**不可辨是结构性的**：日级 sd={sd}bp，要把 {am}bp 的余量判到 2σ 需要 "
        "{n2s} 天（现有 {nd}，差 {ratio} 倍）⇒ 判定 = {dec}。"
        "c 轴只作描述：c(k) 在 k={kcreq} 就到 {creq}、k={kmax} 是 {cmax}，"
        "所以任何 `c(k*)≥{creq}` 都恒真。"
    )
    fields = {
        "nc": "$coverage.cells",
        "nd": "$coverage.days",
        "nb": "$coverage.days_bench",
        "full": "$coverage.is_full_sample",
        "d0": "$reconcile.d0_curve_bp",
        "d239": "$reconcile.d239_curve_bp",
        "d0p": "$reconcile.d0_published_bp",
        "d240p": "$reconcile.d239_published_bp",
        "d0g": "$reconcile.d0_gap_bp",
        "d239g": "$reconcile.d239_gap_bp",
        "tol": "$reconcile.tol_bp",
        "seam": "$reconcile.applicable",
        "be": "$breakeven.line_bp",
        "dm": "$breakeven.delta_239_dayeq_bp",
        "se": "$breakeven.se_day_bp",
        "t": "$breakeven.t",
        "margin": "$breakeven.margin_bp",
        "nboot": "$breakeven.n_boot",
        "p": "$breakeven.p_mean_ge",
        "pe": "$breakeven.p_k_star_defined",
        "cil": "$breakeven.ci95_margin_lo_bp",
        "cih": "$breakeven.ci95_margin_hi_bp",
        "sd": "$breakeven.sd_day_bp",
        "am": "$breakeven.margin_bp",
        "n2s": "$power.n_days_for_2sigma",
        "ratio": "$power.shortfall_ratio",
        "dec": "$verdict.decision",
        "kcreq": "$kstar.k_at_c_required",
        "creq": "$kstar.c_required",
        "kmax": "$curve.k_max",
        "cmax": "$kstar.c_at_k_max",
    }
    out["reading_template"] = tpl
    out["reading_fields"] = fields
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["reading"] = render_reading(out)

    assert_clean(out, "cohort_curve")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in
                      ("coverage", "reconcile", "breakeven", "kstar", "power", "verdict")},
                     ensure_ascii=False, indent=1))
    print("\n" + out["reading"])


if __name__ == "__main__":
    main()
