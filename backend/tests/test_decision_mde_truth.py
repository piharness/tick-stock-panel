"""MDE 公式的验收：已知 DGP 对解析值，而不是对账本的块重抽。

账本只有 8 个 40 天块，bootstrap sd 自身相对误差约 27%，拿它当验收基准会把
估计噪声当成公式误差（reversal 上 0.76 就是这么来的）。这里换成 AR(1)，
γ0 与 acf 都是闭式，两个口径各验一次。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mde_truth = _load("probe_mde_truth", "scripts/probe_mde_truth.py")


@pytest.mark.parametrize("rho", mde_truth.RHOS)
def test_analytic_sd_matches_known_dgp(rho: float) -> None:
    """解析/真值 ∈ [0.95, 1.05]，两个口径都要过。

    reps=4000 时 sd 的相对抽样误差约 1.1%（1/sqrt(2·reps)），带子留了 4 倍余量。
    """
    a_rest, a_all = mde_truth.analytic_sd(rho)
    s_rest, s_all = mde_truth.simulate(rho, reps=4000, seed=int(rho * 1000))
    assert 0.95 <= a_rest / s_rest <= 1.05
    assert 0.95 <= a_all / s_all <= 1.05


def test_full_history_contrast_is_the_rescaled_one() -> None:
    """`vs 全历史` 就是 `vs 其余` 乘 (1−w/n)：p_clim 含窗口，差值被压缩。"""
    a_rest, a_all = mde_truth.analytic_sd(0.82)
    assert a_all == pytest.approx(a_rest * (mde_truth.N - mde_truth.W) / mde_truth.N)


def test_pick_level_variance_is_rejected_by_the_same_band() -> None:
    """单位错（γ0 换成 p(1−p)）会让比值落到 2.6 倍，带子必须能抓住它。

    momentum 的日 sd² = 0.0352，逐 pick 的 p(1−p) = 0.2366，两者差 6.7 倍；
    开方后 se 被放大 2.6 倍。§9.28 的 37–60pp 正是这个倍数。
    """
    probe = _load("probe_real_deff", "scripts/probe_real_deff.py")
    gamma0, v = 0.03517, 0.384 * (1 - 0.384)
    right = probe.mde_two_prop(gamma0, 9.18, 60, 4.9, 265)
    wrong = probe.mde_two_prop(v, 9.18, 60, 4.9, 265)
    assert right == pytest.approx(19.5, abs=0.3)
    assert wrong / right == pytest.approx(math.sqrt(v / gamma0), rel=1e-9)
    assert wrong / right > 1.05
