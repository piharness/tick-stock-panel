"""第二步：三个更硬的检验。

1) 配对差：同一天、同池内「回调日」减「上涨日」，消掉市场共同因子。
2) 额度模型：r3（近3日累计涨幅）越低 = 离 20% 阈值越远 = 「额度越足」。
   若「绕异动」因果成立，r3 应与后续收益负相关。这是对用户因果模型的直接证伪。
3) 九阳 10-22 / 11-03 两个「回调上车点」在同形态样本里的分位。
4) 口径敏感性：T 收盘买 vs T+1 开盘买（跳空吃掉多少）。
"""
import json
import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_pullback_shape_step3.json"
HORIZONS = [1, 3, 5, 10, 20]
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close",
        "volume", "amount", "turnover_rate"]


def load():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["r1"] = g.close.pct_change() * 100
    df["r3"] = g.close.pct_change(3) * 100
    df["r20"] = g.close.pct_change(20) * 100
    df["n"] = g.cumcount()
    for w in (5, 10, 20):
        df[f"ma{w}"] = g.close.transform(lambda s: s.rolling(w).mean())
    df["vol5"] = g.volume.transform(lambda s: s.rolling(5).mean().shift(1))
    df["vr"] = df.volume / df.vol5.replace(0, np.nan)
    df["cl_pos"] = (df.close - df.low) / (df.high - df.low).replace(0, np.nan)
    df["dma10"] = (df.close / df.ma10 - 1) * 100
    df["buy_o"] = g.open.shift(-1)
    for h in HORIZONS:
        df[f"fo{h}"] = (g.close.shift(-h) / df["buy_o"] - 1) * 100      # T+1 开盘买
        df[f"fc{h}"] = (g.close.shift(-h) / df.close - 1) * 100         # T 收盘买
    df["hi20"] = g.close.transform(lambda s: s.rolling(20).max())
    df["hi20_prev"] = g.close.transform(lambda s: s.rolling(20).max().shift(1))
    df["fmax5"] = g.close.transform(lambda s: s.rolling(5).max().shift(-5))
    return df


def daily_mean(ev, col):
    per = ev.dropna(subset=[col]).groupby("date")[col].mean()
    return per


def tstat(per):
    if len(per) < 3:
        return np.nan
    return per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))


def main():
    df = load()
    res = {}
    base = {h: df.groupby("date")[f"fo{h}"].mean() for h in HORIZONS}
    base_df = pd.DataFrame(base)

    d = df[(df.n >= 250) & (df.raw_close > 2)].copy()
    d = d[d.date >= "2016-02-01"]
    for h in HORIZONS:
        d[f"ex{h}"] = d[f"fo{h}"] - d.date.map(base_df[h])
        d[f"exc{h}"] = d[f"fc{h}"] - d.date.map(base_df[h])
    strong = d[d.r20 >= 30].copy()
    strong["is_pb"] = strong.r1 < 0

    # ---------- 1. 配对差 ----------
    rows = []
    for h in HORIZONS:
        a = daily_mean(strong[strong.is_pb], f"ex{h}")
        b = daily_mean(strong[~strong.is_pb], f"ex{h}")
        j = pd.concat([a.rename("pb"), b.rename("up")], axis=1).dropna()
        diff = j.pb - j.up
        rows.append({"h": h, "days": len(j), "pb": round(a.mean(), 3), "up": round(b.mean(), 3),
                     "diff": round(diff.mean(), 3), "t_diff": round(tstat(diff), 2),
                     "diff_med": round(diff.median(), 3),
                     "pos_share": round(float((diff > 0).mean()), 3)})
    res["paired_pb_minus_up"] = rows

    # ---------- 2. 额度模型：r3 分桶 ----------
    pb = strong[strong.is_pb].copy()
    pb["r3b"] = pd.cut(pb.r3, [-99, -5, 0, 5, 10, 15, 20, 25, 99],
                       labels=["<-5", "-5~0", "0~5", "5~10", "10~15", "15~20", "20~25", ">25"])
    rows = []
    for k, sub in pb.dropna(subset=["r3b"]).groupby("r3b", observed=True):
        per = daily_mean(sub, "ex5")
        per10 = daily_mean(sub, "ex10")
        rows.append({"r3": str(k), "n": int(len(sub)), "days": int(per.notna().sum()),
                     "ex5": round(per.mean(), 2), "t5": round(tstat(per), 2),
                     "ex10": round(per10.mean(), 2), "t10": round(tstat(per10), 2)})
    res["r3_budget_h5h10"] = rows

    # 连续版：日度截面回归 ex5 ~ r3（去均值后）
    reg = []
    for y, sub in pb.groupby(pb.date.dt.year):
        s = sub.dropna(subset=["ex5", "r3"])
        if len(s) < 200:
            continue
        x = s.r3 - s.r3.mean()
        yv = s.ex5 - s.ex5.mean()
        beta = (x * yv).sum() / (x * x).sum()
        reg.append({"y": int(y), "n": len(s), "beta_bp_per_1pct_r3": round(beta, 3)})
    res["r3_slope_by_year"] = reg

    # ---------- 3. 九阳分位定位 ----------
    j = d[d.symbol == "002242.SZ"].set_index("date")
    res["jiuyang_points"] = []
    for dt in ["2026-10-22", "2026-10-23", "2026-10-30", "2026-11-03", "2026-11-12"]:
        ts = pd.Timestamp(dt)
        if ts not in j.index:
            continue
        row = j.loc[ts]
        # 同形态：r20 分位 ±5pct、r1 同档、vr 同档
        cand = strong[(strong.date == ts)]
        pool = strong[(strong.r20.between(row.r20 - 25, row.r20 + 25)) &
                      (strong.r1.between(row.r1 - 1.5, row.r1 + 1.5))]
        res["jiuyang_points"].append({
            "date": dt, "r1": round(float(row.r1), 2), "r3": round(float(row.r3), 2),
            "r20": round(float(row.r20), 2), "vr": round(float(row.vr), 2),
            "cl_pos": round(float(row.cl_pos), 3), "dma10": round(float(row.dma10), 2),
            "f5": round(float(row.fo5), 2) if row.fo5 == row.fo5 else None,
            "f10": round(float(row.fo10), 2) if row.fo10 == row.fo10 else None,
            "f20": round(float(row.fo20), 2) if row.fo20 == row.fo20 else None,
            "same_shape_n": int(len(pool)),
            "same_shape_f5_med": round(float(pool.fo5.median()), 2) if len(pool) else None,
            "same_shape_f5_pct_of_jy": round(float((pool.fo5 < row.fo5).mean() * 100), 1) if len(pool) and row.fo5 == row.fo5 else None,
            "same_shape_f20_med": round(float(pool.fo20.median()), 2) if len(pool) else None,
        })

    # ---------- 4. 口径敏感性：T收盘买 ----------
    rows = []
    for h in [3, 5, 10]:
        a = daily_mean(strong[strong.is_pb], f"exc{h}")
        b = daily_mean(strong[~strong.is_pb], f"exc{h}")
        rows.append({"h": h, "pb_close_buy": round(a.mean(), 3), "up_close_buy": round(b.mean(), 3),
                     "gap_cost": round(a.mean() - daily_mean(strong[strong.is_pb], f"ex{h}").mean(), 3)})
    res["close_vs_open_buy"] = rows

    # ---------- 5. 命中率（明确标注：概率，不是钱） ----------
    pb2 = strong[strong.is_pb].copy()
    pb2 = pb2.dropna(subset=["hi20_prev", "fmax5"])
    pb2["hit"] = pb2.fmax5 > pb2.hi20_prev
    up2 = strong[~strong.is_pb].dropna(subset=["hi20_prev", "fmax5"])
    up2 = up2.assign(hit=up2.fmax5 > up2.hi20_prev)
    res["hit_rate_5d_new_high"] = {
        "pullback": round(float(pb2.hit.mean()) * 100, 2),
        "upday": round(float(up2.hit.mean()) * 100, 2),
        "n_pb": int(len(pb2)), "n_up": int(len(up2)),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    for k in ["paired_pb_minus_up", "r3_budget_h5h10", "r3_slope_by_year",
              "close_vs_open_buy", "hit_rate_5d_new_high"]:
        print(f"\n=== {k} ===")
        v = res[k]
        print(pd.DataFrame(v).to_string(index=False) if isinstance(v, list) else json.dumps(v, ensure_ascii=False))
    print("\n=== jiuyang_points ===")
    print(pd.DataFrame(res["jiuyang_points"]).to_string(index=False))


if __name__ == "__main__":
    main()
