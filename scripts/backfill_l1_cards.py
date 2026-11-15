"""回填 L1 决策卡 —— 让账本第一次拥有前向证据。

## 为什么必须从外面打开这条闭环

`pwin.MIN_N = 8`：可见结算样本不足 8 条时 `produce_card` 返回
`skipped_no_evidence`，不建卡；而样本只能来自已结算的卡。空账本 ⇒ 0 样本
⇒ 永远不建卡。这是一条**自己等自己**的闭环，生产配置 `allow_prior=false`
让它永远不会自愈（实测报错原文：`n=0 < 8：只有先验，没有证据`）。

## 打开的方式不是「把 allow_prior 打开」

那样每张卡都会是常数 0.5。正确做法是**按时间顺序回放**：

| as_of | 可见结算 | p_win 来源 | 为什么 |
|---|---|---|---|
| 第 1 天 | 0 | prior | 账本空 |
| 第 2 天 | 0 | prior | 第 1 天的卡 exit == 第 2 天，`exit.date < as_of` 不成立 |
| 第 3 天起 | ≥8 | **ledger** | 第 1 天的卡已可见 |

于是回放结束时账本里有一批**用真实数据算出来的卡**，之后的自动发卡不再需要先验。
前两张的 prior 是账本的既有语义（`source=prior` 随卡封存），不是伪造。

## 它不改什么

- **picks**：全部来自 `l1_cohort.candidates(as_of)`，只用 as_of 之前的数据；
- **结算**：走 `settle()` 原路径，`fill_mode=open_only`（与 L1 研究口径一致）；
- **p_win 规则**：可见性仍是 `exit.date < as_of`，先验仍记 `source=prior`。

## 它改什么

在生产账本里写卡与结算行。每张卡的 `forecast.backfill=True`，
事后可以把「回填批次」与「实时批次」分开统计——不分开就等于让回填样本
永久混进前瞻证据里。

## 用法

    backend/.venv/Scripts/python.exe scripts/backfill_l1_cards.py            # 只打印计划
    backend/.venv/Scripts/python.exe scripts/backfill_l1_cards.py --apply    # 真写

产物：`analysis/_diag_l1_backfill_result.json`
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import ledger, l1_cohort, producer, settle  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "analysis" / "_diag_l1_backfill_result.json"

#: 回放窗口。上界必须是 today−2 或更早：`horizon=0` 时 exit == entry，
#: `as_of=11-12` 会把 entry 定到 11-13，而 11-13 的分区在盘中就有数据
#: （`quote_ts=10:42:47`）。`entry_session_state` 会拒，但不必让它去拒。
DEFAULT_AS_OF = [
    "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04",
    "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11",
]


def _visible_n(data_dir: Path, as_of: str) -> int:
    """`as_of` 当时可见的结算行数（与 `producer.resolve_picks_with_pwin` 同口径）。"""
    rows = ledger.load_settlements(data_dir)
    return sum(1 for r in rows if producer.exit_visible(r, as_of))


#: 结算要读的只读目录。`--apply` 时直接用真实 `data/`，dry-run 时逐个 junction。
LINK_DIRS = ["kline_daily", "kline_daily_enriched", "adj_factor",
             "kline_index_daily", "instruments", "regime_history"]


def _isolated_data_dir() -> tuple[Path, list[str], list[str]]:
    """临时 data_dir：账本落在 tmp，行情只读挂载。

    dry-run 必须**真的把结算跑一遍**才有意义：`p_win` 从 prior 切到 ledger
    发生在第几张卡上，取决于前几张结算出来的可见样本数，光看清单推不出来。
    而真跑就会写账本——所以写到 tmp 里。
    """
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="tsp_backfill_"))
    (tmp / "user_data").mkdir(parents=True, exist_ok=True)
    (tmp / "paper").mkdir(parents=True, exist_ok=True)
    linked, missing = [], []
    for name in LINK_DIRS:
        src = DATA / name
        if not src.is_dir():
            missing.append(name)
            continue
        dst = tmp / name
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           check=True, capture_output=True)
        else:
            dst.symlink_to(src, target_is_directory=True)
        linked.append(name)
    return tmp, linked, missing


def run(as_of_list: list[str], *, apply: bool, top_n: int = l1_cohort.L1_TOP_N,
        force: bool = False) -> dict:
    if apply:
        data_dir, linked, missing = DATA, [], []
    else:
        data_dir, linked, missing = _isolated_data_dir()

    cards_before = ledger.load_cards(data_dir)
    if cards_before and not force and apply:
        raise SystemExit(
            f"账本里已有 {len(cards_before)} 张卡：回填只应在空账本上跑一次。"
            "确实要叠加请加 --force（叠加会让同一 as_of 出现两张卡）。")

    plan: list[dict] = []
    for as_of in as_of_list:
        step: dict = {"as_of": as_of}

        # 先结算「as_of 之前建的卡」——它决定这一步能不能看到证据。
        s = settle.settle(data_dir, as_of=as_of)
        step["settle_before"] = {
            "cards": s["cards"], "written": s["written"], "pending": s["pending"],
        }

        cand = l1_cohort.candidates(data_dir, as_of, top_n=top_n)
        entry = cand.get("entry_date")
        picks = cand.get("candidates") or []
        step.update(entry_date=entry, n_candidates=len(picks))

        if not picks:
            step["status"] = "skipped_no_picks"
            plan.append(step)
            continue
        if not entry:
            step["status"] = "skipped_no_entry_date"
            plan.append(step)
            continue

        state, why = settle.entry_session_state(data_dir, entry)
        step["entry_state"] = state
        if state != "closed":
            step["status"] = f"guard_rejected_{state}"
            step["reason"] = why
            plan.append(step)
            continue

        n_vis = _visible_n(data_dir, as_of)
        use_prior = n_vis < settle.MIN_N
        step.update(visible_n=n_vis, allow_prior=use_prior)

        res = producer.produce_card(
            data_dir,
            as_of=as_of,
            picks=picks,
            horizon_sessions=0,          # L1：入场日收盘出场（回合，不是持有 1 天）
            strategy_id=l1_cohort.L1_STRATEGY_ID,
            account_id="default",
            pwin_basis="median",
            allow_prior=use_prior,
            scan_meta={
                "source": "l1_cohort",
                "backfill": True,
                "entry_date": entry,
                "gap_threshold_pct": picks[0].get("gap_threshold_pct"),
                "top_n": top_n,
            },
            fill_mode="open_only",       # 与 R5 口径一致，不是 limit
        )
        pw = res.get("pwin") or {}
        step.update(
            status=res.get("status"),
            card_id=res.get("card_id"),
            gate=(res.get("gates") or {}).get("decision"),
            pwin_source=pw.get("source"),
            pwin_n=pw.get("n"),
            pwin_prior_weight=pw.get("prior_weight"),
            reason=res.get("reason"),
        )
        plan.append(step)

    final = settle.settle(data_dir, as_of=as_of_list[-1])
    cards_after = ledger.load_cards(data_dir)
    settlements = ledger.load_settlements(data_dir)

    return {
        "_meta": {
            "script": "scripts/backfill_l1_cards.py",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "applied": apply,
            "data_dir": str(data_dir),
            "isolated": not apply,
            "linked_dirs": linked,
            "missing_dirs": missing,
            "as_of_list": as_of_list,
            "horizon_sessions": 0,
            "fill_mode": "open_only",
            "top_n": top_n,
            "ledger_dir": str(ledger.root(data_dir)),
            "cards_before": len(cards_before),
        },
        "plan": plan,
        "summary": {
            "cards_after": len(cards_after),
            "settlement_rows": len(settlements),
            "n_prior_cards": sum(1 for s in plan if s.get("pwin_source") == "prior"),
            "n_ledger_cards": sum(1 for s in plan if s.get("pwin_source") == "ledger"),
            "n_created": sum(1 for s in plan if s.get("status") == "created"),
            "n_exists": sum(1 for s in plan if s.get("status") == "exists"),
            "final_settle": final,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=",".join(DEFAULT_AS_OF))
    ap.add_argument("--top-n", type=int, default=l1_cohort.L1_TOP_N)
    ap.add_argument("--apply", action="store_true", help="真写生产账本；默认只打印计划")
    ap.add_argument("--force", action="store_true", help="账本非空也继续（危险）")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    for step_as_of in args.as_of.split(","):
        d = step_as_of.strip()
        if d:
            datetime.fromisoformat(d)  # 早失败胜过写错分期

    out = run([d.strip() for d in args.as_of.split(",") if d.strip()],
              apply=args.apply, top_n=args.top_n, force=args.force)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"== {mode} ==")
    for s in out["plan"]:
        print(f"  {s['as_of']} entry={s.get('entry_date')} picks={s.get('n_candidates')} "
              f"visible={s.get('visible_n')} prior={s.get('allow_prior')} "
              f"-> {s.get('status')} pwin={s.get('pwin_source')} {s.get('reason') or ''}")
    sm = out["summary"]
    print("== 汇总 ==")
    print(f"  卡 {sm['cards_after']} 张 / 结算 {sm['settlement_rows']} 行 / "
          f"prior {sm['n_prior_cards']} 张 / ledger {sm['n_ledger_cards']} 张")
    print(f"  产物 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
