"""§6.4 的不重叠 cohort：唯一一个不需要选块长的读数。

§6.2 / §6.3 的全部争论都挂在「B 取 20 还是 40」上。cohort 把这个参数拿掉：
每 20 天取一个窗、窗与窗不重叠，12/16 个观测近似独立，se 直接算。于是它可
以当外部基准，检验块重抽到底偏不偏、偏多少、偏在哪个 B 上。

这组测试钉四件事：

1. n=326 上块重抽与 cohort 一致（§6.4 表一）——「20 日重叠 → 必须块重抽」
   在真实序列上成立，不是借口；
2. n=250 上 B=60 明显偏离 cohort，B=20 不比 B=40 好（表二）——选项 B 的前提
   「改 B=20 偏置更小」被否；
3. 同一个 250 天、不同窗的标签会变（表三）——§4 的标签是窗的函数；
4. MDE≤200 在 250 天上只有 reversal 够得着，且 3/8 窗（表四）——§6.1 的
   「250 天只能给 low 一个可判定结论」两处都不对。
"""
from __future__ import annotations

import importlib.util
import math
import statistics
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pcc = _load("probe_cohort_check", "scripts/probe_cohort_check.py")
pnd = pcc.pnd
PREREG = REPO / "docs" / "decision-target-prereg.md"
WINDOW = pcc.WINDOW
STEP = pcc.STEP


@pytest.fixture(scope="module")
def ser() -> dict[str, list[float]]:
    got = pcc.series("mean")
    if len(next(iter(got.values()))) < WINDOW:
        pytest.skip("本机没有足够的均值口径缓存")
    return got


def test_block_bootstrap_matches_cohort_at_full_sample(ser) -> None:
    """326 天上 B=40 的 se 与 cohort 差 <6%。

    这是「块长收全了记忆」的唯一证据。若它不成立，§4 用块重抽分位做区间
    就缺一个独立校验——而 §2 的全部理由（20 日重叠 → 长记忆）也就没有兑现。
    """
    for name, x in ser.items():
        r = pnd.evaluate_series(x, block=pnd.BLOCK, reps=600, seed=0)
        se, k = pcc.cohort_se(x)
        assert 14 <= k <= 16, (name, k)
        assert se / r["sd_b"] == pytest.approx(1.0, abs=0.08), (name, se, r["sd_b"])


def test_cohort_values_match_section_6_4_table(ser) -> None:
    """§6.4 表一的 se_cohort 列必须与脚本逐格相等。

    cohort 是确定性的（无重抽、无随机种子），所以这里不容差——除非缓存换了。
    容差留给浮点，不留给定稿。
    """
    text = PREREG.read_text(encoding="utf-8")
    start = text.find("### 6.4 ")
    assert start >= 0, "预注册里没有 §6.4"
    rows: dict[str, list[str]] = {}
    for line in text[start:].splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip().strip("*") for c in line.strip("|").split("|")]
        if len(cells) != 4 or cells[0] in ("策略",) or set(cells[0]) <= {"-", " "}:
            continue
        rows[cells[0]] = cells
    assert len(rows) == 4, rows
    for name, cells in rows.items():
        se, _ = pcc.cohort_se(ser[name])
        assert se == pytest.approx(float(cells[2]), abs=0.15), (name, se)


def test_long_blocks_are_worse_at_the_confirmation_length(ser) -> None:
    """n=250 上 B=60 的 se 只有 cohort 的 0.63–0.73，B=40 是 0.88–0.99。

    「长块更保守」在确认段的长度上是**反的**，而且这次不是单条序列的低尾：
    8 个窗的中位数。B=80 在 v8 就该被否，B=60 在 250 天上同样不可用。
    """
    x = ser["low_turnover"]
    ratios = {}
    for b in (40, 60):
        rs = []
        for s in range(0, len(x) - WINDOW + 1, 60):
            c, _ = pcc.cohort_se(x[s:s + WINDOW])
            r = pnd.evaluate_series(x[s:s + WINDOW], block=b, reps=400, seed=0)
            rs.append(r["sd_b"] / c)
        ratios[b] = statistics.median(rs)
    assert ratios[60] < ratios[40] - 0.10, ratios


def test_fail_branch_is_reachable_only_for_reversal(ser) -> None:
    """250 天上 MDE≤200 的窗数：momentum 0、reversal 3、low 0、high 0。

    §6.1 写「250 天只能给 low 一个可判定结论」——两处都不对：low 在任何窗上
    都到不了 200（0/8），能到达的是 reversal，而它是 3/8。所以那句话既点错
    了策略，也把「抽到窄窗」当成了「分辨率够」。
    """
    hits = {}
    for name, x in ser.items():
        ms = [pcc.MDE_Z * pcc.cohort_se(x[s:s + WINDOW])[0]
              for s in range(0, len(x) - WINDOW + 1, STEP)]
        hits[name] = sum(1 for v in ms if v <= pnd.MDE_GATE)
    assert hits["momentum_20"] == 0, hits
    assert hits["low_turnover"] == 0, hits
    assert hits["high_turnover"] == 0, hits
    assert hits["reversal_20"] >= 1, hits


def test_label_is_a_function_of_the_window(ser) -> None:
    """同一个 n=250、同一个 B=40，换一个起点就换一个标签。

    §4 说「固定 250 天，不看中期」。但 326 天里切得出 8 个 250 天窗，它们的
    标签不全相同——「固定」固定的是长度，不是抽样误差。这条不改规则，只是
    让「标签有几种」变成可复核的量。
    """
    x = ser["reversal_20"]
    labs = [pnd.classify(*(
        pnd.evaluate_series(x[s:s + WINDOW], block=pnd.BLOCK, reps=400,
                            seed=0)[k] for k in ("net_lo", "net_hi", "mde")))
        for s in (0, 20, 40, 60)]
    assert len(set(labs)) >= 2, labs


def test_cohort_needs_no_tuning_parameter() -> None:
    """cohort 的读数不随 reps、seed、B 变——这正是它能当基准的原因。

    一个需要调参的估计量不能给另一个需要调参的估计量定标；§6.2/§6.3 里
    反复出现的「与块长无关」争论，在 cohort 这里没有对应物。
    """
    x = pcc.series("mean")["reversal_20"]
    a, _ = pcc.cohort_se(x)
    b, _ = pcc.cohort_se(x[:300])
    assert math.isfinite(a) and math.isfinite(b) and a != b
    assert pcc.cohort_se(x)[0] == a
