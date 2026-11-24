"""9/11 之前到底有没有特征：把九阳 9/10 的每个可观测指标放回横截面。

问题重构：
  「有没有特征」是错的问法——任何股票在任何一天都有一堆特征。
  可测的是两件事：
    (1) 稀有度：九阳 9/10 的那些读数，在全市场排第几分位？
    (2) 有效性：把同样的读数当成筛选条件，2016-2026 全样本里
        后续 20/40 日的收益与涨停率是多少？命中多少只？
  只有「稀有 + 有效 + 能收窄到少数几只」三个同时成立，才叫前兆。

口径：
  - 全部特征只用 t 日及之前收盘
  - 入场 = t+1 开盘；停牌/涨跌停不剔除（剔除会人为抬高结果），另报涨停开盘占比
  - 超额 = 同日全市场等权（同买入口径）
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_precursor_jiuyang.json"
COLS = ["symbol", "date", "open", "high", "low", "close", "amount",
        "turnover_rate", "consecutive_limit_ups"]
T = pd.Timestamp("2026-09-10")
SYM = "002242.SZ"


def load():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["symbol"] = df["symbol"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)

    df["rn"] = g.cumcount()
    df["r1"] = g.close.pct_change()
    # 波动率
    df["std20"] = g.r1.transform(lambda s: s.rolling(20).std())
    df["std60"] = g.r1.transform(lambda s: s.rolling(60).std())
    df["amp1"] = (df.high - df.low) / df.close
    df["amp20"] = g["amp1"].transform(lambda s: s.rolling(20).mean())
    # 换手
    df["to20"] = g.turnover_rate.transform(lambda s: s.rolling(20).mean())
    df["to250"] = g.turnover_rate.transform(lambda s: s.rolling(250, min_periods=120).mean())
    df["to_squeeze"] = df.to20 / df.to250          # 换手压缩比
    df["to_ratio5"] = g.turnover_rate.transform(lambda s: s / s.shift(1).rolling(5).mean())
    # 位置
    df["hi250"] = g.close.transform(lambda s: s.rolling(250, min_periods=120).max())
    df["lo250"] = g.close.transform(lambda s: s.rolling(250, min_periods=120).min())
    df["dist_hi250"] = df.close / df.hi250 - 1
    df["dist_lo250"] = df.close / df.lo250 - 1
    df["pos250"] = (df.close - df.lo250) / (df.hi250 - df.lo250)
    # 动量
    for n in (20, 60, 120):
        df[f"ret{n}"] = g.close.pct_change(n)
    # 涨停历史
    df["lu"] = (df.consecutive_limit_ups > 0).astype(float)
    df["lu250"] = g.lu.transform(lambda s: s.rolling(250, min_periods=120).sum())
    # 阴线占比
    df["dn1"] = (df.r1 < 0).astype(float)
    df["dn20"] = g["dn1"].transform(lambda s: s.rolling(20).mean())
    # 前瞻
    df["entry"] = g.open.shift(-1)
    df["open_lu"] = df.entry / df.close - 1
    for h in (20, 40, 60):
        df[f"fwd{h}"] = g.close.shift(-h) / df.entry - 1
    df["fwd_lu20"] = g.lu.transform(
        lambda s: s.shift(-20).rolling(20).sum().shift(-0)).shift(0)
    return df


def pct_rank(day_df, col, val):
    s = day_df[col].dropna()
    if len(s) < 100 or not np.isfinite(val):
        return None
    return round(float((s < val).mean()) * 100, 1)


def main():
    df = load()
    day = df[df.date == T]
    jy = day[day.symbol == SYM]
    res = {"date": str(T.date()), "n_market": int(len(day)), "jiuyang": {}}
    if len(jy) == 0:
        raise SystemExit("九阳 9/10 不在面板")
    r = jy.iloc[0]

    feats = ["close", "turnover_rate", "to20", "to250", "to_squeeze", "to_ratio5",
             "std20", "std60", "amp20", "dist_hi250", "dist_lo250", "pos250",
             "ret20", "ret60", "ret120", "lu250", "dn20", "amount"]
    for f in feats:
        v = float(r[f])
        res["jiuyang"][f] = {
            "value": round(v, 4) if f != "amount" else round(v / 1e8, 2),
            "pctile": pct_rank(day, f, v),
        }
    res["jiuyang"]["amount_yi"] = round(float(r.amount) / 1e8, 2)

    # ---- 条件化检验：把「九阳式前兆」逐条加严，看命中数与后续
    base = df.dropna(subset=["dist_hi250", "to20", "std20", "entry", "fwd20"]).copy()
    base = base[(base.close >= 2) & (base.rn >= 250)]
    # 每日横截面分位
    base["to20_pct"] = base.groupby("date")["to20"].rank(pct=True)
    mkt20 = base.groupby("date")["fwd20"].mean()
    mkt40 = base.groupby("date")["fwd40"].mean()

    # 九阳 9/10 的真实读数：量比 3.57（99.3 分位）+ 20 日均换手 0.94%（15.6 分位）
    jy_conds = {
        "D1 量比>3 且 to20 最低20%": lambda d: (d.to_ratio5 > 3) & (d.to20_pct < 0.20),
        "D2 D1 且 dist_hi250 在-45~-15%": lambda d: (d.to_ratio5 > 3) & (d.to20_pct < 0.20) & d.dist_hi250.between(-0.45, -0.15),
        "D3 D2 且 250日有>=3次涨停": lambda d: (d.to_ratio5 > 3) & (d.to20_pct < 0.20) & d.dist_hi250.between(-0.45, -0.15) & (d.lu250 >= 3),
        "D4 量比>3 且 lu250>=3": lambda d: (d.to_ratio5 > 3) & (d.lu250 >= 3),
        "E1 量比>3（纯量能突变）": lambda d: d.to_ratio5 > 3,
        "E2 量比>5": lambda d: d.to_ratio5 > 5,
    }
    tab2 = []
    for name, fn in jy_conds.items():
        sub = base[fn(base)]
        if len(sub) < 30:
            tab2.append({"cond": name, "n_events": int(len(sub)), "note": "too few"})
            continue
        ex20 = (sub.fwd20 - sub.date.map(mkt20)).dropna()
        ex40 = (sub.fwd40 - sub.date.map(mkt40)).dropna()
        byday = ex40.groupby(sub.loc[ex40.index, "date"]).mean()
        t40 = float(byday.mean() / (byday.std() / np.sqrt(len(byday)))) if len(byday) > 5 else None
        r60 = sub.fwd60.dropna()
        tab2.append({
            "cond": name, "n_events": int(len(sub)),
            "n_per_day": round(len(sub) / sub.date.nunique(), 1),
            "fwd20_mean": round(float(sub.fwd20.mean()) * 100, 2),
            "ex20_mean": round(float(ex20.mean()) * 100, 2),
            "ex20_median": round(float(ex20.median()) * 100, 2),
            "lu20_rate": round(float((sub.fwd_lu20.fillna(0) > 0).mean()) * 100, 2),
            "ex40_mean": round(float(ex40.mean()) * 100, 2),
            "ex40_t_byday": round(t40, 2) if t40 is not None else None,
            "ex40_win": round(float((ex40 > 0).mean()) * 100, 1),
            "ex40_med": round(float(ex40.median()) * 100, 2),
            "fwd60_med": round(float(r60.median()) * 100, 1),
            "fwd60_p90": round(float(r60.quantile(.9)) * 100, 1),
            "fwd60_p99": round(float(r60.quantile(.99)) * 100, 1),
            "fwd60_max": round(float(r60.max()) * 100, 1),
            "fwd60_gt50": round(float((r60 > .5).mean()) * 100, 2),
            "fwd60_gt100": round(float((r60 > 1).mean()) * 100, 2),
        })
    res["jiuyang_style_conditions"] = tab2

    conds = {
        "C1 深跌 dist_hi250<-60%": lambda d: d.dist_hi250 < -0.60,
        "C2 +地量 to20<1%": lambda d: (d.dist_hi250 < -0.60) & (d.to20 < 1.0),
        "C3 +换手压缩 to20/to250<0.8": lambda d: (d.dist_hi250 < -0.60) & (d.to20 < 1.0) & (d.to_squeeze < 0.8),
        "C4 +低波 std20<2.5%": lambda d: (d.dist_hi250 < -0.60) & (d.to20 < 1.0) & (d.to_squeeze < 0.8) & (d.std20 < 0.025),
        "C5 +无涨停 lu250==0": lambda d: (d.dist_hi250 < -0.60) & (d.to20 < 1.0) & (d.to_squeeze < 0.8) & (d.std20 < 0.025) & (d.lu250 == 0),
        "C6 +横盘 |ret20|<8%": lambda d: (d.dist_hi250 < -0.60) & (d.to20 < 1.0) & (d.to_squeeze < 0.8) & (d.std20 < 0.025) & (d.lu250 == 0) & (d.ret20.abs() < 0.08),
    }
    tab = []
    for name, fn in conds.items():
        m = fn(base)
        sub = base[m]
        if len(sub) < 30:
            tab.append({"cond": name, "n": int(len(sub)), "note": "too few"})
            continue
        ex20 = (sub.fwd20 - sub.date.map(mkt20)).dropna()
        ex40 = (sub.fwd40 - sub.date.map(mkt40)).dropna()
        # 按日聚合 t（同日样本共享冲击）
        byday = ex40.groupby(sub.loc[ex40.index, "date"]).mean()
        t40 = float(byday.mean() / (byday.std() / np.sqrt(len(byday)))) if len(byday) > 5 else None
        tab.append({
            "cond": name, "n_events": int(len(sub)),
            "n_per_day": round(len(sub) / sub.date.nunique(), 1),
            "fwd20_mean": round(float(sub.fwd20.mean()) * 100, 2),
            "ex20_mean": round(float(ex20.mean()) * 100, 2),
            "ex20_median": round(float(ex20.median()) * 100, 2),
            "fwd_lu20_rate": round(float((sub.fwd_lu20.fillna(0) > 0).mean()) * 100, 2),
            "ex40_mean": round(float(ex40.mean()) * 100, 2),
            "ex40_t_byday": round(t40, 2) if t40 is not None else None,
            "ex40_win": round(float((ex40 > 0).mean()) * 100, 1),
        })
    res["conditions"] = tab

    # 基准：全样本
    allx = (base.fwd40 - base.date.map(mkt40)).dropna()
    res["baseline_all"] = {
        "n": int(len(allx)), "ex40_mean": round(float(allx.mean()) * 100, 2),
        "ex40_win": round(float((allx > 0).mean()) * 100, 1),
        "lu20_rate": round(float((base.fwd_lu20.fillna(0) > 0).mean()) * 100, 2),
    }

    # ---- 同位置、不同换手的对照（只让「地量」这一维变）
    deep = base[base.dist_hi250 < -0.60]
    deep = deep.assign(to_q=deep.groupby("date")["to20"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 50 else np.nan))
    ctrl = []
    for q in sorted(deep.to_q.dropna().unique()):
        sub = deep[deep.to_q == q]
        ex = (sub.fwd40 - sub.date.map(mkt40)).dropna()
        ctrl.append({"to20_quintile_in_deep": int(q), "n": int(len(sub)),
                     "to20_median": round(float(sub.to20.median()), 2),
                     "ex40_mean": round(float(ex.mean()) * 100, 2),
                     "ex40_win": round(float((ex > 0).mean()) * 100, 1)})
    res["control_deep_by_turnover"] = ctrl

    # ---- 关键：9/10 那天，每个条件命中多少只，九阳排第几
    hit = {}
    for name, fn in conds.items():
        m = fn(day)
        s = day[m].sort_values("dist_hi250")
        hit[name] = {"n_hit": int(len(s)), "jiuyang_in": bool(SYM in set(s.symbol))}
        if SYM in set(s.symbol):
            hit[name]["rank_by_dist_hi"] = int(list(s.symbol).index(SYM) + 1)
    res["hits_on_20260910"] = hit

    # 九阳式条件在 9/10 的命中数
    hit2 = {}
    dayb = base[base.date == T].copy()
    dayb["to20_pct"] = dayb["to20"].rank(pct=True)
    for name, fn in jy_conds.items():
        s = dayb[fn(dayb)].sort_values("to_ratio5", ascending=False)
        hit2[name] = {"n_hit": int(len(s)), "jiuyang_in": bool(SYM in set(s.symbol))}
        if SYM in set(s.symbol):
            hit2[name]["jiuyang_rank"] = int(list(s.symbol).index(SYM) + 1)
    res["jiuyang_hits_on_20260910"] = hit2

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
