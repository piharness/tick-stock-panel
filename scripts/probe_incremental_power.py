"""同一批被告、同一条判据，换四个统计量：谁能在 5% 假警下检出多小的真实变点？

§9.26 证明了 `verdict_incremental` 的被检量在平稳过程里期望为负 ⇒ 它永远 FAIL。
但那一步只否证了**这一个统计量**，没有回答「这个问题本身可不可答」。

本脚本把四个候选放在同一 DGP 上，用**同一条判据**比较：

    MDE(80%) = 让检出率达到 80% 所需的最小真实胜率抬升 a

每个统计量的门槛取它在**平稳 DGP 上的 95 分位**（size 固定为 5%）。
比较的是信息量，不是谁的门槛更松。

    A  published   mean(d)，d = (p_clim-y)^2 - (p-y)^2       §9.26 的被检量
    B  corrected   mean(d - (σ_c²-σ_w²))，逐日 plug-in         §9.26 出路 1
    C  score       window vs 窗口之前的计数，两比例 z           经典同质性检验
    D  cusum       Page CUSUM，对逐日标准化命中数（γ0）          变点检验
    D* cusum LRV   同一个 D，尺度换成截断核长程方差               §9.28 的修正

A/B 是**全程路径平均**（现行口径），C/D 是**末端读数**（今天的账本）。这个差别
本身是结论的一部分：变点落在最后 120 天时，前面 770 天的「无变点」会把路径
平均往回拉。

两个 DGP：

  `iid`      逐日 pick 独立（与 `probe_incremental_null.py` 一致）。9000 个独立
             Bernoulli ⇒ 有效样本就是 9000。
  `overlap`  每张卡的 H 个持有期共享行情冲击 ⇒ 相邻决策日的结果相关，日内 10 个
             pick 也相关。这才是真实账本的形状：9000 条观测的有效样本远小于 9000。

`overlap` 是关键词：在它下面，`window=60` 只含约 `60/H` 个不重叠块。如果结论在
两种 DGP 下不同，分歧点就不是统计量，而是**窗口长度本身**。

用法：

    python scripts/probe_incremental_power.py [DAYS] [REPS] [iid|overlap|both] [RHO]

`RHO` 只对 `overlap` 生效：行情因子占 latent 的方差份额。ρ=0 时它与 `iid`
在边缘分布上等价，但保留了持有期重叠带来的跨日相关。
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "backend"))

from app.decision import pwin, settle  # noqa: E402

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 900
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 200
MODE = sys.argv[3] if len(sys.argv) > 3 else "both"
RHO = float(sys.argv[4]) if len(sys.argv) > 4 else 0.6

PICKS = 10
HORIZON = 10
WINDOW = 60
KAPPA = settle.DEFAULT_KAPPA
P0 = 0.5
CUSUM_K = 0.5
BURN = 120
ALPHAS = (0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30)


def _shrink(hits: int, n: int, prior: float, kappa: float) -> float:
    return (hits + kappa * prior) / (n + kappa)


def _sigma2(n: int, p: float, kappa: float) -> float:
    """收缩估计量的方差：n·p(1-p)/(n+kappa)^2。"""
    return n * p * (1 - p) / (n + kappa) ** 2


def _inv_norm(p: float) -> float:
    """Acklam 有理逼近，误差 < 1.2e-9。"""
    if p <= 0.0:
        return -9.0
    if p >= 1.0:
        return 9.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - pl:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _draws(rates: list[float], seed: int, mode: str) -> list[list[int]]:
    """每日一批 0/1。`overlap` 让 H 期收益重叠、日内 pick 共享行情。"""
    rng = random.Random(seed)
    n = len(rates)
    if mode == "iid":
        return [[1 if rng.random() < p else 0 for _ in range(PICKS)] for p in rates]
    # 会话级行情：卡 i 吃 z[i+1..i+H]，相邻卡因此共享 H-1 个会话
    sessions = [rng.gauss(0, 1) for _ in range(n + HORIZON + 2)]
    a, b = RHO ** 0.5, (1 - RHO) ** 0.5
    outs = []
    for i, p in enumerate(rates):
        seg = sessions[i + 1:i + 1 + HORIZON]
        zbar = sum(seg) / len(seg) * HORIZON ** 0.5      # 方差归一
        mu = _inv_norm(p)                                 # P(y=1)=p，因 a²+b²=1
        row = []
        for _ in range(PICKS):
            row.append(1 if mu + a * zbar + b * rng.gauss(0, 1) > 0 else 0)
        outs.append(row)
    return outs


def replay(outs: list[list[int]]) -> dict:
    """O(n) 重放。可见集恒为前缀，窗口与全史都走前缀和。

    可见性规则与 `pwin.walk_forward` 逐字一致：`exit_j = cal[j+1+H]`，
    `exit_j < today` ⇔ `j <= i-H-2`。
    """
    n = len(outs)
    pref_h = [0] * (n + 1)
    pref_n = [0] * (n + 1)
    for i in range(n):
        pref_h[i + 1] = pref_h[i] + sum(outs[i])
        pref_n[i + 1] = pref_n[i] + len(outs[i])

    d, d_corr = [], []
    for i in range(n):
        last = i - HORIZON - 2
        if last < 0:
            continue
        start = max(0, last - WINDOW + 1)
        n_w = pref_n[last + 1] - pref_n[start]
        h_w = pref_h[last + 1] - pref_h[start]
        n_c = pref_n[last + 1]
        h_c = pref_h[last + 1]
        if n_w == 0:
            continue
        p = _shrink(h_w, n_w, P0, KAPPA)
        p_clim = _shrink(h_c, n_c, P0, KAPPA)
        o = outs[i]
        d.append(sum((p_clim - y) ** 2 - (p - y) ** 2 for y in o) / len(o))
        d_corr.append(d[-1] - (_sigma2(n_c, h_c / n_c, KAPPA)
                               - _sigma2(n_w, h_w / n_w, KAPPA)))

    last = n - 1 - HORIZON - 2
    start = max(0, last - WINDOW + 1)
    return {"d": d, "d_corr": d_corr,
            "n_w": pref_n[last + 1] - pref_n[start],
            "h_w": pref_h[last + 1] - pref_h[start],
            "n_old": pref_n[start], "h_old": pref_h[start],
            "outs": outs}


def _lrv(x: list[float], lag: int) -> float:
    """截断核长程方差：γ0 + 2Σγk。与 `settle.hac_lcb` 同一个核、同一个 lag。"""
    n = len(x)
    mu = sum(x) / n
    dv = [v - mu for v in x]
    g0 = sum(d * d for d in dv) / n
    total = g0
    for k in range(1, min(lag, n - 1) + 1):
        total += 2 * sum(dv[i] * dv[i + k] for i in range(n - k)) / (n - k)
    return max(total, g0)


def _t_cusum(outs: list[list[int]], scale: str = "g0") -> float:
    """Page CUSUM：p0 与逐日尺度都由 burn-in 估，再标准化累加，取路径最大值。

    尺度必须实测：`hits_t` 的日间相关让 `sqrt(n·p0(1-p0))` 低估 sd 好几倍，
    用理论 sd 会把 `k` 变成一个相对噪声极小的数，CUSUM 退化成随机游走。
    它不读 `window`——用全部历史，所以不受 60 天窗口的有效样本限制。

    `scale` 有两种口径，差在**除以哪一条方差**：

      "g0"   逐日方差 γ0。累加后的路径方差是 n·LRV 而不是 n·γ0，所以这条
             路径的步长被放大了 √DEFF 倍——阈值跟着被放大，功效被吃掉。
      "lrv"  长程方差 LRV = γ0·DEFF。路径在零假设下才是单位步长的随机游走，
             门槛与教科书一致（实测名义 5% 下 size=2–4%）。

    **修好刻度不修功效**（§9.28）：`scale="lrv"` 让 size 回到 5%，但 MDE 仍是
    `>0.30`——因为约束不在刻度，在「对 780 个候选变点取最大值」这个动作本身
    （多重比较惩罚随路径长度增长，而 C 只比一个位置）。
    """
    burn = [sum(o) / len(o) for o in outs[:BURN]]
    p0 = sum(burn) / max(1, len(burn))
    p0 = min(max(p0, 0.02), 0.98)
    if scale == "lrv":
        var = _lrv(burn, HORIZON - 1)
    else:
        var = sum((x - p0) ** 2 for x in burn) / max(1, len(burn) - 1)
    s0 = var ** 0.5
    if s0 <= 1e-9:
        return 0.0
    s = 0.0
    best = 0.0
    for o in outs[BURN:]:
        s = max(0.0, s + (sum(o) / len(o) - p0) / s0 - CUSUM_K)
        best = max(best, s)
    return best


def stats(rep: dict) -> dict:
    mean, _, lcb, n_eff, _ = pwin._mean_se_lcb(rep["d"], HORIZON - 1)
    m2, _, _, ne2, _ = pwin._mean_se_lcb(rep["d_corr"], HORIZON - 1)
    n_w, h_w, n_o, h_o = rep["n_w"], rep["h_w"], rep["n_old"], rep["h_old"]
    pw, po = h_w / n_w, h_o / n_o
    pool = (h_w + h_o) / (n_w + n_o)
    se_p = (pool * (1 - pool) * (1 / n_w + 1 / n_o)) ** 0.5
    return {"A": mean, "A_lcb": lcb, "A_neff": n_eff,
            "B": m2, "B_neff": ne2, "p_w": pw, "p_o": po,
            "C": (pw - po) / se_p if se_p > 0 else 0.0,
            "D": _t_cusum(rep["outs"], "g0"),
            "D_lrv": _t_cusum(rep["outs"], "lrv")}


def run(rates: list[float], reps: int, mode: str) -> list[dict]:
    return [stats(replay(_draws(rates, seed, mode))) for seed in range(reps)]


def q(vals: list[float], qq: float) -> float:
    s = sorted(vals)
    k = (len(s) - 1) * qq
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def mde(tau: float, cells: dict[float, list[dict]], key: str) -> float | None:
    for a in sorted(cells):
        if a == 0:
            continue
        if sum(1 for r in cells[a] if r[key] > tau) / len(cells[a]) >= 0.80:
            return a
    return None


def one(mode: str) -> None:
    print(f"\n{'=' * 74}\nDGP = {mode}（{DAYS} 日 × {PICKS} picks，H={HORIZON}，"
          f"window={WINDOW}，reps={REPS}）")
    base = run([P0] * DAYS, REPS, mode)
    cells: dict[float, list[dict]] = {0.0: base}
    for a in ALPHAS[1:]:
        cells[a] = run([P0] * (DAYS - BURN) + [P0 + a] * BURN, REPS, mode)

    # 设计效应：窗口率的跨重抽 sd 相对独立掷币 sd 的平方。
    # 这是「600 条 pick 相当于几个独立观测」的实测值，不依赖任何理论假设。
    sd_pw = (sum((r["p_w"] - q([x["p_w"] for x in base], 0.5)) ** 2
                 for r in base) / (len(base) - 1)) ** 0.5
    sd_iid = (0.25 / (WINDOW * PICKS)) ** 0.5
    deff = (sd_pw / sd_iid) ** 2
    print(f"窗口胜率 p_w 的跨重抽 sd = {sd_pw:.4f}（独立掷币 {sd_iid:.4f}"
          f" ⇒ 设计效应 {deff:.1f}×，{WINDOW * PICKS} 条 pick ≈ "
          f"{WINDOW * PICKS / deff:.0f} 个独立观测）")

    keys = [("A", "A published"), ("B", "B corrected"), ("C", "C score z"),
            ("D", "D cusum γ0"), ("D_lrv", "D cusum LRV")]
    taus = {k: q([r[k] for r in base], 0.95) for k, _ in keys}

    print("\n-- 检出率（门槛 = 平稳 95 分位；a=0 行是 size 自检，应≈5%）--")
    print(f"{'a':>6}" + "".join(f"{n:>14}" for _, n in keys))
    for a in ALPHAS:
        row = [sum(1 for r in cells[a] if r[k] > taus[k]) / len(cells[a]) for k, _ in keys]
        print(f"{a:>6.2f}" + "".join(f"{v:>14.0%}" for v in row))

    print("\n-- MDE(80%)：需要多大的真实抬升 --")
    for k, name in keys:
        m = mde(taus[k], cells, k)
        print(f"   {name:<14}{'>%.2f' % max(ALPHAS) if m is None else '%.2f（%.0fpp）' % (m, m * 100)}")
    # 同质性检验的信息下限：把设计效应算进两比例 SE 后的 80% 功率点
    n_o = (DAYS - WINDOW) * PICKS
    floor = 2.486 * (0.25 * deff * (1 / (WINDOW * PICKS) + 1 / n_o)) ** 0.5
    print(f"   {'信息下限':<14}{floor:.2f}（{floor * 100:.0f}pp）"
          f"  ← 任何统计量都不可能低于它")

    print("\n-- 现行判定规则（lcb>0.001 且 n_eff>=30）--")
    print(f"{'a':>6}{'PASS':>9}{'可判定':>9}")
    for a in ALPHAS:
        rs = cells[a]
        p = sum(1 for r in rs if r["A_lcb"] > pwin.MIN_INCREMENTAL_EFFECT
                and r["A_neff"] >= settle.MIN_NEFF) / len(rs)
        t = sum(1 for r in rs if r["A_neff"] >= settle.MIN_NEFF) / len(rs)
        print(f"{a:>6.2f}{p:>9.0%}{t:>9.0%}")


def main() -> None:
    for mode in (["iid", "overlap"] if MODE == "both" else [MODE]):
        one(mode)
    print("\n读法：size 行由构造保证 5%，所以列间可比的是功率。A→B 是「换门槛」"
          "\n能买到的全部；B→D 是「换问题」能买到的全部。")


if __name__ == "__main__":
    main()
