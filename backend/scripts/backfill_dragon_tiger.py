"""龙虎榜历史回填 — 按交易日把 fuyao 三榜落进 data/dragon_tiger/ 缓存。

为什么需要本脚本:
    fuyao 龙虎榜只允许回溯一年 (显式更早日期返回 code=1003), 而
    services.dragon_tiger 是「读缓存优先、当日现拉」的在线路径 —— 没人访问的
    交易日永远不会有缓存。结果就是历史榜单事实上是空集, 只有用户手动点开过
    的那几天有数据。本脚本把「本地日K分区 ∩ 一年窗口」逐日补齐。

写出的文件格式与 services.dragon_tiger._store_cache 完全一致
(state/requested_date/trade_date + all/org/hot_money), 服务层读回时走
_load_cache → normalize_payload, 无需任何改动。

用法:
    python backend/scripts/backfill_dragon_tiger.py                # 补齐缺失日
    python backend/scripts/backfill_dragon_tiger.py --since 2026-08-01
    python backend/scripts/backfill_dragon_tiger.py --dry-run      # 只报告缺失日
    python backend/scripts/backfill_dragon_tiger.py --force        # 覆盖已有缓存
    python backend/scripts/backfill_dragon_tiger.py --today        # 每日增量 (盘后跑)

退出码: 0 全部成功 | 1 有失败日 (已成功的仍已落盘, 重跑会跳过)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from app.market_time import cn_today  # noqa: E402
from app.services import dragon_tiger as svc  # noqa: E402

_BOARDS = ("all", "org", "hot_money")
_WINDOW_DAYS = 365


def _data_dir() -> Path:
    import os

    env = os.environ.get("TICKFLOW_DATA_DIR")
    return Path(env).resolve() if env else (_REPO_ROOT / "data")


def _target_days(data_dir: Path, since: date_cls | None, until: date_cls | None,
                 include_today: bool = False) -> list[date_cls]:
    """待回填交易日 = 本地日K分区 ∩ 一年窗口 ∩ [since, until]。

    include_today=False 时排除当日: 在线路径在盘中拉到的是未定稿榜单, 交给它自己处理。
    盘后定稿后再落盘 (每日增量) 需要显式打开 include_today。
    """
    today = cn_today()
    horizon = today - timedelta(days=_WINDOW_DAYS - 2)  # 留 2 天余量给服务端时钟
    lo = max(x for x in (horizon, since) if x is not None)
    hi = until or today
    days = [d for d in svc._local_trading_days(data_dir) if lo <= d <= hi]
    return days if include_today else [d for d in days if d < today]


def _fetch_day(provider, d: date_cls) -> dict:
    raw = {bt: svc._boards_of(provider, bt, d.isoformat()) for bt in _BOARDS}
    actual = raw["all"].get("trade_date")
    if actual != d.isoformat():
        # 服务端把日期挪了 (非交易日/未发布) —— 落盘会污染日期语义, 直接判失败
        raise ValueError(f"trade_date={actual} 与请求日 {d.isoformat()} 不符")
    return {
        "state": "ok",
        "requested_date": d.isoformat(),
        "trade_date": actual,
        **raw,
    }


def _summary(payload: dict) -> tuple[int, int]:
    stocks = len((payload.get("all") or {}).get("stock_items") or [])
    seats = len((payload.get("hot_money") or {}).get("hot_money_items") or [])
    return stocks, seats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填龙虎榜历史到本地缓存")
    ap.add_argument("--since", help="起始日 YYYY-MM-DD (默认: 一年窗口起点)")
    ap.add_argument("--until", help="截止日 YYYY-MM-DD (默认: 今日)")
    ap.add_argument("--force", action="store_true", help="覆盖已有缓存")
    ap.add_argument("--today", action="store_true",
                    help="含当日 (盘后定稿后跑每日增量用; 不要放在盘中)")
    ap.add_argument("--dry-run", action="store_true", help="只报告待回填日, 不发请求")
    ap.add_argument("--pause", type=float, default=0.05, help="每日之间停顿秒数")
    args = ap.parse_args(argv)

    data_dir = _data_dir()
    since = date_cls.fromisoformat(args.since) if args.since else None
    until = date_cls.fromisoformat(args.until) if args.until else None

    days = _target_days(data_dir, since, until, include_today=args.today)
    if not days:
        print("没有落在一年窗口内的本地交易日", file=sys.stderr)
        return 0

    existing = {d for d in days if svc._cache_path(data_dir, d).exists()}
    todo = days if args.force else [d for d in days if d not in existing]
    print(f"窗口内交易日 {len(days)} 天 | 已有缓存 {len(existing)} 天 | 待回填 {len(todo)} 天")
    if args.dry_run or not todo:
        if todo:
            print("缺失日:", ", ".join(d.isoformat() for d in todo[:10]),
                  "..." if len(todo) > 10 else "")
        return 0

    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("fuyao"):
        print("fuyao 未启用, 无法回填", file=sys.stderr)
        return 1
    provider = custom_sources.get_provider("fuyao")
    from app.plugins.fuyao.client import FuyaoError

    ok = 0
    failed: list[tuple[str, str]] = []
    n_stocks = n_seats = 0
    t_start = time.time()

    for i, d in enumerate(todo, 1):
        try:
            payload = _fetch_day(provider, d)
            svc._store_cache(svc._cache_path(data_dir, d), payload)
            s, h = _summary(payload)
            n_stocks += s
            n_seats += h
            ok += 1
            print(f"[{i}/{len(todo)}] {d} ok  stocks={s:3d} seats={h:2d}")
        except FuyaoError as e:
            failed.append((d.isoformat(), str(e)))
            print(f"[{i}/{len(todo)}] {d} FAIL {e}", file=sys.stderr)
        except (OSError, ValueError) as e:
            failed.append((d.isoformat(), str(e)))
            print(f"[{i}/{len(todo)}] {d} FAIL {type(e).__name__}: {e}", file=sys.stderr)
        if args.pause:
            time.sleep(args.pause)

    elapsed = time.time() - t_start
    print(f"\n完成 {ok}/{len(todo)} 天, 用时 {elapsed:.1f}s"
          f" ({(elapsed / max(ok, 1)) * 1000:.0f}ms/日),"
          f" 累计 stocks={n_stocks} seats={n_seats}")
    if failed:
        print(f"失败 {len(failed)} 天:", file=sys.stderr)
        for ds, msg in failed[:20]:
            print(f"  {ds}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
