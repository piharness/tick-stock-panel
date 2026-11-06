"""真实账本的设计效应：把「600 条 pick 等于几个独立观测」从假设变成读数。

§9.27 的结论停在「ρ=0.3 是 31 个、ρ=0.6 是 13 个」，而 ρ 是模拟里选的参数，
真实账本从来没测过。本文用 `data/.probe_per_day.json`（`probe_window.py` 用
真实行情重建的逐日命中，325 个决策日 × 20 picks × H=20）直接测三件事：

  1. **日胜率的自相关**。实测 lag1 = 0.68–0.88，与「持有 20 天」能给出的
     上限 0.95 同量级；但衰减比三角核快（lag10 实测 0.27–0.39，三角核给
     0.50）。机制上说得通：同一批股票被连续持有，相邻两天的卡几乎是同一个
     事件。这里只报矩，不裁定机制。
  2. **分块方差曲线** DEFF(B)：把序列切成 B 天一块，看块均值的方差相对
     独立掷币放大多少倍。B≥相关长度后曲线走平 ⇒ 那个平台就是设计效应。
  3. **由 DEFF 反推的 MDE**：把 §9.27 的「信息下限」公式里的 deff 换成实测值。

三个读数都不是模型推断，是样本矩。

⚠️ 一个必须说清的边界：**自相关里混着真实的行情漂移**。零假设要求「p 不
随时间变」，而真实序列的 p 确实在变——所以实测 acf 同时包含「重叠/同股票」
和「策略边际在漂」两种成分。这**不是保守假设，也不是可以打折的上界**：
把漂移拆掉会得到一个更小的 DEFF，但那等于换零假设（允许边际漂移），回答的
是另一个问题。下面的 DEFF 是这份序列在现行定义下的方差刻度，按它算的 MDE
不能再被解释成「真值只会更好」。

用法：
    python scripts/probe_real_deff.py [STRATEGY]
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / ".probe_per_day.json"
_ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
ONLY = _ARGS[0] if _ARGS else None
#: 缓存会随行情同步增长，同一份文档的读数必须固定样本。`--nmax=325` 取最早的
#: 325 个决策日，与 §9.28 的表对齐；不传则用全部。
NMAX = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--nmax=")), None)
BLOCKS = (1, 2, 4, 8, 10, 16, 20, 32, 40, 60, 80)


def load() -> dict[str, dict[str, list[int]]]:
    if not CACHE.exists():
        raise SystemExit(f"缺少 {CACHE}；先跑 `python scripts/probe_window.py` 生成")
    return json.loads(CACHE.read_text(encoding="utf-8"))


def acf(x: list[float], max_lag: int) -> list[float]:
    n = len(x)
    mu = sum(x) / n
    dev = [v - mu for v in x]
    g0 = sum(d * d for d in dev) / n
    if g0 <= 0:
        return [0.0] * max_lag
    return [sum(dev[i] * dev[i + k] for i in range(n - k)) / (n - k) / g0
            for k in range(1, min(max_lag, n - 1) + 1)]


def deff_window(rho: list[float], w: int) -> float:
    """日级序列在窗口 w 上的设计效应：1 + 2Σ(1-k/w)ρ_k。"""
    return 1.0 + 2.0 * sum((1 - k / w) * rho[k - 1]
                           for k in range(1, min(w, len(rho) + 1)))


def mde_two_prop(gamma0: float, deff_w: float, w: int,
                 deff_h: float, h: int) -> float:
    """80% 功效、5% 单侧下两比例检验的最小可检出抬升（pp）。

    `gamma0` 必须是**日胜率**的方差 `Var(hits_d / m)`，不是逐 pick 的
    `p(1-p)`。`deff_window` 的输入是日胜率序列，它度量的是日方差被放大
    几倍，所以两项都乘 `gamma0`。

    代入 `p(1-p)` 等价于把 `w` 天当成 `w` 条独立 pick：漏了每天 `m` 条的
    稀释（`/m`），也漏了日内相关（`DEFF(1)`）；两项合起来让隐含的 `se`
    恰好退化成「单日 sd」（momentum 上 0.1905 vs 实测 0.1875）。§9.28
    的 37–60pp 就是这么来的。
    """
    se = (gamma0 * deff_w / w + gamma0 * deff_h / h) ** 0.5
    return 2.486 * se * 100


def block_sd(series: list[float], w: int, block: int = 40,
             reps: int = 1000, seed: int = 0) -> float:
    """循环块重抽下「窗口均值 − 其余均值」的标准误。

    块长取 40：按持有期 H 分块只消掉重叠，消不掉更慢的那一层（§9.28 四）。
    循环拼接把序列首尾接起来，**破坏漂移**，所以这个数偏低、对应的 MDE 是
    下界。固定种子 ⇒ 同一份缓存每次跑出同一个数。
    """
    n = len(series)
    rng = random.Random(seed)
    nb = math.ceil(n / block)
    vals = []
    for _ in range(reps):
        idx: list[int] = []
        for _ in range(nb):
            s = rng.randrange(n)
            idx.extend((s + k) % n for k in range(block))
        z = [series[i] for i in idx[:n]]
        vals.append(sum(z[-w:]) / w - sum(z[:n - w]) / (n - w))
    mu = sum(vals) / len(vals)
    return (sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def main() -> None:
    data = load()
    print(f"{'=' * 78}\n真实账本的设计效应（{CACHE.name}）\n{'=' * 78}")

    for name, per_day in data.items():
        if ONLY and name != ONLY:
            continue
        days = sorted(per_day)
        if NMAX:
            days = days[:NMAX]
        m = len(per_day[days[0]])
        series = [sum(per_day[d]) / len(per_day[d]) for d in days]
        n = len(series)
        p = sum(series) / n
        v = p * (1 - p)
        # 日胜率的方差，MDE 的分母。它与 `v` 差 `DEFF(1)/m` 倍：
        # 20 条 pick 的日内相关把日方差抬到 `v/m` 的 2.2–5.5 倍。
        gamma0 = sum((x - p) ** 2 for x in series) / n

        # 日内相关：把每天 20 个 0/1 的组内方差和边缘方差比。
        within = 0.0
        for d in days:
            row = per_day[d]
            mu = sum(row) / len(row)
            within += sum((x - mu) ** 2 for x in row) / (len(row) - 1)
        within /= n
        rho_w = 1 - within / v

        rho = acf(series, min(120, n // 3))
        print(f"\n【{name}】 {n} 个决策日 × {m} picks，胜率 {p:.3f}"
              f"（{days[0]} … {days[-1]}）")
        print(f"   日胜率 sd = {math.sqrt(sum((x - p) ** 2 for x in series) / n):.4f}"
              f"（独立掷币下界 {math.sqrt(v / m):.4f}）")
        print(f"   日内相关 ρ_w = {rho_w:+.3f}"
              f"（{m} 条 pick ≈ {m / (1 + (m - 1) * max(rho_w, 0)):.1f} 个独立观测）")
        print("   自相关 lag1..10：" + " ".join(f"{x:+.2f}" for x in rho[:10]))
        print(f"   参照：纯「持有 20 天重叠」的三角核给 lag1={19 / 20:.2f}、"
              f"lag10={10 / 20:.2f}；实测 {rho[0]:+.2f} / {rho[9]:+.2f}")

        print(f"   {'B':>4}{'块数':>6}{'DEFF(B)':>10}")
        curve: dict[int, float] = {}
        for b in BLOCKS:
            nb = n // b
            if nb < 4:
                continue
            means = [sum(series[i * b:(i + 1) * b]) / b for i in range(nb)]
            mu = sum(means) / nb
            var = sum((x - mu) ** 2 for x in means) / (nb - 1)
            curve[b] = var / (v / (b * m))
            print(f"   {b:>4}{nb:>6}{curve[b]:>10.1f}")

        plateaus = [curve[b] for b in curve if b >= 20]
        deff_plateau = sum(plateaus) / len(plateaus) if plateaus else 0.0
        print(f"   → B≥20 的平台 DEFF ≈ {deff_plateau:.1f}"
              f"（{n} 天 × {m} picks ≈ {n * m / max(deff_plateau, 1e-9):.0f} 个独立观测）")

        for w in (60, 120, 250):
            if w >= n:
                continue
            dw = deff_window(rho, w)
            dh = deff_window(rho, n - w)
            mde = mde_two_prop(gamma0, dw, w, dh, n - w)
            # `p_clim` 是**全部**已可见日，包含窗口本身。窗口与全历史的差是
            # `(1-w/n)×(窗口 − 其余)`，方差再小 `(1-w/n)²` 倍。两个口径都要印：
            # 句子说「vs 历史」时用的是后者，而 `h=n-w` 算的是前者。
            mde_all = mde * (n - w) / n
            line = (f"   window={w:<4} DEFF={dw:>5.1f} ⇒ n_eff={w / dw:>4.1f}"
                    f" ⇒ MDE(vs其余)≈{mde:>5.1f}pp"
                    f"  MDE(vs全历史)≈{mde_all:>5.1f}pp")
            if w == 60:
                boot = block_sd(series, w)
                boot_pp = 2.486 * boot * 100
                line += (f"  | 块重抽(vs其余) {boot_pp:>5.1f}pp"
                         f"，解析/重抽={mde / boot_pp:.2f}")
            print(line)

    print("\n读法：DEFF 是「这条序列的方差比独立掷币大几倍」。它出现在三个地方——"
          "\n分母（n_eff）、门槛的 se、以及 CUSUM 的每一步；任何一处用 1 代替它，"
          "\n得到的区间都会窄 sqrt(DEFF) 倍。")
    print("\n⚠️ 这个 DEFF 含真实漂移，是**这份序列的方差**，不是「上界」。"
          "\n零假设（p 不随时间变）不成立时，「观测到的自相关」无法区分「重叠」"
          "\n与「边际在漂」；拆掉漂移等于换零假设，会得到另一个数。所以这里的 "
          "\nMDE 是现行定义下的刻度，不能再当成「可以打折的保守估计」。")


if __name__ == "__main__":
    main()
