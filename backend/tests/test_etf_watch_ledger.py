"""段首日与账本作废：两个都会静默改变「事件是哪一天」的地方。

这两条不是纯风格问题——段首日错一天，整张持有期表的进场价就换一天；
作废语义错一次，重跑写入的修正事件会被自己吞掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch  # noqa: E402


def test_run_starts_marks_only_first_day_of_a_run():
    f = pd.DataFrame(
        {"A": [False, True, True, True, False, True], "B": [True, True, False, True, True, True]},
        index=pd.bdate_range("2026-11-02", periods=6),
    )
    s = etf_watch.run_starts(f)
    assert s["A"].tolist() == [False, True, False, False, False, True]
    assert s["B"].tolist() == [True, False, False, True, False, False]
    assert s.dtypes.tolist() == [bool, bool]      # 不能退化成 int（~True = −2）


def test_void_consumes_one_event_and_keeps_the_corrected_rerecord(tmp_path, monkeypatch):
    p = tmp_path / "ledger.jsonl"
    rows = [
        {"type": "event", "id": "2026-11-20|机械", "date": "2026-11-20", "industry": "机械"},
        {"type": "void", "id": "2026-11-20|机械", "reason": "段首日误判"},
        {"type": "event", "id": "2026-11-20|机械", "date": "2026-11-20", "industry": "机械"},
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    monkeypatch.setattr(etf_watch, "LEDGER", p)

    events, settles = etf_watch.read_ledger()

    assert [e["id"] for e in events] == ["2026-11-20|机械"]
    assert settles == []
