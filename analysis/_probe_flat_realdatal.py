"""空仓卡真实数据探针：证明这条路径在**生产数据**上能造卡、能结算、hash 可复算。

测试只证明代码自洽（fixture 造的行情），不证明它接得上真实数据。
这个脚本用真实行情 + 真实账本代码，只在临时目录里写：

  1. 取 40 个交易日前的某天当 as_of，造一张 horizon=20 的空仓卡 → 应当 created
  2. 复算 `commitment()` → 必须与落盘 digest 相同（哈希层没被 stance 字段破坏）
  3. 用今天结算 → 必须写出一条 avoided / missed，并且 entry 是 as_of 之后第一个交易日
  4. 再跑一次结算 → 必须 0 新增（幂等）

用法:
    backend/.venv/Scripts/python.exe analysis/_probe_flat_realdatal.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import bars, ledger  # noqa: E402
from app.decision.producer import produce_flat_card  # noqa: E402
from app.decision.settle import settle_flats  # noqa: E402

LOOKBACK = 40
HORIZON = 20


def main() -> int:
    calendar = bars.sessions(DATA)
    if len(calendar) < LOOKBACK + HORIZON + 5:
        print(json.dumps({"error": "日历不足", "n": len(calendar)}, ensure_ascii=False))
        return 2
    as_of = calendar[-LOOKBACK]
    today = calendar[-1]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        # 只改账本落盘位置，行情仍读真实 DATA
        original_root = ledger.root
        ledger.root = lambda _d: tmp_root  # type: ignore[assignment]
        try:
            out = produce_flat_card(
                DATA,
                as_of=as_of,
                horizon_sessions=HORIZON,
                reason="探针：候选池正常但门槛全滤，决定不参与（真实数据验证）",
                strategy_id="l1_cohort",
            )
            if out.get("status") != "created":
                print(json.dumps({"step": "produce", "out": out}, ensure_ascii=False,
                                 default=str))
                return 1

            cards = ledger.load_cards(DATA)
            flat_cards = [c for c in cards if ledger.card_stance(c) == ledger.STANCE_FLAT]
            if len(flat_cards) != 1:
                print(json.dumps({"error": "空仓卡数量异常", "n": len(flat_cards)},
                                 ensure_ascii=False))
                return 1
            card = flat_cards[0]

            digest_ok, digest_reason = ledger.verify_card(card)
            if not digest_ok:
                print(json.dumps({"error": "封存校验失败", "reason": digest_reason,
                                  "sealed": card.get("sealed")}, ensure_ascii=False))
                return 1

            first = settle_flats(DATA, as_of=today)
            second = settle_flats(DATA, as_of=today)
            rows = ledger.load_flats(DATA)
            if len(rows) != 1:
                print(json.dumps({"error": "结算行数异常", "n": len(rows)}, ensure_ascii=False))
                return 1
            row = rows[0]
            summary = ledger.flat_summary(DATA)
            expect_entry = calendar[calendar.index(as_of) + 1]
            print(json.dumps({
                "as_of": as_of,
                "today": today,
                "card_id": card["card_id"],
                "stance": card["stance"],
                "digest_ok": digest_ok,
                "entry": row.get("entry"),
                "entry_matches_next_session": row.get("entry") == expect_entry,
                "exit": row.get("exit"),
                "benchmark_ret": row.get("benchmark_ret"),
                "summary": summary,
                "settle_first": first.get("written"),
                "settle_second": second.get("written"),
                "idempotent": second.get("written") == 0,
            }, ensure_ascii=False, indent=2))
            return 0
        finally:
            ledger.root = original_root  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
