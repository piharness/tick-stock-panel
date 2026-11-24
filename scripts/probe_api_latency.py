"""API 延迟门禁 —— 按界面关键路径固定一组只读端点, 重复采样并给出 p50/p95/max。

为什么需要它: 本仓的慢不是「某个接口天生慢」, 而是**同一批状态接口被高频轮询**
放大 (账本里 `/api/pipeline/jobs` 一周 18869 次、`/api/data/integrity` 7151 次)。
所以验收必须同时看两件事:

  1. 单次耗时是否在预算内 (预算按「用户是否在等它」分档, 不搞一刀切);
  2. 同一端点重复调用是否稳定 —— p95/max 远大于 p50 说明它在缓存失效点或
     排队上抖动, 这类抖动在轮询下等于随机几十秒的白屏。

用法::

    python scripts/probe_api_latency.py                    # 默认 http://127.0.0.1:3018
    python scripts/probe_api_latency.py --runs 10 --budget 300
    python scripts/probe_api_latency.py --json out.json     # 存基线做前后对比

判读约定:
  - 退出码 0 = 全部在预算内; 1 = 有超预算项 (可直接当 CI 门禁);
  - 采样前先跑一轮预热并丢弃 —— 冷启动的视图绑定/日历快照不属于稳态成本,
    混进来会把结论变成「冷启动慢」而不是「接口慢」;
  - 只打 GET, 不碰任何会写盘的端点 (维护/同步/清库一律 POST, 不进本脚本)。

预算分档的依据::
  high   500ms  用户点开就会盯着看 (数据状态、偏好、任务列表)
  normal 1200ms 页面区块, 允许一次 DuckDB 聚合
  heavy  8000ms 「点一下算一次」的分析类, 本来就不该被轮询; 超过这条要改任务化
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

#: (路径, 分档)。只收界面会自己发起的只读请求。
PROBES: list[tuple[str, str]] = [
    # ── 常驻轮询 / 每页首屏 ──
    ("/health", "high"),
    ("/api/settings/preferences", "high"),
    ("/api/pipeline/jobs?limit=1", "high"),
    ("/api/pipeline/jobs?limit=20", "high"),
    ("/api/data/status", "high"),
    ("/api/data/version", "high"),
    ("/api/maintenance", "high"),
    ("/api/data/integrity", "high"),
    ("/api/intraday/status", "high"),
    ("/api/intraday/indices", "high"),
    ("/api/alerts", "high"),
    ("/api/settings", "high"),
    ("/api/settings/data-sources", "high"),
    ("/api/capabilities", "high"),
    ("/api/settings/capability-matrix", "high"),
    ("/api/analysis-menus", "high"),
    ("/api/ext-pages", "high"),
    ("/api/custom-signals", "normal"),
    # ── 页面区块 ──
    ("/api/overview/market", "normal"),
    ("/api/regime/coverage", "normal"),
    ("/api/watchlist/groups", "normal"),
    ("/api/watchlist/enriched", "normal"),
    ("/api/kline/daily?symbol=600519.SH&days=250", "normal"),
    ("/api/kline/minute?symbol=600519.SH", "normal"),
    ("/api/etf-watch", "normal"),
    ("/api/citizenship", "normal"),
    ("/api/screener/limit-ladder", "normal"),
    ("/api/abnormal/overview", "normal"),
    ("/api/position-art", "normal"),
    ("/api/position-art/tail", "normal"),
    ("/api/decision/overview", "normal"),
    # ② 响应用的进程内缓存：历史日是快照，当日绝不缓存（见 docs/caching-strategy.md）
    ("/api/kline/minute?symbol=000001.SZ&date=2026-11-20", "normal"),
    ("/api/shortline-radar", "normal"),
    # ③ stale-while-revalidate：写入打脏、下一读后台重算，读路径永不阻塞
    ("/api/stock-analysis/crowding-verdict", "normal"),
    # ── 分析类 (点一次算一次) ──
    ("/api/decision/slate", "heavy"),
    ("/api/market-recap/dragon-tiger", "heavy"),
]

BUDGETS_MS = {"high": 500.0, "normal": 1200.0, "heavy": 8000.0}


def _time_one(url: str, timeout: float) -> tuple[float, int, int]:
    """返回 (毫秒, 状态码, 响应字节)。HTTP 错误也计时 —— 500 快不代表没问题。"""
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 —— 本机地址
            body = resp.read()
            return (time.perf_counter() - started) * 1000.0, resp.status, len(body)
    except urllib.error.HTTPError as exc:
        return (time.perf_counter() - started) * 1000.0, exc.code, 0
    except Exception:
        return float("inf"), 0, 0


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API 延迟门禁")
    parser.add_argument("--base", default="http://127.0.0.1:3018")
    parser.add_argument("--runs", type=int, default=5, help="每个端点的采样次数")
    parser.add_argument("--budget", type=float, default=0.0, help="覆盖 high 档预算(ms), 0=用默认")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--json", default="", help="把结果写成 JSON (做前后基线对比)")
    parser.add_argument(
        "--compare", default="",
        help="与既有基线 JSON 比较, 中位数劣化超阈值即非零退出 (棘轮)",
    )
    parser.add_argument(
        "--tolerance", type=float, default=0.5,
        help="--compare 允许的相对劣化比例 (默认 0.5 = 慢 50%)",
    )
    parser.add_argument(
        "--floor-ms", type=float, default=30.0,
        help="--compare 的绝对噪声地板: 基线低于该值时只按绝对增量判断",
    )
    parser.add_argument("--skip-heavy", action="store_true", help="跳过分析类端点")
    args = parser.parse_args(argv)

    budgets = dict(BUDGETS_MS)
    if args.budget > 0:
        budgets["high"] = args.budget
        budgets["normal"] = args.budget

    # 预热: 丢弃冷启动那一轮 (视图绑定/快照装载不属于稳态)
    for path, tier in PROBES:
        if args.skip_heavy and tier == "heavy":
            continue
        _time_one(args.base + path, args.timeout)

    results: list[dict] = []
    for path, tier in PROBES:
        if args.skip_heavy and tier == "heavy":
            continue
        samples: list[float] = []
        status = 0
        size = 0
        for _ in range(max(1, args.runs)):
            ms, status, size = _time_one(args.base + path, args.timeout)
            samples.append(ms)
        p50 = _pct(samples, 0.5)
        results.append(
            {
                "path": path,
                "tier": tier,
                "n": len(samples),
                "p50": round(p50, 1),
                "p95": round(_pct(samples, 0.95), 1),
                "max": round(max(samples), 1),
                "status": status,
                "bytes": size,
                "budget": budgets[tier],
                "over": status >= 500 or p50 > budgets[tier],
            }
        )

    results.sort(key=lambda r: (-r["p95"], r["path"]))
    over = [r for r in results if r["over"]]

    print(f"{'p50':>9} {'p95':>9} {'max':>9} {'budget':>8} {'kb':>7}  {'code':>4}  path")
    for r in results:
        flag = "  OVER" if r["over"] else ""
        print(
            f"{r['p50']:9.1f} {r['p95']:9.1f} {r['max']:9.1f} {r['budget']:8.0f} "
            f"{r['bytes'] / 1024:7.1f}  {r['status']:4d}  {r['path']}{flag}"
        )

    worst = max((r["p50"] for r in results), default=0.0)
    print(
        f"\n{len(results)} 端点 × {args.runs} 次 | 中位数最差 {worst:.0f}ms | "
        f"超预算 {len(over)} 个"
    )
    if over:
        print("超预算: " + ", ".join(f"{r['path']}({r['p50']:.0f}ms)" for r in over))

    if args.json:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "base": args.base,
            "runs": args.runs,
            "median_worst_ms": round(worst, 1),
            "over_budget": len(over),
            "results": results,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"写得: {args.json}")

    regressions = _compare_baseline(results, args) if args.compare else []

    return 1 if (over or regressions) else 0


def _compare_baseline(results: list[dict], args) -> list[str]:
    """与基线比较中位数，返回劣化说明。棘轮：只许变快，变慢要解释。

    噪声处理：小端点的抖动按**绝对增量**判断（默认 30ms 地板），大端点按
    **相对比例**判断。只用比例会把 4ms→7ms 这种噪声报成回归；只用绝对值会
    放过 400ms→700ms。
    """
    try:
        with open(args.compare, encoding="utf-8") as fh:
            baseline = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"基线读取失败, 跳过对比: {exc}")
        return []

    base_by_path = {r["path"]: r for r in baseline.get("results", [])}
    regressions: list[str] = []
    for r in results:
        old = base_by_path.get(r["path"])
        if old is None:
            continue
        old_p50 = float(old.get("p50") or 0.0)
        if old_p50 <= 0:
            continue
        limit = max(old_p50 * (1.0 + args.tolerance), old_p50 + args.floor_ms)
        if r["p50"] > limit:
            regressions.append(
                f"{r['path']}: {old_p50:.0f}ms → {r['p50']:.0f}ms "
                f"(允许 ≤{limit:.0f}ms)"
            )

    if regressions:
        print(f"\n对比 {args.compare} 发现 {len(regressions)} 处劣化:")
        for item in regressions:
            print("  " + item)
    else:
        print(f"\n对比 {args.compare}: 无劣化 (基线 {baseline.get('generated_at', '?')})")
    return regressions


if __name__ == "__main__":
    sys.exit(main())
