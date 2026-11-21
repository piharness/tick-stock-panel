"""成本模型拆分测试 — 佣金(双边) + 印花税(仅卖出) + 滑点(双边)。

覆盖:
1. 向后兼容: 仅传 fees_pct 时, 买卖成本与旧行为完全一致 (无印花税)。
2. 拆分模型: commission_pct / stamp_tax_pct / slippage_bps 各自参与, 印花税只在卖出侧。
3. 优先级: 显式 commission_pct 覆盖 fees_pct。
4. 撮合传导: 印花税只影响卖出腿, 且精度进入 TradeRecord。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import numpy as np
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig

# ---------------------------------------------------------------
# 复用 portfolio 测试的最小面板/掩码构造
# ---------------------------------------------------------------

def _panel(symbols: list[str], days: int = 4, price: float = 10.0, overrides: dict | None = None) -> pl.DataFrame:
    overrides = overrides or {}
    start = date(2024, 1, 1)
    rows = []
    for sym in symbols:
        for i in range(days):
            patch = overrides.get((sym, i), {})
            rows.append({
                "symbol": sym,
                "name": sym,
                "date": start + timedelta(days=i),
                "open": patch.get("open", price),
                "high": patch.get("high", price),
                "low": patch.get("low", price),
                "close": patch.get("close", price),
                "volume": patch.get("volume", 100_000),
                "score": patch.get("score", 1),
                "signal_limit_up": patch.get("signal_limit_up", False),
                "signal_limit_down": patch.get("signal_limit_down", False),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _mask(panel: pl.DataFrame, marks: set[tuple[str, int]]) -> pl.Series:
    base = date(2024, 1, 1)
    values = []
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        day = (row["date"] - base).days
        values.append((row["symbol"], day) in marks)
    return pl.Series(values, dtype=pl.Boolean)


# ---------------------------------------------------------------
# 1. 单元测试: buy_cost_pct / sell_cost_pct
# ---------------------------------------------------------------

def test_legacy_fees_pct_keeps_symmetric_cost_without_stamp():
    """仅传 fees_pct: 买卖成本相等, 均为 fees + slippage, 不含印花税 (旧行为)。"""
    cfg = MatcherConfig(fees_pct=0.0002, slippage_bps=5.0)
    assert cfg.buy_cost_pct() == 0.0002 + 0.0005
    assert cfg.sell_cost_pct() == 0.0002 + 0.0005  # 无印花税, 与买入对称


def test_decomposed_cost_applies_stamp_only_on_sell():
    """拆分模型: 佣金双边, 印花税仅卖出, 滑点双边。"""
    cfg = MatcherConfig(commission_pct=0.0003, stamp_tax_pct=0.001, slippage_bps=5.0)
    assert cfg.buy_cost_pct() == 0.0003 + 0.0005
    assert cfg.sell_cost_pct() == 0.0003 + 0.001 + 0.0005


def test_commission_pct_overrides_fees_pct():
    """同时给 fees_pct 与 commission_pct 时, 以 commission_pct 为准。"""
    cfg = MatcherConfig(fees_pct=0.0002, commission_pct=0.0009, slippage_bps=0)
    assert cfg.buy_cost_pct() == 0.0009
    assert cfg.sell_cost_pct() == 0.0009  # stamp 未设 → 0


def test_commission_pct_zero_is_not_treated_as_unset():
    """commission_pct=0.0 是有效值, 不应因 falsy 而回退到 fees_pct。"""
    cfg = MatcherConfig(fees_pct=0.0002, commission_pct=0.0, slippage_bps=0)
    assert cfg.buy_cost_pct() == 0.0
    assert cfg.sell_cost_pct() == 0.0


# ---------------------------------------------------------------
# 2. 撮合传导: 印花税只影响卖出腿
# ---------------------------------------------------------------

def _round_trip_trade(cfg_kwargs: dict):
    """价格恒定的一次买卖来回, 返回唯一成交的 TradeRecord。"""
    panel = _panel(
        ["A"],
        days=3,
        overrides={
            ("A", 1): {"open": 10, "high": 10, "low": 10, "close": 10},
            ("A", 2): {"open": 10, "high": 10, "low": 10, "close": 10},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())
    result = BacktestEngine(repo=None).simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            max_positions=1,
            max_hold_days=1,
            initial_capital=100_000,
            **cfg_kwargs,
        ),
    )
    assert len(result.trades) == 1
    return result.trades[0]


def test_stamp_tax_only_deducts_on_sell_leg():
    """价格不变时, 加印花税只影响卖出腿, 不改变买入腿的持仓股数。

    本 PR 最需守护的不变量: 卖出成本不得反向污染买入腿的 sizing。
    因此断言 (a) 两次运行 shares 相等, (b) 亏损差额恰为 卖出市值 乘以 印花税率,
    卖出市值用反推的 shares 计算, 不硬编码 (避免 sizing 逻辑变动导致误报/漏报)。
    """
    stamp = 0.001
    t_no_stamp = _round_trip_trade(dict(commission_pct=0.0003, stamp_tax_pct=0.0, slippage_bps=0))
    t_with_stamp = _round_trip_trade(dict(commission_pct=0.0003, stamp_tax_pct=stamp, slippage_bps=0))

    # (a) 核心不变量: 买入腿由 buy_cost_pct 决定, 与印花税无关 → 股数必须一致。
    assert t_no_stamp.shares == t_with_stamp.shares
    shares = t_no_stamp.shares

    # (b) 差额恰等于 卖出市值(shares * 卖出价 10) 乘以 印花税率。
    delta = t_no_stamp.pnl_amount - t_with_stamp.pnl_amount
    assert delta > 0
    assert abs(delta - shares * 10 * stamp) < 1e-6


def test_independent_candidate_pnl_pct_includes_decomposed_costs():
    """独立候选模式 (close_t): 价格不变时 pnl_pct == -(buy_cost + sell_cost)。"""
    panel = _panel(
        ["A"],
        days=3,
        overrides={("A", 0): {"close": 10}, ("A", 1): {"close": 10}},
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())
    result = BacktestEngine(repo=None).simulate_independent_candidates(
        panel,
        entries,
        exits,
        MatcherConfig(matching="close_t", commission_pct=0.0003, stamp_tax_pct=0.001, slippage_bps=0, max_hold_days=1),
    )
    assert len(result.trades) == 1
    # buy_cost=0.0003, sell_cost=0.0003+0.001=0.0013 → 合计 -0.0016
    assert abs(result.trades[0].pnl_pct - (-(0.0003 + 0.0013))) < 1e-9


# ---------------------------------------------------------------
# 3. SSE 任务缓存键: 成本参数必须参与, 否则不同成本命中同一缓存 / cancel 失配
# ---------------------------------------------------------------

def test_job_key_distinguishes_commission_and_stamp():
    """成本参数不同的两次回测必须得到不同 job_key (避免缓存碰撞与 cancel 失配)。"""
    from app.api.backtest import _make_job_key

    base_args = ("s", None, None, None, "open_t+1", None, None, 0.0002, 5.0, 10, 1.0, 1e6, "equal", None, None, "position", 5)
    k_none = _make_job_key(*base_args)
    k_comm = _make_job_key(*base_args, commission_pct=0.0009)
    k_stamp = _make_job_key(*base_args, stamp_tax_pct=0.001)

    assert k_none != k_comm
    assert k_none != k_stamp
    assert k_comm != k_stamp


# ---------------------------------------------------------------
# 4. 最低佣金 (min_commission) 与手数步长 (lot_size)
#
# 判据不是「加了字段」, 而是两条:
#   (a) min_commission=0 时必须与旧乘式**逐位相等** (== 而非 approx), 否则历史结果全部漂移;
#   (b) min_commission>0 时, 定额档只在 V < m/r 时生效, 越过 kink 必须干净退出
#       (否则一个「永远加 5 元」的实现也能骗过 (a))。
# ---------------------------------------------------------------

_RATES = (0.0000691, 0.00025, 0.0003)
_NOTIONALS = (0.0, 1.0, 1800.0, 3600.0, 9000.0, 20000.0, 72358.9, 150000.0, 1e6)
_PRICES = (1.01, 3.33, 7.77, 18.0, 55.5, 213.7)
_BUDGETS = (500.0, 1800.0, 3000.0, 20000.0, 73333.3, 1e6)


@pytest.mark.parametrize("r", _RATES)
@pytest.mark.parametrize("notional", _NOTIONALS)
def test_min_commission_zero_is_bit_identical(r, notional):
    """m=0: 金额口径成本逐位等于旧乘式 (含 notional<=0 的 0 值路径)。"""
    cfg = MatcherConfig(commission_pct=r, stamp_tax_pct=0.0005, slippage_bps=5.0)
    if notional <= 0:
        assert cfg.buy_outlay(notional) == 0.0
        assert cfg.sell_proceeds(notional) == 0.0
        return
    assert cfg.buy_outlay(notional) == notional * (1.0 + cfg.buy_cost_pct())
    assert cfg.sell_proceeds(notional) == notional * (1.0 - cfg.sell_cost_pct())
    assert cfg.round_trip_cost_pct(notional) == cfg.buy_cost_pct() + cfg.sell_cost_pct()


@pytest.mark.parametrize("r", _RATES)
@pytest.mark.parametrize("budget", _BUDGETS)
@pytest.mark.parametrize("price", _PRICES)
def test_min_commission_zero_shares_bit_identical(r, budget, price):
    """m=0: 整手 sizing 逐位等于旧式 floor(budget/(price*(1+buy)) /100)*100。"""
    cfg = MatcherConfig(commission_pct=r, stamp_tax_pct=0.0005, slippage_bps=5.0)
    legacy = float(np.floor(budget / (price * (1.0 + cfg.buy_cost_pct())) / 100) * 100)
    assert cfg.shares_from_budget(budget, price) == legacy


@pytest.mark.parametrize("r", (0.0000691, 0.00025, 0.0003))
def test_min_commission_kink_is_m_over_r(r):
    """负控: 定额档的退出点必须恰为 m/r —— 否则「免五」的性价比会被算错一个量级。"""
    m = 5.0
    cfg = MatcherConfig(commission_pct=r, stamp_tax_pct=0.0005, slippage_bps=0.0, min_commission=m)
    kink = m / r
    assert cfg.buy_outlay(kink) - kink == pytest.approx(m, rel=0, abs=1e-9)
    # 略高于 kink: 佣金必须已被比例项接管 (严格小于定额 + 严格大于比例值的一半)
    above = kink * 1.01
    assert cfg.buy_outlay(above) - above == pytest.approx(r * above, rel=1e-12)
    # 略低于 kink: 仍为定额
    under = kink * 0.99
    assert cfg.buy_outlay(under) - under == pytest.approx(m, rel=0, abs=1e-12)


def test_max_notional_uses_fixed_branch_below_kink():
    """预算远低于 kink 时必须走定额支 (V=(budget-m)/(1+other)), 不是比例支。

    比例支在定额区会算出更大的 V ⇒ 静默超预算。这条是 `max_notional` 分支的定点检查。
    """
    cfg = MatcherConfig(commission_pct=0.0003, stamp_tax_pct=0.0005, slippage_bps=5.0, min_commission=5.0)
    kink = 5.0 / 0.0003
    low = 1_000.0
    assert low < kink
    assert cfg.max_notional(low) == pytest.approx((low - 5.0) / 1.0005, rel=1e-12)
    high = 1e6
    assert cfg.max_notional(high) == pytest.approx(high / (1.0 + 0.0003 + 0.0005), rel=1e-12)


@pytest.mark.parametrize("r", _RATES)
@pytest.mark.parametrize("budget", _BUDGETS)
@pytest.mark.parametrize("price", _PRICES)
def test_min_commission_shares_never_exceed_budget(r, budget, price):
    """不变量: 任何 (费率, 预算, 价格) 下, 买入支出都不得超过预算 —— 超支是静默的破产。"""
    cfg = MatcherConfig(commission_pct=r, stamp_tax_pct=0.0005, slippage_bps=5.0, min_commission=5.0)
    shares = cfg.shares_from_budget(budget, price)
    assert shares >= 0
    assert shares % cfg.lot() == 0
    if shares > 0:
        assert cfg.buy_outlay(shares * price) <= budget + 1e-9
        # 再加一手就该超预算 (否则说明 sizing 偏保守, 空耗资金)
        assert cfg.buy_outlay((shares + cfg.lot()) * price) > budget - 1e-9


def test_min_commission_does_not_bind_on_large_notional():
    """负控: 大单上定额必须**完全**不起作用 (与 m=0 逐位同值)。

    这一条是防「恒加 5 元」的假实现 —— 只测小单的实现会漏掉它。
    """
    base = MatcherConfig(commission_pct=0.0000691, stamp_tax_pct=0.0005, slippage_bps=5.0)
    with_m = MatcherConfig(
        commission_pct=0.0000691, stamp_tax_pct=0.0005, slippage_bps=5.0, min_commission=5.0
    )
    big = 1_000_000.0
    assert with_m.buy_outlay(big) == pytest.approx(base.buy_outlay(big), rel=1e-12)
    assert with_m.sell_proceeds(big) == pytest.approx(base.sell_proceeds(big), rel=1e-12)
    assert with_m.shares_from_budget(big, 18.0) == base.shares_from_budget(big, 18.0)


def test_engine_applies_min_commission_on_small_slot():
    """端到端: 组合路径上 2 万元本金 / 1 只, 定额佣金必须进 entry_value 与 pnl。"""
    panel = _panel(
        ["A"],
        days=3,
        overrides={
            ("A", 1): {"open": 10, "high": 10, "low": 10, "close": 10},
            ("A", 2): {"open": 10, "high": 10, "low": 10, "close": 10},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    def run(**kwargs):
        return BacktestEngine(repo=None).simulate_portfolio(
            panel, entries, exits,
            MatcherConfig(
                matching="open_t+1", max_positions=1, max_hold_days=1,
                initial_capital=3_000, commission_pct=0.0003, stamp_tax_pct=0.0005, slippage_bps=5.0,
                **kwargs,
            ),
        ).trades[0]

    t0 = run()
    t5 = run(min_commission=5.0)
    assert t0.shares == t5.shares  # 定额档不改 sizing 的整手结果
    assert t5.entry_value - t0.entry_value == pytest.approx(5.0 - 0.0003 * t0.shares * 10, abs=1e-6)
    # 卖出腿同样收定额
    assert t0.exit_value - t5.exit_value == pytest.approx(5.0 - 0.0003 * t0.shares * 10, abs=1e-6)
    assert t5.pnl_pct < t0.pnl_pct


def test_engine_respects_lot_size():
    """lot_size 可配: 200 股步长下 shares 必为 200 的整数倍 (科创板口径)。"""
    panel = _panel(
        ["A"],
        days=3,
        overrides={
            ("A", 1): {"open": 10, "high": 10, "low": 10, "close": 10},
            ("A", 2): {"open": 10, "high": 10, "low": 10, "close": 10},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())
    trade = BacktestEngine(repo=None).simulate_portfolio(
        panel, entries, exits,
        MatcherConfig(
            matching="open_t+1", max_positions=1, max_hold_days=1,
            initial_capital=100_000, commission_pct=0.0003, stamp_tax_pct=0.0005, slippage_bps=0.0,
            lot_size=200,
        ),
    ).trades[0]
    assert trade.shares % 200 == 0
    assert trade.shares > 0
    assert trade.lots == trade.shares / 200

