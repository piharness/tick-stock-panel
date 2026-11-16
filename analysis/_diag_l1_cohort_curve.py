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
from datetime import date
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


def contiguous_blocks(days: list[str]) -> list[int]:
    """把日期序列切成连续块并返回各块天数。

    为什么必须有这个：抓取端断了配额之后，**缺的是整段时间**（2025-12 中旬→2026-10），
    不是随机缺样。133 天里 119 天连成一片，把它当「316 天的随机子样」会让
    `se·√(316/133)` 这类换算全部失效。块长是定义，不是描述。
    """
    if not days:
        return []
    ds = sorted(days)
    blocks, cur = [], 1
    for a, b in zip(ds, ds[1:]):
        if (date.fromisoformat(b) - date.fromisoformat(a)).days <= 7:
            cur += 1
        else:
            blocks.append(cur)
            cur = 1
    blocks.append(cur)
    return blocks


def split_stability(days: list[str], per_day: pl.DataFrame,
                    breakeven: float) -> dict:
    """按日期中位切成前后两半，各算日等权 Δ(kmaz) 的均值/se 与它们的差。

    D2 的判据是**水平**（Δ(239) ≥ 打平线），水平是窗口的函数。若两半的水平差
    已经达到 2σ，那么「用 119 天的水平代表 316 天」就是错的，哪怕 t 看起来显著。
    """
    if per_day.height < 8:
        return {"applicable": False, "why": "天数不足 8，切不出两半"}
    x = per_day.sort("date")
    n = x.height
    a = np.asarray(x["m"], float)[: n // 2]
    b = np.asarray(x["m"], float)[n // 2:]
    dates = x["date"].to_list()
    ma, mb = float(a.mean()), float(b.mean())
    sa = float(a.std(ddof=1) / np.sqrt(a.size))
    sb = float(b.std(ddof=1) / np.sqrt(b.size))
    gap = abs(ma - mb)
    z = gap / float(np.hypot(sa, sb)) if np.hypot(sa, sb) > 0 else float("inf")
    return {
        "applicable": True,
        "split_date": dates[n // 2],
        "first_half": {"days": int(a.size), "date_min": dates[0],
                       "date_max": dates[n // 2 - 1],
                       "mean_bp": round(ma, 2), "se_bp": round(sa, 2),
                       "margin_bp": round(ma - breakeven, 2)},
        "second_half": {"days": int(b.size), "date_min": dates[n // 2],
                        "date_max": dates[-1],
                        "mean_bp": round(mb, 2), "se_bp": round(sb, 2),
                        "margin_bp": round(mb - breakeven, 2)},
        "gap_bp": round(gap, 2),
        "z": round(z, 2),
        "regime_stable": bool(z < T_DECIDE),
        "why": "两半水平差 z≥2 ⇒ 水平不是常数，133 天不能代表 316 天",
    }


def caliber_crosscheck(cells: pl.DataFrame) -> dict:
    """逐格对拍既有产物的两个端点（`_diag_l1_kcurve.endpoints`）的口径。

    为什么必须逐格、不能比聚合：聚合均值会被**样本组成**污染（本样本 133 天 ≈ 2025-H2），
    于是「口径对不对」与「这 133 天是否代表 316 天」两件事在聚合上完全混在一起。
    逐格比只留口径差异，样本组成被配对消掉。没有这一步，D1 在子样本上一律
    `applicable=false` ⇒ 口径从未被检验，只是安静地跳过。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_kc", HERE / "_diag_l1_kcurve.py")
    kc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kc)
    part, daily = kc.load_part(), kc.load_daily()
    ref = part.join(
        daily.select("symbol", "date", "open", "adjf", "dvwap"),
        on=["symbol", "date"], how="inner",
    ).with_columns(
        (pl.col("b0_amt") / (pl.col("b0_vol") * 100.0)).alias("b0_vwap"),
    ).with_columns(
        ((pl.col("b0_vwap") / pl.col("b0_open") - 1.0) * 1e4).alias("d1_bp"),
        ((pl.col("dvwap") / (pl.col("open") * pl.col("adjf")) - 1.0) * 1e4).alias("d240_bp"),
    )
    kmax = int(cells["k"].max())
    mine = cells.filter(pl.col("k").is_in([0, kmax])).pivot(
        values="delta_bp", index=["date", "symbol"], on="k").rename(
        {"0": "d0_min", str(kmax): "d239_min"})
    m = ref.join(mine, on=["date", "symbol"], how="inner")
    if m.is_empty():
        return {"applicable": False, "why": "与本脚本的 cell 无交集"}
    e1 = (m["d1_bp"] - m["d0_min"]).to_numpy()
    e2 = (m["d240_bp"] - m["d239_min"]).to_numpy()
    return {
        "applicable": True,
        "cells_overlap": int(m.height),
        "bar0": {
            "max_abs_diff_bp": round(float(np.abs(e1).max()), 6),
            "n_exact": int((e1 == 0).sum()),
            "mean_diff_bp": round(float(e1.mean()), 4),
            "identical": bool(np.abs(e1).max() < 1e-6),
        },
        "full_day": {
            "max_abs_diff_bp": round(float(np.abs(e2).max()), 2),
            "mean_diff_bp": round(float(e2.mean()), 2),
            "median_diff_bp": round(float(np.median(e2)), 2),
            "mine_mean_bp": round(float(m["d239_min"].mean()), 2),
            "ref_mean_bp": round(float(m["d240_bp"].mean()), 2),
            "why": "差在「240 根分钟求和 vs 日线全日 VWAP」，不是复权（adjf 已同乘）",
        },
        "verdict": "口径逐格一致 ⇒ 与已发布均值的缺口是样本组成，不是定义",
    }


def block_contrast(days: list[str], per_day: pl.DataFrame,
                   breakeven: float) -> dict:
    """最大连续块 vs 其余日：这是**窗口之间**的差，`split_stability` 只是窗口**之内**。

    `split_stability` 在 2025-H2 内部前后切两半，两半当然一致；它无法说明
    「这 133 天能否代表 316 天」。块对比用的是位置不同的两天群，
    差值就是「换个窗口水平会移动多少」的可观测下界。
    """
    if per_day.height < 8:
        return {"applicable": False, "why": "天数不足 8"}
    ds = sorted(days)
    blocks, cur = [], [ds[0]]
    for a, b in zip(ds, ds[1:]):
        if (date.fromisoformat(b) - date.fromisoformat(a)).days <= 7:
            cur.append(b)
        else:
            blocks.append(cur)
            cur = [b]
    blocks.append(cur)
    big = max(blocks, key=len)
    rest = [d for blk in blocks if blk is not big for d in blk]
    if len(rest) < 5 or len(big) < 5:
        return {"applicable": False, "why": "块太短，对比无意义"}
    a = np.asarray(per_day.filter(pl.col("date").is_in(big))["m"], float)
    b = np.asarray(per_day.filter(pl.col("date").is_in(rest))["m"], float)
    gap = float(b.mean() - a.mean())
    se_gap = float(np.hypot(a.std(ddof=1) / np.sqrt(a.size), b.std(ddof=1) / np.sqrt(b.size)))
    return {
        "applicable": True,
        "big_block": {"days": len(big), "date_min": big[0], "date_max": big[-1],
                      "mean_bp": round(float(a.mean()), 2)},
        "rest": {"days": len(rest), "mean_bp": round(float(b.mean()), 2)},
        "gap_bp": round(gap, 2),
        "se_gap_bp": round(se_gap, 2),
        "z": round(gap / se_gap, 2) if se_gap else None,
        "why": "窗口之间的水平差；若它与「本样本对已发布均值的缺口」同量级，"
               "则那个缺口就是窗口效应，不是口径",
    }


def published_margin_check(line: float, d240_day_eq: float, se_day: float) -> dict:
    """已发布的 **316 天全样本** 上，Δ(239) 到打平线还剩多少。

    这是整条链最容易被跳过的一步：上游 `_diag_l1_kcurve` 已经算过全样本端点，
    那里的余量决定「Δ 轴判据」是不是本来就立在线上。若余量 ≪ se，那么
    「再抓 N 天」永远不会改变符号——n_days_for_2σ 会到 10^5 量级。
    """
    margin = d240_day_eq - line
    sd = se_day * np.sqrt(316.0)          # 全样本 316 天
    t = margin / se_day if se_day else float("nan")
    n2 = float((2.0 * sd / margin) ** 2) if margin else float("inf")
    return {
        "full_sample_days": 316,
        "d240_day_eq_bp": round(d240_day_eq, 2),
        "line_bp": round(line, 2),
        "margin_bp": round(margin, 2),
        "se_day_bp": round(se_day, 2),
        "t_vs_line": round(t, 2),
        "margin_pct_of_line": round(abs(margin) / line * 100.0, 2),
        "n_days_for_2sigma": int(n2) if np.isfinite(n2) else None,
        "degenerate": bool(abs(t) < T_DECIDE),
        "why": "全样本余量只有打平线的 0.4% 量级 ⇒ 符号由 121.49/20.00/6.43 "
               "三个上游数的小数位决定，不由样本量决定",
    }


def gate(decision_raw: str, is_full: bool, regime_stable: bool,
         threshold_degenerate: bool = False) -> str:
    """判定门：`decide()` 只看 t，但 t 是**在给定窗口内**的 t。

    三道非数据量必须挡在判定之前，否则「41% 的连续窗口」会被读成整体结论：
      · 待测量在全样本上就贴着判据线（余量 ≪ se）⇒ 判据本身退化，加数据无用；
      · 样本不是全量 ⇒ 只代表该窗口；
      · 两半水平不一致 ⇒ 水平本身不稳定，任何窗口的水平都不外推。
    三者都返回 `undetermined_*`，与 `breakeven_not_reachable` 严格区分。
    """
    if threshold_degenerate:
        return "undetermined_threshold_degenerate"
    if not is_full:
        return "undetermined_partial_window"
    if not regime_stable:
        return "undetermined_regime_split"
    return decision_raw


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
    d240_pub_dayeq = float(kc["endpoints"]["d240"]["day_eq_mean_bp"])
    d240_pub_se = float(kc["endpoints"]["d240"]["se_day_bp"])
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
    all_days = sorted(cells["date"].unique().to_list())
    blocks = contiguous_blocks(all_days)
    is_full = bool(n_day >= bench_days)

    # ---- D1 接缝：与既有产物的唯一可比处
    cell_d0 = float(cells.filter(pl.col("k") == 0)["delta_bp"].mean())
    cell_d239 = float(cells.filter(pl.col("k") == kk.max())["delta_bp"].mean())
    d0_gap, d239_gap = abs(cell_d0 - d1_pub), abs(cell_d239 - d240_pub)
    seam_applicable = is_full
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

    # ---- 判定：三档，不是 all_pass；且先过「口径/窗口/分段/判据退化」四道门
    decision_raw = decide(t_margin)
    per_day_m = per_day.group_by("date").agg(pl.col("delta_bp").mean().alias("m")).sort("date")
    stab = split_stability(all_days, per_day_m, breakeven)
    regime_stable = bool(stab.get("regime_stable", False))
    cc = caliber_crosscheck(cells)
    pubb = published_margin_check(breakeven, d240_pub_dayeq, d240_pub_se)
    bct = block_contrast(all_days, per_day_m, breakeven)
    decision = gate(decision_raw, is_full, regime_stable, pubb["degenerate"])

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
            "days_cohort": bench_days,
            "is_full_sample": is_full,
            "date_min": all_days[0] if all_days else None,
            "date_max": all_days[-1] if all_days else None,
            "contiguous_blocks": blocks,
            "largest_block_days": max(blocks) if blocks else 0,
            "window_note": "缺天是整段时间的缺口（抓取端配额中断），不是随机缺样："
                           "块长与位置都决定这个水平代表哪段行情",
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
            "decision_raw_t_only": decision_raw,
            "D0_window_full": is_full,
            "D1_seam_applicable": seam_applicable,
            "D1_seam_ok": seam_ok,
            "D2_t_abs_ge2": bool(np.isfinite(t_margin) and abs(t_margin) >= T_DECIDE),
            "D3_identifiable": bool(np.isfinite(n_2s) and n_2s <= d_days),
            "gate_rule": "判据退化 → undetermined_threshold_degenerate；非全量 → "
                         "undetermined_partial_window；两半水平 z≥2 → "
                         "undetermined_regime_split；三者都与 breakeven_not_reachable 区分",
        },
        "stability": stab,
        "stability_between_blocks": bct,
        "caliber_crosscheck": cc,
        "published_margin": pubb,
        "noise": {
            "se_delta_k52_bp": round(se52, 2),
            "se_delta_k52_published_bp": se52_pub,
        },
    }

    tpl = (
        "cohort 自己曲线：{nc} 格 / {nd} 天（{dmin}→{dmax}，基准 {nb} 天，{full}；"
        "最长连续块 {blk} 天）⇒ 判定 {dec}（只看 t 的话是 {draw}）。\n"
        "**逐格对拍**：bar0 端 {nex} 格与既有产物逐位相同（最大差 {b0d}bp），"
        "全日端差 {fdd}bp（分钟求和 vs 日线全天 VWAP）⇒ 口径一致，"
        "所以与已发布均值的 {d239g}bp 缺口是**样本组成**，不是定义。\n"
        "端点复现：Δ(0)={d0}bp、Δ(239)={d239}bp，对既有 {d0p}/{d240p} 差 "
        "{d0g}/{d239g}bp（容差 {tol}bp；{seam}，因为子样本本来不同）。\n"
        "**判据已经退化**：全样本 {pfd} 天上 Δ(239) 日等权 {pdm}bp、打平线 {be}bp，"
        "余量只有 {pmar}bp（{pmarp}% 线，t={pt}）⇒ 2σ 需 {pn2} 天。"
        "本样本余量 {margin}bp 是**窗口**给的，不是数据量：窗口间水平差 {bgap2}bp"
        "（z={bz2}）与这个缺口同量级。\n"
        "**本样本读数**：Δ(239) 日等权 {dm}bp（se {se}，t={t}）；按天重采样整条曲线 "
        "{nboot} 次：P(均值破线)={p}，P(k* 有定义)={pe}，"
        "余量 95% 区间 [{cil},{cih}]bp。日级 sd={sd}bp，"
        "要把 {am}bp 判到 2σ 需 {n2s} 天（现有 {nd}，差 {ratio} 倍）——"
        "这个天数只在**该窗口的余量**下成立，随余量趋 0 平方发散。\n"
        "c 轴只作描述：c(k) 在 k={kcreq} 就到 {creq}、k={kmax} 是 {cmax}，"
        "所以任何 `c(k*)≥{creq}` 都恒真。"
    )
    fields = {
        "nc": "$coverage.cells",
        "nd": "$coverage.days",
        "dmin": "$coverage.date_min",
        "dmax": "$coverage.date_max",
        "blk": "$coverage.largest_block_days",
        "dec": "$verdict.decision",
        "draw": "$verdict.decision_raw_t_only",
        "nex": "$caliber_crosscheck.cells_overlap",
        "b0d": "$caliber_crosscheck.bar0.max_abs_diff_bp",
        "fdd": "$caliber_crosscheck.full_day.mean_diff_bp",
        "pfd": "$published_margin.full_sample_days",
        "pdm": "$published_margin.d240_day_eq_bp",
        "pmar": "$published_margin.margin_bp",
        "pmarp": "$published_margin.margin_pct_of_line",
        "pt": "$published_margin.t_vs_line",
        "pn2": "$published_margin.n_days_for_2sigma",
        "bgap2": "$stability_between_blocks.gap_bp",
        "bz2": "$stability_between_blocks.z",
        "ba": "$stability.first_half.mean_bp",
        "bb": "$stability.second_half.mean_bp",
        "bgap": "$stability.gap_bp",
        "bz": "$stability.z",
        "stab": "$stability.regime_stable",
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
