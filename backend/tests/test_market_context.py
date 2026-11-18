"""市场环境层口径测试。

这里测的不是「breadth 等于某个数」，而是**口径**:
- 跨停牌不算一日收益（否则复牌当天会把多日累计记成单日, 广度被污染）;
- 超出涨跌停容忍的样本剔除（前复权跳变/复牌）;
- 缺数据时返回 unknown 而不是抛错（状态条不能因为一个分区坏掉就 500）。

数值断言只用「构造出来的确定分布」, 不依赖真实数据。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services.market_context import build_market_context, clear_cache


def _write_parts(root: Path, rows: list[dict]) -> None:
    dates = sorted({row["date"] for row in rows})
    for day in dates:
        sub = [row for row in rows if row["date"] == day]
        target = root / f"date={day.isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": [row["symbol"] for row in sub],
                "date": [row["date"] for row in sub],
                "close": [row["close"] for row in sub],
            }
        ).write_parquet(target / "part.parquet")


def _series_rows(
    n_days: int, up: int, down: int, *, start: date = date(2026, 1, 5)
) -> tuple[list[dict], list[date]]:
    days = [start + timedelta(days=i) for i in range(n_days)]
    rows: list[dict] = []
    for idx in range(up + down):
        price = 10.0
        rate = 1.01 if idx < up else 0.99
        for day in days:
            rows.append({"symbol": f"S{idx:02d}", "date": day, "close": price})
            price *= rate
    return rows, days


def _daily(root: Path, limit: int = 25):
    from app.services.market_context import _load_daily

    return _load_daily(root, limit)


def test_breadth_uses_only_stocks_whose_previous_day_exists(tmp_path: Path):
    """跨停牌的行不能算一日收益。

    S00 在第 10 个交易日停牌（当天没有行），复牌当天 +3%。若按行偏移算收益,
    这一行会被当成「上涨」多记一个样本; 按交易日历相邻算, 它应当被丢弃。
    其他 19 只当天全部下跌 ⇒ 正确结果是 breadth=0, n=19。
    """
    rows, days = _series_rows(25, up=0, down=19)
    # 再补一只停牌票: 第 0 天 10.0, 缺第 10 天, 第 11 天 10.3
    park_price = {0: 10.0, 9: 10.0, 11: 10.3, 12: 10.4}
    for i, day in enumerate(days):
        if i in park_price:
            for k in (20,):
                rows.append({"symbol": f"S{k:02d}", "date": day, "close": park_price[i]})
    root = tmp_path / "kline_daily_enriched"
    _write_parts(root, rows)

    daily = _daily(root, 25)
    by_day = {row["date"]: row for row in daily.to_dicts()}
    resume = by_day[days[11]]
    assert resume["n"] == 19, "停牌复牌当天不应计入等权样本"
    assert resume["breadth"] == pytest.approx(0.0)


def test_extreme_returns_are_dropped(tmp_path: Path):
    """|ret| >= 11.5% 视为坏数据: 主板涨跌停约束下不该出现。"""
    rows, days = _series_rows(25, up=10, down=10)
    # S99 在最后一天翻倍: 若未剔除, 等权日收益会被这 1 只拉高 5%
    for day in days[:-1]:
        rows.append({"symbol": "S99", "date": day, "close": 10.0})
    rows.append({"symbol": "S99", "date": days[-1], "close": 20.0})
    root = tmp_path / "kline_daily_enriched"
    _write_parts(root, rows)

    daily = _daily(root, 25)
    last = daily.to_dicts()[-1]
    assert last["n"] == 20
    assert abs(last["eq_ret"]) < 0.02


def test_build_market_context_flags_risk_off_and_caches(tmp_path: Path):
    clear_cache()
    rows, _ = _series_rows(25, up=2, down=8)
    root = tmp_path / "kline_daily_enriched"
    _write_parts(root, rows)

    ctx = build_market_context(tmp_path, date(2026, 1, 29))
    assert ctx["state"] == "risk_off"
    assert ctx["breadth_20d"] == pytest.approx(0.2, abs=1e-6)
    assert ctx["eq_ma20_bp"] < 0
    assert ctx["samples"] == 10
    assert ctx["threshold"] == 0.5
    # 证据必须随响应下发: 前端不该自己硬编码这些历史数字
    assert ctx["evidence"]["source"].startswith("analysis/")

    again = build_market_context(tmp_path, date(2026, 1, 29))
    assert again is ctx, "同一参考日应命中缓存"


def test_build_market_context_marks_risk_on_when_majority_advances(tmp_path: Path):
    clear_cache()
    rows, _ = _series_rows(25, up=8, down=2)
    root = tmp_path / "kline_daily_enriched"
    _write_parts(root, rows)

    ctx = build_market_context(tmp_path, date(2026, 1, 29))
    assert ctx["state"] == "risk_on"
    assert ctx["breadth_20d"] == pytest.approx(0.8, abs=1e-6)
    assert "β 环境正常" in ctx["note"]


def test_missing_partitions_degrade_to_unknown_without_raising(tmp_path: Path):
    clear_cache()
    ctx = build_market_context(tmp_path, date(2026, 1, 29))
    assert ctx["state"] == "unknown"
    assert ctx["breadth_20d"] is None
    assert "error" in ctx
    assert ctx["evidence"]["same_state_days"] > 0
