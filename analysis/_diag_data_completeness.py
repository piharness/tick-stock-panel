"""数据完整性独立盘点 —— 不依赖应用判据, 只看文件系统 + 交易日历。

回答三个问题, 每个都必须能给出「哪一天、哪张表、差多少」：
  1. 交易日历说该有、但目录里没有的分区（缺格）；
  2. 分区名与文件内 `date` 列不一致（串味 / 脏分区名如 date=2026-11）；
  3. 分区行数/标的数相对邻近分区的异常（残缺快照）。

只读 parquet 元数据（num_rows）与必要的单列, 不整表加载。
用法:
  python analysis/_diag_data_completeness.py
  python analysis/_diag_data_completeness.py --tables kline_daily --days 60
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis import trading_calendar as cal  # noqa: E402


def _partition_range(name: str) -> tuple[date, date] | None:
    """`date=YYYY-MM-DD` → 当天; `date=YYYY-MM` → 当月首末 (与 partitions.py 同义)。

    本脚本刻意不 import backend: 它是独立盘点工具, 后端坏了也要能跑。
    """
    if not name.startswith("date="):
        return None
    value = name[5:]
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return d, d
        year_s, month_s = value.split("-")
        if len(year_s) != 4 or len(month_s) != 2:
            return None
        year, month = int(year_s), int(month_s)
        if not 1 <= month <= 12:
            return None
        first = date(year, month, 1)
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        return first, nxt - timedelta(days=1)
    except ValueError:
        return None

DATA = ROOT / "data"

# 表 -> (需要 day 分区?, 资产类型)
TABLES = {
    "kline_daily": "stock",
    "kline_daily_enriched": "stock",
    "kline_index_daily": "index",
    "kline_index_enriched": "index",
    "kline_etf_daily": "etf",
    "kline_etf_enriched": "etf",
    "kline_minute": "stock-minute",
    "kline_etf_minute": "etf-minute",
}


def _partitions(table: str) -> list[Path]:
    root = DATA / table
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("date="))


def _read_key_stats(dirpath: Path) -> dict:
    """每个分区: 文件数、总行数、date 列 distinct、symbol 数。

    单文件读失败不提前返回 —— 一个坏文件不该让整个分区的行数/日期变成
    "只统计到它之前" 的半截值 (2026-11-05 曾因此被报成 4069 行, 实际 9641)。
    """
    files = sorted(dirpath.glob("*.parquet"))
    if not files:
        return {"files": 0, "rows": 0, "dates": [], "symbols": None}
    rows = 0
    dates: set[str] = set()
    symbols: set[str] = set()
    errors: list[str] = []
    for f in files:
        try:
            pf = pq.ParquetFile(f)
            rows += pf.metadata.num_rows
            names = pf.schema_arrow.names
            if "date" in names:
                for v in set(pf.read(columns=["date"]).column("date").to_pylist()):
                    if v is not None:
                        dates.add(str(v))
            if "symbol" in names:
                symbols.update(pf.read(columns=["symbol"]).column("symbol").to_pylist())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{f.name}: {exc}")
    out = {"files": len(files), "rows": rows, "dates": sorted(dates),
           "symbols": len(symbols) if symbols else None}
    if errors:
        out["error"] = " | ".join(errors)[:300]
    return out


def _month_daily_stats(dirpath: Path) -> dict[str, tuple[int, int]]:
    """月分区按真实交易日拆开: day → (行数, 唯一 symbol 数)。

    日K表每个 symbol 每个交易日至多一行, 故两者相等; 分开返回是为了让
    "同日重复行" 在盘点里显形 (重复行只在 (date,symbol) 层面才算错)。
    """
    out: dict[str, tuple[int, int]] = {}
    for f in sorted(dirpath.glob("*.parquet")):
        try:
            t = pq.ParquetFile(f).read(columns=["date", "symbol"])
        except Exception:  # noqa: BLE001
            continue
        days = t.column("date").to_pylist()
        syms = t.column("symbol").to_pylist()
        acc: dict[str, list] = {}
        for d, s in zip(days, syms):
            if d is None:
                continue
            acc.setdefault(str(d), []).append(s)
        for k, v in acc.items():
            prev = out.get(k, (0, 0))
            out[k] = (prev[0] + len(v), prev[1] + len(set(v)))
    return out


def survey(table: str, days: int | None) -> dict:
    parts = _partitions(table)
    out: dict = {"table": table, "n_partitions": len(parts), "issues": []}
    if not parts:
        out["issues"].append({"kind": "table_absent"})
        return out

    # --- 1. 分区名合法性 + 串味 (日分区与月分区都合法, 见 partitions.py) ---
    day_sets: dict[str, set[str]] = {}   # 分区名 -> 覆盖的真实交易日
    by_day: dict[str, dict] = {}         # 交易日 -> 当天统计 (月分区按真实日期展开)
    for p in parts:
        raw = p.name[len("date="):]
        try:
            rng = _partition_range(p.name)
        except Exception:  # noqa: BLE001
            rng = None
        if rng is None:
            st = _read_key_stats(p)
            out["issues"].append({
                "kind": "malformed_partition_name", "partition": p.name,
                "rows": st["rows"], "dates_inside": st["dates"], "files": st["files"],
            })
            continue
        st = _read_key_stats(p)
        st["partition"] = p.name
        covered = set(st["dates"]) or {rng[0].isoformat()}
        if rng[0] == rng[1] and st["dates"] and st["dates"] != [rng[0].isoformat()]:
            out["issues"].append({
                "kind": "date_mismatch", "partition": p.name,
                "dates_inside": st["dates"], "rows": st["rows"],
            })
        day_sets[p.name] = covered
        # 月分区: 行数按真实交易日拆开, 否则近 N 天窗口里整月缺位、行数判据失效
        if rng[0] == rng[1]:
            by_day[rng[0].isoformat()] = st
        else:
            for d, (rows_d, syms_d) in _month_daily_stats(p).items():
                by_day[d] = {"files": st["files"], "rows": rows_d, "dates": [d],
                             "symbols": syms_d, "partition": p.name}

    ok_days = sorted(d for ds in day_sets.values() for d in ds)
    out["first"] = ok_days[0] if ok_days else None
    out["last"] = ok_days[-1] if ok_days else None

    # 同一交易日被两个分区同时覆盖 —— 混合布局未迁移完的直接证据
    seen: dict[str, str] = {}
    for name, ds in day_sets.items():
        for d in ds:
            if d in seen and seen[d] != name:
                out["issues"].append({
                    "kind": "day_in_two_partitions", "day": d,
                    "partitions": sorted({seen[d], name}),
                })
            seen[d] = name

    # --- 2. 日历对账（只在 [lo, hi] 内, 右端缺格要靠 3 补） ---
    if ok_days:
        rep = cal.audit(ok_days)
        if rep["missing"]:
            out["issues"].append({"kind": "calendar_missing",
                                  "days": [d.isoformat() for d in rep["missing"]]})
        if rep["extra"]:
            out["issues"].append({"kind": "calendar_extra",
                                  "days": [d.isoformat() for d in rep["extra"]]})

    # --- 3. 右端: 今天是否该有分区 ---
    today = date.today()
    if cal.is_market_day(today) and ok_days and today.isoformat() > ok_days[-1]:
        gap = cal.gap_days(ok_days[-1], today)
        if gap:
            out["issues"].append({"kind": "tail_missing",
                                  "days": [d.isoformat() for d in gap]})

    # --- 4. 行数/标的数异常（近 days 个分区 vs 邻域中位数） ---
    window = ok_days[-days:] if days else ok_days
    rows_series = [(d, by_day.get(d, {}).get("rows", 0), by_day.get(d, {}).get("symbols"))
                   for d in window]
    if len(rows_series) >= 5:
        vals = sorted(r for _, r, _ in rows_series[:-1])
        med = vals[len(vals) // 2]
        if med:
            for d, r, s in rows_series:
                if r < med * 0.8:
                    out["issues"].append({
                        "kind": "row_deficit", "partition": f"date={d}",
                        "rows": r, "median_prev": med, "ratio": round(r / med, 3),
                        "symbols": s,
                    })
    out["latest_stat"] = rows_series[-1] if rows_series else None
    out["history_tail"] = rows_series[-8:]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", nargs="*", default=list(TABLES))
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = {"checked_at": date.today().isoformat(), "tables": {}}
    hard = 0
    for t in args.tables:
        r = survey(t, args.days)
        result["tables"][t] = r
        kinds = {}
        for i in r["issues"]:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        hard += sum(v for k, v in kinds.items()
                    if k in {"malformed_partition_name", "calendar_missing",
                             "tail_missing", "table_absent", "date_mismatch"})
        print(f"[{t}] parts={r.get('n_partitions')} {r.get('first')}..{r.get('last')} "
              f"issues={kinds or '{}'}")
        for i in r["issues"]:
            if i["kind"] == "row_deficit":
                print(f"    row_deficit {i['partition']} rows={i['rows']} "
                      f"median={i['median_prev']} ratio={i['ratio']} sym={i['symbols']}")
            else:
                print("   ", json.dumps(i, ensure_ascii=False)[:400])

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
