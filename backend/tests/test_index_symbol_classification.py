"""指数「拉取集合」与「分类集合」必须同源。

回归对象 (2026-11-12 事故): `_fetch_and_process` 显式拉取集合并了
`CORE | BENCHMARK | 监控规则`, 而 `_process_full_market_records` 的分类集合
只并了 `CORE ∪ instruments 声明`。`000002.SH`/`000688.SH` 因此被当成个股写进
`kline_daily` —— 只在 instruments 指数列表退化成免费通道窄表时发作, 静默、
无告警, 且当天 `check` 仍可能绿。

判据: 分类集合必须是拉取集合的超集。这条不需要任何外部数据, 只读代码里的
两个集合定义, 所以它永远能跑。
"""

from __future__ import annotations

from app.indicators.pipeline import BENCHMARK_INDEX_SYMBOLS
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.quote_service import QuoteService


class _Bare(QuoteService):
    """只取集合计算所需的最少状态: 没有 repo (instruments 读不到)。"""

    def __init__(self) -> None:  # 不调用父类 __init__, 避免拉起真实仓库
        self._repo = None
        self._app_state = None


def _bare() -> _Bare:
    return _Bare()


def test_classified_index_set_covers_explicit_fetch_set() -> None:
    """拉取端会请求的每个指数, 分类端都必须认得。"""
    svc = _bare()
    explicit = svc._explicit_index_symbols()
    universe = svc._index_universe()
    assert explicit <= universe, f"分类集合漏了: {sorted(explicit - universe)}"


def test_benchmark_indices_classified_even_without_instruments() -> None:
    """instruments 读不到 (repo=None) 时, 基准指数仍必须被判为指数。

    这是事故的原始条件: 付费 CN_Index 补充未生效 → instruments 只剩 376 只窄表
    → 基准指数不在其中。分类不能因此把它们让给个股。
    """
    universe = _bare()._index_universe()
    missing = set(BENCHMARK_INDEX_SYMBOLS) - universe
    assert not missing, f"基准指数未被分类为指数: {sorted(missing)}"


def test_core_indices_classified_even_without_instruments() -> None:
    universe = _bare()._index_universe()
    missing = set(CORE_INDEX_SYMBOLS) - universe
    assert not missing, f"核心指数未被分类为指数: {sorted(missing)}"


def test_no_stock_symbol_leaks_into_index_set() -> None:
    """反向守卫: 指数字典不许把普通个股吞进来。

    上一条判据只要求「超集」, 若有人图省事把「所有 symbol」返回, 它会假绿。
    """
    universe = _bare()._index_universe()
    assert "000530.SZ" not in universe
    assert "600261.SH" not in universe
