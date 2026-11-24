"""2026-09-01 → 11-24：九阳这种走势是孤例还是有一批？

三个维度同时量，避免用单一涨幅把「一波流」和「持续向上」混在一起：
  gain   起点收盘 → 11-24（11-24 为盘中快照，标注）
  mdd    期间收盘最大回撤（越接近 0 越「始终向上」）
  hit    期间收盘创区间新高的天数占比（越高越「一路向上」）

同时回答两个精确率问题：
  - 满足 9/10 那套前兆条件的 5 只，有几只最终翻倍？
  - 全市场翻倍的 N 只里，有几只在 9/10 被那套条件命中？
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
NOV = "data/kline_daily/date=2026-11/part.parquet"
OUT = "data/_diag_roundup_2026q4.json"
S, E = pd.Timestamp("2026-09-01"), pd.Timestamp("2026-11-24")


def main():
    p = pd.read_parquet(PANEL, columns=["symbol", "date", "close", "consecutive_limit_ups"])
    p["symbol"] = p["symbol"].astype(str)
    p["date"] = pd.to_datetime(p["date"])
    k = pd.read_parquet(NOV)[["symbol", "date", "close"]]
    k["symbol"] = k["symbol"].astype(str)
    k["date"] = pd.to_datetime(k["date"])
    tail = k[k.date > pd.Timestamp("2026-11-18")]

    full = (pd.concat([p[["symbol", "date", "close"]], tail])
            .drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"]))
    g = full.groupby("symbol", sort=False)
    full["rn"] = g.cumcount()

    win = full[(full.date >= S) & (full.date <= E)]
    stat = win.groupby("symbol").agg(
        n=("close", "size"), first=("close", "first"), last=("close", "last"),
        peak=("close", "max"), trough_after_peak=("close", "min"))
    # 逐票算路径
    rows = []
    for s, d in win.groupby("symbol", sort=False):
        if len(d) < 45:
            continue
        c = d.close.to_numpy()
        peak = np.maximum.accumulate(c)
        dd = c / peak - 1
        rows.append((s, len(c), c[0], c[-1], c[-1] / c[0] - 1, dd.min(),
                     float((c >= peak - 1e-12).mean())))
    t = pd.DataFrame(rows, columns=["symbol", "n", "px0", "px1", "gain", "mdd", "hit"])

    # 只保留 9/1 前已上市满一年的（rn>=250）
    rn0 = full[full.date >= S].groupby("symbol").rn.min()
    listed = rn0[rn0 >= 250].index
    t = t[t.symbol.isin(listed)]
    t = t[t.px0 >= 2]

    res = {"window": [str(S.date()), str(E.date())],
           "n_universe": int(len(t)),
           "top": [], "buckets": []}

    t = t.sort_values("gain", ascending=False)
    for _, r in t.head(30).iterrows():
        res["top"].append({"symbol": r.symbol, "gain": round(r.gain * 100, 1),
                           "mdd": round(r.mdd * 100, 1), "hit": round(r.hit * 100, 1),
                           "px0": round(r.px0, 2), "px1": round(r.px1, 2)})

    for lo, hi, name in [(-1, 0, "<0"), (0, .3, "0-30%"), (.3, .5, "30-50%"),
                         (.5, 1, "50-100%"), (1, 2, "100-200%"), (2, 99, ">200%")]:
        sub = t[(t.gain > lo) & (t.gain <= hi)]
        if len(sub):
            res["buckets"].append({"bucket": name, "n": int(len(sub)),
                                   "mdd_median": round(float(sub.mdd.median()) * 100, 1),
                                   "hit_median": round(float(sub.hit.median()) * 100, 1)})

    # 「持续向上」判定：gain>100% 且 mdd>-20% 且 hit>50%
    up = t[(t.gain > 1) & (t.mdd > -0.20) & (t.hit > 0.50)]
    res["sustained_up"] = {
        "n": int(len(up)),
        "share_of_universe": round(len(up) / len(t) * 100, 2),
        "list": [{"symbol": r.symbol, "gain": round(r.gain * 100, 1),
                  "mdd": round(r.mdd * 100, 1), "hit": round(r.hit * 100, 1)}
                 for _, r in up.sort_values("gain", ascending=False).iterrows()],
    }
    res["n_gain_gt100"] = int((t.gain > 1).sum())
    res["n_gain_gt200"] = int((t.gain > 2).sum())
    res["jiuyang"] = {
        "gain": round(float(t[t.symbol == "002242.SZ"].gain.iloc[0]) * 100, 1),
        "mdd": round(float(t[t.symbol == "002242.SZ"].mdd.iloc[0]) * 100, 1),
        "hit": round(float(t[t.symbol == "002242.SZ"].hit.iloc[0]) * 100, 1),
        "rank_by_gain": int((t.gain > t[t.symbol == "002242.SZ"].gain.iloc[0]).sum() + 1),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
