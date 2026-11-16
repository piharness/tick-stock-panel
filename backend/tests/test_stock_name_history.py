"""简称轨迹 —— 只允许输出「可查的两个日期之间」，不允许编造改名日。

实测动机（600340.SH）：维表当前简称是 `*ST华幸`，而龙虎榜快照里
2025-11-13 还写着 `华夏幸福`。两年日 K 上看不出改名，页面若只显示今天的
名字，等于把这件事删掉了。本地唯一带历史简称的表就是这 243 份快照。

本文件守三条：
  1. 稀疏观测只能推出区间（`after` < 改名 ≤ `before`），不能推出某一天；
  2. 同名连续观测不能重复成多段（`bands` 是真区间，不是观测列表的复读）；
  3. 没有快照时返回空，而不是回退到维表当前简称冒充历史。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.services import stock_name_history


def _snapshot(root: Path, day: str, rows: list[dict]) -> None:
    base = root / "dragon_tiger"
    base.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "ok",
        "trade_date": day,
        "all": {"trade_date": day, "stock_items": rows},
        "org": {"trade_date": day, "stock_items": []},
        "hot_money": {"trade_date": day, "hot_money_items": []},
    }
    (base / f"date={day}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _row(sym: str, name: str) -> dict:
    return {"thscode": sym, "ticker": sym[:6], "name": name}


def test_rename_is_a_bracket_not_a_day(tmp_path: Path) -> None:
    _snapshot(tmp_path, "2025-11-13", [_row("600340.SH", "华夏幸福")])
    _snapshot(tmp_path, "2025-12-02", [_row("600340.SH", "华夏幸福")])
    _snapshot(tmp_path, "2026-01-14", [_row("600340.SH", "*ST华幸")])
    _snapshot(tmp_path, "2026-09-29", [_row("600340.SH", "*ST华幸")])

    r = stock_name_history.name_history(tmp_path, "600340.SH", current_name="*ST华幸")

    assert r["renamed"] is True
    assert r["snapshots"] == 4
    assert [b["name"] for b in r["bands"]] == ["华夏幸福", "*ST华幸"]
    # 连续同名归并：不是 4 段
    assert [b["n"] for b in r["bands"]] == [2, 2]

    change = r["changes"][0]
    assert change["from"] == "华夏幸福" and change["to"] == "*ST华幸"
    # 改名发生在 (2025-12-02, 2026-01-14] 之间 —— 不是 2026-01-01
    assert change["after"] == "2025-12-02"
    assert change["before"] == "2026-01-14"
    assert change["gap_days"] == (
        date.fromisoformat("2026-01-14") - date.fromisoformat("2025-12-02")
    ).days


def test_single_name_is_not_a_rename(tmp_path: Path) -> None:
    _snapshot(tmp_path, "2026-03-02", [_row("000001.SZ", "平安银行")])
    _snapshot(tmp_path, "2026-03-03", [_row("000001.SZ", "平安银行")])

    r = stock_name_history.name_history(tmp_path, "000001.SZ")
    assert r["renamed"] is False
    assert r["changes"] == []
    assert len(r["bands"]) == 1


def test_no_snapshot_returns_empty_not_current_name(tmp_path: Path) -> None:
    """没有证据就是空：不能拿维表当前简称当历史观测填进去。"""
    r = stock_name_history.name_history(tmp_path, "600340.SH", current_name="*ST华幸")
    assert r["observations"] == []
    assert r["bands"] == []
    assert r["renamed"] is False
    assert r["snapshots"] == 0
    # 当前简称单独一个字段，来源不同，不混进 observations
    assert r["current"] == "*ST华幸"


def test_hot_money_rows_also_count_as_observation(tmp_path: Path) -> None:
    """游资席位的 rows 里也会带股票简称，漏读会让改名区间变宽。"""
    base = tmp_path / "dragon_tiger"
    base.mkdir(parents=True)
    payload = {
        "state": "ok",
        "all": {"stock_items": []},
        "org": {"stock_items": []},
        "hot_money": {"hot_money_items": [{"seat": "某游资", "rows": [_row("600340.SH", "华幸ST")]}]},
    }
    (base / "date=2026-10-09.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = stock_name_history.name_history(tmp_path, "600340.SH")
    assert [o["name"] for o in r["observations"]] == ["华幸ST"]


def test_index_cache_invalidates_on_new_snapshot(tmp_path: Path) -> None:
    """新增一天快照必须被看见：缓存签名里带文件数。"""
    _snapshot(tmp_path, "2026-05-06", [_row("600519.SH", "贵州茅台")])
    first = stock_name_history.name_history(tmp_path, "600519.SH")
    assert first["snapshots"] == 1

    _snapshot(tmp_path, "2026-05-07", [_row("600519.SH", "*ST茅台")])
    second = stock_name_history.name_history(tmp_path, "600519.SH")
    assert second["snapshots"] == 2
    assert second["renamed"] is True
