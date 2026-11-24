"""第四步：断点检验 —— 「异动阈值」到底有没有价格代价。

逻辑链：
  用户的因果模型 = 资金为躲避异动而控制涨幅 ⇒ 盘面出现「主动回调」。
  可证伪推论 A：全市场 3 日涨幅在 20% 处应有堆积/断档（已否，比值完全连续）。
  可证伪推论 B：跨过 20% 的股票应受「异动公告/核查」惩罚 ⇒ 在 r3=20 处应有负向跳跃。
                 若断点处无跳跃，则「规避」没有经济动机，因果模型不成立。

方法：模糊断点（fuzzy RD）。r3 是名义断点，真实判定用「偏离值」= r3 − 全市场等权 3 日收益，
     所以 20% 附近存在测量误差。窗口 ±2pct，局部线性 + 左右各 1pct 的均值差。
"""
import json
import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_pullback_shape_step5.json"
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close", "volume", "amount"]


def load():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["r1"] = g.close.pct_change() * 100
    df["r3"] = g.close.pct_change(3) * 100
    df["r20"] = g.close.pct_change(20) * 100
    df["n"] = g.cumcount()
    df["buy_o"] = g.open.shift(-1)
    for h in [1, 5, 10, 20]:
        df[f"fo{h}"] = (g.close.shift(-h) / df["buy_o"] - 1) * 100
    return df


def dm(ev, col):
    return ev.dropna(subset=[col]).groupby("date")[col].mean()


def ts(per):
    return np.nan if len(per) < 3 else per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))


def main():
    df = load()
    res = {}
    # 市场 3 日等权基准（做偏离值）
    m3 = df.groupby("date").r3.mean()
    base = {h: df.groupby("date")[f"fo{h}"].mean() for h in [1, 5, 10, 20]}
    bdf = pd.DataFrame(base)
    d = df[(df.n >= 250) & (df.raw_close > 2)].copy()
    d = d[d.date >= "2016-02-01"]
    d["dev3"] = d.r3 - d.date.map(m3)          # 3 日偏离值（近似，未按板块指数分别算）
    for h in [1, 5, 10, 20]:
        d[f"ex{h}"] = d[f"fo{h}"] - d.date.map(bdf[h])

    # ---------- 1. 断点两侧 1pct 窗的均值 ----------
    rows = []
    for thr in [18, 19, 20, 21, 22]:
        for side, lo, hi in [("left", thr - 1, thr), ("right", thr, thr + 1)]:
            sub = d[(d.r3 >= lo) & (d.r3 < hi)]
            if len(sub) < 200:
                continue
            rows.append({"thr": thr, "side": side, "n": int(len(sub)),
                         "ex1": round(dm(sub, "ex1").mean(), 3),
                         "ex5": round(dm(sub, "ex5").mean(), 3),
                         "ex20": round(dm(sub, "ex20").mean(), 3)})
    res["local_means"] = rows

    # ---------- 2. 局部线性 RD（窗口 ±2, ±3, ±5） ----------
    rd = []
    for w in [2, 3, 5]:
        sub = d[(d.r3 >= 20 - w) & (d.r3 < 20 + w)].dropna(subset=["ex5", "ex20"]).copy()
        sub["D"] = (sub.r3 >= 20).astype(float)
        sub["x"] = sub.r3 - 20
        rows_w = []
        for h in [1, 5, 10, 20]:
            s = sub.dropna(subset=[f"ex{h}"]).copy()
            # 去日均值后回归 ex ~ D + x + D:x
            s["y"] = s[f"ex{h}"] - s.groupby("date")[f"ex{h}"].transform("mean")
            X = np.column_stack([np.ones(len(s)), s.D, s.x, s.D * s.x])
            beta, *_ = np.linalg.lstsq(X, s.y.values, rcond=None)
            # 稳健 SE：按日聚类（简化：按日聚合残差乘积）
            resid = s.y.values - X @ beta
            XtX_inv = np.linalg.pinv(X.T @ X)
            meat = (X * resid[:, None]).T @ (X * resid[:, None])
            V = XtX_inv @ meat @ XtX_inv
            se = np.sqrt(np.diag(V))[1]
            rows_w.append({"w": w, "h": h, "n": int(len(s)), "jump_bp": round(beta[1] * 100, 1),
                           "se_bp": round(se * 100, 1), "t": round(beta[1] / se, 2) if se > 0 else None})
        rd.extend(rows_w)
    res["rd_local_linear"] = rd

    # ---------- 3. 偏离值口径（真实阈值变量） ----------
    rows = []
    for thr in [20]:
        for side, lo, hi in [("left", thr - 1, thr), ("right", thr, thr + 1),
                             ("left2", thr - 3, thr), ("right2", thr, thr + 3)]:
            sub = d[(d.dev3 >= lo) & (d.dev3 < hi)]
            if len(sub) < 200:
                continue
            rows.append({"side": side, "n": int(len(sub)),
                         "ex1": round(dm(sub, "ex1").mean(), 3),
                         "ex5": round(dm(sub, "ex5").mean(), 3),
                         "ex20": round(dm(sub, "ex20").mean(), 3)})
    res["dev3_local"] = rows

    # ---------- 4. 「异动当天」vs「差一点」的次日行为 ----------
    # 若异动有代价，右肩应有更高的「次日下跌概率」
    lft = d[(d.r3 >= 19) & (d.r3 < 20)]
    rgt = d[(d.r3 >= 20) & (d.r3 < 21)]
    res["nextday_behavior"] = {
        "left_n": int(len(lft)), "right_n": int(len(rgt)),
        "left_ret1_mean": round(float(lft.r1.mean()), 3),
        "right_ret1_mean": round(float(rgt.r1.mean()), 3),
        "left_next_up_share": round(float((lft.fo1 > 0).mean()) * 100, 2),
        "right_next_up_share": round(float((rgt.fo1 > 0).mean()) * 100, 2),
        "left_next_open_gap": round(float((lft.buy_o / lft.close - 1).mean() * 100), 3),
        "right_next_open_gap": round(float((rgt.buy_o / rgt.close - 1).mean() * 100), 3),
    }

    # ---------- 5. 九阳实际偏离值 ----------
    j = d[d.symbol == "002242.SZ"].copy()
    j["m3"] = j.date.map(m3)
    seg = j[(j.date >= "2026-10-08") & (j.date <= "2026-11-18")]
    res["jiuyang_dev3"] = [
        {"date": str(r.date.date()), "r3": round(float(r.r3), 2),
         "mkt3": round(float(r.m3), 2), "dev3": round(float(r.dev3), 2),
         "breach20": bool(r.dev3 >= 20)} for _, r in seg.iterrows()
    ]
    res["jiuyang_breach_days"] = int(sum(x["breach20"] for x in res["jiuyang_dev3"]))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("=== local_means ===")
    print(pd.DataFrame(res["local_means"]).to_string(index=False))
    print("\n=== rd_local_linear ===")
    print(pd.DataFrame(res["rd_local_linear"]).to_string(index=False))
    print("\n=== dev3_local ===")
    print(pd.DataFrame(res["dev3_local"]).to_string(index=False))
    print("\n=== nextday ===")
    print(json.dumps(res["nextday_behavior"], ensure_ascii=False, indent=1))
    print("\n=== jiuyang dev3 ===")
    print(pd.DataFrame(res["jiuyang_dev3"]).to_string(index=False))
    print("breach days(>=20):", res["jiuyang_breach_days"], "/", len(res["jiuyang_dev3"]))


if __name__ == "__main__":
    main()
