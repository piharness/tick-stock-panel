"""`scripts/fetch_l1_open_depth.py` 的两处静默失明。

为什么这轮要测它
----------------
`bar0_from` 上一版用**全局** `min(tm)` 当首根。一批 20 只里只要有一只 09:31 无成交
（停牌、一字板无撮合），它就会被写成 09:32 的开盘价，`nbars` 仍是 240，没有任何告警。
下游 `curve[k=0].amount == b0_amt` 的比对会差整整一根——而这是 `curves.parquet`
唯一能校验的数据。

`--dry-run` 上一版报 `len(dates)` 个请求，把"每天 ≤20 只"当成了前提；实际是按
`BATCH` 分批，一天 40 只需两次。少报会让"20 分钟跑完"的估计失真。

五条各配坏样本：全局首根、整批丢弃、空输入、按天计数、amount 身份。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_l1_open_depth as F  # noqa: E402


def _frame(spec: dict[str, datetime], n: int = 3) -> pl.DataFrame:
    rows = []
    for s, t0 in spec.items():
        for i in range(n):
            rows.append({
                "symbol": s,
                "datetime": t0 + timedelta(minutes=i),
                "open": 10.0 + i,
                "high": 10.5 + i,
                "low": 9.5 + i,
                "close": 10.2 + i,
                "amount": 1.0e6 * (i + 1),
                "volume": 1.0e5 * (i + 1),
            })
    return pl.DataFrame(rows).with_columns(
        pl.col("datetime").cast(pl.Datetime("us"))
    )


OPEN = datetime(2026, 10, 9, 9, 31)


def test_bar0_takes_each_symbol_own_first_bar() -> None:
    df = _frame({"000001.SZ": OPEN, "600000.SH": OPEN}, n=3)
    b0, dropped = F.bar0_from(df)
    assert dropped == []
    assert b0.height == 2
    assert set(b0["date"]) == {"2026-10-09"}
    assert set(b0["nbars"]) == {3}
    # 首根的开盘价与成交额必须是第 0 根，不是后面某根
    assert sorted(b0["b0_open"].to_list()) == [10.0, 10.0]
    assert sorted(b0["b0_amt"].to_list()) == [1.0e6, 1.0e6]


def test_bar0_reports_symbols_without_0931() -> None:
    """缺 09:31 的票必须进 dropped，且不能出现在 b0 里（上一版会静默丢）。"""
    df = _frame({"000001.SZ": OPEN, "600000.SH": OPEN + timedelta(minutes=1)}, n=3)
    b0, dropped = F.bar0_from(df)
    assert dropped == ["600000.SH"]
    assert b0["symbol"].to_list() == ["000001.SZ"]
    assert b0["b0_open"].to_list() == [10.0]


def test_bar0_batch_without_any_0931_is_reported_not_silent() -> None:
    """整批都没有 09:31 时，上一版返回空 DataFrame 且 failed 不记 ⇒ 无告警丢一天。"""
    df = _frame({"000001.SZ": OPEN + timedelta(minutes=1)}, n=2)
    b0, dropped = F.bar0_from(df)
    assert b0.is_empty()
    assert dropped == ["000001.SZ"]


def test_bar0_empty_input() -> None:
    b0, dropped = F.bar0_from(pl.DataFrame())
    assert b0.is_empty()
    assert dropped == []


def test_bar0_amount_is_first_bar_identity() -> None:
    """k=0 切片的身份：b0_amt 必须等于该票第 0 根的 amount，差一根就红。"""
    df = _frame({"000001.SZ": OPEN}, n=4)
    b0, _ = F.bar0_from(df)
    first = df.filter(pl.col("datetime") == OPEN)["amount"].to_list()
    assert b0["b0_amt"].to_list() == first


def test_requests_needed_counts_batches_not_days() -> None:
    assert F.requests_needed([40, 8], batch=20) == 3     # 2 + 1
    assert F.requests_needed([20] * 5, batch=20) == 5    # 边界：整批不多发
    assert F.requests_needed([0]) == 0                   # 没有待抓就不发
    assert F.requests_needed([]) == 0


def test_requests_needed_red_when_counted_by_day() -> None:
    """若有人退回按天数计，这条会红：40 只/天的两天不是 2 次请求。"""
    assert F.requests_needed([40, 40], batch=20) == 4
    assert F.requests_needed([40, 40], batch=20) != 2


# ---------------------------------------------------------------- 曲线模式（本轮新增）
def test_curves_keep_every_bar_and_index_k_per_symbol() -> None:
    """k 必须是**单票内**序号：整批 20 只共用行号会让 Δ(k) 指向别人的 bar。"""
    df = _frame({"000001.SZ": OPEN, "600000.SH": OPEN}, n=4)
    cv, dropped = F.curves_from(df)
    assert dropped == []
    assert cv.height == 8                       # 4 根 × 2 只，一根都不许丢
    for s in ("000001.SZ", "600000.SH"):
        sub = cv.filter(pl.col("symbol") == s).sort("k")
        assert sub["k"].to_list() == [0, 1, 2, 3]
        assert sub["open"].to_list() == [10.0, 11.0, 12.0, 13.0]
    assert cv.select("date").unique().height == 1


def test_curves_drop_symbol_without_0931() -> None:
    """没有 09:31 的票不能拿 09:32 当首根：整条曲线进 dropped，不静默截断。"""
    df = _frame({"000001.SZ": OPEN, "600000.SH": OPEN + timedelta(minutes=1)}, n=3)
    cv, dropped = F.curves_from(df)
    assert dropped == ["600000.SH"]
    assert set(cv["symbol"]) == {"000001.SZ"}
    assert cv["k"].to_list() == [0, 1, 2]


def test_curves_empty_input() -> None:
    cv, dropped = F.curves_from(pl.DataFrame())
    assert cv.is_empty() and dropped == []


def _curve_frame(n: int, sym: str = "000001.SZ", date: str = "2026-10-09") -> pl.DataFrame:
    return pl.DataFrame({
        "date": [date] * n,
        "symbol": [sym] * n,
        "k": list(range(n)),
        "open": [10.0] * n,
        "high": [10.5] * n,
        "low": [9.5] * n,
        "close": [10.2] * n,
        "amount": [1.0e6] * n,
        "volume": [1.0e5] * n,
        "nbars": [n] * n,
    })


def test_complete_cells_requires_enough_bars() -> None:
    """半截曲线不算已抓：否则断点续跑会把 200 根的格永久当成完成。"""
    full = _curve_frame(F.CURVE_FULL_BARS)
    short = _curve_frame(50, sym="600000.SH")
    got = F.complete_cells(pl.concat([full, short]))
    assert got == {("2026-10-09", "000001.SZ")}
    assert F.complete_cells(pl.DataFrame()) == set()


def test_curve_integrity_detects_broken_k() -> None:
    """k 缺一段时 rows != kmax+1，必须报 broken；这是 Δ(k) 静默错位的唯一告警。"""
    good = F.curve_integrity(_curve_frame(240))
    assert good["cells"] == 1 and good["broken"] == 0 and good["full"] == 1
    holed = _curve_frame(240).filter(pl.col("k") != 100)
    bad = F.curve_integrity(holed)
    assert bad["broken"] == 1
    assert F.curve_integrity(pl.DataFrame())["cells"] == 0


def test_curve_integrity_detects_nbars_mismatch() -> None:
    """nbars 写 240 但只落 239 根：签名对不上，必须红。"""
    df = _curve_frame(239).with_columns(pl.lit(240).alias("nbars"))
    assert F.curve_integrity(df)["broken"] == 1
