"""L1 容量：入场日成交额分布，以及「补 327 cohort x <=20 只」这个下载量估计的对象对不对。

上一版（§9.3）把「首根 bar 占全日成交额份额」当成吞吐上界，并计划下载
选股池的分钟 bar 把这个份额从 40 只样本换成 8.7 只/日。跑之前先问三件事：

  1. 那个 6.5k 次 = 327 x 20 里的两个因子，分别属于谁？
     - 327 是 20 日持有 cohort（round3/S5）的个数；
     - 20 是 mom20 top20 的名单长度；
     - 而 round5 的日内回合是 **367 个交易日 x 8.7 只**。
     两个都不属于它。所以先按 round5 自己的口径重算 (只-日, 去重 symbol)，
     再乘 `segment_trading_days=20` 得真实请求数。

  2. 分钟 bar 是不是这个问题的必需品？round5 的入场价是 `open`，
     出场是当日 `close`，**没有要求在第一分钟成交完**。所以容量约束的
     分母是「全日成交额」，日线库本地全有；「首根 bar 份额」只影响
     开盘那一笔的滑点，是精度不是可用性。

  3. 容量判据能不能红？把资金 A 当自变量，输出参与率分布，
     再看哪个 A 让 44.5bp 归零。这不需要任何新数据。

ASCII 输出（GBK 控制台）。
"""
from __future__ import annotations

import glob
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asymmetry_probe as ap  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
GAP_MAX = -0.01          # round5 <=1.0% bucket
SEG_DAYS = 20            # sync_minute_batch segment_trading_days
FIRST_BAR_SHARE = 0.0352  # 40-name probe median
FIRST_BAR_SHARE_P10 = 0.0154


def pct(x: np.ndarray, q: float) -> float:
    return float(np.percentile(x, q)) if x.size else float("nan")


def main() -> None:
    t0 = time.time()
    df = ap.add_features(ap.load_panel()).select("symbol", "date", "open", "close", "mom20", "fwd")
    amt = pl.concat([
        pl.read_parquet(f, columns=["symbol", "date", "amount"])
        for f in sorted(glob.glob(str(DATA / "kline_daily_enriched" / "date=*" / "part.parquet")))
    ], how="vertical_relaxed")
    df = df.join(amt, on=["symbol", "date"], how="left")

    dates = sorted(df["date"].unique().to_list())
    d_ix = {str(d): i for i, d in enumerate(dates)}
    o = df.pivot(on="symbol", index="date", values="open").sort("date")
    c = df.pivot(on="symbol", index="date", values="close").sort("date")
    symbols = [x for x in o.columns if x != "date"]
    O = o.select(symbols).to_numpy().astype(np.float64)
    C = c.select(symbols).to_numpy().astype(np.float64)
    A = df.pivot(on="symbol", index="date", values="amount").sort("date").select(symbols).to_numpy().astype(np.float64)
    s_ix = {s: i for i, s in enumerate(symbols)}
    T = len(dates)

    picks = ap.pick_daily(df, "mom20", ascending=False).select("date", "symbols").to_dicts()

    cells = []   # (entry_date, symbol, amount)
    n_per_day = []
    for r0 in picks:
        i0 = d_ix[str(r0["date"])]
        if i0 + 1 >= T:
            continue
        sel = [s_ix[s] for s in r0["symbols"] if s in s_ix]
        sel = [j for j in sel if np.isfinite(O[i0, j]) and np.isfinite(C[i0, j]) and C[i0, j] > 0]
        # gap 用入场日 open vs 信号日 close，与 round5 的 o[d+1]/c[d]-1 同口径
        sel = [j for j in sel if np.isfinite(O[i0 + 1, j]) and C[i0, j] > 0
               and O[i0 + 1, j] / C[i0, j] - 1.0 <= GAP_MAX]
        if len(sel) < 3:
            continue
        n_per_day.append(len(sel))
        for j in sel:
            cells.append((str(dates[i0 + 1]), symbols[j], float(A[i0 + 1, j])))

    n_cell = len(cells)
    u_sym = sorted({x[1] for x in cells})
    u_day = sorted({x[0] for x in cells})
    a = np.array([x[2] for x in cells], dtype=np.float64)
    a = a[np.isfinite(a) & (a > 0)]
    span = len(dates)

    # 真实请求数：每只 x 每 20 个交易日一段
    req = len(u_sym) * math.ceil(span / SEG_DAYS)
    req_by_day = len(u_sym) * math.ceil(len(u_day) / SEG_DAYS)

    # 资金 -> 单票金额 -> 参与率（两种分母）
    rows = []
    for fund_m in (200, 500, 1000, 2000, 5000):
        per = fund_m * 1e6 / 8.7
        pi_full = per / a
        pi_first = per / (a * FIRST_BAR_SHARE)
        pi_first_p10 = per / (a * FIRST_BAR_SHARE_P10)
        rows.append({
            "fund_m_cny": fund_m,
            "per_name_cny": round(per, 0),
            "part_full_med": round(float(np.median(pi_full)), 4),
            "part_full_p90": round(pct(pi_full, 90), 4),
            "part_first_med": round(float(np.median(pi_first)), 4),
            "part_first_p90": round(pct(pi_first, 90), 4),
            "part_first_p10share_med": round(float(np.median(pi_first_p10)), 4),
            "share_over_10pct_full": round(float((pi_full > 0.10).mean()), 3),
            "share_over_10pct_first": round(float((pi_first > 0.10).mean()), 3),
        })

    out = {
        "cohort": {
            "n_cells": n_cell,
            "n_days": len(u_day),
            "n_symbols": len(u_sym),
            "names_per_day_mean": round(float(np.mean(n_per_day)), 2) if n_per_day else None,
            "date_first": u_day[0] if u_day else None,
            "date_last": u_day[-1] if u_day else None,
            "calendar_days": span,
        },
        "amount_cny": {
            "median": round(float(np.median(a)), 0),
            "p10": round(pct(a, 10), 0),
            "p25": round(pct(a, 25), 0),
            "p75": round(pct(a, 75), 0),
            "p90": round(pct(a, 90), 0),
        },
        "download": {
            "req_full_span": req,
            "req_entry_days_only": req_by_day,
            "minutes_at_0p08s": round(req * 0.08 / 60, 1),
            "minutes_entry_only": round(req_by_day * 0.08 / 60, 1),
            "claimed_9min_assumes": "327 cohort x 20 names = 6540 req",
        },
        "capacity": rows,
        "elapsed_s": round(time.time() - t0, 1),
    }

    print(f"cohort: {n_cell} cells / {len(u_day)} days / {len(u_sym)} symbols / "
          f"{out['cohort']['names_per_day_mean']} names per day")
    print(f"amount median {out['amount_cny']['median']/1e8:.3f}e8  p10 {out['amount_cny']['p10']/1e8:.3f}e8")
    print(f"requests: {req} ({out['download']['minutes_at_0p08s']} min) "
          f"entry-days only {req_by_day} ({out['download']['minutes_entry_only']} min)")
    for r in rows:
        print(f"  fund {r['fund_m_cny']:5d}M  per-name {r['per_name_cny']/1e4:7.1f}万  "
              f"part/full med {r['part_full_med']*100:6.2f}% p90 {r['part_full_p90']*100:6.2f}%  "
              f"part/first med {r['part_first_med']*100:6.2f}% p90 {r['part_first_p90']*100:6.2f}%  "
              f">10% first {r['share_over_10pct_first']*100:5.1f}%")
    print(f"elapsed {out['elapsed_s']}s")

    p = ROOT / "analysis" / "_diag_l1_capacity_result.json"
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
