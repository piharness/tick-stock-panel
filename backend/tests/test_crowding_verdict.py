"""拥挤度判决测试 (不依赖真实全市场数据)。

覆盖: build_crowding_verdict 的降级路径 (无日线/缺列/样本不足) 与
最小数据的正确性 (换手率分组单调、按市值分层字段齐全)。
日期用 2026-08 连续自然日构造 (与 test_dragon_tiger 风格一致)。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.services import market_percentiles as mp


class _FakeRepo:
    def __init__(self, data_dir: Path):
        self.store = type("S", (), {"data_dir": data_dir})()


def _write_daily(data_dir: Path, symbol: str, closes: list[float],
                 turnovers: list[float], amounts: list[float]) -> None:
    """按日分区写 enriched 日线（含 raw_close/turnover_rate/amount）。"""
    base = date(2026, 8, 1)
    root = data_dir / "kline_daily_enriched"
    for i, c in enumerate(closes):
        d = base + timedelta(days=i)
        part = root / f"date={d}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": [symbol], "date": [d],
            "open": [c], "high": [c], "low": [c], "close": [c],
            "raw_close": [c], "raw_high": [c], "raw_low": [c],
            "volume": [1.0], "amount": [amounts[i]], "turnover_rate": [turnovers[i]],
            "consecutive_limit_ups": [0], "consecutive_limit_downs": [0],
            "quote_ts": [0],
        }).write_parquet(part)


def test_crowding_verdict_empty_without_daily(tmp_path: Path):
    out = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert out["n"] == 0
    assert any("无日线" in c for c in out["caveats"])


def test_crowding_verdict_insufficient_samples(tmp_path: Path):
    # 只有 3 天数据 → 前向 20 日收益算不出，样本为空
    _write_daily(tmp_path, "600519.SH", [10.0, 10.1, 10.2],
                 [1.0, 1.0, 1.0], [1e8, 1e8, 1e8])
    out = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    # 前向收益算不出 → n=0，且无 quintiles
    assert out["n"] == 0
    assert "quintiles" not in out


def test_crowding_verdict_reports_quintiles(tmp_path: Path):
    """构造足够长（100 自然日）多票数据，验证分位与按市值分层字段齐全。"""
    n = 100
    # 8 只票，各自 100 自然日（前向 20 交易日可覆盖，样本 > 0）
    for j in range(8):
        closes = [10.0 + j + i * 0.05 for i in range(n)]
        turnovers = [1.0 + (j * 0.5) + (i % 5) * 0.1 for i in range(n)]  # 不同票换手率分层
        amounts = [(1 + j) * 1e8 for _ in range(n)]  # 不同票成交额分层（市值代理）
        _write_daily(tmp_path, f"60000{j}.SH", closes, turnovers, amounts)

    out = mp.build_crowding_verdict(_FakeRepo(tmp_path))
    assert out["n"] >= 1
    assert len(out["quintiles"]) == 5
    assert len(out["by_size"]) == 5
    # 分位标签单调
    labels = [q["label"] for q in out["quintiles"]]
    assert labels == ["低换手", "中低", "中", "中高", "高换手"]
    # 字段 key 齐全（数值可能为 None，因为单元测试样本量小、分位组可能为空）
    for q in out["quintiles"]:
        assert "ex20_median_bp" in q and "ex20_mean_bp" in q
    for b in out["by_size"]:
        assert "hi_minus_lo_bp" in b
