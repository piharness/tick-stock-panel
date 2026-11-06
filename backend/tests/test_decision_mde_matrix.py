"""MDE 矩阵的装置测试：出场口径、基准中心、cohort 估计量。

`probe_mde_matrix.py` 有三处「错了不会报错、只会让读数悄悄变」的接缝，
这一组逐个钉住：

1. **出场日是 `t+H`，不是 `t+1+H`**。差一天，逐日读数差到几百 bp，而所有
   形状检查（天数、天数×20、bp 量级）都照常通过。合成行情上算解析值对表。
2. **`bench_all` 必须是全样本均值**，不是按格广播的向量。本测试与
   `probe_neutral.neutralize(basis="mean")` 逐格对比——那是封签 SERIES 的
   生产口径，比复述预期强。
3. **cohort MDE 的 k 与公式**：块均值已知时解析值必须逐位相等；k<3 时返回
   `None` 而不是两个点的 sd。
"""
from __future__ import annotations

import importlib.util
import math
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


MM = _load("probe_mde_matrix", "scripts/probe_mde_matrix.py")
PN = _load("probe_neutral", "scripts/probe_neutral.py")

N_SYM, N_DAY, H = 240, 45, 5


def _prices(s: int, d: int) -> float:
    """按标的主序、按日次序的单调价：mom 与 fwd 都随 s 单调下降，TOP20 无并列。"""
    return 100.0 + 0.1 * s + 0.5 * d


def _synthetic():
    cal = [f"2025-01-{d + 1:02d}" if d < 31 else f"2025-02-{d - 30:02d}"
           for d in range(N_DAY)]
    frames = {}
    for d, day in enumerate(cal):
        frames[day] = pl.DataFrame({
            "symbol": [f"S{s:04d}" for s in range(N_SYM)],
            "close": [_prices(s, d) for s in range(N_SYM)],
            "raw_close": [_prices(s, d) for s in range(N_SYM)],
            "turnover_rate": [float(s) for s in range(N_SYM)],
            "amount": [1e6 + s for s in range(N_SYM)],
        })
    ind = pl.DataFrame({"symbol": [f"S{s:04d}" for s in range(N_SYM)],
                        "ind": ["A"] * N_SYM})
    sh = pl.DataFrame({"symbol": [f"S{s:04d}" for s in range(N_SYM)],
                       "float_shares": [1e8 + s for s in range(N_SYM)]})
    return cal, frames, ind, sh


def test_exit_is_t_plus_horizon_and_bench_is_full_sample_mean():
    cal, frames, ind, sh = _synthetic()
    out = MM.build(cal, frames, ind, sh, H)
    rows = out["series"][("momentum_20", "all")]
    assert len(rows) == N_DAY - MM.LOOKBACK - H

    for day, got in rows:
        i = cal.index(day)
        entry, exit_ = i + 1, i + H          # 不是 i + 1 + H
        fwd = [_prices(s, exit_) / _prices(s, entry) - 1.0 for s in range(N_SYM)]
        mom = [_prices(s, i) / _prices(s, i - MM.LOOKBACK) - 1.0
               for s in range(N_SYM)]
        top = sorted(range(N_SYM), key=lambda s: -mom[s])[:MM.TOP_N]
        want = (sum(fwd[s] for s in top) / MM.TOP_N
                - sum(fwd) / N_SYM) * 1e4
        assert got == pytest.approx(want, rel=1e-9, abs=1e-9), day


def test_excess_matches_sealed_production_path():
    """`_excess` 与 `probe_neutral.neutralize(basis="mean")` 逐格一致。"""
    rng = random.Random(7)
    n = 400
    base = pl.DataFrame({
        "symbol": [f"S{i:04d}" for i in range(n)],
        "fwd": [rng.gauss(0.0, 0.15) for _ in range(n)],
        "raw_close": [10.0 + i % 37 for i in range(n)],
        "float_shares": [1e8 * (1 + i % 11) for i in range(n)],
        "amount": [1e6 * (1 + i % 13) for i in range(n)],
        "ind": [f"I{i % 6}" for i in range(n)],
    })
    mine = MM._excess(base)
    theirs = PN.neutralize(base, basis="mean")
    for col in ("exc_all", "exc_cell"):
        d = (mine[col] - theirs[col]).abs().max()
        assert float(d) < 1e-12, (col, float(d))
    #: 格内回退必须逐股判定：全员同格时 n_cell=n，不会触发回退
    assert float(mine["cell_fallback"].mean()) == 0.0


def test_cohort_mde_has_closed_form():
    h, k = 5, 6
    means = [1.0, -2.0, 3.0, 0.5, -0.5, 2.0]
    x = [m for m in means for _ in range(h)]
    got_k, got = MM.cohort_mde(x, h)
    assert got_k == k
    sd = statistics_stdev(means)
    assert got == pytest.approx(2.486 * sd / math.sqrt(k), rel=1e-12)


def statistics_stdev(v: list[float]) -> float:
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def test_cohort_mde_refuses_two_blocks():
    assert MM.cohort_mde([0.1] * 10, 5) == (2, None)


def test_n_eff_is_bounded_by_observations():
    x = [0.1 * math.sin(i) for i in range(80)]
    se = statistics_stdev(x) / math.sqrt(len(x))
    assert MM.n_eff(x, se) == len(x)          # iid 时退回 n
    assert 1 <= MM.n_eff(x, se * 0.3) <= len(x)


def test_mde_is_blind_to_a_level_shift():
    """MDE 对常数平移不变——所以「MDE 相等」不能证明同一条结算链。

    第五节原来拿 MDE 差去对表。换链最可能的形态是基准中心挪了，即整列每天
    平移同一个数：重抽分布整体平移，sd 一位不变，MDE 差恰好 0.0。这个测试
    把「看不见」本身钉住，免得判据被改回去。
    """
    x = [math.sin(i) * 100 + 20 for i in range(250)]
    shifted = [v + 50.0 for v in x]
    assert MM.EVAL(x)["mde"] == pytest.approx(MM.EVAL(shifted)["mde"], rel=1e-9)
    assert max(abs(a - b) for a, b in zip(x, shifted)) == pytest.approx(50.0)


def test_window_mde_uses_the_requested_window():
    """80 天那一节必须是实测：窗长参数真的换了窗，不是又做了一次外推。"""
    x = [float(i % 7) for i in range(250)]
    _, _, n80 = MM.window_mde(x, 5, window=MM.SMALL_WINDOW)
    assert n80 == (250 - MM.SMALL_WINDOW) // MM.STEP + 1
    #: 窗长等于样本长度时只剩一个起点，中位数没有意义 → 必须返回 None
    assert MM.window_mde(x, 5) == (None, None, 1)


def test_multi_width_build_matches_single_width():
    """多宽度共享一次遍历，必须与单宽度单独跑逐位相同。

    共享 `base` 是宽度扫描唯一的实现风险：排名在外层、切片在内层，一旦
    `ranked` 被某个宽度消耗掉（或 `head()` 写在已经被切过的序列上），
    后面的宽度就会静默偏离——而天数、量级这类形状检查全都照常通过。
    """
    cal, frames, ind, sh = _synthetic()
    one = MM.build(cal, frames, ind, sh, H, top_ns=(MM.TOP_N,))
    many = MM.build(cal, frames, ind, sh, H, top_ns=(MM.TOP_N, 50))
    assert many["series"] == one["series"]          # 主矩阵口径 = top_ns[0]
    for key, rows in one["series"].items():
        assert many["by_top"][MM.TOP_N][key] == rows, key
    #: 宽度必须真的改变选股：50 档的每笔超额不可能与 20 档逐日相同
    assert (many["by_top"][50][("momentum_20", "all")]
            != many["by_top"][20][("momentum_20", "all")])


def test_width_scan_is_anchored_on_the_preregistered_top_n():
    """扫描第一档必须就是预注册的 `TOP_N=20`，且各档升序。

    `TOP_NS` 是可以被编辑的常量，而这一节的全部意义就是「宽度是可调自由度」。
    若有人把 20 从首位挪走、或顺手改了 `TOP_N`，四档对比仍在、图仍好看，
    但锚点已经漂到别处——那就不再是「同一个策略换宽度」，而是换了个策略群。
    """
    assert MM.TOP_NS[0] == MM.TOP_N == 20
    assert list(MM.TOP_NS) == sorted(MM.TOP_NS)


def test_spearman_ranks_ties_at_the_mean_and_refuses_degenerate_input():
    """秩相关要按平均秩处理并列，且全并列时必须返回 `None`。

    并列处理是这一节结论的支点：MDE 在多组之间只差零点几 bp 的情况很常见，
    若给并列项按出现顺序发不同秩，ρ 会凭空长出 ±0.1 量级的结构，而那个
    结构完全来自输入顺序。全并列（分母为 0）返回 0.0 更危险——它会被读成
    「两者无关」，而实际是「问不出来」。
    """
    assert MM.spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert MM.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert MM.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    #: 两两并列但非全并列：平均秩后仍应为 +1.0，不能因并列掉下来
    assert MM.spearman([1, 1, 2, 2], [3, 3, 9, 9]) == pytest.approx(1.0)
    assert MM.spearman([1, 2, 3], [1, 2, 3]) is None      # n < 4 不算
