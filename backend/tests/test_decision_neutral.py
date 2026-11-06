"""`probe_neutral.py` 的契约：格内基准、回退、FWL 残差、中心。

§9.31 的结论是「low_turnover 从 +185 掉到 +99，但 t 从 2.14 升到 2.30」。
这句话里每一个数都依赖下面四件事做对；任何一件漂了，读数会变，但不会报错：

1. 格内中位数必须**只在当日截面内**算。跨日算会把市场 beta 也控掉，读数
   会莫名其妙地接近 0。
2. 格子样本 < 8 时必须退回行业内中位数，而不是留一个由 3 只股票决定的中位数。
3. FWL 残差必须真的等价于 `fwd ~ 行业哑变量 + logmv`。写错成「先减行业均值
   再减规模均值」会让残差多减一次行业均值。
4. 基准的**中心**必须可选。`fwd` 右偏，减中位数会给每个多头组合一个
   +均值−中位数的固定偏移；只有减均值才与组合 P&L 同中心。§9.32 的
   安慰剂（随机 20 只，减中位数 +194bp、减均值 −1.6bp）就是这条的实证。
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


nt = _load("probe_neutral", "scripts/probe_neutral.py")


def _frame(n_ind: int = 6, per_cell: int = 40, seed: int = 0) -> pl.DataFrame:
    """3 个规模档 × n_ind 个行业，每格 `per_cell` 只。

    `fwd` 含行业效应 + 规模效应 + 噪声，所以「中性化后应只剩噪声」是可断言的。
    """
    rng = random.Random(seed)
    rows = []
    sym = 0
    for i in range(n_ind):
        for q in range(3):
            for _ in range(per_cell):
                sym += 1
                # 价格由 q 决定（模拟规模档），不是由 sym 决定——否则 `rank`
                # 会按代码顺序切三分位，整个行业落进同一档，格子数从 18 掉到 8。
                raw = 10.0 + 4.0 * q + rng.uniform(0.0, 1.0)
                ind_eff = 0.01 * i
                size_eff = 0.005 * q
                noise = rng.gauss(0, 0.02)
                rows.append({
                    "symbol": f"{sym:06d}.SZ",
                    "close": raw,
                    "raw_close": raw,
                    "turnover_rate": rng.uniform(0.5, 8.0),
                    "amount": float(sym),
                    "float_shares": 1e9,
                    "ind": f"行业{i}",
                    "fwd": ind_eff + size_eff + noise,
                })
    return pl.DataFrame(rows)


def test_five_benchmarks_are_present_and_ordered() -> None:
    """五个基准必须都算出来，且 `exc_*` 都把对应的基准减掉了。"""
    d = nt.neutralize(_frame())
    for col in ("bench_all", "bench_amt", "bench_ind", "bench_cell_used",
                "exc_all", "exc_amt", "exc_ind", "exc_cell", "resid", "beta"):
        assert col in d.columns, col
    for b, e in (("bench_all", "exc_all"), ("bench_amt", "exc_amt"),
                 ("bench_ind", "exc_ind"), ("bench_cell_used", "exc_cell")):
        assert (d[e] - (d["fwd"] - d[b])).abs().max() < 1e-12


def test_cell_benchmark_is_the_median_of_its_own_cell() -> None:
    """格内基准 = 同格 `fwd` 中位数；不能混入别的格。"""
    d = nt.neutralize(_frame())
    cell = d.group_by("cell").agg(
        pl.col("fwd").median().alias("want"), pl.col("bench_cell").first())
    assert (cell["want"] - cell["bench_cell"]).abs().max() < 1e-12
    # 每个格至少 40 只，不该触发回退
    assert d["cell_fallback"].sum() == 0


def test_small_cell_falls_back_to_industry() -> None:
    """格子只有 3–4 只时，必须退回行业内中位数——3 只的中位数不是基准。

    不能靠「从大样本里删掉几只」来构造：删完 rank 会重排，三分位的边界跟着
    移动，原来的格子已经不是原来的格子。所以直接用一个每格都小到必回退的
    截面。
    """
    rows = []
    for i in range(10):
        raw = 10.0 + i
        rows.append({
            "symbol": f"{i:06d}.SZ", "close": raw, "raw_close": raw,
            "turnover_rate": 1.0, "amount": float(i), "float_shares": 1e9,
            "ind": f"行业{i % 3}", "fwd": 0.01 * (i % 3),
        })
    d = nt.neutralize(pl.DataFrame(rows))
    assert d["n_cell"].max() < nt.MIN_CELL
    assert d["cell_fallback"].all()
    assert (d["bench_cell_used"] == d["bench_ind"]).all()


def test_fwl_residual_removes_industry_and_size() -> None:
    """残差应只剩噪声：行业效应与规模效应都要被剔掉。

    `fwd = 0.01·i + 0.005·q + N(0, 0.02)`，所以残差 sd 应 ≈ 0.02，
    且按行业分组的均值应 ≈ 0、与 logmv 的相关应 ≈ 0。
    """
    d = nt.neutralize(_frame())
    resid = d["resid"]
    assert resid.std() == pytest.approx(0.02, abs=0.004)
    by_ind = d.group_by("ind").agg(pl.col("resid").mean())
    assert by_ind["resid"].abs().max() < 0.004
    assert d.select(pl.corr("resid", "logmv")).item() == pytest.approx(0, abs=0.05)


def test_residual_center_follows_the_basis() -> None:
    """残差要与所选基准同中心：mean 口径均值为 0，median 口径中位数为 0。

    §9.31 曾经把「残差再减中位数」当成必须的修正，理由是「不减会比 `exc_cell`
    低 150–200bp」。§9.32 证明那个修正本身才是偏移：OLS 残差本来就以均值
    为中心，减中位数等于把偏度又加回去。所以现在两个口径各自对齐，不再
    无条件减中位数。
    """
    skewed = (_frame(seed=6).with_row_index("row").with_columns(
        (pl.col("fwd") + pl.when(pl.col("row") % 3 == 0).then(0.5)
         .otherwise(0.0)).alias("fwd")).drop("row"))

    dm = nt.neutralize(skewed, basis="mean")
    assert abs(float(dm["resid"].mean())) < 0.002
    # 右偏 ⇒ 均值在右尾一侧，所以「以均值为中心」时中位数偏左。
    assert float(dm["resid"].median()) < -0.004

    dq = nt.neutralize(skewed, basis="median")
    assert abs(float(dq["resid"].median())) < 0.002
    assert float(dq["resid"].mean()) > 0.004


def test_mean_basis_zeroes_the_cell_excess_median_basis_does_not() -> None:
    """§9.32 的机制：右偏截面上，「减格内中位数」给每个多头组合一个正偏移。

    同一份数据、同一批格子，只换中心：

        减格内均值   ⇒ 全截面平均超额 = 0（与组合 P&L 同中心）
        减格内中位数 ⇒ 全截面平均超额 = 平均的「均值−中位数」> 0

    这不是选股能力，是偏度。安慰剂（随机 20 只）把它量成 +194bp。
    """
    skewed = (_frame(seed=7).with_row_index("row").with_columns(
        (pl.col("fwd") + pl.when(pl.col("row") % 10 == 0).then(0.30)
         .otherwise(0.0)).alias("fwd")).drop("row"))

    dm = nt.neutralize(skewed, basis="mean")
    assert abs(float(dm["exc_cell"].mean())) < 1e-12

    dq = nt.neutralize(skewed, basis="median")
    assert float(dq["exc_cell"].mean()) > 0.02
