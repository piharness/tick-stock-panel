"""九阳 002242 路径解剖 + 全市场「3日偏离」阈值分布检验。

目的：
1) 用户前提「为了绕过异动的3条规则」——先看它是否真的贴在阈值下方（如果早就远超，前提为假）。
2) 全市场 3 日累计偏离的分布，在 20% 附近是否存在堆积（bunching = 节奏控制的痕迹）。
"""
import json
import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
IDX = "data/backfill_20261117/index_daily_full.parquet"
OUT = "data/_diag_pullback_shape_step1.json"

COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close",
        "volume", "amount", "turnover_rate"]


def load_panel():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    return df


def main():
    df = load_panel()
    res = {}

    # ---------- 1. 九阳路径 ----------
    j = df[df.symbol == "002242.SZ"].copy()
    j["r1"] = j.close.pct_change() * 100
    j["r3"] = (j.close / j.close.shift(3) - 1) * 100
    j["r10"] = (j.close / j.close.shift(10) - 1) * 100
    j["r20"] = (j.close / j.close.shift(20) - 1) * 100
    j["ma5"] = j.close.rolling(5).mean()
    j["ma10"] = j.close.rolling(10).mean()
    j["ma20"] = j.close.rolling(20).mean()
    j["vol5"] = j.volume.rolling(5).mean()
    j["vr"] = j.volume / j.vol5
    # 涨停/跌停判定（主板 ±10%，用原始价）
    j["lim_up"] = (j.raw_close / j.raw_close.shift(1) - 1).abs().gt(0.095) & \
                  (j.raw_close > j.raw_close.shift(1))
    j["lim_dn"] = (j.raw_close / j.raw_close.shift(1) - 1).abs().gt(0.095) & \
                  (j.raw_close < j.raw_close.shift(1))
    # 日内位置
    j["cl_pos"] = (j.close - j.low) / (j.high - j.low).replace(0, np.nan)

    seg = j[(j.date >= "2026-10-08") & (j.date <= "2026-11-20")]
    cols = ["date", "open", "high", "low", "close", "r1", "r3", "r10", "r20",
            "turnover_rate", "vr", "cl_pos", "lim_up", "lim_dn"]
    res["jiuyang_path"] = [
        {k: (None if pd.isna(v) else (round(float(v), 4) if isinstance(v, (int, float, np.floating)) else str(v)))
         for k, v in row.items()}
        for row in seg[cols].to_dict("records")
    ]
    res["jiuyang_note"] = "r3/r10 是绝对涨跌幅，未扣指数；异动判定用的是「偏离值」=个股涨跌幅−对应指数涨跌幅"

    # ---------- 2. 深证成指 3 日涨跌幅，算真实偏离 ----------
    idx = pd.read_parquet(IDX, columns=["symbol", "date", "close"])
    idx["date"] = pd.to_datetime(idx["date"])
    # 找深证成指
    cand = idx.groupby("symbol").size().sort_values(ascending=False)
    res["index_symbols_top"] = {str(k): int(v) for k, v in cand.head(8).items()}
    res["jiuyang_path"] = res["jiuyang_path"]

    # ---------- 3. 全市场 3 日累计涨跌幅分布（不扣指数，先看形状） ----------
    df["c3"] = df.groupby("symbol").close.pct_change(3) * 100
    d = df.dropna(subset=["c3"])
    d = d[d.date >= "2016-02-01"]
    bins = [-100, -40, -30, -25, -22, -20, -18, -16, -14, -12, -10, -8, -6, -4,
            -2, 0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 19, 20, 21, 22, 25, 30, 40, 300]
    d["b"] = pd.cut(d.c3, bins=bins, right=False)
    hist = d.groupby("b", observed=True).size()
    res["c3_hist_all"] = {str(k): int(v) for k, v in hist.items()}

    # 只看主板（60/00 开头，非 ST 近似：排除名称不可得，用价格>2 元+换手>0.2）
    mb = d[d.symbol.str.match(r"^(60|00)") & (d.raw_close > 2)]
    mb2 = mb.groupby(pd.cut(mb.c3, bins=bins, right=False), observed=True).size()
    res["c3_hist_mainboard"] = {str(k): int(v) for k, v in mb2.items()}

    # 20% 阈值附近的密度比（19~20 vs 20~21 与 18~19）
    def near(x):
        return {k: int(((x >= k) & (x < k + 1)).sum()) for k in range(14, 26)}
    res["c3_near20_mainboard"] = near(mb.c3)
    res["c3_near20_all"] = near(d.c3)

    # ---------- 4. 九阳自身 3 日偏离（扣深证成指） ----------
    res["jiuyang_c3"] = None
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("wrote", OUT)
    for k in ["c3_hist_mainboard", "c3_near20_mainboard", "c3_near20_all"]:
        print(k, res[k])


if __name__ == "__main__":
    main()
