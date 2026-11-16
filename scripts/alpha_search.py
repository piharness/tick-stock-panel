"""主动 Alpha 搜索 CLI。

用法 (在仓库根目录):
    cd backend && uv run python ../scripts/alpha_search.py --dry-run
    cd backend && uv run python ../scripts/alpha_search.py --max-formulas 360

数据读取走 BacktestEngine / enriched 分区, 与回测、挖掘同源; 结果只写
`data/research/alpha_search/`, 不写用户策略目录, 不自动发布。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.backtest.engine import BacktestEngine  # noqa: E402
from app.services.alpha_search import (  # noqa: E402
    AlphaSearchConfig,
    generate_candidates,
    run_alpha_search,
)
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date: {value}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="主动 Alpha 搜索 (预注册公式族 + 发现/确认两段验证)")
    parser.add_argument("--profile", choices=("exploratory", "balanced", "strict"), default="balanced")
    parser.add_argument("--max-formulas", type=int, default=360)
    parser.add_argument("--discovery-start", type=_parse_date, default=date(2025, 9, 1))
    parser.add_argument("--discovery-end", type=_parse_date, default=date(2026, 4, 30))
    parser.add_argument("--confirmation-start", type=_parse_date, default=date(2026, 5, 1))
    parser.add_argument("--confirmation-end", type=_parse_date, default=date(2026, 11, 6))
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "research" / "alpha_search")
    parser.add_argument("--allow-repeat", action="store_true", help="重测台账中已存在的公式")
    parser.add_argument("--dry-run", action="store_true", help="只生成公式族并打印哈希, 不读行情")
    args = parser.parse_args()

    config = AlphaSearchConfig(
        profile=args.profile,
        discovery_start=args.discovery_start,
        discovery_end=args.discovery_end,
        confirmation_start=args.confirmation_start,
        confirmation_end=args.confirmation_end,
        max_formulas=args.max_formulas,
        allow_repeat=args.allow_repeat,
    )
    config.validate()
    formulas = generate_candidates(config.max_formulas)
    if args.dry_run:
        from app.services.alpha_search import family_hash

        print(f"公式数: {len(formulas)}")
        print(f"家族哈希: {family_hash(formulas)}")
        for item in formulas[:8]:
            print(f"  - {item.formula}")
        print("  ...")
        return 0

    store = DataStore(args.data_dir)
    repo = KlineRepository(store)
    engine = BacktestEngine(repo)
    report = run_alpha_search(engine, config, output_dir=args.output_dir)

    counts = report["counts"]
    print("主动 Alpha 搜索完成")
    print(
        f"  profile={config.profile} 发现期={config.discovery_start}..{config.discovery_end} "
        f"确认期={config.confirmation_start}..{config.confirmation_end}"
    )
    print(
        f"  公式 {counts['generated']} / 新增 {counts['evaluated']} / "
        f"跳过已测 {counts['skipped_prior']}"
    )
    print(
        f"  发现通过 {counts['discovery_passed']} / 确认通过 {counts['confirmation_passed']} / "
        f"confirmed {counts['confirmed']} / provisional {counts['provisional']}"
    )
    candidates = report.get("candidates", [])
    if candidates:
        print("  候选:")
        for row in candidates[:10]:
            print(
                f"    [{row['status']}] {row['formula']}  "
                f"disc IC={row.get('discovery_ic')} t={row.get('discovery_t')}  "
                f"conf IC={row.get('confirmation_ic')} t={row.get('confirmation_t')}  "
                f"DSR={row.get('dsr')}"
            )
    else:
        print("  无候选同时通过发现期与确认期门槛。")
    print(f"  报告: {report['artifacts']['report_md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
