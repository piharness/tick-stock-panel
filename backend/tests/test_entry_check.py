"""买点判据的四条硬约束。

这个模块的失效方式很安静：档位算错、或者把缺失值当成"通过"，页面只会显示一个
更乐观的标签，不会报错。所以下面每条都对应一个具体的错误形态：

  1. 升/降档的边界（bull>=3 / bear>=2）与预注册一致，改阈值必须换版本号；
  2. **缺失值不算通过**：户数只在季报后更新，新股可能没有历史 —— 把 None 当
     "低换手成立"会把信息最少的票推进低风险档；
  3. journal 幂等：同一 (symbol, as_of, criteria) 只写一条，否则"已记录 N 次"
     会随刷新次数增长，看起来像在积累样本，其实只有一条；
  4. 未到期不结算：20 个交易日没走完就写 fwd20，等于用未来数据污染统计。
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from app.services import entry_check


def _state(*, to=None, vol=None, rev=None, hold=None) -> dict:
    return {"turnover_pct": to, "vol_pct": vol, "rev_pct": rev, "holder_pct": hold}


# ---------- 1：档位边界 ----------

def test_low_tier_needs_three_bull_hits():
    v = entry_check.judge(_state(to=0.10, vol=0.10, rev=0.50, hold=0.10))
    assert v["bull_hits"] == 4 and v["tier"] == "low"

    v = entry_check.judge(_state(to=0.10, vol=0.10, rev=0.50, hold=0.90))
    assert v["bull_hits"] == 3 and v["tier"] == "low", "3 项就是低风险，不能要求 4 项"

    v = entry_check.judge(_state(to=0.10, vol=0.10, rev=0.90, hold=0.90))
    assert v["bull_hits"] == 2 and v["tier"] == "mid", "2 项应为中性"


def test_high_tier_overrides_low():
    """空头 2 项必须强制降档，即使多头也成立了 3 项。

    现实中这个组合真的存在：低换手 + 低波动（多头 2 项成立）同时 20 日暴涨
    （空头 1 项）—— 再加一个空头项就是典型的"缩量急拉"，历史胜率只有 40.8%。
    """
    v = entry_check.judge(_state(to=0.10, vol=0.10, rev=0.95, hold=0.10))
    assert v["bull_hits"] == 3
    assert v["bear_hits"] == 1
    assert v["tier"] == "low", "只有 1 项风险提示时不降档"

    v = entry_check.judge(_state(to=0.10, vol=0.90, rev=0.95, hold=0.10))
    assert v["bear_hits"] == 2 and v["tier"] == "high", "2 项空头必须降为高危"


def test_missing_value_is_not_a_hit():
    """None 不能算成立，也不能算"已检查"。"""
    v = entry_check.judge(_state(to=None, vol=None, rev=None, hold=None))
    assert v["bull_hits"] == 0
    assert v["bear_hits"] == 0
    assert v["missing"] == ["C1", "C2", "C3", "C4"]
    assert all(c["hit"] is False for c in v["checks"])

    # 户数缺失但其余三项成立 → 仍是低风险（3 项够了），但必须报告缺失
    v = entry_check.judge(_state(to=0.10, vol=0.10, rev=0.50, hold=None))
    assert v["tier"] == "low"
    assert v["missing"] == ["C4"]


def test_criteria_version_is_pinned():
    v = entry_check.judge(_state(to=0.5, vol=0.5, rev=0.5, hold=0.5))
    assert v["criteria"] == "entry-v1", (
        "改阈值必须同时改版本号，否则新旧记录会被合并统计，"
        "而它们的本底概率不同"
    )


# ---------- 2：journal 幂等 ----------

def test_record_is_idempotent(tmp_path: Path):
    verdict = entry_check.judge(_state(to=0.1, vol=0.1, rev=0.5, hold=0.1))
    state = _state(to=0.1, vol=0.1, rev=0.5, hold=0.1)
    assert entry_check.record(tmp_path, "000001.SZ", "2026-11-17", verdict, state) is True
    assert entry_check.record(tmp_path, "000001.SZ", "2026-11-17", verdict, state) is False

    recs = entry_check.load_journal(tmp_path)
    assert len(recs) == 1
    # 换一天 / 换一只票都是新记录
    assert entry_check.record(tmp_path, "000001.SZ", "2026-11-18", verdict, state) is True
    assert entry_check.record(tmp_path, "600519.SH", "2026-11-17", verdict, state) is True
    assert len(entry_check.load_journal(tmp_path)) == 3


def test_journal_survives_truncated_line(tmp_path: Path):
    """进程被杀会留下半行。半行不能让整份账本作废。"""
    p = tmp_path / "entry_check" / "journal.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps({"symbol": "A", "as_of": "2026-01-01", "criteria": "entry-v1",
                    "tier": "low", "fwd20": 0.01}) + "\n"
        + '{"symbol": "B", "as_of": "2026-0',  # 半行
        encoding="utf-8",
    )
    recs = entry_check.load_journal(tmp_path)
    assert len(recs) == 1 and recs[0]["symbol"] == "A"


# ---------- 3：未到期不结算 ----------

class _FakeRepo:
    def __init__(self, n_days: int):
        self.n_days = n_days

    def get_daily(self, symbol, start, end, columns=None):
        import datetime as dt
        d0 = dt.date(2026, 1, 5)
        return pl.DataFrame({
            "date": [d0 + dt.timedelta(days=i) for i in range(self.n_days)],
            "close": [10.0 + i * 0.1 for i in range(self.n_days)],
        })


def test_settle_skips_until_horizon_complete():
    rec = {"symbol": "X", "as_of": "2026-01-05", "criteria": "entry-v1",
           "tier": "low", "fwd20": None}
    # 只有 20 根 → 不够（需要第 0 根 + 第 20 根）
    assert entry_check._settle_one(_FakeRepo(20), rec) is None
    out = entry_check._settle_one(_FakeRepo(21), rec)
    assert out is not None
    assert out["fwd20"] == pytest.approx(12.0 / 10.0 - 1, abs=1e-6)


# ---------- 4：未校准时不编数字 ----------

def test_uncalibrated_returns_none_not_fake_numbers(tmp_path: Path):
    assert entry_check.load_stats(tmp_path) is None
    # stats.json 损坏也不能让页面拿到半份数据
    p = tmp_path / "entry_check" / "stats.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert entry_check.load_stats(tmp_path) is None
