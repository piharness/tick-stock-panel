"""`probe_incremental_size.py` 的契约：边际校准、翻转换算、插值、区间。

这个探针的读数会被用来决定 `verdict_incremental` 这一栏的去留，所以它自己
的刻度必须先钉住。四条里任何一条漂了，§9.30 的 size/power 表就会变成一个
「看起来很有说服力」的错数：

- `ma20` 的边际若偏 17%（用 2(p−0.5) 近似而不是 √2·Φ⁻¹(p)），a=0.20 那一格
  实际只有 0.166，整条 power 曲线左移；
- `block` 的 `bump` 若是翻转率而不是绝对抬升，胜率 38.4% 的策略会把 +10pp
  悄悄变成 +6.2pp；
- `_interp80` 若线性外推而不是插值，a80 会被网格分辨率绑死。
"""
from __future__ import annotations

import importlib.util
import math
import random
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sz = _load("probe_incremental_size", "scripts/probe_incremental_size.py")


def test_ma20_marginal_is_calibrated() -> None:
    """`ledger_ma20` 的边际必须等于传入的 p，误差只来自抽样。

    共同因子 `f` 的 sd 约为 1/√20（20 日移动平均），所以 `u − f ~ N(0, 1+0.05)`；
    正确的阈值是 √(1+Var(f))·Φ⁻¹(p)，不是 2(p−0.5)。这里用 300 天 × 20 picks
    = 6,000 个观测，容差 1.5pp。
    """
    for p in (0.35, 0.50, 0.65):
        days = sz.ledger_ma20([p] * 300, seed=7)
        got = sum(sum(d) for d in days) / (len(days) * sz.PICKS)
        assert got == pytest.approx(p, abs=0.015), f"p={p} 实测 {got:.3f}"


def test_ma20_autocorrelation_decays_and_cuts_after_20() -> None:
    """日命中数的 acf 必须随 lag 衰减，并在 20 天之后落到 0 附近。

    数值**不**等于 (20−k)/20：二值化把 `Φ(f)` 压成 0.25 的方差，20 只求和
    又把日内噪声加回来，实测 acf(1)≈0.37（理论 0.95）。这里只钉形状——
    「有持续相关、且在持有期处断崖」才是这个 DGP 要复现的东西。真实账本的
    acf(1) 是 0.68–0.88（§9.28），所以 ma20 给的是**只有重叠、没有因子
    持续性**的下界；上界由 block 给。
    """
    days = sz.ledger_ma20([0.5] * 1200, seed=11)
    hits = [sum(d) for d in days]
    mu = sum(hits) / len(hits)
    var = sum((h - mu) ** 2 for h in hits) / len(hits)

    def acf(k: int) -> float:
        cov = sum((hits[i] - mu) * (hits[i + k] - mu)
                  for i in range(len(hits) - k)) / (len(hits) - k)
        return cov / var

    assert 0.25 < acf(1) < 0.55
    assert acf(10) < acf(1)
    assert acf(19) < acf(10)
    assert abs(acf(30)) < 0.12, f"lag 30 仍有 {acf(30):.3f}，断崖没出现"


def test_block_bump_is_absolute_not_flip_rate() -> None:
    """`bump=a` 必须把末段胜率抬 `a` 个绝对点，不是翻 `a` 比例的 0。

    构造一个胜率 0.30 的账本：抬 +0.10 后末 120 天应是 0.40，而不是
    0.30 + 0.10×0.70 = 0.37。
    """
    rng = random.Random(3)
    real = [[1 if rng.random() < 0.30 else 0 for _ in range(20)]
            for _ in range(200)]
    out = sz.ledger_block(real, 800, seed=5, bump=0.10, tail=120)
    tail = out[-120:]
    got = sum(sum(d) for d in tail) / (120 * 20)
    assert got == pytest.approx(0.40, abs=0.03)
    head = out[:400]
    assert sum(sum(d) for d in head) / (400 * 20) == pytest.approx(0.30, abs=0.03)


def test_block_preserves_daily_pick_count() -> None:
    """块自助只能改「哪一天」，不能改「一天几只」——改了就是另一个产品。"""
    real = [[i % 2 for _ in range(20)] for i in range(120)]
    out = sz.ledger_block(real, 500, seed=1)
    assert len(out) == 500
    assert all(len(d) == 20 for d in out)


def test_interp80_hits_the_grid_point_and_interpolates() -> None:
    assert sz._interp80([(0.1, 0.1), (0.2, 0.9)]) == pytest.approx(
        0.1 + (0.8 - 0.1) / (0.9 - 0.1) * 0.1, abs=1e-9)
    assert sz._interp80([(0.1, 0.81)]) == pytest.approx(0.1)
    assert sz._interp80([(0.1, 0.2), (0.2, 0.4)]) is None


def test_shape_detects_inverted_u() -> None:
    """倒 U 是这次最重要的形状；它不能被读成「单调升」。"""
    assert "倒 U" in sz._shape([(0.1, 0.05), (0.2, 0.30), (0.3, 0.12)])
    assert sz._shape([(0.1, 0.01), (0.2, 0.2), (0.3, 0.4)]) == "单调升"
    assert sz._shape([(0.1, 0.4), (0.2, 0.3), (0.3, 0.1)]) == "单调降"


def test_wilson_interval_covers_the_point() -> None:
    lo, hi = sz.wilson(5, 100)
    assert lo < 0.05 < hi
    assert 0 <= lo < hi <= 1
    lo0, hi0 = sz.wilson(0, 100)
    assert lo0 == 0.0 and 0 < hi0 < 0.05
