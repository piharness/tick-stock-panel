"""MDE 公式的已知 DGP 验收：解析值 / 模拟真值。

§9.28 的 19.5 / 12.7 / 31.6 / 15.2pp 是**样本矩代进公式**得到的，公式本身
一直没被单独验过。用真实账本验也不合适：n=325、块长 40 只有 8 块，bootstrap
sd 自身的相对误差约 27%，±15% 的带子比估计量的噪声还窄——reversal 上
解析/重抽 = 0.76 就是这么来的，不是公式错。

所以验收必须换一个已知真值的 DGP：AR(1)，γ0 与 acf 都是闭式，解析 sd 可以
和 20k 次模拟的 sd 逐位对比。两个口径都验：

  vs 其余   D = X̄_w − X̄_{n−w}
  vs 全历史 D = X̄_w − X̄_n = (1 − w/n)·(X̄_w − X̄_{n−w})

判定：解析 / 真值 ∈ [0.95, 1.05]。这条不依赖账本、不依赖块长，改动公式
（比如把 γ0 换回 p(1−p)）会立刻掉出带子。

用法：
    python scripts/probe_mde_truth.py [REPS]
"""
from __future__ import annotations

import sys

import numpy as np

N, W = 325, 60
RHOS = (0.68, 0.82)
BAND = (0.95, 1.05)


def analytic_sd(rho: float, n: int = N, w: int = W) -> tuple[float, float]:
    """γ0=1 时两个口径的标准误（忽略窗口与其余的协方差，见下）。"""
    k = np.arange(1, w)
    deff_w = 1.0 + 2.0 * float(np.sum((1 - k / w) * rho ** k))
    h = n - w
    k2 = np.arange(1, h)
    deff_h = 1.0 + 2.0 * float(np.sum((1 - k2 / h) * rho ** k2))
    se_rest = float(np.sqrt(deff_w / w + deff_h / h))
    return se_rest, se_rest * (n - w) / n


def simulate(rho: float, reps: int, seed: int, n: int = N,
             w: int = W) -> tuple[float, float]:
    """AR(1) 平稳序列，返回两个口径下 D 的模拟 sd。

    协方差项不忽略：窗口与「其余」相邻，两者的均值正相关，解析式漏掉它
    会高估 sd。实测这一项在 ρ=0.82 时只有方差的 1%，所以 1.00x 的比值成立；
    若换成长记忆 DGP，这一项不再可忽略，公式要重推。
    """
    rng = np.random.default_rng(seed)
    x = np.zeros(reps)
    for _ in range(200):                       # 预热到平稳
        x = rho * x + rng.standard_normal(reps) * np.sqrt(1 - rho ** 2)
    path = np.empty((reps, n))
    for t in range(n):
        x = rho * x + rng.standard_normal(reps) * np.sqrt(1 - rho ** 2)
        path[:, t] = x
    d_rest = path[:, -w:].mean(axis=1) - path[:, :-w].mean(axis=1)
    d_all = path[:, -w:].mean(axis=1) - path.mean(axis=1)
    return float(d_rest.std(ddof=1)), float(d_all.std(ddof=1))


def main() -> None:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
    print(f"AR(1)，n={N}，w={W}，reps={reps}，γ0=1")
    print(f"{'ρ':>5}{'口径':>10}{'解析 se':>10}{'模拟 se':>10}{'解析/真值':>10}")
    bad = []
    for rho in RHOS:
        a_rest, a_all = analytic_sd(rho)
        s_rest, s_all = simulate(rho, reps, seed=int(rho * 1000))
        for label, a, s in (("vs 其余", a_rest, s_rest), ("vs 全历史", a_all, s_all)):
            ratio = a / s
            print(f"{rho:>5}{label:>10}{a:>10.4f}{s:>10.4f}{ratio:>10.3f}")
            if not BAND[0] <= ratio <= BAND[1]:
                bad.append((rho, label, ratio))
    print(f"\n判定带 [{BAND[0]}, {BAND[1]}]：" + ("全部通过" if not bad else f"失败 {bad}"))
    if bad:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
