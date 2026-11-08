"""结算收益的分红口径：陈旧的复权序列不能把整段分红吃掉。

`kline_daily_enriched.close` 是按日增量落盘的派生列。除权事件到达与「受影响
标的全历史重算」之间有一段滞后。滞后窗口里 `close/raw_close` 在除权日不阶跃，
于是同一标的的入场、出场两端被**同一个**陈旧因子除过，因子约掉，收益退化成
不复权收益——分红整段消失，而这一行已经被 `done` 键固化，重跑不会修它。

这个测试把滞后**造出来**，不去依赖真实管道恰好落后：复权序列按陈旧口径写死，
事件表按真实口径写入，两者自相矛盾。旧实现在这里给出负收益，正确实现必须给出
含分红的正收益。两种口径连符号都相反，不是小数点后第几位的问题。

四个用例分别钉四件事：含事件时用事件表、没有事件表时**降级并被标注**、
事件表只覆盖更晚日期时也降级并被标注、事件表**只覆盖更早日期**时挂起并等表
追上来。后三个是「不知道」与「没有」的分界——把「不知道」当成「没有」会重新
引入同一个错误；而「不知道」里还要再分一层：等下去会变的那一种不该被写进账本。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.decision import bars, ledger, settle

SYM = "600519.SH"
BENCH = "000300.SH"
START = date(2026, 1, 5)

#: 入场日与出场日的不复权价。出场日的复权价**故意**写成与不复权价相同：
#: 这就是「事件已到、重算还没跑」的形态，`close/raw_close` 在新旧两天相等。
ENTRY_RAW = 10.00
EXIT_RAW = 9.53
#: 真实口径的单事件因子 = 除权前收盘 / 除权参考价。
EX_FACTOR = 10.00 / 9.52


def _write_part(path: Path, df: pl.DataFrame) -> None:
    path.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path / "part.parquet")


def _seed(tmp_path: Path, *, n: int = 25,
          events: list[tuple[str, str, float]] | None = None,
          factor_table: bool = True, anchor: bool = True) -> list[str]:
    """落 25 个交易日：第 2 天入场、第 7 天出场，其余日子价格不变。

    `anchor=True` 会在起始日之前塞一条**别的标的**的事件（因子恰为 1），
    让事件表的时间范围盖住窗口——真实事件表是从更早的日期开始累积的，
    只放窗口内一条记录会造出一张「范围盖不住窗口」的表，那是另一个用例。
    """
    days = [START + timedelta(days=i) for i in range(n)]
    for i, d in enumerate(days):
        iso = d.isoformat()
        raw = ENTRY_RAW if i <= 1 else (EXIT_RAW if i >= 6 else ENTRY_RAW)
        close = raw                      # 陈旧：复权价没有跟着事件重算
        _write_part(tmp_path / "kline_daily" / f"date={iso}", pl.DataFrame({
            "symbol": [SYM], "date": [d], "open": [raw], "high": [raw],
            "low": [raw], "close": [raw], "volume": [1e4], "amount": [1e6],
        }))
        _write_part(tmp_path / "kline_daily_enriched" / f"date={iso}", pl.DataFrame({
            "symbol": [SYM], "date": [d], "open": [raw], "high": [raw],
            "low": [raw], "close": [close], "volume": [1e4], "amount": [1e6],
            "raw_close": [raw], "raw_high": [raw], "raw_low": [raw],
        }))
        _write_part(tmp_path / "kline_index_enriched" / f"date={iso}", pl.DataFrame({
            "symbol": [BENCH], "date": [d], "open": [4000.0], "high": [4000.0],
            "low": [4000.0], "close": [4000.0], "raw_close": [4000.0],
            "volume": [1e9], "amount": [1e12],
        }))
    rows = list(events or [])
    if anchor:
        rows.insert(0, ("000001.SZ", (START - timedelta(days=10)).isoformat(), 1.0))
    if factor_table and rows:
        path = tmp_path / "adj_factor" / "all.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": [r[0] for r in rows],
            "trade_date": [date.fromisoformat(r[1]) for r in rows],
            "ex_factor": [r[2] for r in rows],
        }).write_parquet(path)
    return [d.isoformat() for d in days]


def _settle_once(tmp_path: Path, days: list[str], *, horizon: int = 5) -> dict:
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0], picks=[{"symbol": SYM, "limit_price": ENTRY_RAW, "p_win": 0.6}],
        horizon_sessions=horizon))
    summary = settle.settle(tmp_path)
    rows = ledger.load_settlements(tmp_path)
    assert len(rows) == 1, summary
    return {"summary": summary, "row": rows[0]}


def test_stale_adjusted_series_still_books_the_dividend(tmp_path: Path):
    """事件因子必须进收益：陈旧复权价算出的负收益，与含分红的正收益符号相反。"""
    days = _seed(tmp_path, events=[(SYM, "2026-01-11", EX_FACTOR)])
    out = _settle_once(tmp_path, days)
    row = out["row"]

    expected = EXIT_RAW * EX_FACTOR / ENTRY_RAW - 1.0
    assert row["price_basis"] == "EVENT"
    # 落盘字段是 8 位小数的展示值；承重的是 `ret`，它由未舍入的因子算出。
    assert row["window_ex_factor"] == pytest.approx(EX_FACTOR, rel=0, abs=1e-8)
    # 随行保存的读数：这一行的因子由 1 行事件构成，表端点是当时读到的范围。
    assert row["window_ex_events"] == 1
    assert row["ex_table_first"] == (START - timedelta(days=10)).isoformat()
    assert row["ex_table_last"] == "2026-01-11"
    assert row["exit"]["ret"] == pytest.approx(expected, rel=0, abs=1e-6)
    # 两种口径的结论相反：不含分红是亏的，含分红是赚的。
    naive = EXIT_RAW / ENTRY_RAW - 1.0
    assert naive < 0 < expected
    assert row["outcome"] == 1
    assert out["summary"]["return_basis"]["EVENT"] == 1


def test_without_event_table_the_basis_is_labelled_not_silently_raw(tmp_path: Path):
    """没有事件表 = 不知道有没有分红，必须落到被标注的口径上，而不是假装没有。"""
    days = _seed(tmp_path, events=[(SYM, "2026-01-11", EX_FACTOR)], factor_table=False)
    row = _settle_once(tmp_path, days)["row"]
    assert row["price_basis"] == "ADJUSTED"
    assert row["window_ex_factor"] is None
    # 判不出的窗口：行数必须是 None。写成 0 就与「确实没有除权」混为一谈。
    assert row["window_ex_events"] is None
    assert row["ex_table_first"] is None and row["ex_table_last"] is None
    assert row["exit"]["ret"] == pytest.approx(EXIT_RAW / ENTRY_RAW - 1.0, abs=1e-9)


def test_event_table_that_does_not_cover_the_window_is_not_treated_as_empty(tmp_path: Path):
    """事件表只覆盖更晚的日期时，窗口内的「没有事件」是不可判定的，不许当 0 用。"""
    days = _seed(tmp_path, events=[(SYM, "2026-06-01", 1.2)], anchor=False)
    row = _settle_once(tmp_path, days)["row"]
    assert row["price_basis"] == "ADJUSTED"
    assert bars.ex_factor_product(tmp_path, [SYM], days[1], days[6]) is None


def _rewrite_events(tmp_path: Path, rows: list[tuple[str, str, float]]) -> None:
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "trade_date": [date.fromisoformat(r[1]) for r in rows],
        "ex_factor": [r[2] for r in rows],
    }).write_parquet(tmp_path / "adj_factor" / "all.parquet")


def test_window_beyond_the_table_is_held_then_settles_with_the_dividend(tmp_path: Path):
    """窗口晚于表末 = 同步没追上，这一行不写盘；表追上来后同一条键算成 EVENT。

    「不写盘」不是洁癖：写进去就被 `done` 键固化，而下一个交易日事件表到了，
    同一条键再也不会被重算——两种口径的收益**符号相反**（含分红为正，不含为负），
    固化的不是小数位。
    """
    days = _seed(tmp_path, events=[], anchor=True)   # 表只到 START-10 天，窗口在它之后
    ledger.save_card(tmp_path, ledger.build_card(
        as_of=days[0], picks=[{"symbol": SYM, "limit_price": ENTRY_RAW, "p_win": 0.6}],
        horizon_sessions=5))

    held = settle.settle(tmp_path)
    assert held["written"] == 0
    assert held["pending"] == 1
    assert held["pending_reasons"]["window_uncovered"] == 1
    assert held["pending_reasons"]["not_due"] == 0
    assert ledger.load_settlements(tmp_path) == []   # 账本仍是空的，没有半条降级行
    # 覆盖对照：表末落在日历末日之前，这一格就是「降级还有入口」的读数。
    assert held["event_coverage"]["endpoints_span_calendar"] is False
    assert held["event_coverage"]["ex_table_last"] < held["event_coverage"]["calendar_last"]

    # 同步追上：窗口内的事件到了，表末也越过日历末日。
    _rewrite_events(tmp_path, [
        ("000001.SZ", (START - timedelta(days=10)).isoformat(), 1.0),
        (SYM, "2026-01-11", EX_FACTOR),
        ("000001.SZ", (START + timedelta(days=40)).isoformat(), 1.0),
    ])
    healed = settle.settle(tmp_path)
    assert healed["written"] == 1 and healed["pending"] == 0
    row = ledger.load_settlements(tmp_path)[0]
    expected = EXIT_RAW * EX_FACTOR / ENTRY_RAW - 1.0
    assert row["price_basis"] == "EVENT"
    assert row["exit"]["ret"] == pytest.approx(expected, rel=0, abs=1e-6)
    naive = EXIT_RAW / ENTRY_RAW - 1.0
    assert naive < 0 < expected          # 挂起换来的不是精度，是符号
    assert healed["return_basis"] == {"EVENT": 1, "ADJUSTED": 0, "RAW": 0}
    assert healed["event_coverage"]["endpoints_span_calendar"] is True
