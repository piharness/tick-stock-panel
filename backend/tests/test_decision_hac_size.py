"""判定器的**错误率**：`lcb > 0` 在零假设下有多常发生。

这一条测试是本目录里唯一不看结构、只看结果的：前面所有验收（单调、截断、
标注一致）在 `se` 系统性偏小 18% 的情况下**全部照常通过**——它们约束的是
顺序与文案，不约束尺度。尺度错了，面板上的 `PASS` 就是两倍错误率，而且
加样本不会好转。

真值从解析式来，不从模拟来：重叠 `H` 日、日收益 iid 时

    γ_k = γ_0·(H-k)/H,  k = 0..H-1      LRV = H·γ_0

所以 `se_true = sqrt(H·γ_0/n)`，不需要跑参照实现。

文件末尾三条是**探针的护栏**（不跑模拟）：`scripts/probe_hac_size.py` 的
「LRV → se」全链路必须与 `settle.hac_lcb` 逐位一致，而 `n_eff` 那一路必须与
`settle.effective_sample` 一致。它们守的是第六处同型缺陷：探针曾经对四条
估计量统一按 `max(lr, 0)` 收底，而生产截断核收的是 `max(lr, γ0)`。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from app.decision import settle

H = 20
LAG = H - 1
N = 600
REPS = 400

REPO = Path(__file__).resolve().parents[2]


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "probe_hac_size", REPO / "scripts" / "probe_hac_size.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    """旧核：概率极限 `sqrt(267/400) = 0.817`，误报率约 9%。

    `2/3 + 1/(3H²)` 是这支核在 H=20 的闭式（267/400），`sqrt(2/3) = 0.8165`
    只是 H→∞ 的连续极限——两者差在第四位小数，容差 0.04 看不出来，所以这里
    写清楚：断言常量必须是 267/400，不是 2/3。同一处笔误在 `settle.py` 的核
    注释和 `docs/decision-ledger.md` §9.23 的更正小节里各记了一次。

    留着这条不是为了用它，是为了让「为什么换核」在测试里可查：一旦有人把
    默认值改回去，这条会和上一条一起红。
    """
    rows = _overlap_series(N, REPS, np.random.default_rng(11))
    ratio, size = _run(rows, settle.KERNEL_BARTLETT)
    assert ratio == pytest.approx(math.sqrt(267 / 400), abs=0.04), f"se/真值 = {ratio:.3f}"
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


def _floor_probes() -> list[np.ndarray]:
    """会触发两支核 floor 的向量：交替 ±1 + 负相关 AR，外加一支不触发的核层向量。"""
    rng = np.random.default_rng(4242)
    out = [np.convolve(rng.normal(size=345), np.ones(H), mode="valid"),
           np.tile([1.0, -1.0], 40)]
    for phi in (-0.8, -0.5, -0.2, 0.2, 0.5, 0.8):
        e = rng.standard_normal(80)
        y = np.empty(80)
        y[0] = e[0]
        for t in range(1, 80):
            y[t] = phi * y[t - 1] + e[t]
        out.append(y)
    return out


@pytest.mark.parametrize("kernel", [settle.KERNEL_TRUNCATED, settle.KERNEL_BARTLETT])
def test_probe_se_matches_production_including_the_floor(kernel):
    """探针的 `se` 必须逐位等于 `settle.hac_lcb`——**比 se，不比 `se²·n`**。

    `se²·n` 只在 floor 不绑定时才等于 LRV，所以拿它当桥的护栏会不自觉地把
    探测向量限死在 floor 不触发的那一支；旧护栏正是这样把缺陷排除在断言之外，
    而它的 docstring 里就写着「±1 向量会合理地红」。比 `se` 不需要挑向量。

    断言里带覆盖检查：至少有一个向量真的让 floor 绑定，否则这条测试会静默
    退化成「只查核」。**只对截断核要求绑定**：Bartlett 的 `lr = d'Wd/n`，`W`
    是 Bartlett 核的 Toeplitz 矩阵、半正定，所以 `lr ≥ 0` 恒成立，那句
    `max(lr, 0)` 一个比特都改不动（2 万条随机差分序列 + p∈[2,200] 正弦扫描
    的最小比值是 0.0017，从未为负）。截断核没有这个性质，`max(lr, γ0)` 在
    iid 抽样上绑定 58–69%——**唯一会绑定的 floor，正是被漏掉的那一支**。
    """
    P = _load_probe()
    fn = (P._lrv_trunc if kernel == settle.KERNEL_TRUNCATED else P._lrv_bartlett)
    bound = 0
    for i, v in enumerate(_floor_probes()):
        g0 = float(np.var(v))
        lr = float(fn(v[None, :], LAG)[0])
        got = float(P._se(np.array([lr]), np.array([g0]), v.size, kernel)[0])
        want = settle.hac_lcb(v.tolist(), LAG, kernel=kernel)[1]
        assert got == pytest.approx(want, rel=1e-9), f"探测向量 {i}（n={v.size}）"
        floor_const = g0 if kernel == settle.KERNEL_TRUNCATED else 0.0
        bound += lr < floor_const
    if kernel == settle.KERNEL_TRUNCATED:
        assert bound > 0, "没有一个向量触发 floor——这条护栏又只看得见核了"
    else:
        assert bound == 0, "Bartlett 的 floor 竟然绑定了：半正定论证或 lag 变了"


def test_probe_neff_matches_effective_sample():
    """`PASS%` 的分母也有一份副本：向量化 `n_eff` 必须等于生产函数。

    `n_eff < MIN_NEFF` 时判定是 UNKNOWN、错不到读者眼前，所以 `size|PASS%`
    比 `size%` 更接近面板上真正印出来的错误率——这个分母算错，条件读数就
    整个错位，而它不在 `se` 的护栏覆盖范围内。
    """
    P = _load_probe()
    for n in (17, 61, 250, 326, 1000):
        for se_iid, se in ((1.0, 1.0), (1.0, 2.0), (2.0, 1.0), (1.0, 1.0001),
                           (0.5, 0.9), (1.0, 0.0)):
            got = int(round(float(P._neff(n, np.array([se_iid]),
                                          np.array([se]))[0])))
            want, _ = settle.effective_sample(n, se_iid, se, LAG)
            assert got == want, f"n={n} se_iid={se_iid} se={se}"


def test_truncated_floor_is_never_below_iid_on_adversarial_vectors():
    """floor 层的不等式：截断核在**任意**向量上都不许给出 `se < se_iid`。

    与上面那条逐位一致互补：那条管「算得对不对」，这条管「方向对不对」，
    不依赖任何模拟，也不依赖探针。
    """
    rng = np.random.default_rng(7)
    vectors = _floor_probes() + [rng.normal(size=60) for _ in range(50)]
    for v in vectors:
        _, se, _, se_iid = settle.hac_lcb(v.tolist(), LAG,
                                          kernel=settle.KERNEL_TRUNCATED)
        assert se >= se_iid - 1e-12
