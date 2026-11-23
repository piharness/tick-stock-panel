"""ONE-OFF：现金腿形状放到账户层比较（短融打底比例 × 腿1 刻度）。

窗口受 511360 上市限制：2020-09-28 → 2026-11-20（6.1 年）。
现金腿 = w×511010 + (1-w)×511360，月频再平衡，4bp/次；腿 1 规则不变（六格 / 半仓）。

运行：python analysis/_probe_cash_shape_account.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch_account as ACC  # noqa: E402
import etf_watch_dividend as D  # noqa: E402
import etf_watch_leg2_bond as LB  # noqa: E402
import etf_watch_leg2_rule as LR  # noqa: E402
import etf_watch_portfolio as pf  # noqa: E402

START = pd.Timestamp("2020-09-28")
COST = 0.0004


def blends(sess_idx: pd.DatetimeIndex) -> dict[str, pd.Series]:
    bond, _ = LB.bond_total_return()
    f = D.fetch("511360")
    short = f["ac"] / f["ac"].iloc[0]
    cal = pd.read_parquet(LR.TR_FILE).set_index("date")["close"].sort_index().index
    b = bond.reindex(cal).ffill().reindex(sess_idx).ffill()
    s = short.reindex(cal).ffill().reindex(sess_idx).ffill()
    out = {}
    for w in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        rb, rs = b.pct_change().fillna(0), s.pct_change().fillna(0)
        # 月频再平衡
        val, cur = [], np.array([w, 1 - w], dtype=float)
        months = sess_idx.month
        for i, dt in enumerate(sess_idx):
            r = np.array([rb.get(dt, 0.0), rs.get(dt, 0.0)], dtype=float)
            if not np.all(np.isfinite(r)):
                r = np.zeros(2)
            cur = cur * (1 + r)
            if i + 1 < len(sess_idx) and months[i + 1] != months[i]:
                cur = cur.sum() * np.array([w, 1 - w])
            val.append(cur.sum())
        lvl = pd.Series(val, index=sess_idx)
        out[str(int(w*100))] = lvl
    return out


def main() -> None:
    world = ACC.load_world()
    d, mats, sessions_all, ev, _ = world
    sessions = [t for t in sessions_all if t >= START]
    sym_mat = mats["symbol"]
    ev_w = ev[ev["date"] >= START].copy()
    sess_idx = pd.DatetimeIndex(sessions)
    cash_r = LR.cash_series(sess_idx)
    prices = ACC.X.price_frame(d, {str(s) for s in sym_mat.stack().dropna().unique()}
                               | {"511010.SH", "511360.SH"})

    lv = blends(sess_idx)
    print(f"窗口 {sess_idx[0].date()} → {sess_idx[-1].date()}  N={len(sess_idx)}")
    print(f"{'国债占比':>8} {'腿1':>5} {'年化':>8} {'MDD':>8} {'最差20日':>10} {'ratio':>6} {'水下':>6}")
    rows = []
    for name, lvl in lv.items():
        dr = lvl.pct_change().fillna(0)
        tgt = pd.Series(1.0, index=sess_idx)
        tgt.iloc[0] = 0.0
        for slots, tag in ((6, "六格"), (12, "半仓")):
            old = pf.SLOTS
            pf.SLOTS = slots
            try:
                r = LR.replay_joint(ev_w, sessions, prices, sym_mat, div_r=dr, div_tgt=tgt,
                                    cash_r=cash_r, use_div=True, start=START,
                                    allow_open=False, init=600_000.0)
            finally:
                pf.SLOTS = old
            m = r["metrics"]
            ratio = abs(m["cagr"] / m["max_dd"]) if m["max_dd"] else np.nan
            rows.append({"bond_w": name, "leg1": tag, **m, "ratio": ratio})
            print(f"{name:>8} {tag:>5} {m['cagr']:>+8.2%} {m['max_dd']:>8.2%} "
                  f"{m['worst20']:>10.2%} {ratio:>6.2f} {m['longest_underwater']:>6d}")
    out = pd.DataFrame(rows)
    out.to_json(ROOT / "data/etf_watch/_probe_cash_shape.json", orient="records", force_ascii=False)
    print("\n→ data/etf_watch/_probe_cash_shape.json")


if __name__ == "__main__":
    main()
