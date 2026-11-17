"""前端涨跌停幅度表 vs 后端唯一实现 —— 让「第二份」在漂移时变红。

`frontend/src/lib/price-limit.ts` 的注释写着「与后端 price_limits.py 当前口径一致」。
那句话此前没有任何东西保证：调一次板块规则（北交所 30% → 20%），分时图画的涨跌停带
就与实际规则不符，价格贴着涨停、图上还差得远 —— 不抛异常、不报错、页面很自信。

链路：`app.price_limits.board_limit_pct`（产地）→ `priceLimitFixtures.json`（冻结表）
→ `frontend/src/lib/price-limit.ts::boardLimitRate`。本文件守中间那一跳（CI 里跑），
前端 vitest 守最后一跳（`frontend/src/lib/price-limit.test.ts`）。

基准表用 `python analysis/_verify_price_limit_fixtures.py --write` 重算。
本测试只读冻结表、不读 data/ —— CI 没有数据目录。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.price_limits import (
    BEIJING_BOARD_LIMIT,
    GROWTH_BOARD_LIMIT,
    MAIN_BOARD_LIMIT,
    board_limit_pct,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "frontend" / "src" / "lib" / "priceLimitFixtures.json"


def _fixture() -> dict:
    if not FIXTURE.exists():
        pytest.fail(
            f"缺少前端基准表 {FIXTURE}；"
            "跑 python analysis/_verify_price_limit_fixtures.py --write"
        )
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_shape() -> None:
    """表本身要先说得通：非空、来源标明、前缀与用例同集合。"""
    fx = _fixture()
    assert fx["cases"], "基准表为空 —— 逐项断言会退化成空循环"
    assert fx["source"] == "backend/app/price_limits.py::board_limit_pct"
    prefixes = {
        "BJ" if symbol.endswith(".BJ") else symbol[:3] for symbol in fx["cases"]
    }
    assert prefixes == set(fx["groups"]), "用例覆盖的板块前缀与 groups 声明不一致"


def test_every_case_matches_backend() -> None:
    """冻结表里每一条都必须等于后端此刻的算法结果。"""
    fx = _fixture()
    mismatches = {
        symbol: (recorded, board_limit_pct(symbol))
        for symbol, recorded in fx["cases"].items()
        if abs(float(recorded) - board_limit_pct(symbol)) > 1e-12
    }
    assert not mismatches, (
        "板块规则改了但前端基准表没重算（跑 --write 并同步 frontend/src/lib/price-limit.ts）："
        f"{mismatches}"
    )


@pytest.mark.parametrize(
    "prefix,expected",
    [("BJ", BEIJING_BOARD_LIMIT), ("300", GROWTH_BOARD_LIMIT), ("688", GROWTH_BOARD_LIMIT), ("600", MAIN_BOARD_LIMIT)],
)
def test_fixture_covers_each_limit_tier(prefix: str, expected: float) -> None:
    """三档都必须在表里出现，否则改动可能只被「同档的另一组」掩盖。"""
    fx = _fixture()
    group = fx["groups"].get(prefix)
    assert group is not None, f"基准表缺少板块 {prefix}"
    assert group["rate"] == pytest.approx(expected)


def test_refresh_gate_detects_new_board_prefix() -> None:
    """生成脚本的 --check 必须能报出「出现新板块前缀」。

    这条不能靠数据触发（宇宙里新增 303 需要真实上市），但不测就等于没这道门：
    新板块若沿用旧后缀，`compare()` 只比 cases 会漏掉 —— 而前端正则不认识它。
    """
    sys.path.insert(0, str(REPO / "analysis"))
    from _verify_price_limit_fixtures import compare  # noqa: PLC0415

    fresh = {
        "cases": {"600000.SH": 0.1, "303001.SZ": 0.2},
        "groups": {"600": {"count": 1, "rate": 0.1}, "303": {"count": 1, "rate": 0.2}},
    }
    old = {
        "cases": {"600000.SH": 0.1},
        "groups": {"600": {"count": 1, "rate": 0.1}},
    }
    problems = compare(fresh, old)
    assert any("新板块前缀" in p and "303" in p for p in problems), problems
    assert any("新增" in p and "303001.SZ" in p for p in problems), problems


def test_date_aware_rule_stays_backend_only() -> None:
    """边界声明：带日期/ST 的口径只由后端承担，别让前端表假装覆盖它。

    创业板 2020-08-24 之前是 10%，主板 ST 2026-07-06 之前是 5% ——
    `boardLimitRate(symbol)` 没有日期轴，这两条它算不出来。此断言的作用是
    把「它确实算不出来」写进测试：真有天有人给它加日期参数，这里会红，
    提醒同步基准表的生成口径。
    """
    from datetime import date

    from app.price_limits import price_limit_pct

    assert price_limit_pct("300750.SZ", date(2020, 8, 21)) == MAIN_BOARD_LIMIT
    assert price_limit_pct("300750.SZ", date(2020, 8, 24)) == GROWTH_BOARD_LIMIT
    assert price_limit_pct("600000.SH", date(2026, 7, 3), is_risk_warning=True) == 0.05
    assert price_limit_pct("600000.SH", date(2026, 7, 6), is_risk_warning=True) == MAIN_BOARD_LIMIT
