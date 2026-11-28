"""叙事账本（narrative_ledger）的纯函数单测 —— 只测口径，不碰真实数据。

三件事必须成立，否则页面读数是错的：
  1. 上榜理由的分词把制度标签与题材标签分开（`融资融券`/`沪股通` 不进词频）；
  2. 分位函数对空列返回 None，不返回 0（0 会被页面读成「历史最低」）；
  3. `build` 在任何一块抛异常时仍返回其余块 —— 缺数要说缺，不能整页白屏。

真实数据的装配（读分区、拼指数、算概念涨幅）由分析脚本
`analysis/_diag_narrative_heat.py` 与容器内实测覆盖，不在单测里造 5,000 行假数据。
"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest

from app.services import narrative_ledger as nl


def test_split_reason_拆开题材词与制度词():
    toks = nl._split_reason('机器人投资+厨房小家电+“羊”字辈')
    assert toks == ['机器人投资', '厨房小家电', '羊字辈']


def test_split_reason_去掉空段与括号():
    assert nl._split_reason('算力（租赁）、存储;高阶HDI') == ['算力', '租赁', '存储', '高阶HDI']


def test_split_reason_空串返回空列表():
    assert nl._split_reason('') == []


def test_pct_rank_空列返回_None():
    assert nl._pct_rank(pl.Series('x', [], dtype=pl.Float64), 1.0) is None


def test_pct_rank_全空列返回_None():
    assert nl._pct_rank(pl.Series('x', [None, None], dtype=pl.Float64), 1.0) is None


def test_pct_rank_极值():
    s = pl.Series('x', [1.0, 2.0, 3.0, 4.0])
    assert nl._pct_rank(s, 4.0) == 100.0
    assert nl._pct_rank(s, 0.5) == 0.0
    assert nl._pct_rank(s, 2.5) == 50.0


def test_build_单块失败不影响其余块(monkeypatch, tmp_path):
    """把四块里三块打瘸，`build` 仍要返回结构完整、失败块 ok=False 的 payload。"""
    monkeypatch.setattr(nl, "_temp", lambda _d: {"ok": True, "as_of": "2026-11-27"})
    monkeypatch.setattr(nl, "_mainline", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(nl, "_flows", lambda _d: {"ok": True, "as_of": "2026-11-25"})
    monkeypatch.setattr(nl, "_index", lambda _d: {"ok": False, "error": 'no data'})
    monkeypatch.setattr(nl, "_words", lambda *_a, **_k: {"ok": True, "as_of": None, "rows": []})
    monkeypatch.setattr(nl, "_events", lambda _d: {"ok": True, "as_of": "2026-11-27", "next": {"n": 0}})

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=str(tmp_path)))
    out = nl.build(repo)

    assert out['as_of'] == '2026-11-27'
    assert out['blocks']['temp']['ok'] is True
    assert out['blocks']['mainline']['ok'] is False
    assert 'boom' in out['blocks']['mainline']['error']
    assert out['blocks']['index']['ok'] is False
    assert out['blocks']['words']['ok'] is True
    assert out['blocks']['events']['ok'] is True
    assert 'disclaimer' in out
