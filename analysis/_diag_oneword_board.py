"""一字板之后：可买点在哪、值多少。

九阳 2026-11-24 若为一字板（open=high=low=close=35.05，二连板），
则当日完全买不到；可操作的是 T+1 打开或 T+1 继续一字后的 T+2。

口径：
  - 一字板定义：open==high==low==close 且涨停
  - 分层：consecutive_limit_ups（1 / 2 / 3+）
  - 可买：T+1 开盘跳空 <9.5%（未涨停开盘）
  - 收益：T+1 开盘 → T+1+h 收盘，超额减同日全市场等权
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_oneword_board.json"


def main():
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "high", "low", "close",
                                         "volume", "amount", "consecutive_limit_ups"])
    df["symbol"] = df["symbol"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["rn"] = g.cumcount()
    df["lu"] = (df.consecutive_limit_ups > 0).astype(np.int8)
    df["oneword"] = ((df.open == df.high) & (df.high == df.low) & (df.low == df.close)
                     & (df.lu > 0))
    df["entry"] = g.open.shift(-1)
    df["gap1"] = df.entry / df.close - 1
    for h in (5, 10, 20):
        df[f"fwd{h}"] = g.close.shift(-h) / df.entry - 1
    df["fwd_lu5"] = g.lu.transform(lambda s: s.shift(-5).rolling(5).sum())

    base = df.dropna(subset=["entry", "fwd10"]).copy()
    base = base[(base.close >= 2) & (base.rn >= 250) & (base.amount >= 2e7)]
    res = {}

    for tag, d in [("oneword", base[base.oneword]), ("normal_lu", base[(base.lu > 0) & (~base.oneword)])]:
        res[tag] = {"n": int(len(d))}
        # T+1 是否还能买到
        res[tag]["pct_gap1_gt9.5"] = round(float((d.gap1 >= 0.095).mean()) * 100, 1)
        tradable = d[d.gap1 < 0.095]
        res[tag]["n_buyable"] = int(len(tradable))
        rows = []
        for k, name in [(1, "首板"), (2, "二板"), (3, "三板+")]:
            s = tradable[(tradable.consecutive_limit_ups == k)] if k < 3 else \
                tradable[tradable.consecutive_limit_ups >= 3]
            if len(s) < 50:
                continue
            rows.append({
                "consec": name, "n": int(len(s)),
                "fwd5_%": round(float(s.fwd5.mean()) * 100, 2),
                "fwd10_%": round(float(s.fwd10.mean()) * 100, 2),
                "fwd10_med_%": round(float(s.fwd10.median()) * 100, 2),
                "fwd20_%": round(float(s.fwd20.mean()) * 100, 2),
                "win10_%": round(float((s.fwd10 > 0).mean()) * 100, 1),
                "next5_lu_rate_%": round(float((s.fwd_lu5.fillna(0) > 0).mean()) * 100, 1),
            })
        res[tag]["by_consec"] = rows

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
