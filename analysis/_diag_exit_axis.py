"""出场轴：EV 的定义域，比入场轴宽得多，且完全免费。

背景：全部预算判定把 EV 固定在「涨停价买 → T+1 开盘卖」这一条出场规则上，
     然后把 -75.7bp 的门槛记在「入场信号」头上。
     但 T+1 交割只禁止「当日卖」，不禁止「T+1 收盘卖」。
     ⇒ 若炸板腿的 -443.8bp 主要是 T+1 开盘的瞬时超跌，则最大的免费杠杆是出场，
       而不是分钟级入场筛。这个问题不需要买任何数据。

判据（必须先复现基线，否则本脚本自身的口径不可信）：
  r0 = 复现 _audit_intraday_budget 的 -82.54bp（逐位）

输出：_diag_exit_axis_20261110.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _audit_intraday_budget import build_universe, nw_t, DATA  # noqa: E402

OUT = HERE / "_diag_exit_axis_20261110.txt"
LINES: list[str] = []


def p(s: str) -> None:
    print(s, flush=True)
    LINES.append(s)


def main() -> int:
    df, u = build_universe()
    p(f"universe n={len(u):,} days={u['date'].nunique()} hit={u['y'].mean():.4f}")

    g = df.groupby("symbol", sort=False)
    for lag, col in ((1, "close"), (2, "close"), (3, "open"), (5, "close")):
        df[f"p{lag}{col[0]}"] = g[col].shift(-lag)
        df[f"d{lag}"] = g["date"].shift(-lag)

    ex = pd.read_parquet(DATA / "adj_factor" / "all.parquet")[["symbol", "trade_date", "ex_factor"]]
    ex["trade_date"] = pd.to_datetime(ex["trade_date"])
    exi = ex.set_index(["symbol", "trade_date"])["ex_factor"]

    def fx(dates):
        v = exi.reindex(pd.MultiIndex.from_arrays([df["symbol"], dates])).to_numpy()
        return np.where(np.isfinite(v), v, 1.0)

    for lag in (1, 2, 3, 5):
        df[f"f{lag}"] = fx(df[f"d{lag}"])

    cols = ["symbol", "date", "p1c", "p2c", "p3o", "p5c", "f1", "f2", "f3", "f5", "d3", "d5"]
    m = u.merge(df[cols], on=["symbol", "date"], how="left")
    lp = m["lim_px"].to_numpy()

    def ret(px, f):
        return (px * f / lp - 1.0) * 1e4

    m["r0"] = m["nxt_open"] * m["f1"] / lp - 1.0          # 基线：T+1 开盘
    m["r1c"] = m["p1c"] * m["f1"] / lp - 1.0              # T+1 收盘（T+1 合法）
    m["r2o"] = m["nxt2_open"] * m["f2"] / lp - 1.0        # T+2 开盘
    m["r3o"] = m["p3o"] * m["f3"] / lp - 1.0              # T+3 开盘
    m["r5c"] = m["p5c"] * m["f5"] / lp - 1.0              # T+5 收盘

    base = float(m["r0"].mean()) * 1e4
    p(f"\n[0] 基线复现 r0(T+1 open) = {base:.2f}bp  (目标 -82.54)  delta={base + 82.54:+.3f}")

    def stat(s: pd.DataFrame, col: str, tag: str) -> dict:
        d = s.groupby("date")[col].mean()
        t = nw_t(d)
        return {"rule": tag, "n": int(s[col].notna().sum()), "days": int(d.notna().sum()),
                "ev_bp": round(float(s[col].mean()) * 1e4, 1),
                "nw_t": t[1] if t else None,
                "median_bp": round(float(s[col].median()) * 1e4, 1),
                "p10_bp": round(float(s[col].quantile(0.10)) * 1e4, 1),
                "p90_bp": round(float(s[col].quantile(0.90)) * 1e4, 1)}

    p("\n[1] 出场规则全样本（各腿独立，非事后择优）")
    rows = []
    for col, tag in (("r0", "T+1 open"), ("r1c", "T+1 close"), ("r2o", "T+2 open"),
                     ("r3o", "T+3 open"), ("r5c", "T+5 close")):
        rows.append(stat(m, col, tag))
    for r in rows:
        p(f"  {r['rule']:<11} EV={r['ev_bp']:>8.1f}bp  t={r['nw_t']:>6.2f}  "
          f"med={r['median_bp']:>8.1f}  p10={r['p10_bp']:>8.1f}  p90={r['p90_bp']:>8.1f}  n={r['n']}")

    p("\n[2] 分腿：封住(y=1) vs 炸板(y=0)")
    for col, tag in (("r0", "T+1 open"), ("r1c", "T+1 close")):
        for yv, lab in ((1, "封住"), (0, "炸板")):
            s = m[m["y"] == yv]
            r = stat(s, col, tag)
            p(f"  {lab} {tag:<10} EV={r['ev_bp']:>8.1f}bp  t={r['nw_t']:>6.2f}  "
              f"med={r['median_bp']:>8.1f}  n={r['n']}")

    p("\n[3] 可实施的择时出场：T 日收盘已知是否封住 ⇒ 封住次日开盘卖，炸板次日收盘卖")
    m["r_cond"] = np.where(m["y"] == 1, m["r0"], m["r1c"])
    r = stat(m, "r_cond", "cond exit")
    p(f"  cond  EV={r['ev_bp']:>8.1f}bp  t={r['nw_t']:>6.2f}  n={r['n']}")
    d_base = m.groupby("date")["r0"].mean()
    d_cond = m.groupby("date")["r_cond"].mean()
    diff = (d_cond - d_base).dropna()
    t = nw_t(diff)
    p(f"  逐日配对差 = {float(diff.mean())*1e4:.1f}bp  NW t={t[1]:.2f}  days={t[2]}")

    p("\n[4] 炸板腿的 T+1 开盘→收盘 这一段本身")
    brk = m[m["y"] == 0]
    seg = (brk["p1c"] * brk["f1"] - brk["nxt_open"] * brk["f1"]) / (brk["lim_px"] * brk["f1"])
    p(f"  T+1 日内(o→c) 均值 {float(seg.mean())*1e4:+.1f}bp 中位 {float(seg.median())*1e4:+.1f}bp "
      f"正比例 {float((seg>0).mean()):.3f} n={len(seg)}")

    p("\n[5] 事后上界（不可交易，仅用来量出场轴的总空间）")
    best = np.maximum.reduce([m["r0"].to_numpy(), m["r1c"].to_numpy(), m["r3o"].to_numpy()])
    p(f"  逐事件三选一最优 EV={float(best.mean())*1e4:+.1f}bp（上界，含前视）")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    p(f"\nwritten {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
