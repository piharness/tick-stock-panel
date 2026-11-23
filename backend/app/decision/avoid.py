"""单票回避账本（TSP-DECISION-AVOID-v1）。

**为什么不复用 flat 卡**：`flat` 的语义是「整体空仓」，`build_card` 会拒绝带标的的
空仓卡。而这里要表达的是「这一只我不碰」——对象不同。塞进同一张卡会让
`flat_summary()` 把「回避单票」统计成「看空大盘」，两个不相干的判断从此混在一个
命中率里。

**为什么结算不写回账本**：账本只记录决策（open 事件），结果由行情推导。同一份
`avoid.jsonl` 在任何时候重算都必须得到同一个数，不需要信任某一次写入时的行情快照。
这与 `settlements.jsonl` 的追加式结算不同，是有意的——那边结算的是「成交」，这里
结算的是「如果没回避会怎样」，后者永远是反事实，不该固化。

口径与 `settle.settle_flats` 对齐，避免两套账各差一天：
- `entry` = 决策日之后第一个交易日（决策日停牌/非交易日则顺延）
- `exit` = `entry + horizon_sessions`
- 个股与基准都取 close→close
- `excess = 个股收益 − 基准收益`；负数 = 回避正确（这只票跑输了）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import bars
from .ledger import root

AVOID_KIND = "TSP-DECISION-AVOID-v1"
DEFAULT_BENCHMARK = "000300.SH"
DEFAULT_HORIZON = 20

_REQUIRED = ("as_of", "symbol", "ref_close", "horizon_sessions")


def avoid_path(data_dir: Path) -> Path:
    return root(Path(data_dir)) / "avoid.jsonl"


def avoid_key(as_of: str, symbol: str) -> str:
    return f"{as_of}:{symbol}"


def build_avoid(
    *,
    as_of: str,
    symbol: str,
    ref_close: float,
    rule: str,
    reason: str,
    name: str | None = None,
    mom20: float | None = None,
    turnover: float | None = None,
    horizon_sessions: int = DEFAULT_HORIZON,
    benchmark: str = DEFAULT_BENCHMARK,
    source: str = "analysis",
) -> dict[str, Any]:
    """建一条回避记录。不落盘。"""
    if horizon_sessions <= 0:
        raise ValueError("horizon_sessions 必须为正：0 会让 exit==entry，超额恒为 0")
    price = float(ref_close)
    if not price > 0:
        raise ValueError("ref_close 必须为正")
    return {
        "kind": AVOID_KIND,
        "key": avoid_key(str(as_of), str(symbol)),
        "as_of": str(as_of),
        "symbol": str(symbol),
        "name": name,
        "rule": str(rule),
        "reason": str(reason),
        "ref_close": round(price, 4),
        # 决策时点的可观测值，用于事后复核「当时看到的是什么」，不参与结算
        "mom20": None if mom20 is None else round(float(mom20), 6),
        "turnover": None if turnover is None else round(float(turnover), 4),
        "horizon_sessions": int(horizon_sessions),
        "benchmark": str(benchmark),
        "source": source,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def append_avoids(data_dir: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    path = avoid_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def scan_avoids(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """逐行读，返回 (可用记录, 问题清单)。**不抛异常**。

    与 `ledger.scan_flats` 同构：kind 不对、缺 key、key 重复、缺必填字段都是不可用。
    缺 key 尤其危险——幂等靠 key，无 key 的行每次读都会当成新样本。
    """
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"第 {lineno} 行 JSON 解析失败（{exc.msg}）")
            continue
        if not isinstance(row, dict) or row.get("kind") != AVOID_KIND:
            problems.append(f"第 {lineno} 行不是回避流水")
            continue
        key = row.get("key")
        if not key:
            problems.append(f"第 {lineno} 行缺 key")
            continue
        key = str(key)
        if key in seen:
            problems.append(f"第 {lineno} 行 key 重复（同第 {seen[key]} 行）")
            continue
        missing = [f for f in _REQUIRED if row.get(f) is None]
        if missing:
            problems.append(f"第 {lineno} 行缺字段 {','.join(missing)}")
            continue
        seen[key] = lineno
        rows.append(row)
    return rows, problems


def load_avoids(data_dir: Path) -> list[dict[str, Any]]:
    path = avoid_path(data_dir)
    if not path.exists():
        return []
    rows, _problems = scan_avoids(path)
    return rows


def _session_index(calendar: list[str], day: str) -> int | None:
    """决策日在日历里就取次一日；不在（非交易日/停牌）就顺延到之后第一个交易日。"""
    if not calendar:
        return None
    if day in calendar:
        return calendar.index(day) + 1
    return next((i for i, d in enumerate(calendar) if d > day), None)


def _close_of(data_dir: Path, day: str, symbol: str, *, table: str) -> float | None:
    rows = bars.bar_map(data_dir, day, [symbol], table=table)
    if not rows:
        return None
    try:
        return float(rows[symbol]["close"])
    except (KeyError, TypeError, ValueError):
        return None


def settle_avoids(data_dir: Path, *, as_of: str | None = None) -> dict[str, Any]:
    """结算所有到期的回避记录。纯函数：不写盘，重复调用结果相同。

    `as_of` 用于「假装站在某一天」看当时的成绩（历史回放），超过该日的记录不参与。
    """
    data_path = Path(data_dir)
    rows = load_avoids(data_path)
    calendar = bars.sessions(data_path)
    out: list[dict[str, Any]] = []
    pending = {"not_due": 0, "price_missing": 0, "benchmark_missing": 0}

    if not calendar:
        return {"records": len(rows), "settled": 0, "pending": pending,
                "pending_total": 0, "rows": [], "error": "日历为空"}

    for row in rows:
        day = str(row["as_of"])
        if as_of is not None and day > str(as_of):
            continue
        entry_i = _session_index(calendar, day)
        if entry_i is None:
            pending["not_due"] += 1
            continue
        horizon = int(row["horizon_sessions"])
        exit_i = entry_i + horizon
        if entry_i >= len(calendar) or exit_i >= len(calendar):
            pending["not_due"] += 1
            continue
        entry, exit_day = calendar[entry_i], calendar[exit_i]

        symbol = str(row["symbol"])
        p0 = _close_of(data_path, entry, symbol, table="kline_daily_enriched")
        p1 = _close_of(data_path, exit_day, symbol, table="kline_daily_enriched")
        if p0 is None or p1 is None or p0 <= 0:
            pending["price_missing"] += 1
            continue
        benchmark = str(row.get("benchmark") or DEFAULT_BENCHMARK)
        b0 = _close_of(data_path, entry, benchmark, table=bars.INDEX_ENRICHED)
        b1 = _close_of(data_path, exit_day, benchmark, table=bars.INDEX_ENRICHED)
        if b0 is None or b1 is None or b0 <= 0:
            pending["benchmark_missing"] += 1
            continue

        ret = p1 / p0 - 1.0
        bench = b1 / b0 - 1.0
        out.append({
            "key": row["key"],
            "as_of": day,
            "symbol": symbol,
            "name": row.get("name"),
            "rule": row.get("rule"),
            "entry": entry,
            "exit": exit_day,
            "ref_close": row.get("ref_close"),
            "entry_close": round(p0, 4),
            "exit_close": round(p1, 4),
            "ret": round(ret, 6),
            "benchmark_ret": round(bench, 6),
            # 负数 = 这只票跑输基准 = 回避正确；正数 = 踏空
            "excess": round(ret - bench, 6),
        })

    return {
        "records": len(rows),
        "settled": len(out),
        "pending": pending,
        "pending_total": sum(pending.values()),
        "rows": out,
    }


def avoid_summary(data_dir: Path, *, as_of: str | None = None) -> dict[str, Any]:
    """成绩单。与 `flat_summary` 的 avoided/missed 并列口径对齐。

    - `avoided`：excess < 0，回避躲开了相对下跌
    - `missed` ：excess > 0，回避踏空了相对上涨。它不是失败，是回避的成本。
    - `net_pct`：躲开的 − 踏空的。正数才说明这套回避是对的。
    """
    settled = settle_avoids(data_dir, as_of=as_of)
    rows = settled["rows"]
    avoided = [r for r in rows if r["excess"] < 0]
    missed = [r for r in rows if r["excess"] > 0]
    avoided_sum = sum(-r["excess"] for r in avoided)
    missed_sum = sum(-r["excess"] for r in missed)
    n = len(rows)
    return {
        "records": settled["records"],
        "settled": n,
        "pending": settled["pending_total"],
        "avoided": len(avoided),
        "missed": len(missed),
        "hit_rate": round(len(avoided) / n, 4) if n else None,
        "avoided_sum_pct": round(avoided_sum * 100, 2),
        "missed_sum_pct": round(missed_sum * 100, 2),
        "net_pct": round((avoided_sum + missed_sum) * 100, 2),
        "mean_excess_pct": round(sum(r["excess"] for r in rows) / n * 100, 2) if n else None,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 扫描：把「20 日涨幅 ≥50% 且当日换手 ≥15%」翻译成回避记录
# ---------------------------------------------------------------------------

RULE_HIGH_MOM = "high_mom_high_turn"
RULE_VERSION = "v1"
MOM_THRESHOLD = 0.50
TURN_THRESHOLD = 15.0
LOOKBACK_SESSIONS = 21


def _day_panel(data_dir: Path, day: str) -> dict[str, dict[str, float]]:
    """读单日 enriched 分区，只要 (close, turnover_rate)。

    不走 `bars.bar_map`：那条路径为了性能裁掉了 turnover_rate，拿不到换手率会让
    扫描静默返回 0 命中（看着像「今天没有风险股」，其实是字段没读）。
    """
    import pyarrow.parquet as pq

    path = Path(data_dir) / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    if not path.exists():
        return {}
    table = pq.ParquetFile(path).read(columns=["symbol", "close", "turnover_rate"])
    out: dict[str, dict[str, float]] = {}
    for sym, close, turn in zip(table.column("symbol").to_pylist(),
                                table.column("close").to_pylist(),
                                table.column("turnover_rate").to_pylist()):
        if close is None or turn is None:
            continue
        out[str(sym)] = {"close": float(close), "turnover_rate": float(turn)}
    return out


def scan_and_record(
    data_dir: Path,
    *,
    as_of: str,
    mom_threshold: float = MOM_THRESHOLD,
    turn_threshold: float = TURN_THRESHOLD,
    dry_run: bool = False,
    source: str = "scan_and_record",
) -> dict[str, Any]:
    """扫描 `as_of` 当日全市场，把触发的票写进回避账本。

    判据只用 `as_of` 收盘可观测的量（21 个交易日前收盘、当日换手），不含未来信息。
    同一 `as_of` 重复运行不会写重：已存在的 key 会被跳过（账本仍只追加一次）。
    盘中调用会拿到未定盘的换手率——调用方必须确认 `as_of` 已收盘。
    """
    data_path = Path(data_dir)
    calendar = bars.sessions(data_path)
    if as_of not in calendar:
        return {"as_of": as_of, "status": "not_a_session", "written": 0, "hits": []}
    i = calendar.index(as_of)
    if i < LOOKBACK_SESSIONS:
        return {"as_of": as_of, "status": "insufficient_history", "written": 0, "hits": []}
    base_day = calendar[i - LOOKBACK_SESSIONS]

    today = _day_panel(data_path, as_of)
    base = _day_panel(data_path, base_day)
    if not today or not base:
        return {"as_of": as_of, "status": "no_bars", "written": 0, "hits": []}

    hits: list[dict[str, Any]] = []
    for sym, row in today.items():
        b = base.get(sym)
        if not b:
            continue
        c0, c1, turn = b["close"], row["close"], row["turnover_rate"]
        if c0 <= 0:
            continue
        mom20 = c1 / c0 - 1.0
        if mom20 >= mom_threshold and turn >= turn_threshold:
            hits.append({
                "symbol": sym,
                "ref_close": c1,
                "mom20": mom20,
                "turnover": turn,
            })

    existing = {r["key"] for r in load_avoids(data_path)}
    fresh = []
    for h in hits:
        key = avoid_key(as_of, h["symbol"])
        if key in existing:
            continue
        fresh.append(build_avoid(
            as_of=as_of,
            symbol=h["symbol"],
            name=None,
            ref_close=h["ref_close"],
            rule=f"{RULE_HIGH_MOM}:{RULE_VERSION}",
            reason=(f"20日涨幅 {h['mom20']*100:.1f}% ≥ {mom_threshold*100:.0f}% "
                    f"且当日换手 {h['turnover']:.1f}% ≥ {turn_threshold:.0f}%"),
            mom20=h["mom20"],
            turnover=h["turnover"],
            horizon_sessions=DEFAULT_HORIZON,
            source=source,
        ))

    written = 0 if dry_run else append_avoids(data_path, fresh)
    return {
        "as_of": as_of,
        "status": "ok",
        "scanned": len(today),
        "hits": sorted(hits, key=lambda x: -x["mom20"]),
        "new": len(fresh),
        "written": written,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="单票回避账本（TSP-DECISION-AVOID-v1）")
    p.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[3] / "data"))
    p.add_argument("--as-of", default=None, help="扫描日；默认最后一个交易日")
    p.add_argument("--scan", action="store_true", help="扫描并写入回避记录")
    p.add_argument("--settle", action="store_true", help="结算到期记录")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    data_dir = Path(a.data_dir)

    if a.scan:
        day = a.as_of or (bars.latest_session(data_dir) or "")
        print(json.dumps(scan_and_record(data_dir, as_of=day, dry_run=a.dry_run),
                         ensure_ascii=False, indent=2))
    if a.settle or a.summary:
        print(json.dumps(avoid_summary(data_dir, as_of=a.as_of), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
