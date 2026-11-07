"""决策账本的端到端回放：produce -> settle -> 读回，并用**第二条代码路径**重算结算价。

## 为什么要有这个脚本

账本上线以来，`produce -> settle -> score` 这条链只在人造的 polars 分区上跑过
（`test_decision_producer.py` / `test_decision_settle_concurrency.py` 的种子数据）。
人造数据证明的是「代码按我理解的口径走」，不证明「口径与真实行情一致」：撮合价、
复权因子、全市场中位数三处只要有一处读错列，种子数据不会红，真实账本会。

这个脚本把整条链放到**真实 parquet** 上跑一遍，然后**不复用** `app.decision.bars`
的任何一行代码，直接从分区文件重算每一笔的成交价、出场价、收益、全市场中位数，
逐项比对。

    sandbox/                     临时目录，跑完整份删掉
      kline_daily -> 真实分区    （软链接，不复制）
      kline_daily_enriched ->
      kline_index_enriched ->
      user_data/                 真账本根目录（空 ⇒ 冷启动）

真实账本 `data/user_data/decision` 全程不被触碰：`settle()` 与 `produce()`
都只接受传入的 `data_dir`。

## 两条独立路径各自负责什么

- 主路径（被测对象）：`producer.produce_card` → `settle.settle` →
  `ledger.load_settlements`，走 `bars.py` 的 `bar_map / adj_factor / day_rows`。
- 第二路径（本脚本）：`pl.read_parquet` 直接读 `date=<day>/part.parquet`，
  复权用 `adj_factor/all.parquet` 的**事件表**逐步累乘，不碰 enriched 的
  `close` 列；中位数也由不复权价 + 事件表重算。

第二条路径复用的是**列语义**，不是代码：`ret = raw_exit · Π(factor) / fill − 1`。
两边算错同一个数需要两份实现同时错在同一个地方——这是它比单路径强的地方，
也是它的边界：它证明的是「与不复权价 + 除权事件自洽」，不是「与交易所对账」。

## 用法

    python scripts/verify_decision_replay.py
    python scripts/verify_decision_replay.py --json out.json --as-of 2026-10-27
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import ledger, producer, settle  # noqa: E402

#: 只读挂进沙盒的四张表。缺任何一张，结算路径上就有一段读不到（`sessions` 用
#: `kline_daily`、撮合与中位数用 `kline_daily_enriched`、基准用 index）。
#:
#: `adj_factor` 必须挂进来，否则收益会**静默**退回复权列口径：事件表缺失时
#: `bars.ex_factor_product` 返回 None，结算如实降级成 `ADJUSTED` 并标注——降级
#: 本身是对的，错的是回放看不见它降级了。少挂一张表就少测一条路径，而且是最
#: 该测的那条。
TABLES = ("kline_daily", "kline_daily_enriched", "kline_index_enriched", "adj_factor")
ENRICHED = "kline_daily_enriched"
#: 判定阈值：一笔结算的收益与第二路径差多少算不一致。
TOL_BP = 1.0


# ---------------------------------------------------------------------------
# 沙盒
# ---------------------------------------------------------------------------


def build_sandbox(data_root: Path) -> Path:
    """软链接真实分区 + 一个空的 `user_data`。不复制任何 parquet。"""
    missing = [t for t in TABLES if not (data_root / t).is_dir()]
    if missing:
        raise SystemExit(f"缺分区目录：{missing}（先跑盘后管道同步日K）")
    tmp = Path(tempfile.mkdtemp(prefix="dc-replay-"))
    for table in TABLES:
        os.symlink((data_root / table).resolve(), tmp / table, target_is_directory=True)
    (tmp / "user_data").mkdir()
    return tmp


# ---------------------------------------------------------------------------
# 第二路径：直接从分区文件重算（不 import bars）
# ---------------------------------------------------------------------------


def sessions_of(data_root: Path) -> list[str]:
    """交易日 = 已落盘的分区目录名。不查库、不问「今天减一天」。"""
    base = data_root / "kline_daily"
    out = []
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("date="):
            out.append(child.name.split("=", 1)[1])
    return sorted(out)


def day_frame(data_root: Path, day: str, columns: list[str]) -> pl.DataFrame:
    part = data_root / ENRICHED / f"date={day}" / "part.parquet"
    if not part.exists():
        return pl.DataFrame()
    df = pl.read_parquet(part)
    keep = [c for c in columns if c in df.columns]
    return df.select(keep)


def factor_product(data_root: Path, symbols: list[str],
                   entry: str, exit_: str) -> dict[str, float]:
    """`entry` 之后、`exit` 当天及之前的事件因子累乘（不复权价 → 可比价）。

    除权事件落在 `(entry, exit]` 上时，区间内的不复权收益要把这段因子乘回去；
    落在区间外的事件对本次比较没有影响（它同时缩放两端）。
    """
    return {s: 1.0 for s in symbols} | _factor_events(data_root, symbols, entry, exit_)


def _factor_events(data_root: Path, symbols: list[str],
                   entry: str, exit_: str) -> dict[str, float]:
    path = data_root / "adj_factor" / "all.parquet"
    out: dict[str, float] = {}
    if not path.exists():
        return out
    ev = pl.read_parquet(path).filter(pl.col("symbol").is_in(symbols))
    if ev.is_empty():
        return out
    ev = ev.with_columns(pl.col("trade_date").cast(pl.Utf8))
    ev = ev.filter((pl.col("trade_date") > entry) & (pl.col("trade_date") <= exit_))
    if ev.is_empty():
        return out
    for sym, val in ev.group_by("symbol").agg(pl.col("ex_factor").product()).iter_rows():
        out[str(sym)] = float(val)
    return out


def symbols_with_events(data_root: Path, entry: str, exit_: str) -> set[str]:
    path = data_root / "adj_factor" / "all.parquet"
    if not path.exists():
        return set()
    ev = (pl.read_parquet(path)
          .with_columns(pl.col("trade_date").cast(pl.Utf8))
          .filter((pl.col("trade_date") > entry) & (pl.col("trade_date") <= exit_)
                  & (pl.col("ex_factor") != 1.0)))
    return {str(s) for s in ev["symbol"].to_list()}


def adjustment_check(data_root: Path, days: list[str], entry: str,
                     exit_: str) -> dict[str, Any]:
    """窗口内的除权事件有没有被 enriched 的 `close` 序列吸收。

    这是回放顺带量出来的**数据新鲜度**读数，不是账本的口径问题：`close` 是
    「按最新因子折算」的复权价，一个新事件到达后，事件之前的所有日子都要重新
    折算。没重算的那些日子，`close/raw_close` 在除权日不会变——于是
    `exit_close / entry_close` 退化成不复权价收益，**整段分红被丢掉**。

    对结算的后果是硬的：这一行一旦写入就被 `done` 键固化，收益再也改不回来。
    所以这里把它数出来，而不是让回放"恰好"挑到没有事件的标的就报绿。
    """
    path = data_root / "adj_factor" / "all.parquet"
    if not path.exists():
        return {"checked": 0, "baked": 0, "unbaked": 0, "unbaked_symbols": [],
                "detail": "没有复权因子表：无法核对"}
    ev = (pl.read_parquet(path)
          .with_columns(pl.col("trade_date").cast(pl.Utf8))
          .filter((pl.col("trade_date") > entry) & (pl.col("trade_date") <= exit_)
                  & (pl.col("ex_factor") != 1.0)))
    if ev.is_empty():
        return {"checked": 0, "baked": 0, "unbaked": 0, "unbaked_symbols": [],
                "detail": "窗口内没有除权事件"}

    syms = sorted({str(s) for s in ev["symbol"].to_list()})
    ratio: dict[str, dict[str, float]] = {s: {} for s in syms}
    for day in days:
        part = data_root / ENRICHED / f"date={day}" / "part.parquet"
        if not part.exists():
            continue
        df = (pl.read_parquet(part).filter(pl.col("symbol").is_in(syms))
              .select(["symbol", "close", "raw_close"]))
        for sym, c, r in df.iter_rows():
            if c and r and float(c) > 0 and float(r) > 0:
                ratio[str(sym)][day] = float(c) / float(r)

    pos = {d: i for i, d in enumerate(days)}
    baked, unbaked, skipped = 0, [], 0
    for sym, exdate, fac in ev.select(["symbol", "trade_date", "ex_factor"]).iter_rows():
        sym, exdate = str(sym), str(exdate)
        i = pos.get(exdate)
        series = ratio.get(sym, {})
        if i is None or i == 0 or exdate not in series or days[i - 1] not in series:
            skipped += 1
            continue
        if abs(series[exdate] / series[days[i - 1]] - 1.0) > 1e-6:
            baked += 1
        else:
            unbaked.append({"symbol": sym, "ex_date": exdate,
                            "dropped_pct": round((float(fac) - 1.0) * 100, 4)})
    return {
        "checked": baked + len(unbaked),
        "baked": baked,
        "unbaked": len(unbaked),
        "unbaked_symbols": unbaked[:10],
        "skipped": skipped,
        "detail": ("窗口内除权事件已并入复权序列" if not unbaked else
                   f"{len(unbaked)} 个除权事件没有并入 enriched 序列："
                   f"这些标的在窗口内的收益不含分红"),
    }


def median_from_raw(data_root: Path, entry: str, exit_: str) -> tuple[float | None, int]:
    """全市场收益中位数，由不复权价 + 事件因子重算。

    只保留两端都有正价、且窗口内因子已算清的标的；样本数一并返回，因为
    `settle._median_ret` 在样本不足时给 None，两边要能对齐「为什么是 None」。
    """
    a = day_frame(data_root, entry, ["symbol", "raw_close"])
    b = day_frame(data_root, exit_, ["symbol", "raw_close"])
    if a.is_empty() or b.is_empty():
        return None, 0
    j = a.join(b, on="symbol", suffix="_end")
    j = j.filter((pl.col("raw_close") > 0) & (pl.col("raw_close_end") > 0))
    if j.height < 100:
        return None, j.height
    syms = j["symbol"].to_list()
    factors = factor_product(data_root, syms, entry, exit_)
    fac = pl.Series("fac", [factors.get(str(s), 1.0) for s in syms], dtype=pl.Float64)
    j = j.with_columns(fac)
    ret = (j["raw_close_end"] * j["fac"] / j["raw_close"] - 1.0)
    return float(ret.median()), j.height


def recompute_pick(data_root: Path, calendar: list[str], *, as_of: str, symbol: str,
                   limit: float, horizon: int) -> dict[str, Any]:
    """一笔 pick 的成交价 / 出场价 / 收益，全部从原始分区重算。"""
    future = [i for i, d in enumerate(calendar) if d > as_of]
    if not future:
        return {"symbol": symbol, "error": "as_of 之后没有交易日"}
    entry_i = future[0]
    entry = calendar[entry_i]
    if entry_i + horizon >= len(calendar):
        return {"symbol": symbol, "error": "尚未到出场日"}
    exit_day = calendar[entry_i + horizon]

    row = day_frame(data_root, entry, ["symbol", "open", "low", "close", "raw_close"])
    row = row.filter(pl.col("symbol") == symbol)
    if row.is_empty():
        return {"symbol": symbol, "error": "入场日无行情"}
    r = row.row(0, named=True)
    # 不复权开盘：enriched 只落 raw_close，OHLC 同日同因子 ⇒ open·raw_close/close。
    raw_open = float(r["open"]) * float(r["raw_close"]) / float(r["close"])

    if raw_open <= limit + 1e-9:
        fill, basis = raw_open, "OPEN"
    elif r["low"] is not None and float(r["low"]) * float(r["raw_close"]) / float(r["close"]) <= limit + 1e-9:
        fill, basis = float(limit), "LIMIT"
    else:
        return {"symbol": symbol, "fill_basis": "NOT_FILLED", "fill_price": None,
                "raw_open": raw_open, "entry": entry, "exit": exit_day}

    ex = day_frame(data_root, exit_day, ["symbol", "raw_close"])
    ex = ex.filter(pl.col("symbol") == symbol)
    if ex.is_empty():
        return {"symbol": symbol, "error": "出场日无行情"}
    raw_exit = float(ex.row(0, named=True)["raw_close"])
    fac = factor_product(data_root, [symbol], entry, exit_day)[symbol]
    ret = raw_exit * fac / fill - 1.0
    return {
        "symbol": symbol,
        "entry": entry,
        "exit": exit_day,
        "fill_basis": basis,
        "fill_price": round(fill, 6),
        "raw_open": round(raw_open, 6),
        "raw_exit": round(raw_exit, 6),
        "exit_factor": round(fac, 8),
        "ret": ret,
    }


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


def _choose_picks(data_root: Path, as_of: str, calendar: list[str],
                  horizon: int, n: int, blocked: set[str] | None = None,
                  wanted: list[str] | None = None
                  ) -> list[dict[str, Any]]:
    """挑 n 个标的，限价 = as_of 收盘（与 `producer` 同口径）。

    刻意在候选里**均匀取**而不是取前 n 个：按代码排序取头部只会拿到同一类
    标的（例如全部 000xxx），撮合分支可能整批落在同一支上。

    `blocked` 里的标的在持有窗口内有除权事件，且该事件是否已并入复权序列
    本就由 `adjustment_check` 单独度量——把它们留在价格比对里，会让
    「结算价算错了」和「参考序列没重算」两种成因混在同一个 diff 上。

    `wanted` 跳过挑选，直接指定标的。它是**反向**用途：均匀抽样大概率抽不到
    窗口内有未并入事件的那几只，而「抽样没抽到」会被读成「没问题」。
    """
    day = day_frame(data_root, as_of, ["symbol", "open", "low", "close", "raw_close"])
    ok = day.filter((pl.col("raw_close") > 0) & (pl.col("open") > 0) & (pl.col("low") > 0))
    if blocked:
        ok = ok.filter(~pl.col("symbol").is_in(sorted(blocked)))
    if wanted:
        have = set(ok["symbol"].to_list())
        missing = [s for s in wanted if s not in have]
        if missing:
            raise SystemExit(f"指定标的在 {as_of} 无可用行情（停牌或价非正）：{missing}")
        chosen = list(wanted)
    else:
        pool = sorted(ok["symbol"].to_list())
        if len(pool) < n:
            raise SystemExit(f"{as_of} 可用标的只有 {len(pool)} 个，不足 {n}")
        step = max(1, len(pool) // n)
        chosen = [pool[i * step] for i in range(n)]

    # 指定标的时允许部分被剔除（停牌、开盘即高于限价）：窗口内未并入事件的那几只
    # 本来就可能当天买不到。少一两只不影响「事件因子有没有进收益」这个比对，
    # 但一只都不剩就没有比对对象了。
    need = 1 if wanted else n
    picks, rejected = [], []
    for sym in chosen:
        limit = float(ok.filter(pl.col("symbol") == sym).row(0, named=True)["raw_close"])
        probe = recompute_pick(data_root, calendar, as_of=as_of, symbol=sym,
                               limit=limit, horizon=horizon)
        if probe.get("error") or probe.get("fill_basis") == "NOT_FILLED":
            rejected.append({"symbol": sym, "why": probe.get("error") or "not_filled"})
            continue
        picks.append({"symbol": sym, "limit_price": limit})
    if len(picks) < need:
        raise SystemExit(f"可用标的不足：{need - len(picks)} 个被剔除 {rejected}")
    return picks


def replay(data_root: Path, *, as_of: str | None = None, horizon: int = 5,
           n_picks: int = 3, block_events: bool = True,
           symbols: list[str] | None = None) -> dict[str, Any]:
    data_root = Path(data_root)
    sandbox = build_sandbox(data_root)
    try:
        calendar = sessions_of(data_root)
        if as_of is None:
            # 留出 horizon 个交易日给出场，再留两天缓冲（避免挑到最后一个分区）。
            as_of = calendar[-(horizon + 3)]
        as_of_i = calendar.index(as_of)
        entry = calendar[as_of_i + 1]
        exit_day = calendar[as_of_i + 1 + horizon]
        blocked = symbols_with_events(data_root, entry, exit_day) if block_events else set()
        picks = _choose_picks(data_root, as_of, calendar, horizon, n_picks, blocked, symbols)
        n_picks = len(picks)

        # (1) 冷启动：没有证据就不发卡。这是账本第一次运行时的正确行为。
        cold = producer.produce_card(
            sandbox, as_of=as_of, picks=[dict(p) for p in picks],
            horizon_sessions=horizon, strategy_id="replay-verify")

        # (2) 显式 allow_prior：建卡 -> 结算 -> 读回。
        made = producer.produce_card(
            sandbox, as_of=as_of, picks=picks,
            horizon_sessions=horizon, strategy_id="replay-verify", allow_prior=True)
        summary = settle.settle(sandbox) if made.get("status") == "created" else {}
        rows = ledger.load_settlements(sandbox)

        # (3) 第二路径重算。
        med_recomputed, med_n = median_from_raw(data_root, entry, exit_day)
        adj = adjustment_check(data_root, calendar[as_of_i + 1: as_of_i + 2 + horizon],
                               entry, exit_day)
        by_symbol = {str(r["symbol"]): r for r in rows}
        details = []
        for pick in picks:
            sym = pick["symbol"]
            mine = recompute_pick(data_root, calendar, as_of=as_of, symbol=sym,
                                  limit=float(pick["limit_price"]), horizon=horizon)
            rec = by_symbol.get(sym)
            item: dict[str, Any] = {"symbol": sym, "recomputed": mine,
                                    "settled": None, "diff": {}}
            if rec is not None and "ret" in mine:
                ret_stored = float(rec["exit"]["ret"])
                d_ret = abs(ret_stored - mine["ret"])
                d_fill = abs(float(rec["fill"]["price"]) - mine["fill_price"])
                med_stored = rec.get("median_ret")
                d_med = (abs(float(med_stored) - float(med_recomputed))
                         if med_stored is not None and med_recomputed is not None else None)
                item["settled"] = {
                    "fill_basis": rec["fill"]["basis"],
                    "fill_price": rec["fill"]["price"],
                    "ret": ret_stored,
                    "median_ret": med_stored,
                    "outcome": rec.get("outcome"),
                    "outcome_med": rec.get("outcome_med"),
                }
                item["diff"] = {
                    "ret_bp": round(d_ret * 1e4, 6),
                    "fill_bp": round(d_fill * 1e4, 6),
                    "median_bp": None if d_med is None else round(d_med * 1e4, 6),
                    "outcome_match": rec.get("outcome") == (1 if mine["ret"] > 0 else 0),
                    "outcome_med_match": (
                        None if med_recomputed is None or rec.get("outcome_med") is None
                        else rec.get("outcome_med") == (1 if mine["ret"] > med_recomputed else 0)),
                }
            details.append(item)

        ret_bps = [d["diff"]["ret_bp"] for d in details if d["diff"].get("ret_bp") is not None]
        med_bps = [d["diff"]["median_bp"] for d in details if d["diff"].get("median_bp") is not None]
        all_ok = (
            cold.get("status") == "skipped_no_evidence"
            and made.get("status") == "created"
            and summary.get("written", 0) == n_picks
            and summary.get("median_covered") == summary.get("written")
            and len(rows) == n_picks
            and len(ret_bps) == n_picks and max(ret_bps) < TOL_BP
            and all(d["diff"].get("outcome_match") for d in details)
            and all(d["diff"].get("outcome_med_match") in (True, None) for d in details)
        )
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_root": str(data_root),
            "as_of": as_of,
            "horizon": horizon,
            #: 抽样是否避开了窗口内有除权事件的标的。`False` 才覆盖「事件表进收益」
            #: 这条路径；`True` 的 0 bp 只说明没事件的标的算得对。两个都要能跑。
            "block_events": block_events,
            "symbols": list(symbols) if symbols else None,
            "cold_start_status": cold.get("status"),
            "produce_status": made.get("status"),
            "pwin_source": (made.get("pwin") or {}).get("source"),
            "settle": summary,
            "read_back": len(rows),
            "median_pool": med_n,
            "median_recomputed": med_recomputed,
            #: 窗口内除权事件有没有并入复权序列。**不计入 `ok`**：它是数据新鲜度
            #: 读数，不是账本口径。但它决定「这笔结算的收益含不含分红」，
            #: 所以必须与结论一起出现，不能让回放挑到无事件的标的就报绿。
            "adjustment_check": adj,
            "health_after": ledger.ledger_health(sandbox)["status"],
            "picks": details,
            "max_ret_diff_bp": max(ret_bps) if ret_bps else None,
            "max_median_diff_bp": max(med_bps) if med_bps else None,
            "tolerance_bp": TOL_BP,
            "ok": bool(all_ok),
        }
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="决策账本端到端回放 + 结算价第二路径重算")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--picks", type=int, default=3)
    ap.add_argument("--symbols", default=None,
                    help="逗号分隔：跳过抽样，直接指定标的（覆盖有事件的路径）")
    ap.add_argument("--include-events", action="store_true",
                    help="不把窗口内有除权事件的标的排除在抽样之外")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    rep = replay(args.data_dir, as_of=args.as_of, horizon=args.horizon,
                 n_picks=args.picks,
                 block_events=not args.include_events,
                 symbols=[s.strip() for s in args.symbols.split(",") if s.strip()]
                 if args.symbols else None)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "picks"},
                     ensure_ascii=False, indent=2))
    for p in rep["picks"]:
        print(f"  {p['symbol']}: {p['recomputed'].get('fill_basis')} "
              f"{p['diff'].get('fill_bp')}bp / ret {p['diff'].get('ret_bp')}bp "
              f"/ median {p['diff'].get('median_bp')}bp "
              f"/ outcome {'=' if p['diff'].get('outcome_match') else '!='}")
    adj = rep.get("adjustment_check") or {}
    if adj.get("unbaked"):
        print(f"\n! 窗口内 {adj['unbaked']}/{adj['checked']} 个除权事件未并入复权序列："
              f"{[u['symbol'] for u in adj['unbaked_symbols']]}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
