"""v13b（预注册 §12.10）：确认段跨线格的 cohort 校准门。

`classify()` 的跨线格原用裸门 200 判 FAIL / UNDERPOWERED，而确认窗
（n=250、B=40）上块重抽的 se 被 cohort 定标为偏窄（比值带 0.88–0.99）：
测量 MDE 落在 (176, 200] 的格真实 MDE 可能已越过 200，会被错判成 FAIL
——错分方向恒为「未知→已检验」。修法是确认段判定用
`CONFIRM_GATE = MDE_GATE * CONFIRM_CALIB[0]`（单点定义），探索段诊断
保持默认门 200、输出逐位不变。

这里的断言钉的是**常数与结构**，不是某个样本的读数：`CONFIRM_GATE` 的
值直接写字面 176.0（预注册验收「测试直接钉住常量值」），不从
`CONFIRM_CALIB` 推导——推导式本身也要被钉住，两处对不上任何一个都要红。
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


net = _load("probe_net_diff", "scripts/probe_net_diff.py")

#: 跨线格的构造输入：区间跨过成本线（net_lo ≤ 0 ≤ net_hi），判定由 mde 决定。
CROSS = (-10.0, 10.0)


def test_confirm_gate_is_the_preregistered_constant() -> None:
    """验收「确认入口钉住」：字面 176.0，与单点定义式同时成立。"""
    assert net.CONFIRM_GATE == pytest.approx(176.0)
    assert net.CONFIRM_GATE == pytest.approx(net.MDE_GATE * net.CONFIRM_CALIB[0])


def test_confirm_gate_is_single_point_definition() -> None:
    """`CONFIRM_CALIB[0]` 不得在别处重新推导出门的值。"""
    src = (REPO / "scripts" / "probe_net_diff.py").read_text(encoding="utf-8")
    assert src.count("CONFIRM_GATE = MDE_GATE * CONFIRM_CALIB[0]") == 1


def test_blade_left_edge_is_exactly_176() -> None:
    """验收「刀锋左沿」：mde=176.0 判 FAIL，176.0+ε 判 UNDERPOWERED。"""
    assert net.classify(*CROSS, mde=176.0, gate=net.CONFIRM_GATE) == "FAIL"
    assert net.classify(*CROSS, mde=176.0 + 1e-9,
                        gate=net.CONFIRM_GATE) == "UNDERPOWERED"
    # 裸门下 176 早已是 FAIL——等效门收紧的是 FAIL 的 reach，不是别的。
    assert net.classify(*CROSS, mde=176.0, gate=net.MDE_GATE) == "FAIL"


def test_calibration_only_widens_to_underpowered() -> None:
    """验收「方向单调」：对任意 mde ∈ [150, 250]，校准判定 ∈
    {裸门判定, UNDERPOWERED}；不存在任何输入使 UNDERPOWERED→FAIL。"""
    for mde10 in range(1500, 2501, 5):
        mde = mde10 / 10.0
        bare = net.classify(*CROSS, mde=mde, gate=net.MDE_GATE)
        calib = net.classify(*CROSS, mde=mde, gate=net.CONFIRM_GATE)
        assert calib in {bare, "UNDERPOWERED"}, mde
        if bare == "UNDERPOWERED":
            assert calib == "UNDERPOWERED", mde


def test_econ_cells_are_immune_to_gate() -> None:
    """验收「ECON 格免疫」：方向由区间定，与 gate 取值无关。"""
    for gate in (0.0, 176.0, 200.0, 500.0):
        assert net.classify(1.0, 10.0, 999.0, gate=gate) == "ECON+"
        assert net.classify(-10.0, -1.0, 999.0, gate=gate) == "ECON−"


def test_evaluate_series_default_gate_stays_bare() -> None:
    """验收「探索段逐位不变」的代码侧钉子：默认参数必须是裸门。

    五条探索路径（主表、扫描、预算、校准、cohort）都不传 gate——逐位不变
    由改动前后的脚本输出 diff 复核；这里钉住的是「默认值没有被换掉」，
    防止将来有人顺手改默认值让校准泄漏进诊断路径。
    """
    default = inspect.signature(net.evaluate_series).parameters["gate"].default
    assert default == net.MDE_GATE == 200.0
    x = [float((i * 37) % 101) - 50 for i in range(240)]
    assert net.evaluate_series(x) == net.evaluate_series(x, gate=net.MDE_GATE)


def test_classify_four_cell_structure_unchanged() -> None:
    """§12.10 边界 4：四格互斥且穷尽的结构不动，校准只换跨线格的门值。"""
    assert net.classify(1.0, 10.0, 0.0, gate=net.CONFIRM_GATE) == "ECON+"
    assert net.classify(-10.0, -1.0, 0.0, gate=net.CONFIRM_GATE) == "ECON−"
    assert net.classify(-1.0, 1.0, 175.9, gate=net.CONFIRM_GATE) == "FAIL"
    assert net.classify(-1.0, 1.0, 176.1, gate=net.CONFIRM_GATE) == "UNDERPOWERED"
