"""横截面 alpha 统计链的统计正确性测试。

用可手算的合成数据验证口径: 收益/超额/t 检验/MDE 是否与 _diag_signal_to_product.py
确立的公式一致。这是 S4 最关键的守卫——统计量算错会让五道门误判。
"""
from __future__ import annotations

import datetime

import polars as pl
import pytest

from app.services import cross_section_alpha as csa


def _trading_dates(n: int, start: datetime.date = datetime.date(2026, 1, 5)) -> list[str]:
    """生成 n 个连续交易日(跳过周末), 返回 ISO 字符串列表。"""
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _make_daily(n_days: int = 30, n_symbols: int = 50, seed: int = 0) -> pl.DataFrame:
    """构造合成日线: 每个 symbol 连续 n_days 个交易日, 复权价与原始价相同。"""
    import random
    rng = random.Random(seed)
    dates = _trading_dates(n_days)
    rows = []
    for s in range(n_symbols):
        sym = f"{s:06d}.SH"
        price = 10.0 + rng.random() * 10
        for d in dates:
            ret = rng.uniform(-0.05, 0.05)
            price = price * (1 + ret)
            rows.append({
                "symbol": sym,
                "date": d,
                "open": price,
                "close": price * (1 + rng.uniform(-0.01, 0.01)),
                "raw_close": price,
            })
    return pl.DataFrame(rows)


def test_tstat_by_day_basic():
    """手算验证: 单日单信号, t 应能算出来且 mean_bp 正确。"""
    # 构造一个信号: 所有 50 只股票在某一天都被选中
    daily = _make_daily(n_days=30, n_symbols=50, seed=1)
    dates = _trading_dates(30)
    signal_day = dates[15]
    signals = pl.DataFrame({
        "date": [signal_day] * 50,
        "symbol": [f"{s:06d}.SH" for s in range(50)],
    })
    result = csa.compute_signal_alpha(signals, daily, holding_days=(1, 2))
    # market 口径 h=1 应有估计
    est = result.estimate(1, "market")
    assert est is not None
    assert est.n_signals > 0
    # 超额按日聚合, 单日时 n_days == 1, 应无 t (se 无穷)
    assert est.n_days == 1
    # 单日无法算 t 与 sd (sd of single value undefined) → t 为 None
    assert est.t_stat is None


def test_alpha_detects_injected_edge():
    """关键判据: 人为给一批股票注入持续正超额, 统计链必须测出显著为正。

    注入方式: 对「信号日」选中的股票, 抬高其 T+2 收盘价 +3% (300bp),
    r2 = close_2/next_open - 1 因此被抬高约 3%, 而基准是全市场等权(只含 10/200 选中),
    基准几乎不动 ⇒ 超额 ≈ 300bp。用 30 个信号日让 t 显著。
    """
    daily = _make_daily(n_days=40, n_symbols=200, seed=2)
    dates = _trading_dates(40)
    signal_dates = dates[3:33]
    sel_symbols = sorted(f"{s:06d}.SH" for s in range(10))
    # (symbol, T+2 日期) → 抬高 close ×1.03
    date_idx = {d: i for i, d in enumerate(dates)}
    adjust = set()
    for sd in signal_dates:
        t2 = dates[date_idx[sd] + 2]
        for sym in sel_symbols:
            adjust.add((sym, t2))
    df = daily.with_columns(
        pl.struct(["symbol", "date"]).map_elements(
            lambda kv: 1.03 if (kv["symbol"], kv["date"]) in adjust else 1.0,
            return_dtype=pl.Float64,
        ).alias("adj")
    ).with_columns(
        (pl.col("close") * pl.col("adj")).alias("close")
    )

    signals = pl.DataFrame({
        "date": [sd for sd in signal_dates for _ in range(10)],
        "symbol": [s for _ in signal_dates for s in sel_symbols],
    })
    result = csa.compute_signal_alpha(signals, df, holding_days=(2,))
    est = result.estimate(2, "market")
    assert est is not None
    # 注入 ~300bp 超额, mean_bp 应显著为正
    assert est.mean_bp is not None and est.mean_bp > 200
    # 30 天 × 一致正超额 ⇒ t 显著
    assert est.t_stat is not None and est.t_stat > 5


def test_no_signal_yields_empty():
    daily = _make_daily(n_days=10, n_symbols=10, seed=3)
    signals = pl.DataFrame({"date": [], "symbol": []})
    result = csa.compute_signal_alpha(signals, daily, holding_days=(1,))
    assert result.estimate(1, "market").n_signals == 0


def test_build_claim_marks_auto_derived():
    """Claim 必须显式标 auto_derived, 且 matched 用 bucket 口径时 benchmark_dims 含 pct。"""
    daily = _make_daily(n_days=30, n_symbols=100, seed=4)
    dates = _trading_dates(30)
    signal_dates = dates[10:20]
    signals = pl.DataFrame({
        "date": [sd for sd in signal_dates for _ in range(20)],
        "symbol": [f"{s:06d}.SH" for _ in range(10) for s in range(20)],
    })
    result = csa.compute_signal_alpha(signals, daily, holding_days=(2,))
    claim = csa.build_claim(
        "test_strategy", result, benchmark="bucket", h=2, event_type="limit_up",
        n_stock_days=len(daily),
    )
    assert claim["provenance"]["auto_derived"] == "true"
    assert claim["agg_unit"] == "per_day"
    assert claim["benchmark_dims"] == frozenset({"pct"})
    assert claim["event_type"] == "limit_up"
    # 给了 n_stock_days, 年化应能算出来(不再 None)
    assert claim["annual_net_pct"] is not None


def test_build_claim_derives_holding_days_from_entry_exit():
    """holding_days 必须从 (entry, exit) 推导填值, 保证与成交价口径自洽(不会 mismatch)。"""
    daily = _make_daily(n_days=30, n_symbols=100, seed=5)
    dates = _trading_dates(30)
    signals = pl.DataFrame({
        "date": [dates[15]] * 20,
        "symbol": [f"{s:06d}.SH" for s in range(20)],
    })
    result = csa.compute_signal_alpha(signals, daily, holding_days=(2,))
    claim = csa.build_claim("t", result, benchmark="market", h=2, event_type="limit_up")
    # open_t1 → close_t2 推导 = 1 个交易日
    assert claim["holding_days"] == 1


def test_unfillable_share_computed_from_buyable():
    """unfillable_share 必须由 buyable 口径算出, 不是硬传 0.0。

    注入「次日开盘触及涨停」(next_open 相对昨收涨 ~10%) 的信号, 应算出
    unfillable_share > 0; 而正常波动(次日开盘不触及涨停)的信号, unfillable_share 应 ≈ 0。
    """
    daily = _make_daily(n_days=30, n_symbols=100, seed=8)
    dates = _trading_dates(30)

    # 正常信号: 所有股票, unfillable 应接近 0 (随机波动, 次日开盘不会触及涨停)
    signals_normal = pl.DataFrame({
        "date": [sd for sd in dates[10:20] for _ in range(20)],
        "symbol": [f"{s:06d}.SH" for _ in range(10) for s in range(20)],
    })
    res_normal = csa.compute_signal_alpha(signals_normal, daily, holding_days=(2,))
    assert res_normal.unfillable_share is not None
    assert res_normal.unfillable_share < 0.5  # 绝大多数可买

    # 注入: 给部分股票的 next_open 抬高到接近涨停价(涨 10%), 使其 buyable=False
    # 选 5 只股票, 把它们的 open 整体抬高, 使 next_open 相对 prev_raw_close 涨 ~10%
    df = daily.clone()
    sel = {f"{s:06d}.SH" for s in range(5)}
    # 抬高这些股票的 open 列(影响 next_open 的计算)
    df = df.with_columns(
        pl.when(pl.col("symbol").is_in(list(sel)))
        .then(pl.col("open") * 1.10)
        .otherwise(pl.col("open"))
        .alias("open")
    )
    res_injected = csa.compute_signal_alpha(signals_normal, df, holding_days=(2,))
    assert res_injected.unfillable_share is not None
    # 注入后应有部分信号不可买(次日开盘触及涨停)
    assert res_injected.unfillable_share > 0.05


def test_date_dtype_mismatch_is_handled():
    """真实数据 date 是 date 类型, trades 提取的信号 date 是 str, join 不能 SchemaError。

    这是容器实测暴露的 bug: 合成数据用 str date 测不出来, 真实 enriched 的 date 是 date。
    内部必须统一 cast, 与输入 dtype 无关。
    """
    daily = _make_daily(n_days=30, n_symbols=100, seed=9)
    # 把 daily 的 date cast 成 date 类型(模拟真实 enriched)
    daily_date = daily.with_columns(pl.col("date").str.to_date().alias("date"))
    dates = _trading_dates(30)
    # 信号 date 是 str(模拟 trades 提取)
    signals = pl.DataFrame({
        "date": [sd for sd in dates[10:20] for _ in range(20)],
        "symbol": [f"{s:06d}.SH" for _ in range(10) for s in range(20)],
    })
    # 不应抛 SchemaError
    result = csa.compute_signal_alpha(signals, daily_date, holding_days=(2,))
    assert result.estimate(2, "market") is not None
    assert result.n_signals_total > 0


def test_board_bucket_controls_board_and_pct():
    """board_bucket 口径必须声明控制 board + pct 两个维度(否则 G3 恒红)。"""
    daily = _make_daily(n_days=30, n_symbols=100, seed=6)
    dates = _trading_dates(30)
    signals = pl.DataFrame({
        "date": [sd for sd in dates[10:20] for _ in range(20)],
        "symbol": [f"{s:06d}.SH" for _ in range(10) for s in range(20)],
    })
    result = csa.compute_signal_alpha(signals, daily, holding_days=(2,))
    claim = csa.build_claim("t", result, benchmark="board_bucket", h=2, event_type="turnover_shock")
    assert claim["benchmark_dims"] == frozenset({"board", "pct"})


def test_end_to_end_incomplete_claim_is_rejected():
    """端到端判据: 自动推导不完整的 Claim 必须被五道门 REJECT(不能静默放行)。

    这是验收标准 #4 的一半: 防止「恒放行」的伪自动化。unfillable_share 现在由
    buyable 口径自动算出(不再缺失), 但缺 capacity_annual_pct / 效应量不足时,
    协议仍必须拒绝。
    """
    import sys
    from pathlib import Path
    analysis_dir = str(Path(__file__).resolve().parents[2] / "analysis")
    if analysis_dir not in sys.path:
        sys.path.insert(0, analysis_dir)
    from eval_protocol import Claim, evaluate

    daily = _make_daily(n_days=30, n_symbols=100, seed=7)
    dates = _trading_dates(30)
    signals = pl.DataFrame({
        "date": [sd for sd in dates[10:20] for _ in range(20)],
        "symbol": [f"{s:06d}.SH" for _ in range(10) for s in range(20)],
    })
    result = csa.compute_signal_alpha(signals, daily, holding_days=(2,))
    # unfillable_share 已由 buyable 口径自动算出(不再是 None)
    assert result.unfillable_share is not None
    claim_dict = csa.build_claim(
        "auto", result, benchmark="board_bucket", h=2,
        event_type="turnover_shock", n_stock_days=len(daily),
    )
    claim = Claim(**{k: v for k, v in claim_dict.items() if k in Claim.__dataclass_fields__})
    verdict = evaluate(claim)
    # 缺 capacity_annual_pct / 效应量不足 ⇒ 仍 REJECT (不是因 unfillable_share_missing)
    assert verdict.decision != "PASS"
    assert "unfillable_share_missing" not in verdict.reasons


def test_end_to_end_complete_claim_passes():
    """端到端判据: 补齐三项后, build_claim 产出的 Claim 必须 PASS(防止「恒拒绝」)。

    这是验收标准 #4 的另一半。采集管道补 unfillable_share + capacity_annual_pct
    (holding_days 由 build_claim 自动推导), 协议必须放行。这一对测试合起来证明
    协议「会分辨」, 而不是「只会拒绝」。
    """
    import sys
    from pathlib import Path
    analysis_dir = str(Path(__file__).resolve().parents[2] / "analysis")
    if analysis_dir not in sys.path:
        sys.path.insert(0, analysis_dir)
    from eval_protocol import Claim, evaluate

    # 构造有真实 edge 的信号(注入 +3% 超额), 让统计链测出显著正 alpha
    import datetime
    import random
    rng = random.Random(7)
    daily = _make_daily(n_days=40, n_symbols=200, seed=2)
    dates = _trading_dates(40)
    sel = sorted(f"{s:06d}.SH" for s in range(10))
    date_idx = {d: i for i, d in enumerate(dates)}
    adjust = set()
    for sd in dates[3:33]:
        t2 = dates[date_idx[sd] + 2]
        for sym in sel:
            adjust.add((sym, t2))
    df = daily.with_columns(
        pl.struct(["symbol", "date"]).map_elements(
            lambda kv: 1.03 if (kv["symbol"], kv["date"]) in adjust else 1.0,
            return_dtype=pl.Float64,
        ).alias("adj")
    ).with_columns((pl.col("close") * pl.col("adj")).alias("close"))
    signals = pl.DataFrame({
        "date": [sd for sd in dates[3:33] for _ in range(10)],
        "symbol": [s for _ in dates[3:33] for s in sel],
    })

    result = csa.compute_signal_alpha(signals, df, holding_days=(2,))
    claim_dict = csa.build_claim(
        "ai_test", result, benchmark="board_bucket", h=2,
        event_type="turnover_shock", n_stock_days=len(daily),
        capacity_annual_pct={5: 18.0, 20: 17.5, 100: 17.0},
        entry_unfillable_share=0.0,
    )
    claim = Claim(**{k: v for k, v in claim_dict.items() if k in Claim.__dataclass_fields__})
    assert claim.holding_days == 1
    verdict = evaluate(claim)
    assert verdict.decision == "PASS"
