"""§6.5 的作用域：`MIN_BLOCKS` 管扫描表，确认段由 cohort 带管。

v10 留下一个矛盾（§6.3 第 3 条）：`n=250`、`B=40` 只有 7 个块，低于
`MIN_BLOCKS=8`，而 §4 说 250 天后出判定。提案是把常数降到 7。这组测试钉的是
**为什么没降**，以及降了会发生什么：

1. `n=326` 的四列 nb 是 17/9/6/5——**没有 7**，所以 7 与 8 给出同一张扫描表。
   一个在每张现有表上都零效果的改动，不是定标；
2. 确认段的 0.88–0.99 必须能从缓存重算，不能只是抄进常数；
3. 带宽跨门时标签是「刀锋」，不是 FAIL——190bp 的打印值乘 1.14 就是 217；
4. §6.5 必须把这两件事写下来，否则下一次又会有人去降常数。
"""
from __future__ import annotations

import importlib.util
import statistics
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pnd = _load("probe_net_diff", "scripts/probe_net_diff.py")
pcc = _load("probe_cohort_check", "scripts/probe_cohort_check.py")
PREREG = REPO / "docs" / "decision-target-prereg.md"


@pytest.fixture(scope="module")
def ser() -> dict[str, list[float]]:
    got = pcc.series("mean")
    if len(next(iter(got.values()))) < pcc.WINDOW:
        pytest.skip("本机没有足够的均值口径缓存")
    return got


def test_min_blocks_value_is_inert_on_the_exploration_table(ser) -> None:
    """7 与 8 在 `n=326` 的扫描表上给出同一组可读列与判定。

    这是「08→7 不是定标，是措辞」的全部依据：`n=326` 的 nb 是 17/9/6/5，
    阈值落在 6 与 9 之间，取 7 还是 8 都落在同一个空档里。若哪天块长扫描
    多了一档（例如 B=45，nb=8）这条断言会先红，提醒改口径要重算。
    """
    n = len(next(iter(ser.values())))
    nbs = {-(-n // b) for b in pnd.BLOCK_SCAN}
    assert nbs == {17, 9, 6, 5}, nbs
    assert 7 not in nbs, "若出现 nb=7 的列，MIN_BLOCKS 的取值就不再是零效果"
    rows = pnd.scan_blocks(ser, reps=200)

    def readable(floor: int) -> dict[str, tuple[str, ...]]:
        return {r["strategy"]: tuple(c["verdict"] for c in r["cells"]
                                     if -(-n // c["block"]) >= floor)
                for r in rows}

    assert readable(7) == readable(8)


def test_confirmation_calibration_matches_the_cohort_table(ser) -> None:
    """0.88–0.99 必须能从缓存重算出来，不能只是抄进 `CONFIRM_CALIB`。

    抄进去的常数和算出来的常数长得一样，区别只在缓存换掉之后：后者会跟着变，
    前者不会。容差 0.04 留给 reps=400 与 reps=1000 的差。
    """
    rows = pcc.window_table(ser, reps=400, blocks=(40,))
    meds = [statistics.median(v[40]) for v in rows.values()]
    assert len(meds) == 4, meds
    assert min(meds) == pytest.approx(pnd.CONFIRM_CALIB[0], abs=0.04), meds
    assert max(meds) == pytest.approx(pnd.CONFIRM_CALIB[1], abs=0.04), meds
    assert pnd.confirm_width_bound() == pytest.approx(1 / 0.88, abs=1e-9)


def test_band_turns_a_near_gate_row_into_a_blade() -> None:
    """带宽跨过 200bp 时标签是「刀锋」——这正是 reversal 那些 189–197 的窗。

    `FAIL` 与 `UNDERPOWERED` 的区分在门附近本来就不在数据里；加上单边带宽
    之后它变成一件可以打印的事，而不是靠读者记得 ±14%。
    """
    assert pnd.gate_tag({"mde_proj": 190.0, "mde_meas": 185.0}) == "刀锋"
    assert pnd.gate_tag({"mde_proj": 120.0, "mde_meas": 130.0}) == "门内"
    assert pnd.gate_tag({"mde_proj": 300.0, "mde_meas": 320.0}) == "门外"
    # 单边：下界不因「可能偏窄」而下调，否则 260 会被写成 228 而误判成刀锋。
    assert pnd.gate_tag({"mde_proj": 260.0, "mde_meas": 260.0}) == "门外"


def test_confirmation_citation_prints_the_band(ser, capsys) -> None:
    """第六节必须打印 band 与 nb，且不再用「不可读」否决确认段。"""
    pnd.budget_table(ser)
    out = capsys.readouterr().out
    assert "1.14" in out, out
    assert "不可区分" in out, out
    assert "不可读" not in out, out
    assert "nb=7" in out, out


def test_section_6_5_declares_the_scope() -> None:
    """§6.5 必须同时写下「没有一列等于 7」与「(5,7]」——两句缺一就会回到降常数。"""
    text = PREREG.read_text(encoding="utf-8")
    start = text.find("### 6.5 ")
    assert start >= 0, "预注册里没有 §6.5"
    body = text[start:text.find("\n## ", start)]
    assert "17 / 9 / 6 / 5" in body, body[:400]
    assert "(5, 7]" in body, body[:400]
    assert "1.14" in body, body[:400]


def test_nb_is_a_proxy_for_block_length_not_a_floor_on_count(ser) -> None:
    """`nb=5` 本身不坏：同样是 5 个块，块长 40 时四档比值 0.88–0.91。

    固定 n 时 `nb = n/b`，所以「块数少」与「块太长」在现有每张表上都同时出现。
    `n=190, B=40` 是唯一能把两者分开的一格，它把 0.63 那组读数归给了 B=60。
    这条测试守的是归类，不是阈值：`MIN_BLOCKS=8` 在 `n=250` 上真正等价于
    「B ≤ 40」，把它读成「块数下限」会让下一次的人去数块数而不是看块长。
    """
    n = len(next(iter(ser.values())))
    #: 190 天窗：每个策略取所有起点（步长 10）的中位比值。
    def med_ratio(window: int, block: int) -> list[float]:
        out = []
        for x in ser.values():
            rs = []
            for s in range(0, n - window + 1, 10):
                w = x[s:s + window]
                #: reps=1000 是 `probe_net_diff.REPS` 的规范值；v10/v11 文档里的
                #: 0.63–1.01、0.88–0.99、0.99–1.01 都出自它。用 400 会让 low 漂 2pp。
                se = statistics.stdev(pnd.block_dist(w, block=block, reps=1000, seed=0))
                c, _ = pcc.cohort_se(w)
                rs.append(se / c)
            out.append(statistics.median(rs))
        return out

    long_block = med_ratio(250, 60)      # nb=5, b/n=0.24
    short_n = med_ratio(190, 40)         # nb=5, b/n=0.21
    at_250 = med_ratio(250, 40)          # nb=7, b/n=0.16

    assert min(long_block) < 0.70, long_block       # 0.63：长块的失败模式
    assert min(short_n) > 0.85, short_n             # 0.87：同样 5 块但块不长
    assert min(at_250) > 0.85, at_250
    #: 三格的 nb 是 5/5/7，比值却是「坏/好/好」——块数不是那个变量。
    assert -(-250 // 60) == -(-190 // 40) == 5
    assert -(-250 // 40) == 7
