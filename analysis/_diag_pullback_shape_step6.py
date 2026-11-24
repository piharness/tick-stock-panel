"""第五步：「额度」到底是什么，它有没有独立信息。

上一条回答把 r20 当「额度」用了，这是概念混用：
  r20 = 已经涨了多少（位置）；
  额度 = 还剩多少空间（约束下的余量）。
两者可以是同一个变量，但必须先证明，不能假定。

四个候选额度（都可测、都先于信号可得）：
  A budget3   = 20 − dev3       异动第1条剩余（用户原始叙事里的「额度」）
  B budget10  = 100 − dev10     严重异常波动剩余（真正的停牌线）
  C to20sum   = 过去20日换手率之和  筹码额度（浮筹换完了没有）
  D days_since_peak = 距 60 日内 r20 峰值的天数  时间额度
  E dist_ma250 = close/ma250−1   估值/位置额度

检验方式：双排序。先按 r20 五档切开，档内再按额度三分位。
  若额度在每一档内都没有单调性 ⇒ 额度只是 r20 的影子。
再做 FWL：ex20 对 r20 五档哑变量回归取残差，看残差是否随额度单调（增量信息）。
"""
import json
import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_pullback_shape_step6.json"
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close",
        "volume", "amount", "turnover_rate"]


def load():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["r1"] = g.close.pct_change() * 100
    df["r3"] = g.close.pct_change(3) * 100
    df["r10"] = g.close.pct_change(10) * 100
    df["r20"] = g.close.pct_change(20) * 100
    df["n"] = g.cumcount()
    df["ma250"] = g.close.transform(lambda s: s.rolling(250).mean())
    df["to20sum"] = g.turnover_rate.transform(lambda s: s.rolling(20).sum())
    df["hi250"] = g.close.transform(lambda s: s.rolling(250).max())
    df["buy_o"] = g.open.shift(-1)
    for h in [5, 20]:
        df[f"fo{h}"] = (g.close.shift(-h) / df["buy_o"] - 1) * 100
    # 距 60 日内 r20 峰值的天数
    df["r20peak"] = g.r20.transform(lambda s: s.rolling(60, min_periods=20).max())
    hit = (df.r20 >= df.r20peak * 0.999).astype(int)
    grp = (hit == 0).cumsum()
    df["days_since_peak"] = df.groupby([df.symbol, grp]).cumcount()
    return df


def dm(ev, col):
    return ev.dropna(subset=[col]).groupby("date")[col].mean()


def ts(per):
    return np.nan if len(per) < 3 else per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))


def main():
    df = load()
    res = {}
    m3 = df.groupby("date").r3.mean()
    m10 = df.groupby("date").r10.mean()
    base = {h: df.groupby("date")[f"fo{h}"].mean() for h in [5, 20]}
    bdf = pd.DataFrame(base)
    d = df[(df.n >= 250) & (df.raw_close > 2)].copy()
    d = d[d.date >= "2016-02-01"]
    d["dev3"] = d.r3 - d.date.map(m3)
    d["dev10"] = d.r10 - d.date.map(m10)
    for h in [5, 20]:
        d[f"ex{h}"] = d[f"fo{h}"] - d.date.map(bdf[h])
    s = d[d.r20 >= 30].copy()

    s["budget3"] = 20 - s.dev3
    s["budget10"] = 100 - s.dev10
    s["dist_ma250"] = (s.close / s.ma250 - 1) * 100
    cands = ["budget3", "budget10", "to20sum", "days_since_peak", "dist_ma250"]

    # ---------- 相关性：额度 vs r20 ----------
    corr = {c: round(float(s[[c, "r20"]].dropna().corr().iloc[0, 1]), 3) for c in cands}
    res["corr_with_r20"] = corr

    # ---------- 1. 单变量：额度五分位 → ex20 ----------
    rows = []
    for c in cands:
        x = s.dropna(subset=[c])
        q = pd.qcut(x[c], 5, labels=False, duplicates="drop")
        for k in sorted(q.dropna().unique()):
            sub = x[q == k]
            p = dm(sub, "ex20")
            rows.append({"var": c, "q": int(k) + 1, "n": int(len(sub)),
                         "lo": round(float(sub[c].min()), 2), "hi": round(float(sub[c].max()), 2),
                         "ex5": round(dm(sub, "ex5").mean(), 2),
                         "ex20": round(p.mean(), 2), "t20": round(ts(p), 2)})
    res["univariate_q5"] = rows

    # ---------- 2. 双排序：r20 五档 × 额度三分位 ----------
    s["r20b"] = pd.cut(s.r20, [30, 45, 60, 85, 140, 1e9],
                       labels=["30-45", "45-60", "60-85", "85-140", "140+"])
    rows = []
    for c in cands:
        x = s.dropna(subset=[c, "r20b"]).copy()
        x["qb"] = x.groupby("r20b", observed=True)[c].transform(
            lambda v: pd.qcut(v, 3, labels=False, duplicates="drop"))
        for rb, sub in x.groupby("r20b", observed=True):
            line = {"var": c, "r20": str(rb), "n": int(len(sub))}
            for k in [0, 1, 2]:
                s2 = sub[sub.qb == k]
                line[f"T{k+1}_ex20"] = round(dm(s2, "ex20").mean(), 2) if len(s2) > 100 else None
                line[f"T{k+1}_n"] = int(len(s2))
            line["spread_T3_T1"] = (round(line["T3_ex20"] - line["T1_ex20"], 2)
                                    if line["T3_ex20"] is not None and line["T1_ex20"] is not None else None)
            rows.append(line)
    res["double_sort_ex20"] = rows

    # ---------- 3. FWL 残差：额度对 ex20 的增量 ----------
    rows = []
    for c in cands:
        x = s.dropna(subset=[c, "ex20", "r20b"]).copy()
        # 去掉 r20 五档均值
        x["res"] = x.ex20 - x.groupby("r20b").ex20.transform("mean")
        x["qb"] = pd.qcut(x[c], 5, labels=False, duplicates="drop")
        line = {"var": c, "n": int(len(x))}
        vals = []
        for k in range(5):
            sub = x[x.qb == k]
            p = dm(sub, "res")
            v = p.mean()
            vals.append(v)
            line[f"Q{k+1}"] = round(v, 3)
            line[f"t{k+1}"] = round(ts(p), 2)
        # Q5-Q1 差与线性斜率
        line["Q5_Q1"] = round(vals[4] - vals[0], 3)
        xx = x[c] - x[c].mean()
        line["slope_bp_per_unit"] = round(float((xx * x.res).sum() / (xx * xx).sum() * 100), 2)
        rows.append(line)
    res["fwl_increment"] = rows

    # ---------- 4. 九阳当前的额度读数 ----------
    j = d[d.symbol == "002242.SZ"].copy()
    j["dev3"] = j.r3 - j.date.map(m3)
    j["dev10"] = j.r10 - j.date.map(m10)
    j["budget3"] = 20 - j.dev3
    j["budget10"] = 100 - j.dev10
    j["dist_ma250"] = (j.close / j.ma250 - 1) * 100
    seg = j[j.date >= "2026-11-13"]
    res["jiuyang_budget"] = [
        {"date": str(r.date.date()), "r20": round(float(r.r20), 1),
         "budget3": round(float(r.budget3), 1), "budget10": round(float(r.budget10), 1),
         "to20sum": round(float(r.to20sum), 0), "dist_ma250": round(float(r.dist_ma250), 1),
         "days_since_peak": int(r.days_since_peak)}
        for _, r in seg.iterrows()
    ]

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("corr with r20:", json.dumps(corr, ensure_ascii=False))
    print("\n=== 单变量五分位 ex20 ===")
    print(pd.DataFrame(res["univariate_q5"]).to_string(index=False))
    print("\n=== 双排序（档内三分位） ===")
    print(pd.DataFrame(res["double_sort_ex20"]).to_string(index=False))
    print("\n=== FWL 增量 ===")
    print(pd.DataFrame(res["fwl_increment"]).to_string(index=False))
    print("\n=== 九阳 ===")
    print(pd.DataFrame(res["jiuyang_budget"]).to_string(index=False))


if __name__ == "__main__":
    main()
