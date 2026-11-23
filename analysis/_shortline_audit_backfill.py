"""回填短线雷达排序体检台账 + 重算产出物。

预注册：docs/structural-edge/prereg-heat-sort.md
  - `--source backfill`（默认）：登记 PREREG_START 之前的交易日，样本内，只作对照。
  - `--source forward`：登记 PREREG_START 之后的交易日（正常由 17:10 定时任务做，
    这里只用于手工补当天）。

登记用生产函数 `citizenship.get_citizenship`（同一份池子定义，不另建第二套），
每天 ~0.5s；回填两年 ≈ 5 分钟。同日期已存在则跳过（幂等）。

用法：
  python analysis/_shortline_audit_backfill.py                       # 回填两年 + rebuild
  python analysis/_shortline_audit_backfill.py --from 2026-01-01     # 只回填今年
  python analysis/_shortline_audit_backfill.py --today               # 手工补今天(forward)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date as date_cls
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import citizenship, shortline_audit  # noqa: E402

BACKFILL_DEFAULT_FROM = "2024-11-01"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default=BACKFILL_DEFAULT_FROM)
    ap.add_argument("--to", dest="date_to", default=None, help="默认：预注册起始日前最后一个交易日")
    ap.add_argument("--source", choices=["backfill", "forward"], default="backfill")
    ap.add_argument("--today", action="store_true", help="只补最新交易日（source=forward）")
    ap.add_argument("--rebuild-only", action="store_true")
    args = ap.parse_args()

    data_dir = ROOT / "data"
    have = {r["date"] for r in shortline_audit.read_ledger(data_dir)}
    print(f"台账已有 {len(have)} 天")

    if not args.rebuild_only:
        if args.today:
            row = shortline_audit.record_day(data_dir, None, source="forward")
            print("补今天:", "已存在" if row is None else row["date"])
        else:
            days = citizenship._enriched_days(data_dir)
            lo = date_cls.fromisoformat(args.date_from)
            hi = (date_cls.fromisoformat(args.date_to)
                  if args.date_to else date_cls.fromisoformat(shortline_audit.PREREG_START))
            todo = [d for d in days if lo <= d < hi and d.isoformat() not in have]
            print(f"待登记 {len(todo)} 天（{todo[0] if todo else '—'} → {todo[-1] if todo else '—'}）")
            t0 = time.time()
            done = 0
            for i, d in enumerate(todo, 1):
                row = shortline_audit.record_day(data_dir, d, source=args.source)
                if row:
                    done += 1
                if i % 25 == 0 or i == len(todo):
                    print(f"  {i}/{len(todo)}  {time.time()-t0:.0f}s", flush=True)
            print(f"新登记 {done} 天，用时 {time.time()-t0:.0f}s")

    art = shortline_audit.rebuild(data_dir)
    print(f"\n产出物 {shortline_audit.artifact_path(data_dir)}")
    print(f"登记 {art['days_registered']} 天（前向 {art['fwd_days']} / 回填 {art['backfill_days']}）/"
          f" 可评估 {art['days_evaluated']} 天 / 待后续数据 {art['days_pending']} 天")
    for tag in ("forward", "all"):
        w = art.get(tag)
        if not w:
            continue
        print(f"[{tag}] 月 {w['months']} · 现行 lift {w['cur_lift_bp']}bp (t={w['cur_t']}) · "
              f"候选 lift {w['shadow_lift_bp']}bp (t={w['shadow_t']}) · "
              f"配对差 {w['paired_delta_bp']}bp (t={w['paired_t']}) · 候选净 {w['shadow_net_bp']}bp")
    s = (art.get("forward") or art.get("all") or {}).get("series") or []
    if s:
        last = s[-1]
        print(f"最近月 {last['month']}: 池 {last['pool_bp']}bp · 现行 {last['cur_bp']}bp "
              f"(lift {last['cur_lift_bp']}bp, z={last['cur_z']}) · 候选 {last['shadow_bp']}bp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
