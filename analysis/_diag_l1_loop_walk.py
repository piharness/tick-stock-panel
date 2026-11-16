"""L1 回路演练：从 candidates() 走到 settle()，逐 cell 与研究侧对账。

## 为什么要有这个脚本

`data/user_data/decision/` 从未被写过一张卡，`data/paper/` 的 100 万账户
八个交易日 0 笔订单。也就是说：TSP 有完整的「候选 → 卡 → 结算 → p_win」
代码，但这条链路**从未在任何一个真实交易日上跑完整过**。

本脚本用历史窗口把它跑一遍，只回答一个问题：**断在哪一段。**

它不产出策略、不改生产账本、不写 `data/`。所有账本产物落在系统临时目录，
数据目录用 junction 只读挂载。

## 口径（与 `_diag_l1_cohort_curve.py` 同源）

    as_of 收盘  : candidates(as_of) 给出次日挂单清单（限价 = prev_raw × 0.99）
    entry 日    : open_raw ≤ limit ⇒ 按 open_raw 成交（fill_mode=open_only）
    exit        : 同一交易日收盘（`horizon_sessions=0`，账本里这是"入场日收盘出场"）
    收益        : 复权 close / 复权 open − 1

**L1 的收益单位是回合，不是天数。** `horizon_sessions=1` 会多隔一夜，
按 R5 实测那是 −67.2bp（t −8.88），与 L1 无关。所以这里只能传 0。

## 两个已知的坑，本脚本显式绕开

1. **`date=2026-11-13` 是盘中快照**（`quote_ts` 10:42:47），不是收盘价。
   `horizon=0` 时 exit == entry，故只要 `as_of ≤ 11-11` 就不会拿它结算。
   脚本对每个 entry 日校验 `quote_ts`，越界即拒绝——不靠调用者记得。
2. **`produce_card` 没有 `fill_mode` 参数**，自动路径恒造 `limit` 卡。
   L1 必须 `open_only`。本脚本直接调 `produce_card`，因此这一格**也会是 limit**，
   脚本把两人个口径都算一遍并报出差异——差异的大小就是"接通前必须先修的洞"。

## 运行

    backend/.venv/Scripts/python.exe analysis/_diag_l1_loop_walk.py
    backend/.venv/Scripts/python.exe analysis/_diag_l1_loop_walk.py --as-of 2026-11-05,2026-11-06

产物：`analysis/_diag_l1_loop_walk_result.json`
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import bars, ledger, l1_cohort, producer, settle  # noqa: E402

DATA = ROOT / "data"
DEFAULT_AS_OF = ["2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11"]

#: 结算需要的只读目录。缺一个就是 FileNotFoundError，故显式列出而非通配。
LINK_DIRS = [
    "kline_daily",
    "kline_daily_enriched",
    "adj_factor",
    "kline_index_daily",
    "instruments",
    "regime_history",
]

#: 收盘判据：`quote_ts` 落在 15:00 之后。盘后同步的日线是 15:xx–21:xx，
#: 盘中快照是 09:30–15:00。
CLOSE_HHMMSS = "15:00:00"


def _session_state(day: str, ts: str | None, today: str) -> tuple[str, str]:
    """这一天的日线是不是**收盘后**的数据。三态，不许两态。

    为什么不能只看 `quote_ts`：`date=2026-11-09` 与 `date=2026-11-10` 两个
    分区的 `quote_ts` **整列为 null**（5572/5571 行全空）。把 null 当"盘中"会
    误杀两天合法收盘数据；把 null 当"没问题"又会让今天的盘中快照混进来。
    所以判据分两层：**日期先于数据**——过去的日子必然已收盘；
    只有"今天"才需要 `quote_ts` 作证，而它缺席时只能是 UNKNOWN。
    """
    if day < today:
        return "CLOSED", "过去的交易日"
    if day > today:
        return "FUTURE", "晚于今天的日期"
    if ts is None:
        return "UNKNOWN", f"{day} 是今天且 quote_ts 缺失，无法证明已收盘"
    if ts >= CLOSE_HHMMSS:
        return "CLOSED", f"quote_ts={ts} ≥ {CLOSE_HHMMSS}"
    return "INTRADAY", f"quote_ts={ts} 早于 {CLOSE_HHMMSS}，是盘中快照"


def _link(src: Path, dst: Path) -> None:
    """Windows junction（不需要管理员），Unix 走 symlink。

    `cmd` 的输出是本机 ANSI 代码页（GBK），不是 UTF-8：`text=True` 会在读取
    管道时抛 `UnicodeDecodeError`，而且是在后台线程里抛——主流程照样往下走，
    只在 stderr 留一串栈。这里显式按 errors="replace" 解码。
    """
    if dst.exists() or dst.is_symlink():
        return
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            check=True, capture_output=True,
        )
    else:
        dst.symlink_to(src, target_is_directory=True)


def _make_isolated_data_dir() -> Path:
    """临时 data_dir：只有账本是真的本地目录，行情全部只读挂载。

    这样 `ledger.root(tmp)` 落在 tmp 里，生产账本一个字节都不动；
    而 `_panel()` / `bars.sessions()` 读到的仍是真实行情。
    """
    tmp = Path(tempfile.mkdtemp(prefix="tsp_l1_walk_"))
    (tmp / "user_data").mkdir(parents=True, exist_ok=True)
    (tmp / "paper").mkdir(parents=True, exist_ok=True)
    linked, missing = [], []
    for name in LINK_DIRS:
        src = DATA / name
        if src.is_dir():
            _link(src, tmp / name)
            linked.append(name)
        else:
            missing.append(name)
    return tmp, linked, missing


def _last_quote_ts(day: str) -> str | None:
    """该分区里 `quote_ts` 的最大值（本地时间 HH:MM:SS）。判"收盘 vs 盘中"。"""
    part = DATA / "kline_daily_enriched" / f"date={day}"
    if not part.is_dir():
        return None
    files = sorted(part.glob("*.parquet"))
    if not files:
        return None
    ts = (
        pl.scan_parquet(files)
        .select(pl.col("quote_ts").max())
        .collect()
        .item()
    )
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts) / 1000).strftime("%H:%M:%S")


def _adjusted_turn(data_dir: Path, entry: str, symbols: list[str]) -> dict[str, tuple]:
    """复权 open/close 与原始 close，按 symbol。对账的研究侧口径。"""
    df = (
        pl.scan_parquet(
            DATA / "kline_daily_enriched" / f"date={entry}" / "*.parquet"
        )
        .filter(pl.col("symbol").is_in(symbols))
        .select(["symbol", "open", "close", "raw_close"])
        .collect()
    )
    return {
        r["symbol"]: (r["open"], r["close"], r["raw_close"])
        for r in df.iter_rows(named=True)
    }


def _prev_adjusted_close(data_dir: Path, as_of: str, symbols: list[str]) -> dict[str, float]:
    df = (
        pl.scan_parquet(
            DATA / "kline_daily_enriched" / f"date={as_of}" / "*.parquet"
        )
        .filter(pl.col("symbol").is_in(symbols))
        .select(["symbol", "close"])
        .collect()
    )
    return {r["symbol"]: r["close"] for r in df.iter_rows(named=True)}


def _research_cells(end: str | None = None) -> set[tuple[str, str]]:
    """研究口径 cohort（`require_next=True`，含两处前视）。

    **不能传 `end`**：`cohort_cells()` 要求 `i0 + 2 < n_days`，截断到某天会把
    该天及前一天的 entry 整段跳过（尾部两天永远无 cell）。实盘侧要对比的正是
    "同一批 entry 在研究口径里长什么样"，所以取全量再按 entry 日期过滤。
    """
    cells = l1_cohort.cohort_cells(DATA, end=end)
    return {(str(r["date"]), str(r["symbol"])) for r in cells.iter_rows(named=True)}


def run(as_of_list: list[str], *, horizon: int = 0, top_n: int = l1_cohort.L1_TOP_N,
        fill_mode: str = "auto", allow_prior: bool = True,
        research: bool = True) -> dict:
    tmp, linked, missing = _make_isolated_data_dir()
    data_dir = tmp
    ledger_dir = ledger.root(data_dir)
    today = date.today().isoformat()

    rows: list[dict] = []
    stages: list[dict] = []
    guards: list[dict] = []

    for as_of in as_of_list:
        stage: dict = {"as_of": as_of}
        try:
            cand = l1_cohort.candidates(data_dir, as_of, top_n=top_n)
        except Exception as exc:  # noqa: BLE001 - 演练要的是原因，不是栈
            stage.update(status="candidates_failed", reason=f"{type(exc).__name__}: {exc}")
            stages.append(stage)
            continue

        entry = cand.get("entry_date")
        stage.update(
            entry_date=entry,
            n_candidates=cand["n_candidates"],
            warns=cand.get("warnings") or [],
        )

        # 守卫：L1 的 exit == entry，拿盘中快照结算会造出一个会被 `done` 键
        # 永久固化的假收益。日期先于数据：过去的日子直接放行。
        if entry:
            ts = _last_quote_ts(entry)
            state, why = _session_state(entry, ts, today)
            stage["entry_quote_ts"] = ts
            stage["entry_state"] = state
            if state != "CLOSED":
                guards.append({
                    "as_of": as_of, "entry": entry, "quote_ts": ts,
                    "state": state, "verdict": "REJECT", "why": why,
                })
                stage["status"] = f"guard_rejected_{state.lower()}"
                stages.append(stage)
                continue

        picks = cand.get("candidates") or []
        if not picks:
            stage.update(status="skipped_no_picks")
            stages.append(stage)
            continue

        scan_meta = {
            "source": "l1_cohort",
            "entry_date": entry,
            "gap_threshold_pct": picks[0].get("gap_threshold_pct"),
            "top_n": top_n,
        }
        res = producer.produce_card(
            data_dir,
            as_of=as_of,
            picks=picks,
            horizon_sessions=horizon,
            strategy_id=l1_cohort.L1_STRATEGY_ID,
            account_id="default",
            pwin_basis="median",
            allow_prior=allow_prior,
            scan_meta=scan_meta,
            fill_mode=("limit" if fill_mode == "auto" else fill_mode),
        )
        pw = res.get("pwin") or {}
        stage.update(
            status=res.get("status"),
            card_id=res.get("card_id"),
            gate=((res.get("gates") or {}).get("decision")),
            pwin_source=pw.get("source"),
            pwin_prior_weight=pw.get("prior_weight"),
            pwin_n=pw.get("n"),
            reason=res.get("reason"),
        )
        stages.append(stage)

    # ---- 结算：as_of 取最后一个 entry 日之后，让窗口内全部到期 ----
    last_entry = max((s.get("entry_date") for s in stages if s.get("entry_date")), default=None)
    settle_summary: dict = {}
    if last_entry:
        settle_summary = settle.settle(data_dir, as_of=last_entry)

    # ---- 读结算账本 ----
    settlements = []
    if ledger.settlements_path(data_dir).exists():
        settlements = ledger.load_settlements(data_dir)

    # ---- 逐 cell 对账：settle 的 ret vs 研究侧复权 open→close ----
    detail: list[dict] = []
    by_as_of: dict[str, list[str]] = {}
    for r in settlements:
        by_as_of.setdefault(str(r.get("as_of")), []).append(str(r["symbol"]))

    for as_of, syms in by_as_of.items():
        entry_day = None
        for s in stages:
            if s.get("as_of") == as_of:
                entry_day = s.get("entry_date")
                break
        if not entry_day:
            continue
        adj = _adjusted_turn(data_dir, entry_day, syms)
        prev = _prev_adjusted_close(data_dir, as_of, syms)
        for r in settlements:
            if str(r.get("as_of")) != as_of:
                continue
            sym = str(r["symbol"])
            a = adj.get(sym)
            fill = r.get("fill") or {}
            ex = r.get("exit") or {}
            if a is None:
                detail.append({"as_of": as_of, "symbol": sym, "verdict": "no_entry_bar"})
                continue
            o_adj, c_adj, raw_c = a
            # 研究侧：复权 open 是否 ≤ 复权 close[d] × 0.99
            gap = float(r.get("limit_price") or 0) / raw_c - 1 if raw_c else None
            fill_px = fill.get("price")
            prod_ret = ex.get("ret")
            res_ret = None
            if fill_px and o_adj and c_adj:
                res_ret = c_adj / o_adj - 1.0
            diff_bp = None
            if prod_ret is not None and res_ret is not None:
                diff_bp = round((prod_ret - res_ret) * 1e4, 4)
            detail.append({
                "as_of": as_of,
                "symbol": sym,
                "entry": entry_day,
                "fill_status": fill.get("status"),
                "fill_basis": fill.get("basis"),
                "fill_price_raw": fill_px,
                "open_adj": None if o_adj is None else round(o_adj, 4),
                "close_adj": None if c_adj is None else round(c_adj, 4),
                "prod_ret_bp": None if prod_ret is None else round(prod_ret * 1e4, 3),
                "res_ret_bp": None if res_ret is None else round(res_ret * 1e4, 3),
                "diff_bp": diff_bp,
                "price_basis": r.get("price_basis"),
            })

    filled = [d for d in detail if d.get("fill_status") == "FILLED"]
    diffs = [abs(d["diff_bp"]) for d in filled if d.get("diff_bp") is not None]

    # 研究口径的覆盖上界。`require_next=True` 要求 `fwd = close.shift(-20)` 有限，
    # 所以**最后 20 个交易日永远没有 cell**——这不是缺陷，是那条前视过滤的定义。
    # 后果：实盘要用的那 20 天，研究侧一天都判不了。对账表必须写出这个上界，
    # 否则读者会以为「研究侧也覆盖了这几天」。
    coverage: dict = {"skipped": True} if not research else {}
    try:
        cells = _research_cells(None) if research else set()
        if cells:
            days = sorted({c[0] for c in cells})
            window = sorted({s["entry_date"] for s in stages if s.get("entry_date")})
            coverage = {
                "n_rows": len(cells),
                "n_days": len(days),
                "first_day": days[0],
                "last_day": days[-1],
                "window_entry_days": window,
                "overlap_days": [d for d in window if d <= days[-1]],
                "uncovered_days": [d for d in window if d > days[-1]],
                "why": "require_next=True 要 fwd=close.shift(-20) 有限 ⇒ 尾部 20 个交易日无 cell",
            }
    except Exception as exc:  # noqa: BLE001
        coverage = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "_meta": {
            "script": "analysis/_diag_l1_loop_walk.py",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "as_of_list": as_of_list,
            "horizon_sessions": horizon,
            "top_n": top_n,
            "isolated_data_dir": str(data_dir),
            "linked_dirs": linked,
            "missing_dirs": missing,
            "ledger_dir": str(ledger_dir),
            "production_ledger_untouched": not (DATA / "user_data" / "decision").exists(),
        },
        "guards": guards,
        "stages": stages,
        "settle": settle_summary,
        "reconcile": {
            "fill_mode": fill_mode,
            "cards_created": sum(1 for s in stages if s.get("status") == "created"),
            "cards_exists": sum(1 for s in stages if s.get("status") == "exists"),
            "settlement_rows": len(settlements),
            "filled_rows": len(filled),
            "not_filled_rows": sum(1 for d in detail if d.get("fill_status") == "NOT_FILLED"),
            "max_abs_diff_bp": None if not diffs else round(max(diffs), 4),
            "n_diff_gt_001bp": sum(1 for d in diffs if d > 0.01),
            "mean_prod_ret_bp": None if not filled else round(
                sum(d["prod_ret_bp"] for d in filled if d.get("prod_ret_bp") is not None)
                / max(1, sum(1 for d in filled if d.get("prod_ret_bp") is not None)), 3),
            "research_vs_prod_cells": coverage,
            "detail": detail[:400],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=",".join(DEFAULT_AS_OF))
    ap.add_argument("--horizon", type=int, default=0)
    ap.add_argument("--top-n", type=int, default=l1_cohort.L1_TOP_N)
    ap.add_argument("--fill-mode", default="auto", choices=["auto", "limit", "open_only"],
                    help="auto=走 produce_card（恒为 limit）；其余走 build_card 直传")
    ap.add_argument("--no-prior", dest="allow_prior", action="store_false",
                    help="关掉先验：复现决策账本冷启动时的真实行为")
    ap.add_argument("--no-research", dest="research", action="store_false",
                    help="跳过研究侧 cohort 覆盖检查（它要读全量面板，慢）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else Path(__file__).with_name(
        f"_diag_l1_loop_walk_{args.fill_mode}_result.json")
    out = run([d.strip() for d in args.as_of.split(",") if d.strip()],
              horizon=args.horizon, top_n=args.top_n, fill_mode=args.fill_mode,
              allow_prior=args.allow_prior, research=args.research)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"== 阶段 (fill_mode={args.fill_mode}) ==")
    for s in out["stages"]:
        print(f"  {s['as_of']} entry={s.get('entry_date')} "
              f"picks={s.get('n_candidates')} status={s.get('status')} "
              f"gate={s.get('gate')} pwin={s.get('pwin_source')} "
              f"prior_w={s.get('pwin_prior_weight')} {s.get('reason') or ''}")
    print("== 守卫 ==")
    for g in out["guards"]:
        print(f"  {g}")
    print("== 结算 ==", json.dumps(out["settle"], ensure_ascii=False)[:400])
    rc = out["reconcile"]
    print("== 对账 ==")
    print(f"  fill_mode={rc['fill_mode']} 卡 created={rc['cards_created']} "
          f"exists={rc['cards_exists']} 结算行={rc['settlement_rows']} "
          f"成交={rc['filled_rows']} 未成交={rc['not_filled_rows']}")
    print(f"  生产均值={rc['mean_prod_ret_bp']}bp  max|diff|={rc['max_abs_diff_bp']}bp  "
          f"超 0.01bp 的行={rc['n_diff_gt_001bp']}")
    rv = rc.get("research_vs_prod_cells") or {}
    if rv.get("n_days"):
        print(f"  研究 cohort: {rv['n_rows']} 行 / {rv['n_days']} 日，"
              f"覆盖到 {rv['last_day']}；窗口内重叠 {len(rv['overlap_days'])} 天、"
              f"未覆盖 {len(rv['uncovered_days'])} 天")
    elif rv.get("error"):
        print("  研究 cohort 覆盖检查失败:", rv["error"])
    print(f"  产物 -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
