"""depth_service 盘中触板轨迹缓冲(depth5_touch)— 研究旁路线的数据契约。

depth5 定版快照只含收盘仍在板上的股票, 炸板股不会出现; 打板成交率研究
(analysis/_diag_board_shape_findings.md §8.1)需要「首次触板时间 / 封单量路径 /
是否回封」, 由每轮轮询追加的当日轨迹缓冲在定版时落盘。这里验证:
  1. 每轮追加全部拉到的股票(封住与将炸都在缓冲里), 行结构与文档一致;
  2. 交易日切换重置缓冲(不跨日串味);
  3. 定版原子写 depth5 + depth5_touch 两个 parquet。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.depth_service import DepthService

TOUCH_COLS = ["fetched_at", "symbol", "sealed_up", "sealed_down",
              "ask1_vol", "bid1_vol", "status"]


def _depth_entry(ask1, bid1, ts=1_700_000_000_000):
    return {"ask_volumes": [ask1], "bid_volumes": [bid1], "timestamp": ts}


@pytest.fixture
def svc(tmp_path: Path) -> DepthService:
    s = DepthService()
    s.set_repo(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    return s


def _seal(svc: DepthService, sym, ask1, bid1, d: date, persist: bool = False):
    svc._seal_locked({sym: _depth_entry(ask1, bid1)}, [sym], [], d, persist)


def test_rounds_append_and_day_reset(svc: DepthService):
    d1 = date(2026, 11, 23)
    _seal(svc, "000001.SZ", 0, 12345, d1)    # 涨停真封: 卖一 0
    _seal(svc, "000001.SZ", 0, 12345, d1)    # 第二轮仍在板上
    _seal(svc, "000002.SZ", 500, 0, d1)      # 卖一有量: 将炸/未封死
    assert len(svc._touch_rows) == 3
    assert svc._touch_date == d1
    # 行结构: (fetched_at, symbol, sealed_up, sealed_down, ask1_vol, bid1_vol, status)
    rows = [r for r in svc._touch_rows if r[1] == "000001.SZ"]
    assert len(rows) == 2 and all(r[2] is True for r in rows) and rows[0][5] == 12345
    will_break = [r for r in svc._touch_rows if r[1] == "000002.SZ"]
    assert will_break[0][2] is False and will_break[0][4] == 500
    # 交易日切换: 缓冲重置, 不跨日串味
    d2 = date(2026, 11, 24)
    _seal(svc, "000003.SZ", 0, 99, d2)
    assert len(svc._touch_rows) == 1 and svc._touch_rows[0][1] == "000003.SZ"


def test_finalize_persists_depth5_and_touch_log(
    svc: DepthService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # 用 patch 把测试环境置于「交易时段」: 旗标由 _seal_locked 的真实路径赋值
    monkeypatch.setattr(DepthService, "_is_trading_hours", staticmethod(lambda: True))
    d = date(2026, 11, 23)
    _seal(svc, "000001.SZ", 0, 12345, d, persist=True)
    depth5 = tmp_path / "depth5" / f"date={d.isoformat()}" / "part.parquet"
    touch = tmp_path / "depth5_touch" / f"date={d.isoformat()}" / "part.parquet"
    assert depth5.exists() and touch.exists()
    tdf = pl.read_parquet(touch)
    assert tdf.columns == TOUCH_COLS
    assert tdf.height == 1 and tdf["sealed_up"][0] is True and tdf["bid1_vol"][0] == 12345
    # 轨迹按轮累积: 第二轮追加后再定版, 原子覆盖为 2 行
    _seal(svc, "000001.SZ", 0, 12345, d, persist=True)
    assert pl.read_parquet(touch).height == 2


def test_boot_finalize_without_trading_round_writes_no_touch_log(
    svc: DepthService, tmp_path: Path
):
    # 周末/盘后 boot_check 补跑定版: depth5 照常落盘, 触板轨迹必须不落盘
    # (否则补跑时刻会污染「首次触板时间」)
    d = date(2026, 11, 22)
    assert svc._touch_saw_trading_round is False
    _seal(svc, "000001.SZ", 0, 12345, d, persist=True)
    assert (tmp_path / "depth5" / f"date={d.isoformat()}" / "part.parquet").exists()
    assert not (tmp_path / "depth5_touch" / f"date={d.isoformat()}").exists()


def test_empty_buffer_writes_nothing(svc: DepthService, tmp_path: Path):
    # 定版前没有任何轮询(如启动补跑且无涨跌停) → 不产生空 depth5_touch 文件
    d = date(2026, 11, 23)
    svc._touch_saw_trading_round = True
    svc._persist_touch_log(d)
    assert not (tmp_path / "depth5_touch" / f"date={d.isoformat()}").exists()
