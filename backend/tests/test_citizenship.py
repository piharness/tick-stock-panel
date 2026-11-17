"""市场公民分级测试 — 锁「会让等级变假」的三件事。

1. **等级可解释**：每只票都带三轴分位（p_scale / p_turnover / p_amount），
   等级必须由这三轴映射而来，不是黑盒标量。改了 LEVEL_RULES 却让映射失效，
   是这类页面最容易长期藏住的错。
2. **等级时变**：同一只票在「活跃期」和「沉默期」的等级必须不同。若等级是
   静态标签，就退化成「一次贴标签」，失去注意力预算的意义。
3. **空数据降级**：无日线时返回空 rows，不抛异常、不产生假等级。

夹具是纯合成的 6 只 × 90 个交易日，不联网、不读用户数据。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services import citizenship

SYMBOLS = ["600001.SH", "600002.SH", "000001.SZ", "300001.SZ", "002001.SZ", "688001.SH"]


def _trading_days(n: int) -> list[date]:
    out: list[date] = []
    d = date(2026, 6, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _write_store(root: Path, n_days: int = 90) -> Path:
    """写一份最小可用的本地存储：日线分区 + 交易日目录 + 标的表。

    6 只票被设计成三组，覆盖三个等级：
      - 600001.SH / 000001.SZ：大市值 + 高换手 + 高成交额 → core
      - 600002.SH / 300001.SZ：中档 → active/background
      - 002001.SZ / 688001.SH：小市值 + 低换手 + 低成交额 → silent
    """
    data = root / "data"
    days = _trading_days(n_days)
    rows: list[dict] = []
    # 每只票的「身份」：规模倍率、换手基数、成交额基数、流通股本。
    profile = {
        "600001.SH": (100.0, 8.0, 1e9, 1e10),
        "000001.SZ": (80.0, 7.0, 8e8, 8e9),
        "600002.SH": (10.0, 4.0, 2e8, 1e9),
        "300001.SZ": (8.0, 5.0, 1.5e8, 8e8),
        "002001.SZ": (1.0, 1.0, 2e7, 1e8),
        "688001.SH": (0.8, 0.8, 1e7, 8e7),
    }
    inst_rows = []
    for sym in SYMBOLS:
        scale, turn, amt, shares = profile[sym]
        inst_rows.append({"symbol": sym, "name": f"测试{sym}", "float_shares": shares})
        close = 5.0
        for ti, d in enumerate(days):
            prev = close
            close = prev * (1 + 0.002 * math.sin(ti * 0.5 + scale))
            rows.append({
                "symbol": sym, "date": d,
                "open": prev, "high": max(prev, close), "low": min(prev, close), "close": close,
                "volume": amt / max(close, 1e-9), "amount": amt,
                "turnover_rate": turn,
                "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
            })
    frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for d in sorted(frame["date"].unique().to_list()):
        tag = d.isoformat()
        out = data / "kline_daily_enriched" / f"date={tag}"
        out.mkdir(parents=True, exist_ok=True)
        frame.filter(pl.col("date") == d).write_parquet(out / "part.parquet")
        (data / "kline_daily" / f"date={tag}").mkdir(parents=True, exist_ok=True)
    inst = data / "instruments"
    inst.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(inst_rows).write_parquet(inst / "instruments.parquet")
    return data


# ===== 1. 等级可解释 =====


def test_every_row_carries_three_axis_percentiles(tmp_path: Path):
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    assert out["rows"], "有日线时不应返回空 rows"
    for row in out["rows"]:
        assert row["level"] in citizenship.LEVEL_ORDER, row["symbol"]
        # 三轴分位必须在 [0, 1]，且是「分位」而非原始值。
        for axis in ("p_scale", "p_turnover", "p_amount"):
            assert 0.0 <= row[axis] <= 1.0, (row["symbol"], axis, row[axis])


def test_level_matches_axis_rule(tmp_path: Path):
    """等级必须由三轴分位按 LEVEL_RULES 映射而来，不是凭空标量。

    核心判据是成交额（第一性），换手率是「小而活跃」的补充信号：
      - 高成交额 → core（即使换手率不高，大盘蓝筹天然如此）
      - 低成交额 + 低换手 → silent
    """
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    by_sym = {r["symbol"]: r for r in out["rows"]}
    # 成交额最高 → core（换手率不必高，模拟大盘蓝筹）。
    big = by_sym["600001.SH"]
    assert big["level"] == "core", (big["symbol"], big["p_amount"])
    assert big["p_amount"] > 0.5
    # 成交额最低 + 换手最低 → silent。
    small = by_sym["688001.SH"]
    assert small["level"] == "silent", (small["symbol"], small["p_amount"], small["p_turnover"])


def test_summary_counts_add_up(tmp_path: Path):
    data = _write_store(tmp_path)
    out = citizenship.compute(data)
    total = out["summary"]["total"]
    assert total == len(out["rows"])
    assert sum(l["count"] for l in out["summary"]["levels"]) == total


# ===== 2. 等级时变 =====


def test_level_is_time_varying(tmp_path: Path):
    """等级是「当日全市场分位」，不同交易日会得出不同等级。

    构造一只票在前半段「活跃」（高成交额）、后半段「沉默」（低成交额），
    分别用两个 as_of 计算，其等级必须变化 —— 证明等级不是静态标签。
    """
    data = _write_store(tmp_path, n_days=90)
    days = _trading_days(90)

    # 把 002001.SZ 的成交额按日改成：前半段很高、后半段很低。
    mid = days[45]
    for d in days:
        f = data / "kline_daily_enriched" / f"date={d.isoformat()}" / "part.parquet"
        df = pl.read_parquet(f)
        new_amount = 5e9 if d < mid else 1e6  # 前半段成交额极高，后半段极低
        df = df.with_columns(
            pl.when(pl.col("symbol") == "002001.SZ")
            .then(pl.lit(new_amount))
            .otherwise(pl.col("amount"))
            .alias("amount"),
        )
        df.write_parquet(f)

    early = citizenship.compute(data, as_of=days[10])
    late = citizenship.compute(data, as_of=days[80])
    early_by = {r["symbol"]: r for r in early["rows"]}
    late_by = {r["symbol"]: r for r in late["rows"]}

    # 前半段成交额极高 → 应进入 core 或 active；后半段极低 → silent 或 background。
    assert early_by["002001.SZ"]["p_amount"] > 0.5, "前半段应是高成交额"
    assert late_by["002001.SZ"]["p_amount"] < 0.5, "后半段应是低成交额"
    assert early_by["002001.SZ"]["level"] != late_by["002001.SZ"]["level"], (
        "同一只票在不同交易日的等级必须不同，否则退化成静态标签"
    )


# ===== 3. 空数据降级 =====


def test_empty_data_degrades_gracefully(tmp_path: Path):
    data = tmp_path / "data"
    (data / "kline_daily").mkdir(parents=True, exist_ok=True)
    out = citizenship.compute(data)
    assert out["rows"] == []
    assert out["summary"]["total"] == 0


def test_get_citizenship_returns_same_shape(tmp_path: Path):
    data = _write_store(tmp_path)
    out = citizenship.get_citizenship(data)
    assert set(out.keys()) == {"as_of", "prev_as_of", "window_days", "summary", "rows", "disclaimer"}
    assert out["as_of"] is not None


# ===== 4. 等级迁移检测（降格预警）=====


def test_transition_detects_downgrade(tmp_path: Path):
    """一只票从「高成交额（core/active）」降到「低成交额（silent）」，
    其 transition 必须为 'down'，且 prev_level 记录降格前的等级。

    构造：把 002001.SZ 在最后一天改成极低成交额，前一日保持高成交额，
    使最后一天跌入 silent，而前一日仍是 active/core。
    """
    data = _write_store(tmp_path, n_days=90)
    days = _trading_days(90)

    # 默认 profile 里 002001.SZ 是小票（amount=2e7，应落 silent），
    # 但我们需要它「前一天是活跃、最后一天是沉默」。直接把最后一天改成低成交额，
    # 前一日改成高成交额，构造一个明确的降格。
    last = days[-1]
    prev = days[-2]
    for d, target_amt in [(prev, 5e9), (last, 1e5)]:
        f = data / "kline_daily_enriched" / f"date={d.isoformat()}" / "part.parquet"
        df = pl.read_parquet(f)
        df = df.with_columns(
            pl.when(pl.col("symbol") == "002001.SZ")
            .then(pl.lit(target_amt))
            .otherwise(pl.col("amount"))
            .alias("amount"),
        )
        df.write_parquet(f)

    out = citizenship.compute(data, as_of=last)
    by_sym = {r["symbol"]: r for r in out["rows"]}
    row = by_sym["002001.SZ"]
    # 最后一天被改成极低成交额，应落 silent 或 background（低等级）
    assert row["level"] in ("silent", "background"), row["level"]
    # 前一日是高成交额（active/core），故 transition 应为 'down'
    assert row["transition"] == "down", (row["prev_level"], row["level"], row["transition"])
    assert row["prev_level"] in ("active", "core"), row["prev_level"]
    # summary 里降格计数应 >= 1
    assert out["summary"]["transitions"]["down"] >= 1
