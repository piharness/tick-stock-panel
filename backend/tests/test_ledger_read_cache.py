"""回归测试: 决策账本读取的签名缓存。

回归动机（2026-11-24）：`/api/decision/overview` 首屏把同一批文件读好几遍 ——
`load_cards` 被它自己与 `ledger_health` 各调一次，settlements 被
`outstanding` / `load_settlements` / `ledger_health` 各扫一次。容器内实测
**42.7 + 49.3 + 57.1ms**，其中绝大部分是同一份 283KB 文件的重复解析。

判据（四条，缺一条就不许开缓存）：
  1. 签名不变 ⇒ 不重读盘（否则缓存等于装饰）；
  2. 磁盘变化 ⇒ **立即可见**（Cards 是新文件/改文件，settlements 是追加）；
  3. 损坏账本**不许缓存异常**：每次都要重新读、重新抛；
  4. 返回的 list 是副本 —— 调用方 sort/切片不许改坏缓存（行 dict 是共享只读）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.decision import ledger


def _card(as_of: str, card_id: str = "dc-1") -> dict:
    return {
        "kind": ledger.CARD_KIND,
        "card_id": card_id,
        "as_of": as_of,
        "created_at": f"{as_of}T09:00:00+08:00",
        "picks": [],
    }


def _write_card(data: Path, card: dict) -> Path:
    d = ledger.cards_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{card['card_id']}.json"
    p.write_text(json.dumps(card), encoding="utf-8")
    return p


def _settlement_row(key: str) -> dict:
    return {"kind": ledger.SETTLEMENT_KIND, "key": key, "outcome": 0.01}


@pytest.fixture(autouse=True)
def _clear():
    ledger.invalidate_ledger_cache()
    yield
    ledger.invalidate_ledger_cache()


def test_cards_are_not_reparsed_when_signature_unchanged(tmp_path: Path, monkeypatch):
    _write_card(tmp_path, _card("2026-11-20"))
    assert len(ledger.load_cards(tmp_path)) == 1

    calls = {"n": 0}
    real = json.loads

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(ledger.json, "loads", counted)
    ledger.load_cards(tmp_path)
    ledger.load_cards(tmp_path)

    assert calls["n"] == 0, "签名未变时不得重复解析卡片"


def test_new_card_is_visible_immediately(tmp_path: Path):
    _write_card(tmp_path, _card("2026-11-20", "dc-1"))
    assert len(ledger.load_cards(tmp_path)) == 1

    _write_card(tmp_path, _card("2026-11-21", "dc-2"))
    cards = ledger.load_cards(tmp_path)

    assert [c["card_id"] for c in cards] == ["dc-1", "dc-2"]


def test_modified_card_is_visible_immediately(tmp_path: Path):
    p = _write_card(tmp_path, _card("2026-11-20", "dc-1"))
    ledger.load_cards(tmp_path)

    card = _card("2026-11-20", "dc-1")
    card["picks"] = [{"symbol": "600519.SH"}]
    p.write_text(json.dumps(card), encoding="utf-8")

    assert ledger.load_cards(tmp_path)[0]["picks"] == [{"symbol": "600519.SH"}]


def test_corrupt_card_is_never_cached(tmp_path: Path):
    """坏账本要每次都抛 —— 不能把异常变成"记住的结论"。"""
    d = ledger.cards_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "dc-bad.json").write_text("{not json", encoding="utf-8")

    for _ in range(2):
        with pytest.raises(ledger.LedgerCorruptError):
            ledger.load_cards(tmp_path)


def test_settlements_append_is_visible_immediately(tmp_path: Path):
    path = ledger.settlements_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_settlement_row("k1")) + "\n", encoding="utf-8")
    assert len(ledger.load_settlements(tmp_path)) == 1

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_settlement_row("k2")) + "\n")
    assert len(ledger.load_settlements(tmp_path)) == 2


def test_returned_list_is_a_copy(tmp_path: Path):
    path = ledger.settlements_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_settlement_row("k1")) + "\n", encoding="utf-8")

    first = ledger.load_settlements(tmp_path)
    first.clear()  # 调用方就地清空
    assert len(ledger.load_settlements(tmp_path)) == 1, "缓存不许被调用方改坏"

    _write_card(tmp_path, _card("2026-11-20"))
    cards = ledger.load_cards(tmp_path)
    cards.sort(reverse=True)
    assert len(ledger.load_cards(tmp_path)) == 1


def test_health_lines_equals_rows_plus_problems(tmp_path: Path):
    """`ledger_health` 的非空行数由「记录数 + 问题数」推出（省掉第二次全量 read_text）。

    这个恒等式成立的前提是：`_scan_settlements` 对每个非空行要么收进 rows、
    要么记一条 problem。一旦有人改成"一行记两条问题"或"跳行不记"，这里会红。
    """
    path = ledger.settlements_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(_settlement_row("k1"))
    dup = json.dumps(_settlement_row("k1"))
    broken = "{not json"
    path.write_text("\n".join([good, dup, broken, ""]) + "\n", encoding="utf-8")

    info = ledger.ledger_health(tmp_path)["settlements"]

    assert info["rows"] == 1
    assert len(info["problems"]) == 2
    assert info["lines"] == 3, "空行不算；2 条问题 + 1 条记录"
