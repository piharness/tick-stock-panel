"""第三步：修正同期性偏差 + 找「唯一可能为正」的那一格。

修两处：
  - 命中率原口径（5日内创新高）在「上涨日」组有同期性偏差：当日就已创新高。
    改成「当日收盘未破前20日最高」作为共同前提，再看未来 5 日是否突破。
  - 九阳 f20 缺数据不是 bug（10-22 之后 20 个交易日超出面板），用 f15 替代。

找的正格子：r20 分档 × 回调，看有没有哪一格 ex20 转正。
"""
import json
import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_pullback_shape_step4.json"
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close",
        "volume", "amount", "turnover_rate"]


def load():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["r1"] = g.close.pct_change() * 100
    df["r5"] = g.close.pct_change(5) * 100
    df["r20"] = g.close.pct_change(20) * 100
    df["r60"] = g.close.pct_change(60) * 100
    df["n"] = g.cumcount()
    for w in (5, 10, 20):
        df[f"ma{w}"] = g.close.transform(lambda s: s.rolling(w).mean())
    df["vol5"] = g.volume.transform(lambda s: s.rolling(5).mean().shift(1))
    df["vr"] = df.volume / df.vol5.replace(0, np.nan)
    df["cl_pos"] = (df.close - df.low) / (df.high - df.low).replace(0, np.nan)
    df["dma10"] = (df.close / df.ma10 - 1) * 100
    df["dma5"] = (df.close / df.ma5 - 1) * 100
    df["buy_o"] = g.open.shift(-1)
    for h in [5, 10, 15, 20]:
        df[f"fo{h}"] = (g.close.shift(-h) / df["buy_o"] - 1) * 100
    df["hi20_prev"] = g.close.transform(lambda s: s.rolling(20).max().shift(1))
    df["fmax5"] = g.close.transform(lambda s: s.rolling(5).max().shift(-5))
    return df


def dm(ev, col):
    return ev.dropna(subset=[col]).groupby("date")[col].mean()


def ts(per):
    return np.nan if len(per) < 3 else per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))


def main():
    df = load()
    res = {}
    base = {h: df.groupby("date")[f"fo{h}"].mean() for h in [5, 10, 15, 20]}
    bdf = pd.DataFrame(base)
    d = df[(df.n >= 250) & (df.raw_close > 2)].copy()
    d = d[d.date >= "2016-02-01"]
    for h in [5, 10, 15, 20]:
        d[f"ex{h}"] = d[f"fo{h}"] - d.date.map(bdf[h])
    strong = d[d.r20 >= 30].copy()
    strong["is_pb"] = strong.r1 < 0

    # ---------- 1. 修命中率 ----------
    s = strong.dropna(subset=["hi20_prev", "fmax5"]).copy()
    s = s[s.close < s.hi20_prev]        # 共同前提：当日未创新高
    s["hit"] = s.fmax5 > s.hi20_prev
    res["hit_5d_fair"] = {
        "pullback": round(float(s[s.is_pb].hit.mean()) * 100, 2),
        "upday": round(float(s[~s.is_pb].hit.mean()) * 100, 2),
        "n_pb": int(s.is_pb.sum()), "n_up": int((~s.is_pb).sum()),
        "note": "共同前提=当日收盘未破前20日最高；原口径在上涨日有同期性偏差",
    }

    # ---------- 2. r1 连续关系（回调日） ----------
    pb = strong[strong.is_pb].copy()
    pb["r1b"] = pd.cut(pb.r1, [-11, -8, -6, -4, -2, -1, 0])
    rows = []
    for k, sub in pb.groupby("r1b", observed=True):
        p5, p20 = dm(sub, "ex5"), dm(sub, "ex20")
        rows.append({"r1": str(k), "n": int(len(sub)), "ex5": round(p5.mean(), 2),
                     "t5": round(ts(p5), 2), "ex20": round(p20.mean(), 2), "t20": round(ts(p20), 2)})
    res["r1_depth_continuous"] = rows

    # ---------- 3. r20 分档 × 回调 ----------
    strong["r20b"] = pd.cut(strong.r20, [30, 40, 50, 65, 85, 120, 200, 10000],
                            labels=["30-40", "40-50", "50-65", "65-85", "85-120", "120-200", ">200"])
    rows = []
    for k, sub in strong.groupby("r20b", observed=True):
        for grp, s2 in [("pb", sub[sub.is_pb]), ("up", sub[~sub.is_pb])]:
            p20 = dm(s2, "ex20")
            rows.append({"r20": str(k), "grp": grp, "n": int(len(s2)),
                         "ex5": round(dm(s2, "ex5").mean(), 2),
                         "ex20": round(p20.mean(), 2), "t20": round(ts(p20), 2)})
    res["r20_x_pb_h20"] = rows

    # ---------- 4. 全样本（不设 r20 门槛）的 r20 分档，看池子本身 ----------
    d["r20b"] = pd.cut(d.r20, [-100, -20, -10, 0, 10, 20, 30, 40, 50, 65, 85, 120, 200, 10000],
                       labels=["<-20", "-20~-10", "-10~0", "0~10", "10~20", "20~30", "30~40",
                               "40~50", "50~65", "65~85", "85~120", "120~200", ">200"])
    rows = []
    for k, sub in d.groupby("r20b", observed=True):
        p20 = dm(sub, "ex20")
        rows.append({"r20": str(k), "n": int(len(sub)), "ex5": round(dm(sub, "ex5").mean(), 3),
                     "ex20": round(p20.mean(), 3), "t20": round(ts(p20), 2)})
    res["r20_all_h20"] = rows

    # ---------- 5. 找正格：r20 30-50 + 回调 + 缩量 ----------
    cand = strong[(strong.is_pb) & (strong.r20 < 50)]
    rows = []
    for name, sub in [
        ("30-50 回调", cand),
        ("30-50 回调 & vr<0.8", cand[cand.vr < 0.8]),
        ("30-50 回调 & vr<0.8 & 守MA10", cand[(cand.vr < 0.8) & (cand.dma10 > 0)]),
        ("30-50 回调 & 破MA5", cand[cand.dma5 < 0]),
        ("30-50 回调 & cl_pos<0.3", cand[cand.cl_pos < 0.3]),
        ("30-50 回调 & r5<5", cand[cand.r5 < 5]),
    ]:
        if len(sub) == 0:
            continue
        for h in [5, 20]:
            p = dm(sub, f"ex{h}")
            rows.append({"sel": name, "h": h, "n": int(len(sub)), "days": int(p.notna().sum()),
                         "ex": round(p.mean(), 2), "t": round(ts(p), 2),
                         "ex_med": round(sub[f"ex{h}"].median(), 2)})
    res["positive_cells"] = rows

    # ---------- 6. 正格分年（h=20） ----------
    sel = cand[(cand.vr < 0.8) & (cand.dma5 < 0)]
    yr = []
    for y, sub in sel.groupby(sel.date.dt.year):
        p = dm(sub, "ex20")
        yr.append({"y": int(y), "n": int(len(sub)), "ex20": round(p.mean(), 2), "days": int(p.notna().sum())})
    res["pos_cell_by_year"] = yr

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    for k in ["hit_5d_fair", "r1_depth_continuous", "r20_x_pb_h20", "r20_all_h20",
              "positive_cells", "pos_cell_by_year"]:
        print(f"\n=== {k} ===")
        v = res[k]
        print(pd.DataFrame(v).to_string(index=False) if isinstance(v, list) else json.dumps(v, ensure_ascii=False))


if __name__ == "__main__":
    main()
