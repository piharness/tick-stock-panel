"""判定器的**错误率**：`lcb > 0` 在零假设下有多常发生。

这一条测试是本目录里唯一不看结构、只看结果的：前面所有验收（单调、截断、
标注一致）在 `se` 系统性偏小 18% 的情况下**全部照常通过**——它们约束的是
顺序与文案，不约束尺度。尺度错了，面板上的 `PASS` 就是两倍错误率，而且
加样本不会好转。

真值从解析式来，不从模拟来：重叠 `H` 日、日收益 iid 时

    γ_k = γ_0·(H-k)/H,  k = 0..H-1      LRV = H·γ_0

所以 `se_true = sqrt(H·γ_0/n)`，不需要跑参照实现。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.decision import settle

H = 20
LAG = H - 1
N = 600
REPS = 400


def _overlap_series(n: int, reps: int, rng: np.random.Generator) -> np.ndarray:
    """等权 MA(H-1)：重叠 H 日收益的自协方差形状，γ0=1、LRV=H。"""
    e = rng.standard_normal((reps, n + LAG))
    c = np.cumsum(e, axis=1)
    head = np.concatenate([np.zeros((reps, 1)), c[:, :-LAG - 1]], axis=1)
    return (c[:, LAG:] - head) / math.sqrt(H)


def _drift_series(n: int, reps: int, rng: np.random.Generator) -> np.ndarray:
    """MA(H-1) + AR(1) ρ=0.995：记忆**超过** H-1 的情形。"""
    x = _overlap_series(n, reps, rng)
    rho = 0.995
    z = np.empty((reps, n))
    z[:, 0] = rng.standard_normal(reps)
    for t in range(1, n):
        z[:, t] = rho * z[:, t - 1] + math.sqrt(1 - rho ** 2) * rng.standard_normal(reps)
    return x + z


def _run(rows: np.ndarray, kernel: str) -> tuple[float, float]:
    """返回 (se/se_true 均值, H0 下 lcb>0 的比例%)。"""
    sized = 0
    ratios = []
    for row in rows:
        est = settle.hac_lcb(row.tolist(), LAG, kernel=kernel)
        assert est is not None
        _, se, lcb, _ = est
        ratios.append(se)
        sized += lcb > 0
    se_true = math.sqrt(H / rows.shape[1])     # γ0 = 1 ⇒ se_true = sqrt(H/n)
    return float(np.mean(ratios)) / se_true, 100.0 * sized / len(rows)


def test_truncated_kernel_is_unbiased_and_has_nominal_size():
    """默认核：`se` 对得上真值，H0 下误报率 ≈ 5%。"""
    rows = _overlap_series(N, REPS, np.random.default_rng(11))
    ratio, size = _run(rows, settle.KERNEL_TRUNCATED)
    assert ratio == pytest.approx(1.0, abs=0.06), f"se/真值 = {ratio:.3f}"
    assert size <= 8.0, f"H0 误报率 {size:.1f}%（门槛 8%）"


def test_bartlett_kernel_reproduces_the_known_bias():
    """旧核：概率极限 `sqrt(2/3) = 0.817`，误报率约 9%。

    留着这条不是为了用它，是为了让「为什么换核」在测试里可查：一旦有人把
    默认值改回去，这条会和上一条一起红。
    """
    rows = _overlap_series(N, REPS, np.random.default_rng(11))
    ratio, size = _run(rows, settle.KERNEL_BARTLETT)
    assert ratio == pytest.approx(math.sqrt(2 / 3), abs=0.04), f"se/真值 = {ratio:.3f}"
    assert size >= 7.0, f"旧核误报率只有 {size:.1f}%，偏差模型可能变了"


def test_drift_dgp_is_not_fixed_by_kernel_choice():
    """**已知边界**：记忆超过 H-1 时，任何固定带宽的核都控不住误报率。

    这条测试断言的是「没修好」。它存在的意义是挡住一句更容易说的话——
    「换核之后 size = 5%」。真实账本上慢漂移是可能的（战术胜率可以有几个月
    的持续期），所以面板上的 `PASS` 在这个方向仍然没有保护。要真正解决，
    得让带宽随 n 增长或者改用 block bootstrap，那是另一件事；在那之前，
    这个数字必须留在这里。
    """
    rows = _drift_series(N, 200, np.random.default_rng(12))
    for kernel in (settle.KERNEL_TRUNCATED, settle.KERNEL_BARTLETT):
        _, size = _run(rows, kernel)
        assert size > 20.0, f"{kernel} 在漂移 DGP 上误报率 {size:.1f}%，边界变了？"


def test_lrv_is_floored_at_gamma0():
    """截断核不得给出 `se < se_iid`：重叠序列上 `LRV ≥ γ0` 是硬下界。

    没有这条下界时，样本里的负自相关会把 `LRV` 压到 0 ⇒ `se = 0` ⇒
    `lcb = mean`，等于宣称零误差（iid 压力测试里 2.5% 的重复踩到）。
    """
    # 交替正负：一阶自相关强负，裸截断核会算出负的 LRV。
    vals = [1.0, -1.0] * 30
    mean, se, lcb, se_iid = settle.hac_lcb(vals, 5, kernel=settle.KERNEL_TRUNCATED)
    assert se >= se_iid
    assert lcb < mean
