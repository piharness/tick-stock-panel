"""块长扫描：200bp 那道门是不是块长的函数，以及长块为什么会说谎。

§4 的判定固定 `BLOCK=40`，但 40 是**选定**的，不是估出来的。若判定随块长
翻转，那「它过了 / 它没过」就不是关于数据的陈述。这一组钉三件事：

1. 扫描走的是 `evaluate_series()`，与 §4 同一条代码路径——B=40 那列必须与
   `main()` 打印的判定逐位相同，否则扫描只是另写了一个判定；
2. 均值不随块长变，变的只有分位与 MDE——扫描若动了均值，说明它动了别的；
3. **长块会低估 se**：`n=326`、`B=80` 时 `b/n=0.25`，循环块重抽已越过适用
   边界。用 20 日重叠 MA 的合成序列（长程方差的解析值已知）标定，B=20 的
   se 接近真值，B=80 低约三分之一。所以 B=80 给 reversal 的 ECON− 是伪影。

第 3 条是这一组里唯一**独立于本仓库数据**的检验：合成序列的真值由解析式给出，
不依赖缓存、不依赖封签，也不依赖我们对市场的判断。
"""
from __future__ import annotations

import importlib.util
import json
import math
import random
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
GATE = _load("decision_gate", "scripts/decision_gate.py")


def _series() -> dict[str, list[float]]:
    """均值口径的日序列；没有缓存就 skip（CI 上不是每台机器都有数据）。"""
    cache = REPO / "data" / ".probe_per_day_exc_mean.json"
    if not cache.exists():
        pytest.skip("本机没有均值口径缓存")
    exc = json.loads(cache.read_text(encoding="utf-8"))
    days = sorted(set.intersection(*(set(v) for v in exc.values())))
    return {k: [sum(v[d]) / len(v[d]) * 1e4 for d in days] for k, v in exc.items()}


@pytest.fixture(scope="module")
def scan() -> dict:
    ser = _series()
    return {"n": len(next(iter(ser.values()))),
            "rows": {r["strategy"]: {c["block"]: c for c in r["cells"]}
                     for r in pnd.scan_blocks(ser)}}


def test_scan_runs_the_same_verdict_path_as_section_4(scan: dict) -> None:
    """B=40 那一列必须与 `evaluate_series()` 的默认值逐位相同。

    若扫描自己实现一套判定，它就是在复述预期——安慰剂、敏感性表都会跟着
    失去意义（`probe_net_diff.py` 里对安慰剂的注释是同一个理由）。
    """
    ser = _series()
    for name, x in ser.items():
        want = pnd.evaluate_series(x)
        got = scan["rows"][name][pnd.BLOCK]
        for k in ("mu", "sd_b", "lo", "hi", "mde", "net_lo", "net_hi",
                  "verdict", "p"):
            assert got[k] == want[k], (name, k)


def test_only_the_interval_moves_with_the_block(scan: dict) -> None:
    """换块长不许动均值：动了均值，说明重抽之外还有别的东西在变。"""
    for cells in scan["rows"].values():
        mus = {c["mu"] for c in cells.values()}
        assert len(mus) == 1, mus
        for c in cells.values():
            assert c["lo"] <= c["hi"]
            assert c["net_lo"] == pytest.approx(c["lo"] - pnd.COST_BP)
            assert c["net_hi"] == pytest.approx(c["hi"] - pnd.COST_BP)


def test_high_is_excluded_at_every_block(scan: dict) -> None:
    """「只有 high 被排除」在**可读列**（nb≥8）里成立，且不依赖块长。"""
    cells = scan["rows"]["high_turnover"]
    readable = {b: c for b, c in cells.items()
                if -(-scan["n"] // b) >= pnd.MIN_BLOCKS}
    assert readable, "至少要有两列可读"
    assert {c["verdict"] for c in readable.values()} == {"ECON−"}


def test_low_stays_underpowered_in_the_readable_columns(scan: dict) -> None:
    """low 的 MDE 在可读列里都 >200——B=80 的 179 只出现在 nb=5 的不可读区。"""
    cells = scan["rows"]["low_turnover"]
    for b, c in cells.items():
        if -(-scan["n"] // b) >= pnd.MIN_BLOCKS:
            assert c["mde"] > pnd.MDE_GATE, (b, c["mde"])
    below = [b for b, c in cells.items() if c["mde"] <= pnd.MDE_GATE]
    assert below, "若没有低于门的列，§6.2 第 3 条读数要重写"
    assert all(-(-scan["n"] // b) < pnd.MIN_BLOCKS for b in below), below


def test_reversal_sits_on_the_gate(scan: dict) -> None:
    """reversal 在 326 天上正好骑在 200bp 门上：196↔211，判定翻转。

    这不是要挑一个块长让它过门，而是把「刀锋」写下来：326 天的探索读数是
    FAIL/UNDERPOWERED 的边缘，单看它不能支撑任何结论。§6.1 的 250 天预算走
    另一条路（se∝n^-1/2 外推到 249），所以确认段的结论不受这一格影响。
    """
    cells = scan["rows"]["reversal_20"]
    mdes = {b: c["mde"] for b, c in cells.items()
            if -(-scan["n"] // b) >= pnd.MIN_BLOCKS}
    assert min(mdes.values()) <= pnd.MDE_GATE < max(mdes.values()), mdes
    assert {c["verdict"] for b, c in cells.items() if b in mdes} == {
        "FAIL", "UNDERPOWERED"}


def _ma_series(n: int, m: int = 20, seed: int = 7) -> list[float]:
    """20 日重叠 MA 的白噪声——与 `exc` 同类的序列，但真值有解析式。"""
    rng = random.Random(seed)
    e = [rng.gauss(0.0, 1.0) for _ in range(n + m - 1)]
    return [sum(e[i:i + m]) / m for i in range(n)]


def _ma_se(b: int, n: int, m: int = 20) -> float:
    """CBB 理论 se（有限块长）：sqrt(f_b/(n·m))，f_b = 1+2Σ(1−k/b)ρ_k。

    γ_k = (m−k)/m²、γ_0 = 1/m ⇒ ρ_k = 1−k/m。块长把 k≥b 的项截掉，
    所以 f_b 随 b 上升并收敛到 m。
    """
    f = 1 + 2 * sum((1 - k / b) * max(0.0, 1 - k / m) for k in range(1, b))
    return math.sqrt(f / (n * m))


def test_large_blocks_underestimate() -> None:
    """长块给出**更窄**的区间——与「块更长更保守」的直觉相反。

    `block_dist()` 的循环块在 `b/n→0.25` 时，每次重抽都覆盖几乎整条序列，
    重抽均值贴着全样本均值，se 因此塌下去。合成序列把这条钉死：B=20 的比值
    接近 1，B=80 只剩三分之二。这张扫描表的 B=60/80 两列因此不能引用。
    """
    n = 326
    x = _ma_series(n)
    ratios = {}
    for b in (20, 80):
        se = statistics.stdev(pnd.block_dist(x, block=b, reps=4000, seed=0))
        ratios[b] = se / _ma_se(b, n)
    assert ratios[20] > 0.85, ratios
    assert ratios[80] < 0.78, ratios
    assert ratios[80] < ratios[20] - 0.10, ratios


def test_block_table_rows_are_recomputable_from_the_prereg(scan: dict) -> None:
    """§6.2 表里的四列 MDE 必须等于重算值——文档的数字第一次有了验算。

    §12.3 的「v7 不建立什么」第 4 条是「§6 的数字都对」：封签只证明来源，
    不证明算术。这条把新增的那张表纳入重算范围，格式固定为
    `| 策略 | B=20 | B=40 | B=60 | B=80 | 判定 |`。
    """
    text = (REPO / "docs" / "decision-target-prereg.md").read_text(encoding="utf-8")
    start = text.find("### 6.2")
    assert start >= 0, "预注册里没有 §6.2 块长敏感性一节"
    rows = {}
    for line in text[start:].splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 6 or cells[0] in ("策略", "---") or set(cells[0]) <= {"-"}:
            continue
        try:
            rows[cells[0]] = [float(c) for c in cells[1:5]]
        except ValueError:
            continue
    assert set(rows) == set(scan["rows"]), (set(rows), set(scan["rows"]))
    for name, want in rows.items():
        got = [round(scan["rows"][name][b]["mde"], 1) for b in pnd.BLOCK_SCAN]
        assert got == want, (name, got, want)


def test_seal_still_covers_the_scan_code() -> None:
    """扫描代码在 `probe_net_diff.py` 里，而它在封签集内——改扫描要重签。"""
    sealed = {str(p.relative_to(REPO)).replace("\\", "/") for p in pnd.GATE.SEALED}
    assert "scripts/probe_net_diff.py" in sealed
