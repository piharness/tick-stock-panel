"""用当前 slate 产地重算历史卡那天的名单，逐位比对。

## 为什么需要它

`test_producer_card_picks_equal_slate_rows` 只证明**新卡**从名单取 picks。
它不回答「换了产地之后，历史名单有没有被悄悄改过」：`mom20` 的窗口、
`rank` 的方向、限价的取整方式，任何一处改动都不会让那条测试变红——
新卡会心安理得地用新口径生成，历史卡则留在账本里假装还是同一件事。

所以判据必须是**跨版本**的：拿今天的产地重算昨天那张卡那天的名单，
symbol 序列逐位相同。

## 用法

    python analysis/_verify_slate_vs_cards.py --check

`--check`：有分叉退出 1。不加只打印。
`--limit N`：只核对最近 N 张卡（默认全部 L1 卡）。

## 它可能红在哪，以及红了怎么办

- **真的改了口径**：接受它，并在 `docs/l1-gap-reversal.md` 登记「哪一天起
  名单不同、为什么」。历史卡不能改（封签），但读者必须知道它们不是同一策略。
- **数据被回填/修正**：`raw_close` 变了，限价跟着变。这属于数据事件，
  在输出里逐条列出差异数值，不要只看「不一致」三个字。
- **卡是旧版生产者发的**（`_l1_picks` 直接调 candidates 的时代）：它应该
  完全一致；若不一致，说明当时的 candidates 与今天的不是同一份代码。

**不要为了让这条变绿去改期望值。** 它存在的理由是「跨版本同名」，不是
「今天的名单对不对」——后者由 `test_real_slate_tracks_latest_complete_session`
负责。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import l1_cohort, ledger, slate  # noqa: E402


def _cards(data_dir: Path, limit: int | None) -> list[dict]:
    try:
        cards = ledger.load_cards(data_dir)
    except ledger.LedgerCorruptError as exc:
        print(f"账本残缺，无法核对：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    l1 = [c for c in cards if c.get("strategy_id") == l1_cohort.L1_STRATEGY_ID]
    l1.sort(key=lambda c: str(c.get("as_of")))
    return l1[-limit:] if limit else l1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--check", action="store_true", help="有分叉退出 1")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    data_dir = Path(args.data)
    cards = _cards(data_dir, args.limit)
    if not cards:
        # 空账本不是「通过」：没有可比对的样本，判据没有执行过。
        print("没有 L1 决策卡可比对——判据未执行，按失败处理")
        return 2

    diverged = 0
    for card in cards:
        as_of = str(card["as_of"])
        rebuilt = slate.build_slate(data_dir, as_of)
        if rebuilt["status"] not in slate.READY_STATES:
            print(f"{as_of}  无法重算：{rebuilt['status']} ({rebuilt.get('reason')})")
            diverged += 1
            continue

        new = [(str(r["symbol"]), float(r["limit_price"])) for r in rebuilt["rows"]]
        old = [(str(p["symbol"]), float(p["limit_price"])) for p in card.get("picks") or []]

        # 「逐位相同」= 同序 + 同价（1e-6 容差）。限价是 0.01 的整数倍，
        # 真正的分叉至少差一分钱；1e-15 的尾数不是口径。
        same = (
            [s for s, _ in new] == [s for s, _ in old]
            and all(abs(a - b) <= 1e-6 for (_, a), (_, b) in zip(new, old))
        )
        if same:
            print(f"{as_of}  {len(new):2d} 只  逐位相同")
            continue

        diverged += 1
        new_map = dict(new)
        old_map = dict(old)
        only_new = [s for s in new_map if s not in old_map]
        only_old = [s for s in old_map if s not in new_map]
        # 限价比较用 1e-6 容差：挂单价是 0.01 的整数倍，真正的分叉至少差一分钱，
        # 而 1e-15 的二进制尾数不是口径问题。用浮点严格相等会把「同一份名单」
        # 判成分叉——2026-11-15 的 9 张卡就是这么被误报的。
        price_moved = [
            (s, new_map[s], old_map[s]) for s in new_map
            if s in old_map and abs(new_map[s] - old_map[s]) > 1e-6
        ]
        order_moved = [s for s, _ in new] != [s for s, _ in old]
        tags = []
        if order_moved:
            tags.append("顺序")
        if price_moved:
            tags.append("限价")
        if only_new or only_old:
            tags.append("成员")
        print(f"{as_of}  {len(new):2d} 只  分叉（{' + '.join(tags) or '未知'}）")
        if only_new:
            print(f"         名单多出: {only_new[:8]}")
        if only_old:
            print(f"         卡里多出: {only_old[:8]}")
        for s, a, b in price_moved[:8]:
            print(f"         限价变了: {s} 名单 {a} vs 卡 {b}")

    print(f"\n{len(cards)} 张卡，{diverged} 张分叉")
    if args.check and diverged:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
