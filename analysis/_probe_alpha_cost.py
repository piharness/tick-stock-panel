"""主动搜索 (alpha_search) 的成本与可行性探针。

要回答一个问题：**能不能把它挂到周度调度上**。
如果 12 个公式的批量回测要十几分钟，那 360 个公式的默认配置就不是「每周跑一次」，
而是「每周把机器占满一次」——那必须先改配置或改执行方式，不能先挂上去。

同时验证滚动窗口：原 `AlphaSearchConfig` 的四个日期是硬编码的
(2025-09-01~2026-04-30 / 2026-05-01~2026-11-06)，硬编码窗口的搜索只能跑一次，
第二次跑出的「确认」仍然是在同一段历史上确认，等于把同一份数据反复算到过拟合。

用法:
    backend/.venv/Scripts/python.exe analysis/_probe_alpha_cost.py --formulas 6
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formulas", type=int, default=6)
    ap.add_argument("--out", default=str(ROOT / "data" / "_probe_alpha_search"))
    args = ap.parse_args()

    from app.services.alpha_schedule import rolling_config  # noqa: E402
    from app.services.alpha_search import run_alpha_search  # noqa: E402
    from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402
    from app.backtest.engine import BacktestEngine  # noqa: E402

    store = DataStore()
    repo = KlineRepository(store)
    engine = BacktestEngine(repo)

    cfg = rolling_config(date.fromisoformat("2026-11-18"), max_formulas=args.formulas)
    print(json.dumps({
        "discovery": [cfg.discovery_start.isoformat(), cfg.discovery_end.isoformat()],
        "confirmation": [cfg.confirmation_start.isoformat(), cfg.confirmation_end.isoformat()],
        "max_formulas": cfg.max_formulas,
    }, ensure_ascii=False))

    out = Path(args.out)
    t0 = time.time()
    report = run_alpha_search(engine, cfg, output_dir=out)
    elapsed = time.time() - t0

    counts = report.get("counts") or {}
    per_formula = elapsed / max(int(counts.get("evaluated") or 0), 1)
    print(json.dumps({
        "elapsed_s": round(elapsed, 1),
        "counts": counts,
        "s_per_formula": round(per_formula, 2),
        "projected_360_s": round(per_formula * 360, 0),
        "projected_360_min": round(per_formula * 360 / 60, 1),
        "status": report.get("status"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
