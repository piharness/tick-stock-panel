"""回执读侧的合同：区间查询、空洞、失败标的、周末接壤。

这些测试不依赖任何卡、不依赖真实行情目录——回执是自解释的日志，
读侧只应该依赖它自己的字段。凡是需要日历的地方都显式传入 sessions，
免得测试为了造一个交易日去写一堆分区目录。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.decision import receipt

SYM = "000001.SZ"
OTHER = "000002.SZ"


def _row(frm: str, to: str, *, failed: list[str] | None = None,
         outcome: str = "ok", scope: str = "universe") -> dict:
    return {
        "schema_version": 1,
        "run_id": "20260101T000000Z-test",
        "provider": "tickflow",
        "asset_type": "stock",
        "scope": scope,
        "requested_n": 5571,
        "requested_from": frm,
        "requested_to": to,
        "outcome": outcome,
        "failed_symbols": failed or [],
    }


def _write(tmp_path: Path, rows: list[dict], *, mode: str = "w") -> None:
    path = receipt.receipt_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_adjacent_runs_widen_the_answer_without_rewriting_the_log(tmp_path: Path):
    """三段相邻的请求 → 查询得到最后一格；再请求一段 → 空洞被后一段回填。

    注意这里没有「合并成一段」这个动作：日志是 append-only，合并只发生在
    查询里。所以断言的是 `covered_through` 的返回值，而不是文件内容。
    """
    _write(tmp_path, [_row("2026-01-05", "2026-01-05"),
                      _row("2026-01-06", "2026-01-06"),
                      _row("2026-01-07", "2026-01-07")])
    assert receipt.covered_through(tmp_path, SYM, "2026-01-07") == "2026-01-07"
    assert receipt.covered_through(tmp_path, SYM, "2026-01-06") == "2026-01-06"

    _write(tmp_path, [_row("2026-01-07", "2026-01-09")], mode="a")
    # 01-08 从没被单独请求过，但它落在后一段里 → 由后一段作答。
    assert receipt.covered_through(tmp_path, SYM, "2026-01-08") == "2026-01-09"
    assert receipt.covered_through(tmp_path, SYM, "2026-01-09") == "2026-01-09"


def test_a_20_day_outage_stays_a_hole_but_the_covering_run_still_answers(tmp_path: Path):
    """停机 20 天：空洞内答 None，但空洞之前的请求**不能**把后一段一起否掉。

    后半句是这条测试的重点。把「水位不推进」写成实现，会让 `2026-01-16`
    也答 None——那时数据是好的，坏的只是判据，而操作员会去修同步。
    """
    _write(tmp_path, [
        _row("2026-01-05", "2026-01-05"),
        _row("2026-01-12", "2026-01-30"),
        # 无权限而跳过的那次：请求根本没发生，不许把 01-06..01-11 接起来。
        _row("2026-01-06", "2026-01-11", outcome="skipped"),
    ])
    assert receipt.covered_through(tmp_path, SYM, "2026-01-07") is None
    assert receipt.covered_through(tmp_path, SYM, "2026-01-16") == "2026-01-30"
    assert receipt.covered_through(tmp_path, SYM, "2026-01-31") is None


def test_a_failed_symbol_gets_no_coverage_from_that_run(tmp_path: Path):
    """分块失败的标的：这一行对它不算覆盖，同批其他标的照旧。

    失败是「这次没拿到」，不是「这段没数据」——所以不撤回它此前拿到的覆盖，
    也不替同批其他标的背锅。
    """
    _write(tmp_path, [_row("2026-01-12", "2026-01-30", failed=[SYM], outcome="partial")])
    assert receipt.covered_through(tmp_path, SYM, "2026-01-16") is None
    assert receipt.covered_through(tmp_path, OTHER, "2026-01-16") == "2026-01-30"


def test_a_weekend_between_two_runs_is_not_a_hole(tmp_path: Path):
    """周五一段 + 周一一段：自然日上有两天缺口，交易日上无缝。

    `requested_from/to` 是自然日，覆盖判定必须落在交易日上。把相邻判据
    写成「日期差一天」，每个周末都会造一个假空洞。
    """
    sessions = ["2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13"]
    _write(tmp_path, [_row("2026-01-08", "2026-01-09"),
                      _row("2026-01-12", "2026-01-12")])
    assert receipt.window_covered(tmp_path, SYM, "2026-01-08", "2026-01-12", sessions)
    # 周六不在任何区间——这仍然成立，只是没有任何调用方会问周六。
    assert receipt.covered_through(tmp_path, SYM, "2026-01-10") is None


def test_malformed_lines_are_skipped_but_named(tmp_path: Path):
    """坏行不静默：跳过要留行号，否则「少了一行」和「本来就没那行」分不开。"""
    path = receipt.receipt_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_row("2026-01-05", "2026-01-05"), ensure_ascii=False) + "\n"
        + "{not json}\n"
        + json.dumps({"requested_from": "2026-01-06"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rows, problems = receipt.load_receipts(tmp_path)
    assert len(rows) == 1
    assert len(problems) == 2
    assert problems[0].startswith("2:") and problems[1].startswith("3:")


def test_an_empty_window_is_not_covered(tmp_path: Path):
    """窗口内没有交易日 ≠ 已覆盖。空集真值是 fail-open 的标准样板。"""
    _write(tmp_path, [_row("2026-01-05", "2026-01-30")])
    assert not receipt.window_covered(tmp_path, SYM, "2026-01-12", "2026-01-12",
                                      ["2026-01-09", "2026-01-13"])


# ---------------------------------------------------------------------------
# 写侧
# ---------------------------------------------------------------------------


def _built(**over: object) -> dict:
    base = dict(asset_type="stock", provider="tickflow", scope="universe",
                requested=[SYM, OTHER], requested_from="2026-01-05",
                requested_to="2026-01-30", outcome="ok",
                store={"created": True, "rows": 2,
                       "first": "2026-01-06", "last": "2026-01-29"})
    base.update(over)
    return receipt.build_receipt(**base)          # type: ignore[arg-type]


def test_a_receipt_carries_its_own_identity_and_the_store_it_left(tmp_path: Path):
    """回执记自己是谁（run_id 全串）+ 离开时表是什么样（store_*）。

    它**不**记空洞、不指账本：前者要日历（同步层没有），后者方向反了
    （回执活得比账本短）。
    """
    row = _built(run_id="20261108T040244Z-1a2b3c4d")
    assert row["run_id"] == "20261108T040244Z-1a2b3c4d"
    assert row["schema_version"] == receipt.SCHEMA_VERSION
    assert row["requested_n"] == 2
    assert (row["store_created"], row["store_rows"]) == (True, 2)
    assert (row["store_first"], row["store_last"]) == ("2026-01-06", "2026-01-29")
    assert "event_gap_runs" not in row and "settlement_key" not in row


def test_two_runs_leave_two_lines_and_the_old_one_keeps_its_bytes(tmp_path: Path):
    """追加不是覆盖：第二次同步只能往后加，第一行的字节不动。"""
    receipt.append_receipt(tmp_path, _built(run_id="20260101T000000Z-aaaaaaaa"))
    before = receipt.receipt_path(tmp_path).read_bytes()
    receipt.append_receipt(tmp_path, _built(run_id="20260102T000000Z-bbbbbbbb"))
    after = receipt.receipt_path(tmp_path).read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == 2


def test_the_file_is_chosen_by_the_rows_own_asset_type(tmp_path: Path):
    """ETF 回执进 ETF 文件：股票回执盖不住 ETF 的窗口，隔离由路径保证。"""
    receipt.append_receipt(tmp_path, _built(asset_type="etf"))
    receipt.append_receipt(tmp_path, _built())
    etf_rows, _ = receipt.load_receipts(tmp_path, "etf")
    stock_rows, _ = receipt.load_receipts(tmp_path, "stock")
    assert len(etf_rows) == 1 and len(stock_rows) == 1
    # 股票读侧看不见 ETF 那一行：跨市场假覆盖从路径上就不成立。
    assert receipt.coverage_intervals(tmp_path, SYM, "etf") == [("2026-01-05", "2026-01-30")]


def test_a_subset_scope_grants_no_coverage(tmp_path: Path):
    """`scope=symbols` 的行不给任何标的记覆盖：清单没随行保存，判不了就 fail-closed。"""
    receipt.append_receipt(tmp_path, _built(scope="symbols"))
    assert receipt.covered_through(tmp_path, SYM, "2026-01-16") is None


def test_a_reversed_window_is_refused_at_write_time():
    """坏区间写不进去。读侧虽然也会拒，但那时它已经混在文件里了。"""
    with pytest.raises(ValueError):
        _built(requested_from="2026-02-01")
    with pytest.raises(ValueError):
        _built(scope="some")
    with pytest.raises(ValueError):
        _built(outcome="mostly-ok")
