"""§4 判定表必须是一个**分区**：互斥、穷尽、每一格都可达。

v5 的表不是。它的写法是「net_lo>0 → ECON+；否则 MDE>200 → UNRESOLVED」，
于是「区间整段落在成本线下方」这一格无处可去：

    high_turnover  均值 −601.1bp  净 LCB −819.2  p=0.000  MDE 328.4
    evaluate_series → ECON−      §4 字面 → UNRESOLVED

同一个名字（UNRESOLVED）被两个不同的谓词共用：一个是「分辨率不足，什么都
不能说」，另一个是「已证明它在亏钱」。前者是缺信息，后者是结论——把它们
写成一格，等于让账本里最强的读数无法被引用。

这一组钉三件事：

1. `classify()` 是分区，且 MDE 门只挂在「跨线」那一格；
2. `decide()` 把方向与 Holm 分开，PASS/EXCLUDED 都要求家族校正；
3. 用真实读数重放 high_turnover 那一行，确认它不再是 UNRESOLVED。
"""
from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pnd = _load("probe_net_diff", "scripts/probe_net_diff.py")
STATES = {"ECON+", "ECON−", "FAIL", "UNDERPOWERED"}


def test_classify_covers_every_cell_exactly_once() -> None:
    """三档方向 × 两档分辨率，六个组合落进四格，且都落在 STATE 里。"""
    seen = set()
    for net_lo, net_hi in itertools.product((-50.0, 50.0), repeat=2):
        for mde in (100.0, 400.0):
            v = pnd.classify(net_lo, net_hi, mde)
            assert v in STATES, (net_lo, net_hi, mde, v)
            seen.add(v)
    assert seen == STATES, f"有格不可达：{STATES - seen}"


def test_mde_gate_only_applies_to_the_spanning_cell() -> None:
    """方向已经由区间定下来之后，再问正方向的 MDE 是范畴错误。"""
    # 整段在成本线上方：MDE 再大也是 ECON+，不是 UNRESOLVED
    assert pnd.classify(10.0, 900.0, mde=9999.0) == "ECON+"
    # 整段在成本线下方：MDE 再大也是 ECON−，这是结论不是缺信息
    assert pnd.classify(-900.0, -10.0, mde=9999.0) == "ECON−"
    # 跨线才谈分辨率
    assert pnd.classify(-10.0, 10.0, mde=100.0) == "FAIL"
    assert pnd.classify(-10.0, 10.0, mde=300.0) == "UNDERPOWERED"


def test_zero_is_not_a_side() -> None:
    """边界取严：净 LCB 恰好等于 0 不算「在成本线上方」。"""
    assert pnd.classify(0.0, 100.0, mde=10.0) != "ECON+"
    assert pnd.classify(-100.0, 0.0, mde=10.0) != "ECON−"


def test_decide_requires_holm_for_both_directions() -> None:
    """PASS 与 EXCLUDED 对称：都要过家族校正，不是只有正向才管多重比较。"""
    assert pnd.decide("ECON+", holm_p=0.01) == "PASS"
    assert pnd.decide("ECON+", holm_p=0.20) == "ECON+ n.s."
    assert pnd.decide("ECON−", holm_p=0.01) == "EXCLUDED"
    assert pnd.decide("ECON−", holm_p=0.20) == "ECON− n.s."
    # 无方向的两格不受 Holm 影响
    for v in ("FAIL", "UNDERPOWERED"):
        assert pnd.decide(v, holm_p=0.99) == v


def test_high_turnover_row_is_no_longer_unresolved() -> None:
    """真实读数重放：v5 把这一行判成 UNRESOLVED，与「分辨率不足」同名。"""
    cache = REPO / "data" / ".probe_per_day_exc_mean.json"
    if not cache.exists():
        pytest.skip("本机没有均值口径缓存")
    exc = json.loads(cache.read_text(encoding="utf-8"))
    days = sorted(set.intersection(*(set(v) for v in exc.values())))[:325]
    x = [sum(exc["high_turnover"][d]) / len(exc["high_turnover"][d]) * 1e4
         for d in days]
    r = pnd.evaluate_series(x)
    assert r["net_hi"] < 0, r
    assert r["mde"] > pnd.MDE_GATE, "这一行之所以被误判，前提是 MDE 过不了门"
    assert r["verdict"] == "ECON−", "整段在成本线下方，必须判 ECON− 而不是缺信息"
