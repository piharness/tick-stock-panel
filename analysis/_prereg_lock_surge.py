"""预注册 lock-surge 的跑数入口。

判据与定义在 `docs/structural-edge/prereg-lock-surge.md`；
实现点在 `backend/app/services/lock_surge.py`（登记/评估一套代码，不重写）。

跑法（在仓库根目录，用 backend 的 venv）：

    # 1. 回填两段（只跑一次；design 已看过，holdout 是 pseudo-OOS）
    backend/.venv/Scripts/python.exe analysis/_prereg_lock_surge.py --backfill design
    backend/.venv/Scripts/python.exe analysis/_prereg_lock_surge.py --backfill holdout

    # 2. 读数（forward 段为空时只报进度，不裁决）
    backend/.venv/Scripts/python.exe analysis/_prereg_lock_surge.py --evaluate

    # 3. 邻域诊断（1.5 / 3.0），不改判据
    backend/.venv/Scripts/python.exe analysis/_prereg_lock_surge.py --neighborhood

    # 4. 前向登记（每天收盘后；盘中调用会被收盘守卫拒绝）
    backend/.venv/Scripts/python.exe analysis/_prereg_lock_surge.py --record

注意：holdout 只能跑一次。跑完即封存，之后任何基于它的调整都是样本内。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services import lock_surge  # noqa: E402

DATA = ROOT / "data"

WINDOWS = {
    "design": ("2021-04-01", "2026-09-30"),
    "holdout": ("2019-01-01", "2020-12-31"),
}


def _print_stats(name: str, s: dict | None, gate: bool = False) -> None:
    print(f"\n== {name} ==")
    if not s or not s.get("days"):
        print("  台账为空（先 --backfill；forward 段要等登记）")
        return
    print(f"  基期 {s['days']} 天 / {s['months']} 个月；放量组日均 {s['n_surge']:.1f} 只")
    print(
        f"  月均配对差 {s['diff_bp']:+.1f}bp（t {s['diff_t']:+.2f}）"
        f" | 放量 {s['surge_bp']:+.1f}bp vs 安静 {s['calm_bp']:+.1f}bp"
    )
    print(
        f"  放量组净超额 {s['net_surge_bp']:+.1f}bp | 正月份 {s['pos_month_share']*100:.0f}%"
        f" | 一字占比 {s['oneword_share']*100:.1f}%"
    )
    if gate:
        print(
            f"  判据：差≥{lock_surge.GATE_DIFF_BP:.0f}bp / t≥{lock_surge.GATE_T}"
            f" / 净≥0 / 正≥{lock_surge.GATE_POS_MONTHS*100:.0f}%"
            f" / 日均≥{lock_surge.GATE_MIN_PER_DAY:.0f}"
        )
    # 分年（看是不是某一年的孤例）
    by_year: dict[str, list[float]] = {}
    for m in s["monthly"]:
        by_year.setdefault(m["month"][:4], []).append(m["diff"])
    line = " ".join(
        f"{y}:{sum(v)/len(v):+.0f}bp" for y, v in sorted(by_year.items())
    )
    print(f"  按年：{line}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", choices=sorted(WINDOWS))
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--neighborhood", action="store_true")
    ap.add_argument("--step", type=int, default=20)
    args = ap.parse_args()
    if not any([args.backfill, args.evaluate, args.record, args.neighborhood]):
        print(__doc__)
        return 0

    if args.record:
        row = lock_surge.record_day(DATA)
        if row is None:
            print("未登记：不是交易日 / 分区未收盘 / 今日已登记")
            return 0
        s, c = lock_surge._split(row["band"])
        print(f"已登记 {row['date']}：池 {row['n_band']} 只 · 放量 {len(s)} · 安静 {len(c)}")

    if args.backfill:
        start, end = WINDOWS[args.backfill]
        out = lock_surge.backfill(DATA, start, end, window=args.backfill, step=args.step)
        print(f"回填 {args.backfill} {start}..{end}：{out}")

    if args.neighborhood:
        for win in ("design", "holdout", "forward"):
            for ratio in (1.5, 2.0, 3.0):
                s = lock_surge.stats_for_window(DATA, win, ratio=ratio)
                if not s["days"]:
                    continue
                print(
                    f"  邻域 {win:7s} ratio={ratio:.1f}: 差 {s['diff_bp']:+.1f}bp "
                    f"t {s['diff_t']:+.2f} 放量日均 {s['n_surge']:.1f}"
                )

    if args.evaluate:
        out = lock_surge.evaluate(DATA)
        _print_stats("DESIGN（已看过，仅对照）", out["windows"].get("design", {}))
        _print_stats("HOLDOUT 2019-2020（pseudo-OOS，只作先验）", out["windows"].get("holdout", {}))
        _print_stats("FORWARD（唯一参与裁决）", out["windows"].get("forward", {}), gate=True)
        print(f"\n裁决：{out.get('verdict')} {out.get('checks') or out.get('reason')}")
        print(f"产物：{lock_surge.artifact_path(DATA)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
