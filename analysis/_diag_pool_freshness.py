"""票池「新鲜度」的三个读数：换手、市场宽度、除权。

## 为什么这几个数要落盘

2026-11-12 的结论是「名单也是每日变量」，但落到口径上只有一个 16/20 的 anecdotes，
而且那个 16/20 是在 **11:30 盘中快照**上算的 —— 它自己就站在被自己闸门拒绝的那一侧。
一个不能复算的「16/20」不能进任何验收口径，所以这里把它换成三个可复算的读数：

1. **相邻日换手**：`|pool[d] - pool[next]|`，**只统计自然日跨度 == 1 的对**。
   这决定「用昨天的名单」每天错几只。中位是几，就是几 —— 不是「偶尔」。
   跨周末/长假的两格（实测 13/59）不是「一天」，混进来就是拿 3 天的换手当 1 天。
2. **北交所票-日**：池子里出现过多少只 `BJ`。这决定 `qq_symbol` 缺 `bj` 分支的代价
   是「理论问题」还是「已经发生过」。
3. **入场日除权票-日**：这决定 `check` 的「帧前收 ≠ 日线收盘」告警里，
   「除权」这个病因一年会喊几次 —— 喊得越少，越不能拿它当默认解释。

## 三条口径（写死在这里，不写在注释里）

- 只统计 `candidates()` 自己声明的 `entry_date` 存在的相邻对；最后一格若还在盘中，
  它算出的池子**不是**次日名单，`dropped_intraday` 会记成 true 并把它排除。
- 换手 = 前一日的池子里**不在**次日池子的只数（出），不是并集大小。
- 除权只认 `data/adj_factor/all.parquet` 的 `(symbol, trade_date)`，不猜。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUT = Path(__file__).with_name("_diag_pool_freshness_result.json")
WINDOW = 60
CLOSE_CUTOFF_H = 15
CLOSE_CUTOFF_M = 30


def _stat(xs: list[int]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    return {"n": len(s), "min": s[0], "p25": s[len(s) // 4],
            "p50": s[len(s) // 2], "p75": s[3 * len(s) // 4], "max": s[-1],
            "mean": round(sum(s) / len(s), 3), "hist": dict(sorted(Counter(s).items()))}


def main() -> int:
    import polars as pl

    from app.decision.l1_cohort import _panel, candidates

    df, m = _panel(ROOT / "data")
    dates = m.dates
    # 最后一格是否定版：判据与 `auction_burst._bar_is_final` 同一条线（15:30），
    # 这里只读 enriched 的 quote_ts 最大值，与 `auction_order._enriched_quote_ts` 同口径。
    last = dates[-1]
    qts = None
    base = ROOT / "data" / "kline_daily_enriched" / f"date={last}"
    fs = sorted(base.glob("*.parquet"))
    if fs:
        import pyarrow.parquet as pq
        vals = [v for v in pq.ParquetFile(fs[0]).read(columns=["quote_ts"])
                .column("quote_ts").to_pylist() if v is not None]
        qts = max(vals) if vals else None
    last_final = True
    if qts is not None:
        t = datetime.fromtimestamp(qts / 1000)
        last_final = (t.hour, t.minute) >= (CLOSE_CUTOFF_H, CLOSE_CUTOFF_M)

    end = len(dates) if last_final else len(dates) - 1
    lo = dates[max(0, end - WINDOW):end]

    pools = {d: [r["symbol"] for r in candidates(ROOT / "data", d)["candidates"]] for d in lo}
    pairs = []
    for a, b in zip(lo, lo[1:]):
        if not (pools[a] and pools[b]):
            continue
        out = sorted(set(pools[a]) - set(pools[b]))
        pairs.append({"from": a, "to": b,
                      "gap_days": (date.fromisoformat(b) - date.fromisoformat(a)).days,
                      "n_out": len(out), "out": out})
    # 「相邻日换手」的自变量是**相邻**，而 `lo` 是交易日序列：相邻两格之间可能跨
    # 周末、跨长假。混在同一个分布里，就是拿「3 天的换手」回答「1 天的换手」。
    # 2026-11-12 实测 n=59：gap==1 只有 46 对，11 对跨周末（`from` 全是周五），
    # 2 对跨国庆（09-24→09-28 / 09-30→10-08，其中后者 n_out=6）。
    # 这正是 `auction_order` 那个 P0 的同构复发 —— 「相邻」在两个文件里都没有定义点。
    adj = [p for p in pairs if p["gap_days"] == 1]
    churn = [p["n_out"] for p in adj]
    by_gap: dict[str, list[int]] = {}
    for p in pairs:
        by_gap.setdefault(str(p["gap_days"]), []).append(p["n_out"])

    n_bj = sum(sum(1 for s in pools[d] if s.endswith(".BJ")) for d in lo)
    days_bj = sum(1 for d in lo if any(s.endswith(".BJ") for s in pools[d]))

    af = pl.read_parquet(ROOT / "data" / "adj_factor" / "all.parquet")
    ex = {(str(s), str(t)[:10]) for s, t in zip(af["symbol"].to_list(),
                                               af["trade_date"].to_list())}
    ex_days, ex_rows = 0, 0
    for a, b in zip(lo, lo[1:]):
        hit = [s for s in pools[a] if (s, b) in ex]
        ex_rows += len(hit)
        ex_days += bool(hit)

    doc = {
        "generated_for": "票池新鲜度：换手 / 北交所 / 除权",
        "window": {"from": lo[0], "to": lo[-1], "n_days": len(lo)},
        "last_partition": {"date": last, "quote_ts_ms": qts, "is_final": last_final,
                           "dropped_intraday": not last_final},
        "churn_n_out": _stat(churn),
        "churn_by_gap_days": {g: _stat(v) for g, v in sorted(by_gap.items())},
        "churn_pairs": pairs,
        "bj": {"symbol_days": n_bj, "days_with_bj": days_bj, "n_days": len(lo)},
        "ex_div_on_entry": {"symbol_days": ex_rows, "days": ex_days, "n_pairs": len(lo) - 1},
        "caliber": {
            "churn": "前一交易日池子中不在次日池子的只数；**只统计 gap_days==1 的对**",
            "churn_by_gap_days": "同一读数按两格的自然日跨度分层；gap>1 的一对不是「一天」",
            "churn_quantile": "最近秩（sorted[n*p]），不是线性插值；p75 因此偏高一档",
            "bj": "symbol 以 .BJ 结尾（上游前缀必须 bj，sz 会返回 v_pv_none_match）",
            "ex_div": "data/adj_factor/all.parquet 的 (symbol, trade_date) 命中",
        },
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    c = doc["churn_n_out"]
    print(f"换手(out只数, 仅 gap_days==1) p50={c.get('p50')}  p25={c.get('p25')}  "
          f"p75={c.get('p75')}  max={c.get('max')}  n={c.get('n')}  "
          f"| 全部对 n={len(pairs)}（其中 gap>1 的 {len(pairs) - len(adj)} 对不计入）")
    print(f"北交所 {n_bj} 只-日 / {days_bj} 天 | 入场日除权 {ex_rows} 只-日 / {ex_days} 天")
    print(f"最后一格 {last} final={last_final} ⇒ dropped_intraday={not last_final}")
    print(f"产物 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
