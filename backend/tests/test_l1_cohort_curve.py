"""`_diag_l1_cohort_curve` 的口径同族检查器。

为什么单开一个文件
------------------
这条链的**唯一**风险是「口径悄悄换了」：`Δ(k)` 的分母是 `open_0`、分子是
`Σamount/Σ(volume×100)`，volume 的单位（手/股）差 100 倍而不会报错，只会让 Δ 变成
一个荒谬的大数——**不抛异常**。所以每条都配一个能红的坏样本。

① Δ(k)/c(k) 必须与参照类逐字同定义（含 volume×100）；
② 端点复现的容差必须真的能红（把 Δ 平移 2bp 就判失败）；
③ crossing 必须解在两条相邻 k 之间，平坦段不能除以 0；
④ day_se 的样本单位是天，不是 cell。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "analysis"))

import _diag_l1_cohort_curve as C  # noqa: E402


def _bars(spec: list[tuple[float, float, float]], sym: str = "000001.SZ",
          date: str = "2026-10-09") -> pl.DataFrame:
    """spec = [(open, 手, 价), …]，amount = 手×100×价。"""
    rows = []
    for k, (o, vol_hand, px) in enumerate(spec):
        rows.append({
            "date": date, "symbol": sym, "k": k, "open": o,
            "high": px, "low": px, "close": px,
            "amount": vol_hand * 100.0 * px, "volume": vol_hand,
            "nbars": len(spec),
        })
    return pl.DataFrame(rows)


def test_delta_and_c_follow_the_reference_definition() -> None:
    """k=0 的 Δ 是首根 VWAP 对开盘；c 的末值必须是 1。"""
    df = _bars([(10.0, 10, 10.0), (11.0, 20, 11.0), (12.0, 30, 12.0)])
    cc = C.cell_curves(df).sort("k")
    # k0: 1000 股 @10 → vwap=10 → Δ=0
    assert cc["delta_bp"][0] == pytest.approx(0.0, abs=1e-9)
    # k1: (10000+22000)/(1000+2000)=10.6667 → 666.67bp
    assert cc["delta_bp"][1] == pytest.approx((32000 / 3000 / 10 - 1) * 1e4, abs=1e-6)
    # k2: 68000/6000=11.3333 → 1333.3bp，且 c=1
    assert cc["delta_bp"][2] == pytest.approx((68000 / 6000 / 10 - 1) * 1e4, abs=1e-6)
    assert cc["c"].to_list() == pytest.approx([10000 / 68000, 32000 / 68000, 1.0])


def test_delta_red_when_volume_unit_is_shares() -> None:
    """volume 若按「股」而不是「手」进公式，Δ 会大 100 倍——必须能被算出来并红。"""
    df = _bars([(10.0, 10, 10.0), (11.0, 20, 11.0)])
    good = C.cell_curves(df)["delta_bp"].to_list()[-1]
    bad = C.cell_curves(df.with_columns(pl.col("volume") * 100.0))["delta_bp"].to_list()[-1]
    assert good < 2000
    assert bad < 0 or abs(bad) > 50 * abs(good)      # 单位错会立刻把量级打歪


def test_cumulative_is_within_cell_not_across_cells() -> None:
    """两个 cell 混在一起累加会让 c 在第二只票上从 0.5 起——必须各算各的。"""
    df = pl.concat([_bars([(10.0, 10, 10.0), (11.0, 20, 11.0)]),
                    _bars([(20.0, 10, 20.0), (22.0, 20, 22.0)], sym="600000.SH")])
    cc = C.cell_curves(df)
    for s in ("000001.SZ", "600000.SH"):
        sub = cc.filter(pl.col("symbol") == s).sort("k")
        assert sub["c"].to_list()[-1] == pytest.approx(1.0)
        assert sub["delta_bp"][0] == pytest.approx(0.0, abs=1e-9)


def test_crossing_solves_between_neighbours() -> None:
    kk = np.arange(0, 240, dtype=float)
    y = 2.0 * kk
    assert C.crossing(kk, y, 50.0) == pytest.approx(25.0, abs=1e-9)
    assert C.crossing(kk, y, 1e9) != C.crossing(kk, y, 1e9)   # nan：打不到


def test_crossing_red_on_flat_segment() -> None:
    """平坦段不能除以 0；无解必须返回 nan，而不是抛异常或给 0。"""
    kk = np.arange(0, 10, dtype=float)
    y = np.array([0.0] * 5 + [5.0] * 5)
    k = C.crossing(kk, y, 5.0)
    assert np.isfinite(k)
    assert C.crossing(kk, np.zeros(10), 1.0) != C.crossing(kk, np.zeros(10), 1.0)


def test_slope_recovers_known_line() -> None:
    kk = np.arange(0, 240, dtype=float)
    y = -0.06949 * kk + 3.0
    assert C.slope(kk, y, 40, 70) == pytest.approx(-0.06949, abs=1e-9)
    assert C.slope(kk, y, 0, 239) == pytest.approx(-0.06949, abs=1e-9)


def test_day_se_uses_days_not_cells() -> None:
    """同一天放 5 个 cell、另一天 1 个：se 必须由 2 天算，不能由 6 个 cell 算。"""
    rows = []
    for date, vals in (("2026-10-09", [10.0] * 5), ("2026-10-10", [20.0])):
        for i, v in enumerate(vals):
            rows.append({"date": date, "symbol": f"{i:06d}.SZ", "k": 52,
                         "delta_bp": v, "c": 0.5})
    df = pl.DataFrame(rows)
    mean, se, n = C.day_se(df, "delta_bp")
    assert n == 2
    assert mean == pytest.approx(15.0)
    # std(ddof=1) of [10,20] = 7.0711，再除以 √2 ⇒ 5.0（两天的 se 恰好是半差）
    assert se == pytest.approx(5.0, abs=1e-9)


def test_load_curves_exits_when_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(C, "CURVES", tmp_path / "nope.parquet")
    with pytest.raises(SystemExit):
        C.load_curves()


# ---------------------------------------------------------------- D2 判据的可红性
# 旧版 C1–C5 里有四条对数据不敏感（跨度恒 ≥2 倍、单调升必有解、斜率同号），
# `all_pass` 因此近乎恒真。新的 D2 只有一条载荷判据，下面三条钉住它「该红的能红」。

def test_crossings_returns_every_solution() -> None:
    """曲线先破线再回落再破 ⇒ 三个交点；只取第一个会把「回落」读成「单调穿过」。"""
    kk = np.arange(0, 10, dtype=float)
    y = np.array([0.0, 1.0, 2.0, 1.0, 0.0, -1.0, 0.0, 1.0, 2.0, 3.0])
    sols = C.crossings(kk, y, 1.5)
    assert len(sols) == 3, sols                      # k=1.5 升穿、k=2.5 降穿、k=7.5 再升穿
    assert sols[0] == pytest.approx(1.5, abs=1e-9)
    assert C.crossings(kk, y, 99.0) == []            # 打不到 ⇒ 空表，不是 nan


def test_first_reach_distinguishes_zero_from_nan() -> None:
    """「开盘就够」必须给 0，不能和无解共用同一个空表——这是 D2 的核心语义。"""
    kk = np.arange(0, 5, dtype=float)
    assert C.first_reach(kk, np.array([5.0, 4.0, 3.0, 2.0, 1.0]), 1.0) == 0.0
    assert C.first_reach(kk, np.array([0.0, 0.0, 0.0, 0.0, 0.0]), 1.0) != \
        C.first_reach(kk, np.array([0.0, 0.0, 0.0, 0.0, 0.0]), 1.0)   # nan
    assert C.first_reach(kk, np.array([0.0, 2.0, 1.0, 0.0, 3.0]), 1.0) == pytest.approx(0.5)
    # 先破线再回落：首次到达仍是第一个上升交点，不是最后一个
    assert C.first_reach(kk, np.array([0.0, 2.0, 1.0, 0.0, 3.0]), 1.5) == pytest.approx(0.75)


def test_decide_has_three_distinct_branches() -> None:
    """阈值 ±2 的两侧与中间必须互斥；1.99 不许落到「可达」。"""
    assert C.decide(2.0) == "breakeven_reachable"
    assert C.decide(-2.0) == "breakeven_not_reachable"
    assert C.decide(1.99) == "undetermined"
    assert C.decide(-0.1) == "undetermined"
    assert C.decide(float("nan")) == "undetermined"


def test_bootstrap_p_moves_with_shift() -> None:
    """整条曲线平移 +250bp，P(均值破线) 必须从 0 跳到 1——否则它没在测数据。"""
    rng = np.random.default_rng(0)
    n_days, per_day, K = 24, 3, 5
    base = np.linspace(0.0, 100.0, K)
    rows, cell_day = [], []
    for d in range(n_days):
        for _ in range(per_day):
            rows.append(base + rng.normal(0.0, 1.0, K))
            cell_day.append(d)
    M = np.array(rows)
    cd = np.array(cell_day)
    kk = np.arange(K, dtype=float)
    lo = C.bootstrap_curves(M, cd, n_days, kk, 200.0, n_boot=200)
    hi = C.bootstrap_curves(M + 250.0, cd, n_days, kk, 200.0, n_boot=200)
    assert np.mean(lo["means"] >= 200.0) == 0.0
    assert np.mean(hi["means"] >= 200.0) == 1.0
    assert np.mean(np.isfinite(lo["kstar"])) == 0.0
    # 平移后整条线都在打平线之上 ⇒ k*=0（开盘就够），不是「无解」
    assert np.mean(np.isfinite(hi["kstar"])) == 1.0
    assert np.all(hi["kstar"] == 0.0)
