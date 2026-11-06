"""净收益口径的契约：成本、MDE 常数、块重抽。

这三个数一旦漂移，`docs/decision-target-prereg.md` 里的 19bp / 200bp 就
不再对应同一套计算——预注册会自动失效，而且没有任何报错。所以钉住的是
**跨文件的常数**，不是某个样本的读数。
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


net = _load("probe_net_diff", "scripts/probe_net_diff.py")


def test_cost_is_the_sum_of_its_parts() -> None:
    """19bp = 佣金 2×2 + 滑点 5×2 + 印花 5。改任何一项都要改预注册。"""
    assert net.COST_BP == pytest.approx(2 * 2 + 2 * 5 + 5)


def test_mde_z_matches_the_published_constant() -> None:
    """探针里的 2.486 必须与 `pwin._MDE_Z` 同源。

    两处各写一个常数是必要的重复：探针不 import 后端，后端也不 import
    探针。但重复必须一致，否则「MDE」在文档与代码里是两个数。
    `pwin` 用两位小数相加（1.645+0.842=2.487），探针用 1.6449+0.8416
    =2.4865 再截断，差 0.02%——带子留 0.002，够抓住「换了个 z」这类错。
    """
    import sys

    sys.path.insert(0, str(REPO / "backend"))
    from app.decision import pwin

    assert 2.486 == pytest.approx(pwin._MDE_Z, abs=2e-3)


def test_block_dist_is_seeded_and_degenerate_for_a_constant() -> None:
    x = [3.0] * 200
    d = net.block_dist(x, block=40, reps=50)
    assert len(d) == 50
    assert max(d) - min(d) == pytest.approx(0.0)
    assert net.block_dist([1.0, 2.0, 3.0] * 50, block=20, reps=30) == \
        net.block_dist([1.0, 2.0, 3.0] * 50, block=20, reps=30)


def test_percentile_is_monotone() -> None:
    x = [float(i) for i in range(101)]
    assert net.q(x, 0.05) < net.q(x, 0.5) < net.q(x, 0.95)
    assert net.q(x, 0.0) == 0.0 and net.q(x, 1.0) == 100.0


def test_one_sided_p_is_symmetric_in_sign() -> None:
    """负 t 不能返回 0.998——那是单侧检验最常见的实现错。"""
    assert net.p_one(-2.0) == pytest.approx(net.p_one(2.0))
    assert net.p_one(0.0) == pytest.approx(0.5)
    assert net.p_one(1.645) == pytest.approx(0.05, abs=1e-3)
