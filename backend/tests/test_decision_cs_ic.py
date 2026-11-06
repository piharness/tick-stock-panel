"""全截面 IC 探针的契约：刻度、秩相关、层内相关、日序同源。

这几条一旦漂移，`docs/decision-ledger.md` §9.29 的读数会变，但**不会报错**：
换成正态分数刻度，bp 读数整体涨 50%；换一种分位切法，层内 IC 会跳；日序不同源，
样本悄悄变成 326 天。所以钉住的是刻度与对齐，不是某天的数值。
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import polars as pl
import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load("probe_cs_ic", "scripts/probe_cs_ic.py")


def test_mde_z_matches_the_published_constant() -> None:
    """与 `pwin._MDE_Z` 同源；两处各写一个是必要的重复，但必须是同一个数。"""
    import sys

    sys.path.insert(0, str(REPO / "backend"))
    from app.decision import pwin

    assert cs.MDE_Z == pytest.approx(pwin._MDE_Z, abs=2e-3)


def test_rank_z_is_standardized() -> None:
    """z 必须是均值 0、sd 1 的标准化秩，否则 IC 与 OLS 斜率不同刻度。

    分母用秩的**总体** sd，所以样本 sd（polars 默认 ddof=1）会大
    √(n/(n-1))；n=5,400 时是 1.00009，可忽略，但测试要按 ddof 断言。
    """
    d = pl.DataFrame({"x": list(range(1, 101))})
    z = d.select(cs._rank_z("x", d.height))["x"]
    assert float(z.mean()) == pytest.approx(0.0, abs=1e-9)
    assert float(z.std()) == pytest.approx(math.sqrt(100 / 99), rel=1e-9)


def test_zbar_matches_the_closed_form() -> None:
    """top-20 的 z̄ 有解析值；这是 bp 映射的刻度，写死防漂移。

    n=5,400 时 top-20 的均值秩是 5,390.5，减 (n+1)/2 再除秩的 sd，
    得 1.726。正态分数会给 2.6——差 50%，且不会有任何报错。
    """
    n = 5400
    d = pl.DataFrame({"x": list(range(1, n + 1))})
    z = d.select(cs._rank_z("x", n))["x"]
    expect = ((n - 9.5) - (n + 1) / 2) / math.sqrt((n * n - 1) / 12)
    assert float(z.tail(20).mean()) == pytest.approx(expect, abs=1e-9)
    assert float(z.tail(20).mean()) == pytest.approx(1.726, abs=0.002)


def test_spearman_endpoints_and_ties() -> None:
    """同序 = +1，反序 = −1；全并列时相关无定义，返回 NaN 由调用方丢弃。"""
    d = pl.DataFrame({"s": list(range(50)), "fwd": list(range(50))})
    assert cs._spearman(d, "s", "fwd") == pytest.approx(1.0)
    assert cs._spearman(d, "s", "fwd") == pytest.approx(
        -cs._spearman(d.with_columns((-pl.col("fwd")).alias("fwd")), "s", "fwd"))
    flat = pl.DataFrame({"s": [1.0] * 30, "fwd": list(range(30))})
    assert math.isnan(cs._spearman(flat, "s", "fwd"))


def test_quintile_ic_is_one_for_a_monotone_relation() -> None:
    """层内单调 → 层内相关 = 1；这是「同层基准」在高分辨率下的口径。"""
    n = 1000
    d = pl.DataFrame({
        "s": [float(i) for i in range(n)],
        "fwd": [float(i) for i in range(n)],
        "amount": [float(i % 97) for i in range(n)],
    })
    assert cs._quintile_ic(d) == pytest.approx(1.0)


def test_cache_days_are_the_same_sample_as_net_diff() -> None:
    """决策日必须与 `probe_net_diff.py` 同源：缺一天就是另一个样本。"""
    exc = REPO / "data" / ".probe_per_day_exc.json"
    ic = REPO / "data" / ".probe_cs_ic.json"
    if not (exc.exists() and ic.exists()):
        pytest.skip("探针缓存未生成")
    want = set.intersection(
        *(set(v) for v in json.loads(exc.read_text(encoding="utf-8")).values()))
    got = set(json.loads(ic.read_text(encoding="utf-8"))["momentum_20"])
    assert want <= got
