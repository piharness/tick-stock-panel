"""基准中心的契约：中心必须等于「无能力时的期望」，否则判定在测偏度。

§9.31 报的是 low_turnover +99bp（t=2.30）。§9.32 的安慰剂说这个数里有
+195.7bp 是「均值 − 中位数」。§9.33 把两件事合成一条可断言的性质：

    **换中心只许平移一个常数，不许改变 picks、不许改变配对差。**

这条性质一旦成立，中位数口径的整张表就是均值口径的平移副本——任何
「显著 vs 0」都只是在检验那个常数。下面六个测试分别钉住：

1. 三个中心的定义（median / mean / mcap 各自等于它的数学定义）；
2. 同一批 picks 下，两个口径的差恒等于「截面均值 − 截面中位数」，逐行相同；
3. 配对差（同日两策略之差）与中心无关——常数在相减时消掉；
4. 判定函数对同一条序列的平移是敏感的：+195.7bp 足以把 UNRESOLVED 变成
   ECON+。这就是 §9.32 的机制，不是修辞；
5. 随机组合的期望等于截面均值、且**不等于**中位数（抽样意义上的检验）；
6. 判定函数可复现：同 seed 两次调用逐字段相同。
"""
from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import polars as pl
import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pnd = _load("probe_net_diff", "scripts/probe_net_diff.py")
pw = _load("probe_window", "scripts/probe_window.py")


def _pool(n: int = 400, seed: int = 3) -> pl.DataFrame:
    """右偏的当日截面：多数股票小赚，少数股票大涨。均值明显高于中位数。"""
    rng = random.Random(seed)
    fwd = []
    for i in range(n):
        base = rng.gauss(0.0, 0.12)
        fwd.append(base + (1.20 if i % 20 == 0 else 0.0))
    return pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(n)],
        "fwd": fwd,
        "raw_close": [10.0 + (i % 7) for i in range(n)],
        "float_shares": [1e8 * (1 + i % 11) for i in range(n)],
    }).with_columns((pl.col("raw_close") * pl.col("float_shares")).alias("mv"))


def test_three_centers_match_their_definitions() -> None:
    """mcap 是市值加权均值，不是等权均值、也不是中位数。"""
    d = _pool()
    med = pw.center_of(d, "median")
    mean = pw.center_of(d, "mean")
    mcap = pw.center_of(d, "mcap")
    assert med == pytest.approx(float(d["fwd"].median()), abs=1e-15)
    assert mean == pytest.approx(float(d["fwd"].mean()), abs=1e-15)
    assert mcap == pytest.approx(
        float((d["fwd"] * d["mv"]).sum() / d["mv"].sum()), abs=1e-15)
    # 右偏截面：中位数必须低于均值，否则这个测试没有区分力
    assert med < mean - 0.03
    # 市值加权与等权必须不同，否则 mcap 口径等于没换
    assert abs(mcap - mean) > 1e-6


def test_shift_between_centers_is_a_constant_per_row() -> None:
    """同一批 picks：`exc_median − exc_mean` 逐行等于「均值 − 中位数」。

    若这条不成立，说明换中心时 picks 或池子也动了，两张表不可比。
    """
    d = _pool(seed=11)
    gap = pw.center_of(d, "mean") - pw.center_of(d, "median")
    diff = (d["fwd"] - pw.center_of(d, "median")) - (d["fwd"] - pw.center_of(d, "mean"))
    assert (diff - gap).abs().max() < 1e-15


def test_paired_difference_is_invariant_to_center() -> None:
    """配对差与中心无关：常数在相减时消掉。

    §9.31 的 low_turnover−high_turnover = +590.6bp 因此在两种口径下都是
    +590.6bp。这是账本里唯一**不需要**重算的一类数。
    """
    d = _pool(seed=5)
    a = d.head(20)
    b = d.tail(20)
    for basis in ("median", "mean", "mcap"):
        c = pw.center_of(d, basis)
        gap = float(a["fwd"].mean()) - float(b["fwd"].mean())
        ga = float((a["fwd"] - c).mean()) - float((b["fwd"] - c).mean())
        assert ga == pytest.approx(gap, abs=1e-15)


def test_verdict_flips_when_the_series_is_shifted_by_the_artifact() -> None:
    """§9.32 的机制：+195.7bp 的常数足以把 UNRESOLVED 翻成 ECON+。

    这条序列本身在均值中心下明确为负（无净收益）。中位数口径只是把它整体
    平移，判定就翻了。判定函数没有错，是输入的中心错了。
    """
    x = [-40.0 + 25.0 * ((i * 37) % 11 - 5) for i in range(160)]
    lo = pnd.evaluate_series(x, reps=400, block=20, seed=1)
    hi = pnd.evaluate_series([v + 195.7 for v in x], reps=400, block=20, seed=1)
    assert lo["verdict"] != "ECON+"
    assert hi["verdict"] == "ECON+"
    assert hi["mu"] - lo["mu"] == pytest.approx(195.7, abs=1e-9)
    # 块重抽的离散度不变：平移只动中心，不动形状
    assert hi["sd_b"] == pytest.approx(lo["sd_b"], rel=1e-9)


def test_random_draw_expectation_is_the_mean_not_the_median() -> None:
    """随机抽 20 只的期望 = 截面均值。这就是「中心只能用均值」的全部理由。"""
    d = _pool(seed=17)
    vals = d["fwd"].to_list()
    rng = random.Random(0)
    draws = [sum(rng.sample(vals, 20)) / 20 for _ in range(400)]
    grand = sum(draws) / len(draws)
    assert grand == pytest.approx(pw.center_of(d, "mean"), abs=1e-2)
    # 中位数不是期望：两者差一个偏度，且差得远大于抽样误差
    assert abs(grand - pw.center_of(d, "median")) > 0.03


def test_evaluate_series_is_reproducible() -> None:
    """同 seed 两次调用的每一个字段都相同——否则校准检验的率不可复现。"""
    x = [(-1) ** i * 60.0 + i for i in range(120)]
    a = pnd.evaluate_series(x, reps=300, block=20, seed=7)
    b = pnd.evaluate_series(x, reps=300, block=20, seed=7)
    assert a == b
    assert a["mde"] == pytest.approx(2.486 * a["sd_b"], rel=1e-12)


def test_holm_is_at_least_as_conservative_as_raw() -> None:
    ps = [0.001, 0.02, 0.04, 0.30]
    adj = pnd.holm(ps)
    assert all(a >= p - 1e-12 for a, p in zip(adj, ps))
    assert adj[sorted(range(4), key=lambda i: ps[i])[0]] == pytest.approx(0.004)
