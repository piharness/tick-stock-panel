"""广度检验：同时有多少个行业在「低位 + 高波」区间里，后续 20/60 日如何。

回答的问题是「今天六个行业一起触发，算六个机会还是一个机会」，以及
「同时触发越多是否越该买」。口径与 `analysis/etf_watch.py` 完全同源
（build_nav → features → dd/vol_pct），只把 asof 从「今天」换成「历史上每一天」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import etf_watch as W  # noqa: E402

OUT = ROOT / "data" / ".scratch" / "breadth.json"
COST = 0.0006
LOW, DEEP = -0.35, -0.50
VOL_HI = 0.66


def main() -> None:
    etf, _ = W.fresh_etf()
    d, _ = W.core.load(etf_raw=etf)
    panel = W.core.aggregate(d, require_fwd=False)
    nav = W.core.industry_nav(panel)
    f = W.features(nav)
    dd = f.pivot(index="date", columns="industry", values="dd")
    vp = f.pivot(index="date", columns="industry", values="volpct")
    print(f"行业净值 {nav.shape[0]} 日 × {nav.shape[1]} 行业 | 特征末日 {dd.index.max().date()}")

    inband = (dd >= DEEP) & (dd <= LOW) & (vp > VOL_HI)
    breadth = inband.sum(axis=1)

    fwd20 = nav.shift(-21) / nav.shift(-1) - 1
    fwd60 = nav.shift(-61) / nav.shift(-1) - 1
    base20 = fwd20.mean(axis=1)          # 全行业等权（当日）
    base60 = fwd60.mean(axis=1)

    rows = []
    for lab, lo, hi in (("0", 0, 0), ("1-2", 1, 2), ("3-4", 3, 4), ("5-7", 5, 7), ("8+", 8, 99)):
        mask = (breadth == 0) if lab == "0" else ((breadth >= lo) & (breadth <= hi))
        for h, fut, base in (("20", fwd20, base20), ("60", fwd60, base60)):
            vals = []
            for dt in dd.index[mask.to_numpy() & base.notna().to_numpy()]:
                # 买「当日真正在区间内的行业」等权；广度 0 档买全行业（纯基准，应≈−成本）
                picked = fut.columns if lab == "0" else inband.columns[inband.loc[dt].to_numpy()]
                v = fut.loc[dt, picked].dropna()
                if len(v):
                    vals.append(float(v.mean() - COST - base.loc[dt]))
            if not vals:
                continue
            arr = np.asarray(vals)
            rows.append({
                "breadth": lab, "h": int(h), "days": len(arr),
                "rel_bp": round(float(arr.mean()) * 1e4, 1),
                "med_bp": round(float(np.median(arr)) * 1e4, 1),
                "win": round(float((arr > 0).mean()) * 100, 1),
            })

    out = pd.DataFrame(rows)
    print("\n=== 广度 → 后续相对全行业等权的超额（扣 6bp）===")
    print(out.to_string(index=False))

    # 广度>=5 的「簇」：按间隔 >60 交易日切分
    hot = dd.index[(breadth >= 5) & base20.notna()]
    clusters, last = [], None
    for dt in hot:
        i = dd.index.get_loc(dt)
        if last is None or i - last > 60:
            clusters.append(dt)
        last = i
    print(f"\n广度≥5 的交易日 {len(hot)} 天 → 独立簇 {len(clusters)} 个")
    print("簇起始:", [str(x.date()) for x in clusters])

    today = dd.index.max()
    print(f"\n今天({today.date()}) 广度 {int(breadth.loc[today])} | 在区间行业 "
          f"{[c for c in inband.columns[inband.loc[today].to_numpy()]]}")

    res = {
        "meta": {"asof": str(today.date()), "n_industries": int(nav.shape[1]),
                 "band": [DEEP, LOW], "vol_hi": VOL_HI, "cost": COST,
                 "note": "相对基准=当日全行业等权同窗口收益；簇=广度≥5 且间隔>60交易日"},
        "table": rows,
        "clusters": [str(x.date()) for x in clusters],
        "hot_days": int(len(hot)),
        "breadth_today": int(breadth.loc[today]),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
