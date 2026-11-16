"""落盘闸门: 隔离「越界时刻上的副本」。

## 判据只拦会双计的那一支

2026-11-05 的 40 行、2026-11-12 的 32 行 13:00, 与同 symbol 当日 11:30 的 volume
**逐只相等** (40/40、32/32), OHLCV 五个字段全等, amount 恰好是它的「万元两位小数」
重编码 (差 ≤49 元 = 0.01 万元量化) —— 同一根 bar 的第二次写入。这种行存下来就是
全天量额双计。

## 处置: 逐行隔离, 不是整帧拒绝

`on_segment` 一次交给落盘的是「20 交易日 × 100 只」的帧。拒绝的粒度若停在帧上,
一条幽灵行的代价是 48 万行不落盘, 而且抛错会顺着 chunk 循环把后面所有 chunk 一起
带走 (2026-11-13 实测: 同一次调用里 day1 有幽灵, 干净 day2 也没写)。所以:
**丢掉那一行、留一份证据、其余照写**。隔离写不成时反而要拒绝 —— 没有记录就不许丢。

## 不拦的两支, 以及为什么

* **单根 09:30** (区间首日标注的开盘部分分钟): 拦掉 = 每天开盘丢一根、之后永远
  缺 15:00, 是闸门自己制造的故障。口径问题归审计的 L 轴报警。
* **13:00 顶替 11:30 的停牌合成格** (11-12 那 7 只): 正确动作是**改标**, 拦掉就
  再没有证据知道该改标 (改标在 `plugins/tdxquant/provider.py` 的边界上做)。

这两支都有用例钉住 —— 它们红, 说明闸门被写宽成了「越界就拒」。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from app.market_time import minute_grid
from app.services.kline_sync import _write_minute_partition


def _bar(sym: str, stamp: str, px: float, vol: float = 100.0) -> dict:
    return {
        "symbol": sym, "datetime": datetime.fromisoformat(stamp),
        "open": px, "high": px, "low": px, "close": px,
        "volume": vol, "amount": px * vol * 100,
    }


def _frame(*rows: dict) -> pl.DataFrame:
    return pl.DataFrame(list(rows))


def _day(sym: str = "000001.SZ", day: str = "2026-01-05") -> list[dict]:
    """240 根规范网格。"""
    return [_bar(sym, f"{day} {t.strftime('%H:%M')}:00", 10.0) for t in minute_grid()]


def _quarantine_files(data_dir: Path, day: str = "2026-01-05") -> list[Path]:
    root = data_dir / "_quarantine" / "kline_minute" / f"date={day}"
    return sorted(root.glob("*.json")) if root.exists() else []


def test_grid_is_240_and_end_labeled():
    grid = minute_grid()
    assert len(grid) == 240
    assert grid[0].strftime("%H:%M") == "09:31"
    assert grid[119].strftime("%H:%M") == "11:30"
    assert grid[120].strftime("%H:%M") == "13:01"
    assert grid[-1].strftime("%H:%M") == "15:00"


def test_phantom_1300_is_quarantined_not_stored(tmp_path, monkeypatch):
    """负控: 2026-11-05 的原样复现 —— 11:30 那根被贴了第二个标签 13:00。

    期望: 不落盘、进隔离区、分区仍是上一版。
    闸门若被删掉, 这里会变成 3 根 (全天量额双计) —— 用例测的是闸门本身。
    """
    from app.services import kline_sync

    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "tdxquant")
    minute_dir = tmp_path / "kline_minute"
    out = minute_dir / "date=2026-01-05" / "part.parquet"
    _write_minute_partition(
        _frame(_bar("000001.SZ", "2026-01-05 09:31:00", 10.0),
               _bar("000001.SZ", "2026-01-05 11:30:00", 10.2)), minute_dir)

    written = _write_minute_partition(
        _frame(_bar("000001.SZ", "2026-01-05 13:00:00", 10.2)), minute_dir,
        origin="postclose")

    stored = pl.read_parquet(out)
    # 整帧只有那一根副本 ⇒ 0 行落盘; 但分区文件不被重写、也不留 .tmp 残骸
    assert written == 0
    assert stored.height == 2
    assert "13:00" not in {t.strftime("%H:%M") for t in stored["datetime"].dt.time()}
    assert sorted(p.name for p in out.parent.iterdir()) == ["part.parquet"]

    files = _quarantine_files(tmp_path)
    assert len(files) == 1 and files[0].name.endswith("-postclose.json")
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["rows"] == 1 and rec["origin"] == "postclose"
    # 「谁写的」的答案是源, 不是入口: 两个 TDX 源都走这张表, 只记 origin 分不开
    assert rec["provider"] == "tdxquant"
    assert rec["symbols"] == ["000001.SZ"] and rec["times"] == ["13:00:00"]
    assert rec["n_input"] == 1 and rec["reason"] == "off_grid_duplicate"
    assert rec["minute"][0]["datetime"].startswith("2026-01-05 13:00")


def test_one_bad_day_does_not_take_the_next_day(tmp_path):
    """同一次调用里的另一天必须落盘。

    修复前 `_assert_no_off_grid_duplicate` 在 `partition_by` 循环里抛错: 这一天没写,
    后面的天也一个都没写 (实测 day2 的 part.parquet 根本不存在)。段末帧是
    「20 交易日 × 100 只」, 一条幽灵行赔掉 48 万行。
    """
    minute_dir = tmp_path / "kline_minute"
    rows = [
        _bar("000001.SZ", "2026-01-05 09:31:00", 10.0),
        _bar("000001.SZ", "2026-01-05 11:30:00", 10.2),
        _bar("000001.SZ", "2026-01-05 13:00:00", 10.2),   # 副本
        _bar("000001.SZ", "2026-01-06 09:31:00", 10.0),
        _bar("000001.SZ", "2026-01-06 11:30:00", 10.2),
    ]
    written = _write_minute_partition(_frame(*rows), minute_dir, origin="postclose")

    assert written == 4, "5 行输入 - 1 行副本"
    d1 = pl.read_parquet(minute_dir / "date=2026-01-05" / "part.parquet")
    d2 = pl.read_parquet(minute_dir / "date=2026-01-06" / "part.parquet")
    assert d1.height == 2 and d2.height == 2


def test_one_bad_symbol_does_not_take_the_other_symbols(tmp_path):
    """隔离的粒度是行, 不是帧: 同帧里另一只标的 240 根一根不少。"""
    minute_dir = tmp_path / "kline_minute"
    rows = _day("000001.SZ") + _day("000002.SZ") + [
        _bar("000002.SZ", "2026-01-05 13:00:00", 10.0),
    ]
    written = _write_minute_partition(_frame(*rows), minute_dir, origin="refresh")

    assert written == 480
    stored = pl.read_parquet(minute_dir / "date=2026-01-05" / "part.parquet")
    per = stored.group_by("symbol").agg(pl.len().alias("bars")).sort("symbol")
    assert per.to_dicts() == [{"symbol": "000001.SZ", "bars": 240},
                              {"symbol": "000002.SZ", "bars": 240}]


def test_preexisting_ghost_does_not_lock_the_day(tmp_path):
    """分区里已有历史脏行时, 后续写入必须还能成功, 且不删旧行。

    修复前闸门查的是「合并后」的帧: 旧脏行会让这一天**永久**拒绝写入 ——
    只要源不回头修, 这一天每天都撞同一堵墙, 拒绝的还是一整帧好数据。
    写路径无从判断该删哪一根 (11-12 那 7 只要改标), 所以只报不删: 归审计的修法管。
    """
    minute_dir = tmp_path / "kline_minute"
    out = minute_dir / "date=2026-01-05" / "part.parquet"
    out.parent.mkdir(parents=True)
    pl.DataFrame([                                   # 绕过写路径直接造脏
        _bar("000001.SZ", "2026-01-05 11:30:00", 10.2),
        _bar("000001.SZ", "2026-01-05 13:00:00", 10.2),
    ]).write_parquet(out)

    written = _write_minute_partition(
        _frame(_bar("000001.SZ", "2026-01-05 09:31:00", 10.0)), minute_dir,
    )

    stored = pl.read_parquet(out)
    assert written == 1
    assert stored.height == 3, "新行照写; 旧脏行保留 (它是改标的唯一证据)"
    assert not _quarantine_files(tmp_path), "本次输入干净 ⇒ 不该有隔离记录"


def test_quarantine_write_failure_refuses_to_write(tmp_path, monkeypatch):
    """隔离记录写不成 ⇒ 拒绝落盘: 宁可这一天没更新, 也不许「丢了又没记录」。"""
    from app.services import fs_utils

    def _disk_full(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(fs_utils, "atomic_write_text", _disk_full)
    minute_dir = tmp_path / "kline_minute"
    with pytest.raises(ValueError, match="隔离失败"):
        _write_minute_partition(
            _frame(_bar("000001.SZ", "2026-01-05 09:31:00", 10.0),
                   _bar("000001.SZ", "2026-01-05 11:30:00", 10.2),
                   _bar("000001.SZ", "2026-01-05 13:00:00", 10.2)), minute_dir)
    assert not (minute_dir / "date=2026-01-05").exists(), "拒绝就是一行都不写"


def test_postclose_origin_is_threaded_from_the_call_site(monkeypatch, tmp_path):
    """`sync_and_persist_minute` 的落盘必须带 origin=postclose。

    这是「谁写的」这条链路上最容易断的一环: 测试里自造一个 `origin="postclose"`
    也能全绿, 但真实入口忘了传参就没红。四十分钟的 13:00 排查只差这一条。
    """
    from types import SimpleNamespace

    from app.services import kline_sync
    from app.tickflow.capabilities import Cap, CapabilitySet

    clean = pl.DataFrame(_day())
    ghost = pl.DataFrame([_bar("000001.SZ", "2026-01-05 13:00:00", 10.0)])

    def _fake_sync(_symbols, **kw):  # 段末回调: 240 行干净 + 1 根幽灵
        kw["on_segment"](pl.concat([clean, ghost]))
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_minute_batch", _fake_sync)
    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda _n: (None, True, None))
    monkeypatch.setattr(kline_sync, "_cleanup_null_datetime_minute", lambda _r: None)
    monkeypatch.setattr(kline_sync, "_migrate_symbol_to_date_partition", lambda _r: None)
    monkeypatch.setattr(kline_sync, "_latest_minute_datetime", lambda _r: None)
    monkeypatch.setattr(kline_sync.minute_adjust, "minute_basis_is_raw", lambda _d: False)
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)
    monkeypatch.setattr(kline_sync, "resolve_limit",
                        lambda *_a, **_k: SimpleNamespace(batch=100, rpm=30))
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        _write_lock=__import__("threading").Lock(),
        refresh_view=lambda *_a: None,
    )

    written = kline_sync.sync_and_persist_minute(
        ["000001.SZ"], repo, CapabilitySet({Cap.KLINE_MINUTE_BATCH: None}))

    assert written == 240, "240 行干净数据照常落盘"
    files = _quarantine_files(tmp_path)
    assert len(files) == 1 and files[0].name.endswith("-postclose.json")


def test_clean_input_writes_no_quarantine(tmp_path):
    """负控的反面: 干净输入不许凭空产生隔离目录 (否则「有记录」就不再是信号)。"""
    minute_dir = tmp_path / "kline_minute"
    written = _write_minute_partition(_frame(*_day()), minute_dir, origin="live")
    assert written == 240
    assert not (tmp_path / "_quarantine").exists()


def test_lone_off_grid_bar_is_allowed(tmp_path):
    """单根 09:30 (开盘部分分钟) 必须放行 —— 拦它就是每天开盘丢一根。"""
    minute_dir = tmp_path / "kline_minute"
    written = _write_minute_partition(
        _frame(_bar("000001.SZ", "2026-01-05 09:30:00", 10.0),
               _bar("000001.SZ", "2026-01-05 09:31:00", 10.1)), minute_dir)
    assert written == 2
    assert not _quarantine_files(tmp_path)


def test_zero_volume_off_grid_is_allowed(tmp_path):
    """停牌合成格 (13:00 顶替 11:30, 0 量) 放行 —— 它要的是改标, 不是丢弃。"""
    minute_dir = tmp_path / "kline_minute"
    written = _write_minute_partition(
        _frame(_bar("000001.SZ", "2026-01-05 13:00:00", 0.0, vol=0.0)), minute_dir)
    assert written == 1
    assert not _quarantine_files(tmp_path)


def test_same_price_bars_of_an_illiquid_stock_are_not_flagged(tmp_path):
    """同价同量不是脏: 2026-11-11 实测 14,018 组 (小票冻结价), 全在网格上。"""
    minute_dir = tmp_path / "kline_minute"
    written = _write_minute_partition(
        _frame(*[_bar("000001.SZ", f"2026-01-05 10:{m:02d}:00", 10.0, vol=1.0)
                 for m in range(10)]), minute_dir)
    assert written == 10
    assert not _quarantine_files(tmp_path)


def test_analysis_audit_shares_the_backend_grid():
    """审计不许自建第二份网格 (同一映射写两遍只覆盖一份的分支)。

    绑定而非比较: 判据本体在 `app.services.minute_audit`, 脚本只许**转发** ——
    `is` 判身份, 所以「复制一份等值 tuple」也会红。这一条红了, 说明有人在
    `analysis/` 里又把网格或判据抄了一份。
    """
    script = Path(__file__).resolve().parents[2] / "analysis" / "_diag_minute_audit_1113.py"
    if not script.exists():  # analysis/ 不随后端镜像发布
        pytest.skip("analysis/ 不在本检出中")
    spec = importlib.util.spec_from_file_location("_minute_audit_cli_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from app.services import minute_audit

    assert mod.GRID is minute_audit.GRID
    assert tuple(mod.GRID) == minute_grid()
    assert not hasattr(mod, "canonical_grid"), "脚本里不许再有第二份网格构造函数"
