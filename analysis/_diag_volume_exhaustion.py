"""检验「量能先枯、价格后跌」——以及九阳当下的「二连板 + 缩量」是什么位置。

两个可证伪命题：
  H1（我上一轮的说法）：价格在 20 日高点附近时，5 日量能相对 20 日萎缩，
      之后 20 日收益更差。
  H2（九阳 11-23/24 的实况）：涨停日成交额相对前日萎缩（缩量板），
      之后 5/10 日更差还是更好？

口径：T 日收盘观测 → T+1 开盘买入，T+1 开盘涨停（跳空≥9.5%）剔除。
超额 = 减同日全市场等权（同口径）。
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_volume_exhaustion.json"


def load():
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "high", "low", "close",
                                         "volume", "amount", "turnover_rate",
                                         "consecutive_limit_ups"])
    df["symbol"] = df["symbol"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["rn"] = g.cumcount()
    df["lu"] = (df.consecutive_limit_ups > 0).astype(np.int8)
    # 量能
    df["v5"] = g.volume.transform(lambda s: s.rolling(5).mean())
    df["v20"] = g.volume.transform(lambda s: s.rolling(20).mean())
    df["vr"] = df.v5 / df.v20
    df["amt_ratio"] = g.amount.transform(lambda s: s / s.shift(1))
    # 位置
    df["hi20"] = g.close.transform(lambda s: s.rolling(20).max())
    df["pos20"] = df.close / df.hi20
    df["ret20"] = g.close.pct_change(20)
    # 前瞻
    df["entry"] = g.open.shift(-1)
    df["gap1"] = df.entry / df.close - 1
    for h in (5, 10, 20):
        df[f"fwd{h}"] = g.close.shift(-h) / df.entry - 1
    return df


def report(d, col, bins, label):
    rows = []
    for lo, hi, name in bins:
        s = d[(d[col] > lo) & (d[col] <= hi)]
        if len(s) < 100:
            continue
        r = {"bucket": name, "n": int(len(s))}
        for h in (5, 10, 20):
            v = s[f"fwd{h}"].dropna()
            r[f"fwd{h}_mean_%"] = round(float(v.mean()) * 100, 2)
            r[f"fwd{h}_med_%"] = round(float(v.median()) * 100, 2)
        v = s.fwd10.dropna()
        r["fwd10_win_%"] = round(float((v > 0).mean()) * 100, 1)
        rows.append(r)
    return {label: rows}


def main():
    df = load()
    res = {}

    # ---------- H1：高位 + 量能萎缩
    base = df.dropna(subset=["vr", "pos20", "ret20", "entry", "fwd20"]).copy()
    base = base[(base.close >= 2) & (base.rn >= 250) & (base.amount >= 2e7)]
    hi = base[(base.pos20 >= 0.97) & (base.ret20 > 0.30)].copy()
    res["H1_n_events"] = int(len(hi))
    res.update(report(hi, "vr", [(-1, .5, "vr<0.5"), (.5, .7, "0.5-0.7"),
                                 (.7, 1.0, "0.7-1.0"), (1.0, 1.5, "1.0-1.5"),
                                 (1.5, 99, ">1.5")], "H1_high_pos_by_vr"))
    # 对照：不在高位
    lo = base[base.pos20 < 0.90]
    res.update(report(lo, "vr", [(-1, .5, "vr<0.5"), (.5, .7, "0.5-0.7"),
                                 (.7, 1.0, "0.7-1.0"), (1.0, 99, ">1.0")],
                      "H1_low_pos_by_vr"))

    # ---------- H2：涨停日缩量
    lu = df[(df.lu > 0) & (df.gap1 < 0.095)].copy()
    lu = lu.dropna(subset=["amt_ratio", "entry", "fwd10"])
    lu = lu[(lu.close >= 2) & (lu.rn >= 250)]
    lu["consec"] = lu.consecutive_limit_ups.clip(upper=4)
    res["H2_n_events"] = int(len(lu))
    res.update(report(lu, "amt_ratio", [(-1, .5, "amt<50%"), (.5, .8, "50-80%"),
                                        (.8, 1.2, "80-120%"), (1.2, 2, "120-200%"),
                                        (2, 99, ">200%")], "H2_all_limitup_by_amt"))
    for k in (1, 2, 3):
        sub = lu[lu.consecutive_limit_ups == k]
        res.update(report(sub, "amt_ratio", [(-1, .5, "amt<50%"), (.5, .8, "50-80%"),
                                             (.8, 1.2, "80-120%"), (1.2, 99, ">120%")],
                          f"H2_consec{k}_by_amt"))

    # 二次过滤：高位（20 日涨幅>50%）的二连板
    hi2 = lu[(lu.consecutive_limit_ups == 2) & (lu.ret20 > 0.50)]
    res["H2_hi2_n"] = int(len(hi2))
    res.update(report(hi2, "amt_ratio", [(-1, .5, "amt<50%"), (.5, .8, "50-80%"),
                                         (.8, 1.2, "80-120%"), (1.2, 99, ">120%")],
                      "H2_highpos_consec2_by_amt"))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
