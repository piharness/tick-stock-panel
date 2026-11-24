"""股东集中度能不能解释「持续上涨」：表面流通盘 vs 实际可交易盘。

九阳 2026-09-30 十大流通股东：上海力鸿 50.48% + JS Global 17.06% = 67.54%，两期均「不变」。
表面流通市值 65.6 亿，扣掉这两家只剩约 21 亿可交易。
若「控股股东锁仓 → 实际流通盘小」是持续上涨的结构条件，它应当在首板事件上可回放。

口径（严格防前视）：
  - 股东数据按 notice_date（公告日）可用，不用 period_end
  - 事件 T 日只匹配 notice_date <= T 的最近一期
  - 入场 T+1 开盘，超额 = 减同日全市场等权
样本：top_float 覆盖 2024-12-31 起 8 期 → 只能测 2025-01 以后的事件（约 1.5 年）
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
TOP = "data/shareholder/top_float/part.parquet"
INS = "data/instruments/instruments.parquet"
OUT = "data/_diag_concentration_ignition.json"
SYM = "002242.SZ"


def main():
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "high", "low", "close",
                                         "turnover_rate", "consecutive_limit_ups"])
    df["symbol"] = df["symbol"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["rn"] = g.cumcount()
    df["lu"] = (df.consecutive_limit_ups > 0).astype(np.int8)
    df["cum_lu"] = g["lu"].cumsum()
    df["cum_lu_60"] = df["cum_lu"].shift(60)
    df["first_ign"] = (df.lu == 1) & (df.rn >= 60) & (df.cum_lu - df.cum_lu_60 == 1)
    df["entry"] = g["open"].shift(-1)
    df["gap1"] = df["entry"] / df["close"] - 1
    for h in (20, 40, 60):
        df[f"fwd{h}"] = g["close"].shift(-h) / df["entry"] - 1

    # ---- 股东集中度：按公告日可用
    t = pd.read_parquet(TOP)
    t["code"] = t["code"].astype(str).str.zfill(6)
    agg = (t.groupby(["code", "period_end"])
           .apply(lambda d: pd.Series({
               "top1": float(d.loc[d["rank"] == 1, "ratio"].sum()),
               "top2": float(d.loc[d["rank"] <= 2, "ratio"].sum()),
               "top10": float(d["ratio"].sum()),
               "avail": pd.to_datetime(d["notice_date"]).max(),
           }), include_groups=False)
           .reset_index())
    agg["avail"] = pd.to_datetime(agg["avail"]).astype("datetime64[ns]")
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    agg["code6"] = agg["code"]

    ins = pd.read_parquet(INS)[["symbol", "code", "float_shares"]]
    ins["symbol"] = ins["symbol"].astype(str)
    ins["code"] = ins["code"].astype(str).str.zfill(6)
    agg = agg.merge(ins[["symbol", "code"]], on="code", how="left").dropna(subset=["symbol"])
    agg = agg.sort_values("avail")

    ev = df[df.first_ign & (df.gap1 < 0.095)].copy()
    ev = ev[ev.date >= pd.Timestamp("2025-02-01")]
    ev = ev.sort_values("date")
    m = pd.merge_asof(ev, agg[["avail", "top1", "top2", "top10", "symbol"]].rename(
        columns={"symbol": "sym2"}), left_on="date", right_on="avail",
        left_by="symbol", right_by="sym2", direction="backward")
    m = m.dropna(subset=["top2"])
    res = {"n_events": int(len(m)), "span": [str(m.date.min().date()), str(m.date.max().date())],
           "n_symbols": int(m.symbol.nunique())}

    mkt = df.dropna(subset=["fwd40"]).groupby("date")["fwd40"].mean()
    m["ex40"] = m.fwd40 - m.date.map(mkt)
    m["ex20"] = m.fwd20 - m.date.map(df.dropna(subset=["fwd20"]).groupby("date")["fwd20"].mean())
    m["ex60"] = m.fwd60 - m.date.map(df.dropna(subset=["fwd60"]).groupby("date")["fwd60"].mean())

    out = []
    for lo, hi, name in [(0, 40, "<40%"), (40, 50, "40-50%"), (50, 60, "50-60%"),
                         (60, 70, "60-70%"), (70, 101, ">70%")]:
        s = m[(m.top2 >= lo) & (m.top2 < hi)]
        if len(s) < 50:
            continue
        e40 = s.ex40.dropna()
        byday = e40.groupby(s.loc[e40.index, "date"]).mean()
        t40 = float(byday.mean() / (byday.std() / np.sqrt(len(byday)))) if len(byday) > 5 else None
        out.append({"top2": name, "n": int(len(s)),
                    "ex20_%": round(float(s.ex20.mean()) * 100, 2),
                    "ex40_%": round(float(e40.mean()) * 100, 2),
                    "ex40_t_byday": round(t40, 2) if t40 is not None else None,
                    "ex40_med_%": round(float(e40.median()) * 100, 2),
                    "ex40_win_%": round(float((e40 > 0).mean()) * 100, 1),
                    "ex60_%": round(float(s.ex60.mean()) * 100, 2)})
    res["by_top2"] = out

    # 用「实际可交易流通市值」再切一次（float_shares * (1 - top2/100) * close）
    m2 = m.merge(ins[["symbol", "float_shares"]], on="symbol", how="left", suffixes=("", "_i"))
    m2["free_mcap"] = m2.float_shares * (1 - m2.top2 / 100) * m2.close / 1e8
    m2 = m2.dropna(subset=["free_mcap"])
    m2["fq"] = pd.qcut(m2.free_mcap.rank(method="first"), 5, labels=False)
    res["by_free_mcap"] = []
    for q, s in m2.groupby("fq"):
        e40 = s.ex40.dropna()
        res["by_free_mcap"].append({
            "q": int(q), "n": int(len(s)),
            "free_mcap_med_yi": round(float(s.free_mcap.median()), 1),
            "ex40_%": round(float(e40.mean()) * 100, 2),
            "ex40_win_%": round(float((e40 > 0).mean()) * 100, 1)})

    # 九阳读数
    jy = agg[(agg.symbol == SYM)].sort_values("avail")
    if len(jy):
        r = jy.iloc[-1]
        res["jiuyang"] = {"top1": round(float(r.top1), 2), "top2": round(float(r.top2), 2),
                          "top10": round(float(r.top10), 2),
                          "free_mcap_yi": round(float(
                              ins[ins.symbol == SYM].float_shares.iloc[0] * (1 - r.top2 / 100)
                              * 8.61 / 1e8), 1)}
        res["jiuyang"]["pctile_top2_in_events"] = round(
            float((m.top2 < r.top2).mean()) * 100, 1)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
