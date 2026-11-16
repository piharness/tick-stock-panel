"""分钟审计的落地位：唯一实现、只读、单日失败不中断整批。

三条不变量，每条都配了会红的输入：

1. **网格/判据只有一份** —— `minute_audit` 是定义点，`analysis/` 的 CLI 与
   盘后管道都转发它。有人再抄一份，`GRID is` 立刻红（复制一份等值 tuple 也红）。
2. **审计只读** —— 修数据是显式动作（`repair_plan` + `--apply`），审计本身
   不许动盘：静默修完就没有证据知道源还在产脏。
3. **观测不许变成故障源** —— 单日读不动（分区多文件/键重复）记 `error`，
   另一日照常出数；盘后管道因此不会因为一次审计异常而丢阶段。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.market_time import cn_today, minute_grid
from app.services import data_integrity, minute_audit, preferences
from app.tickflow.capabilities import Cap, CapabilitySet

DAY = date(2026, 11, 12)


def _bar(sym: str, stamp: str, px: float = 10.0, vol: float = 1000.0) -> dict:
    return {
        "symbol": sym, "datetime": datetime.fromisoformat(stamp),
        "open": px, "high": px, "low": px, "close": px,
        "volume": vol, "amount": px * vol * 100,
    }


def _clean_day(sym: str = "000001.SZ", px: float = 10.0, vol: float = 1000.0) -> list[dict]:
    return [_bar(sym, f"{DAY} {t.strftime('%H:%M')}:00", px, vol) for t in minute_grid()]


def _write_minute(data_dir: Path, day: date, rows: list[dict], name: str = "part.parquet") -> Path:
    out = data_dir / "kline_minute" / f"date={day}" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(out)
    return out


def _write_daily(data_dir: Path, day: date, sym: str, volume: float,
                 amount: float, px: float = 10.0) -> Path:
    out = data_dir / "kline_daily" / f"date={day}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"symbol": sym, "date": day, "high": px, "low": px, "close": px,
                   "volume": volume, "amount": amount}]).write_parquet(out)
    return out


# ---------------------------------------------------------------- 唯一实现


def test_grid_has_exactly_one_definition():
    assert minute_audit.GRID == minute_grid()
    assert len(minute_audit.GRID) == 240


def test_cli_module_is_shipped():
    """判据必须能进镜像：容器里 `python -m app.services.minute_audit` 是唯一修法。"""
    assert Path(minute_audit.__file__).name == "minute_audit.py"
    assert callable(minute_audit.main)


# ---------------------------------------------------------------- 判据本身


def test_phantom_1300_is_counted_on_both_axes(tmp_path):
    """11:30 被贴第二个标签 13:00：L 多一根、M 多一份量，而 D 归零。

    D 归零正是「差额全部来自网格外那几行」的签名 —— 归因与判据不是同一列。
    """
    rows = _clean_day()
    rows.append(_bar("000001.SZ", f"{DAY} 13:00:00"))  # 与 11:30 的 OHLCV 全等
    _write_minute(tmp_path, DAY, rows)
    _write_daily(tmp_path, DAY, "000001.SZ", volume=240 * 1000, amount=240 * 1000 * 100 * 10)

    r = minute_audit.audit_day(tmp_path, DAY)
    assert r["rows"] == 241
    assert r["phantom"] == 1
    assert r["label_bad"] == 1  # bars != 240
    assert r["money_bad"] == 1  # Σ全量 多 1000 手
    assert r["gap_bad"] == 0    # 网格内相等 ⇒ 差额确实全在网格外
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_MONEY


def test_latest_days_skips_the_unclosed_today(tmp_path, monkeypatch):
    """盘中不许把「今天」当成品审计, 收盘后不许因为「是今天」放过它。

    两半都要钉住: 只测「跳过」会把真异常一起跳过, 只测「不跳过」会留下盘中的假红。

    第三条是 2026-11-13 复核补的：**日期与钟点必须来自同一个 `now`**。
    上一版 `clock` 用 `now`、日期用 `cn_today()`，于是「回放 11-16 早上」时
    实现会去问真实当下（11-13 早上），把盘上最新那天当成未收盘的今天丢掉。
    这里把 `cn_now` 钉在 11-13 09:37：任何「用 cn_now 补日期」的实现都会在
    第一条上红。
    """
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    bj = ZoneInfo("Asia/Shanghai")
    for d in ("2026-11-11", "2026-11-12", "2026-11-13"):
        (tmp_path / "kline_minute" / f"date={d}").mkdir(parents=True)
    monkeypatch.setattr(minute_audit, "cn_now", lambda: dt(2026, 11, 13, 9, 37, tzinfo=bj))

    intraday = dt(2026, 11, 13, 9, 37, tzinfo=bj)
    assert minute_audit.latest_days(tmp_path, now=intraday) == ["2026-11-11", "2026-11-12"]

    after_close = dt(2026, 11, 13, 15, 30, tzinfo=bj)
    assert minute_audit.latest_days(tmp_path, now=after_close)[-1] == "2026-11-13"

    # 回放 11-16（周一）早上: 盘上最新 11-13 是上周五, 是成品, 必须进窗口
    assert minute_audit.latest_days(
        tmp_path, now=dt(2026, 11, 16, 9, 37, tzinfo=bj))[-1] == "2026-11-13"


def test_clean_day_is_green(tmp_path):
    _write_minute(tmp_path, DAY, _clean_day())
    _write_daily(tmp_path, DAY, "000001.SZ", volume=240 * 1000, amount=240 * 1000 * 100 * 10)
    r = minute_audit.audit_day(tmp_path, DAY)
    assert (r["label_bad"], r["money_bad"], r["gap_bad"], r["phantom"],
            r["block_1500"], r["daily_bad"]) == (0, 0, 0, 0, 0, 0)
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_GREEN


def test_block_axis_sees_volume_that_never_entered_ohlc(tmp_path):
    """收盘 bar 的 amount 比 volume·100·close 多 40 手 —— 不进 OHLC 却进量额。"""
    rows = _clean_day()
    rows[-1] = {**rows[-1], "amount": rows[-1]["amount"] + 40 * 100 * 10.0}
    _write_minute(tmp_path, DAY, rows)
    _write_daily(tmp_path, DAY, "000001.SZ", volume=240 * 1000, amount=240 * 1000 * 100 * 10)
    r = minute_audit.audit_day(tmp_path, DAY)
    assert r["block_1500"] == 1
    assert r["money_bad"] == 0  # 量对得上，只有额不对 —— 两条轴必须分开
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_BLOCK


# ------------------------------------------------- 大宗交易口径（BJ）的分层


def _bj_block_day(tmp_path, sym="920375.BJ", *, close=10.0, block_px=9.5,
                  block_hands=400.0, split=False):
    """BSE 形态：400 手大宗折价成交，分钟侧并入 15:00 那根，日线不含它。

    日线 = 竞价部分（240,000 手）；分钟 = 竞价 + 大宗。`split=True` 时把大宗
    拆成 100 手（10:00，不进收盘 bar）+ 300 手，用来打断 M↔B 的桥。
    """
    rows = _clean_day(sym, close)
    tail_hands = block_hands - 100.0 if split else block_hands
    if split:
        mid = rows[29]  # 10:00（09:31 起第 30 根）
        rows[29] = {**mid, "volume": mid["volume"] + 100.0,
                    "amount": mid["amount"] + 100.0 * 100 * block_px}
    last = rows[-1]
    rows[-1] = {**last, "volume": last["volume"] + tail_hands,
                "amount": last["amount"] + tail_hands * 100 * block_px}
    _write_minute(tmp_path, DAY, rows)
    _write_daily(tmp_path, DAY, sym, volume=240 * 1000, amount=240 * 1000 * 100 * close,
                 px=close)
    return minute_audit.audit_day(tmp_path, DAY)


def test_bj_block_trade_is_a_basis_not_a_failure(tmp_path):
    """6 天 16/16 条明细都是这个形态 —— 让它驱动 rc 就是每天恒红（=静音）。"""
    r = _bj_block_day(tmp_path)
    assert (r["money_bad"], r["money_bad_basis"]) == (1, 1)
    assert (r["block_1500"], r["block_1500_basis"]) == (1, 1)
    assert r["bridge_bad"] == 0
    tot = minute_audit.summarize([r])
    assert tot["basis_rows"] == 2
    assert (tot["money_bad_unexplained"], tot["block_1500_unexplained"]) == (0, 0)
    assert minute_audit.verdict(tot) == minute_audit.RC_GREEN
    # 但必须看得见：产物里有隐含大宗价，不是「忽略 BJ」
    assert abs(r["money_bad_detail"][0]["implied_px"] - 9.5) < 1e-6


def test_same_shape_outside_bj_is_still_red(tmp_path):
    """非 BJ 一旦出现这个形态就是新事实 —— 没有先验，不能按「已知口径」放过。"""
    r = _bj_block_day(tmp_path, sym="000001.SZ")
    assert r["money_bad_basis"] == 0 and r["block_1500_basis"] == 0
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_MONEY


def test_implausible_implied_price_is_not_a_basis(tmp_path):
    """折价 10% 讲得通，折到二十分之一讲不通 —— 2× 包络就是为这一支。"""
    r = _bj_block_day(tmp_path, block_px=0.5)  # 5% of close
    assert r["money_bad_basis"] == 0
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_MONEY


def test_bridge_catches_a_residual_that_is_not_in_the_closing_bar(tmp_path):
    """M 说 400 手、B 只看见 300 手 ⇒ 差额并不全在 15:00 那根，那才是要红的。

    「两条轴都红」不等于「两条轴说的是同一件事」：拆分后 M 仍可解释为 400 手大宗，
    但收盘 bar 只承载了 300 手，另一个 100 手在 10:00 —— 那不是大宗会有的位置。
    """
    r = _bj_block_day(tmp_path, split=True)
    assert r["money_bad_basis"] == 1 and r["block_1500_basis"] == 1
    assert r["bridge_bad"] == 1
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_MONEY


def test_bj_daily_average_out_of_range_is_a_basis(tmp_path):
    """日线均价被折价单拉出 [low, high]：同一机制在日线侧的投影。"""
    rows = _clean_day("920375.BJ")
    rows = [{**r, "amount": r["amount"] * 0.99} for r in rows]
    _write_minute(tmp_path, DAY, rows)
    _write_daily(tmp_path, DAY, "920375.BJ", volume=240 * 1000,
                 amount=240 * 1000 * 100 * 10 * 0.99)
    r = minute_audit.audit_day(tmp_path, DAY)
    assert (r["daily_bad"], r["daily_bad_basis"]) == (1, 1)
    assert r["money_bad"] == 0            # 量额都对得上
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_GREEN


def test_daily_average_out_of_range_outside_bj_is_red(tmp_path):
    """同样的越界发生在 SH/SZ 就是异常 —— 没有先验，不许当口径放过。"""
    rows = _clean_day("000001.SZ")
    rows = [{**r, "amount": r["amount"] * 0.99} for r in rows]
    _write_minute(tmp_path, DAY, rows)
    _write_daily(tmp_path, DAY, "000001.SZ", volume=240 * 1000,
                 amount=240 * 1000 * 100 * 10 * 0.99)
    r = minute_audit.audit_day(tmp_path, DAY)
    assert (r["daily_bad"], r["daily_bad_basis"]) == (1, 0)
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_BLOCK


def test_quarantine_is_counted_but_does_not_move_verdict(tmp_path):
    """隔离行数进产物、不进退出码。

    隔离之后**盘上的数据是对的**（副本没落盘、量额不双计），这一项说的是「源还在
    产脏」。让它驱动退出码 = 每天恒红 = 报警静音（同 M 轴刻意不进 stage_errors）。
    但它必须**看得见**：产物里没有这一项，「隔离」就成了静默丢弃。
    """
    _write_minute(tmp_path, DAY, _clean_day())
    _write_daily(tmp_path, DAY, "000001.SZ", volume=240 * 1000, amount=240 * 1000 * 100 * 10)
    qdir = tmp_path / "_quarantine" / "kline_minute" / f"date={DAY}"
    qdir.mkdir(parents=True)
    (qdir / "20261113_201500_000000-postclose.json").write_text(
        json.dumps({"rows": 40, "origin": "postclose", "reason": "off_grid_duplicate"}),
        encoding="utf-8")
    (qdir / "half-written.json").write_text("{not json", encoding="utf-8")

    r = minute_audit.audit_day(tmp_path, DAY)
    assert r["quarantined"] == 40          # 半截文件不算数, 也不许抛
    assert minute_audit.verdict(minute_audit.summarize([r])) == minute_audit.RC_GREEN
    assert "已隔离副本 40" in minute_audit.format_day_line(r)


def test_quarantine_without_partition_is_not_read_as_no_data(tmp_path):
    """分区不在但隔离区有记录 = 那天的行全被判成副本。两项必须一起看。"""
    qdir = tmp_path / "_quarantine" / "kline_minute" / f"date={DAY}"
    qdir.mkdir(parents=True)
    (qdir / "x-live.json").write_text(json.dumps({"rows": 7}), encoding="utf-8")
    r = minute_audit.audit_day(tmp_path, DAY)
    assert r["minute_partition"] is False and r["quarantined"] == 7


def test_audit_is_read_only(tmp_path):
    """审计不许动盘：改数据是显式动作（repair_plan + --apply）。"""
    part = _write_minute(tmp_path, DAY, _clean_day())
    before = (part.stat().st_mtime_ns, hashlib.sha256(part.read_bytes()).hexdigest())
    minute_audit.audit_days(tmp_path, [DAY.isoformat()])
    after = (part.stat().st_mtime_ns, hashlib.sha256(part.read_bytes()).hexdigest())
    assert before == after
    assert sorted(p.name for p in part.parent.iterdir()) == ["part.parquet"]


def test_one_broken_day_does_not_swallow_the_batch(tmp_path):
    """分区里多一个文件 = 同一天被读两遍。那一天记 error，另一天照常出数。"""
    _write_minute(tmp_path, DAY, _clean_day(), name="a.parquet")
    _write_minute(tmp_path, DAY, _clean_day()[:1], name="b.parquet")  # 与 a 有重叠键
    other = date(2026, 11, 11)
    _write_minute(tmp_path, other, _clean_day())
    _write_daily(tmp_path, other, "000001.SZ", volume=240 * 1000, amount=240 * 1000 * 100 * 10)

    rep = minute_audit.audit_days(tmp_path, [DAY.isoformat(), other.isoformat()])
    assert "重复" in rep[0]["error"]
    assert rep[1]["minute_partition"] and rep[1]["rows"] == 240
    assert minute_audit.summarize(rep)["days_error"] == 1


# ---------------------------------------------------------------- 管道接线


def _run_pipeline(
    tmp_path: Path, monkeypatch, *, minute_result, sink: list[str] | None = None
) -> dict:
    """跑一遍盘后管道（复用 test_daily_pipeline_minute_gate 的搭法）。

    `sink` 记录分钟阶段之后的阶段是否仍被执行（结算阶段会往里 append）。
    """
    sink = [] if sink is None else sink
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False, "pipeline_pull_etf": False,
        "pipeline_regime_enabled": False, "minute_sync_enabled": True,
        "minute_sync_days": 5, "minute_data_provider": "tickflow",
        "adj_factor_provider": "tickflow",
    })
    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda *_: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *_: [])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *_: None)
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *_, **__: [])
    monkeypatch.setattr(
        daily_pipeline.kline_sync, "_resolve_minute_provider", lambda name: (None, True, None),
    )

    def fake_sync_minute(*_a, **_k):
        if isinstance(minute_result, Exception):
            raise minute_result
        return minute_result

    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_minute", fake_sync_minute)

    from app.strategy import paper

    def _list_accounts(_data_dir):
        sink.append("paper_settle")
        return []

    monkeypatch.setattr(paper, "list_account_ids", _list_accounts)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date(2026, 9, 28),
    )
    # 分钟阶段必须真的被调用：没有 KLINE_MINUTE_BATCH 时 `minute_allowed` 为假，
    # 整段静默跳过 —— 那样「闸门拒绝」这条路径根本不会被执行。
    return daily_pipeline.run_now(
        repo, CapabilitySet({Cap.KLINE_MINUTE_BATCH: None}),
    )


def test_gate_rejection_does_not_skip_later_stages(tmp_path, monkeypatch):
    """落盘闸门拒绝一天 ⇒ 终态仍 failed，但结算/台账/复盘照常执行。

    拒绝本身不能把「后面所有阶段」一起带走：那等于一天脏数据让整条管道空转。
    """
    sink: list[str] = []
    boom = ValueError("分钟越界时刻上的副本 (date=2026-11-12, 1 行 / 本次输入 240 行)")
    with pytest.raises(daily_pipeline.PipelineStageError) as ei:
        _run_pipeline(tmp_path, monkeypatch, minute_result=boom, sink=sink)
    assert "sync_minute" in str(ei.value)
    assert "paper_settle" in sink, "分钟被拒后，结算阶段仍必须跑到"


def test_pipeline_audits_today_and_writes_artifact(tmp_path, monkeypatch):
    today = cn_today()
    _write_minute(tmp_path, today, _clean_day())
    _write_daily(tmp_path, today, "000001.SZ", volume=240 * 1000, amount=240 * 1000 * 100 * 10)

    result = _run_pipeline(tmp_path, monkeypatch, minute_result=1)
    summary = result["minute_audit"]
    assert summary["day"] == today.isoformat()
    assert summary["verdict"] == minute_audit.RC_GREEN
    assert summary["totals"]["days"] == 1
    art = tmp_path / "_audit" / "minute" / f"date={today.isoformat()}.json"
    assert art.exists(), "结论必须落盘，容器日志会被轮转掉"
    assert '"verdict"' in art.read_text(encoding="utf-8")


def test_pipeline_warns_when_rows_were_quarantined(tmp_path, monkeypatch, caplog):
    """隔离行不进退出码，但每天必须留一条 WARNING —— 否则隔离就是静默丢弃。"""
    today = cn_today()
    _write_minute(tmp_path, today, _clean_day())
    _write_daily(tmp_path, today, "000001.SZ", volume=240 * 1000, amount=240 * 1000 * 100 * 10)
    qdir = tmp_path / "_quarantine" / "kline_minute" / f"date={today}"
    qdir.mkdir(parents=True)
    (qdir / "x-postclose.json").write_text(json.dumps({"rows": 4}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.jobs.daily_pipeline"):
        result = _run_pipeline(tmp_path, monkeypatch, minute_result=1)

    assert result["minute_audit"]["verdict"] == minute_audit.RC_GREEN, "数据是对的, 不该红"
    assert "被隔离" in caplog.text and "4 行" in caplog.text


def test_pipeline_audit_failure_is_soft(tmp_path, monkeypatch):
    """审计自己读不动时只记 error，不许把管道变成 failed。"""
    today = cn_today()
    _write_minute(tmp_path, today, _clean_day(), name="a.parquet")
    _write_minute(tmp_path, today, _clean_day()[:1], name="b.parquet")

    result = _run_pipeline(tmp_path, monkeypatch, minute_result=1)
    assert result["stage_errors"] == []
    assert result["minute_audit"]["totals"]["days_error"] == 1
