"""第六步：把「赌」放到赔率上算，而不是均值上算。

用户的论点：不需要普世规律，只要一次风险收益比划算的赌。
那就必须回答三件事，且必须用「事前」口径：
  1. P(赚) 与 盈亏比（赢时平均赚 / 输时平均亏）分别是多少 —— 划算与否由这两者相乘决定
  2. 加止盈止损后，赔付结构变好还是变坏 —— 凸性到底能不能被线性持有拿到
  3. Kelly 仓位是多少 —— 若为 0，任何仓位都是负几何增长

事件：首次 40 日涨幅 ≥ 100%（两个月翻倍），60 日冷却去重。
入场：T+1 开盘。出场：持有 20/60 日收盘，或触发的止盈/止损。
同日双触发按「先止损」计（悲观）。
"""
import json
import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_pullback_shape_step7.json"
COLS = ["symbol", "date", "open", "high", "low", "close", "raw_close", "volume", "amount"]


def load():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["r40"] = g.close.pct_change(40) * 100
    df["r60"] = g.close.pct_change(60) * 100
    df["n"] = g.cumcount()
    df["buy"] = g.open.shift(-1)
    return df, g


def stats(x, label):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 20:
        return {"event": label, "n": len(x)}
    win = x[x > 0]
    loss = x[x <= 0]
    p = float((x > 0).mean())
    avg_w = float(win.mean()) if len(win) else 0.0
    avg_l = float(-loss.mean()) if len(loss) else 0.0
    b = avg_w / avg_l if avg_l > 0 else np.nan
    ev = float(x.mean())
    kelly = (p * b - (1 - p)) / b if b and b > 0 else np.nan
    return {"event": label, "n": len(x), "mean": round(ev, 2), "median": round(float(np.median(x)), 2),
            "p10": round(float(np.percentile(x, 10)), 2), "p90": round(float(np.percentile(x, 90)), 2),
            "skew": round(float(pd.Series(x).skew()), 2), "win_rate": round(p * 100, 1),
            "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2), "odds": round(b, 2),
            "need_win": round(100 / (1 + b), 1) if b and b > 0 else None,
            "kelly": round(kelly, 3) if kelly == kelly else None}


def main():
    df, g = load()
    res = {}
    d = df[(df.n >= 250) & (df.raw_close > 2)].copy()
    d = d[d.date >= "2016-02-01"]

    # ---------- 1. 事件抽取：首次 r40>=100，60 日冷却 ----------
    ev = d[d.r40 >= 100].copy()
    ev = ev.sort_values(["symbol", "date"])
    keep, last = [], {}
    for sym, dt in zip(ev.symbol.values, ev.date.values):
        prev = last.get(sym)
        if prev is None or (dt - prev).astype("timedelta64[D]").astype(int) > 90:
            keep.append(True)
            last[sym] = dt
        else:
            keep.append(False)
    ev = ev[np.array(keep)].copy()
    res["event_count"] = {"n": int(len(ev)), "symbols": int(ev.symbol.nunique()),
                          "days": int(ev.date.nunique()),
                          "first": str(ev.date.min().date()), "last": str(ev.date.max().date())}
    res["event_by_year"] = {int(y): int(v) for y, v in ev.groupby(ev.date.dt.year).size().items()}

    # 未来收益（持有到 T+20 / T+60 收盘）
    for h in [20, 60]:
        ev[f"f{h}"] = (g.close.shift(-h).reindex(ev.index) / ev.buy - 1) * 100

    # ---------- 2. 无条件赔率 ----------
    res["odds_fixed_horizon"] = [
        stats(ev.f20, "翻倍后 20 日"),
        stats(ev.f60, "翻倍后 60 日"),
    ]
    # 基线：同一时点全市场
    base_s = d.dropna(subset=["buy"]).copy()
    base_s["f20b"] = (g.close.shift(-20).reindex(base_s.index) / base_s.buy - 1) * 100
    base_s["f60b"] = (g.close.shift(-60).reindex(base_s.index) / base_s.buy - 1) * 100
    res["odds_baseline"] = [
        stats(base_s.f20b.sample(min(300000, len(base_s)), random_state=7), "全市场 20 日"),
        stats(base_s.f60b.sample(min(300000, len(base_s)), random_state=7), "全市场 60 日"),
    ]

    # ---------- 3. 赔付网格：止盈/止损（20 日窗口，先止损） ----------
    dh = df.copy()
    dh["fh20"] = g.high.transform(lambda s: s.rolling(20, min_periods=1).max().shift(-20))
    dh["fl20"] = g.low.transform(lambda s: s.rolling(20, min_periods=1).min().shift(-20))
    ev = ev.merge(dh[["symbol", "date", "fh20", "fl20"]], on=["symbol", "date"], how="left")
    rows = []
    for tp in [20, 30, 50]:
        for sl in [10, 15, 20]:
            up_px = ev.buy * (1 + tp / 100)
            dn_px = ev.buy * (1 - sl / 100)
            hit_tp = ev.fh20 >= up_px
            hit_sl = ev.fl20 <= dn_px
            r = np.where(hit_sl & ~hit_tp, -sl,          # 只碰止损
                 np.where(hit_tp & ~hit_sl, tp,          # 只碰止盈
                  np.where(hit_tp & hit_sl, -sl,         # 双触发按先止损（悲观）
                   ev.f20.fillna(0).values)))            # 都没碰，持到 20 日
            rows.append({"tp": tp, "sl": sl, **stats(r, f"tp{tp}/sl{sl}")})
    res["payoff_grid"] = rows

    # ---------- 4. 分 regime ----------
    regs = [("2016-2019", "2016-01-01", "2019-12-31"),
            ("2020-2021", "2020-01-01", "2021-12-31"),
            ("2022-2023", "2022-01-01", "2023-12-31"),
            ("2024-2026", "2024-01-01", "2026-12-31")]
    rr = []
    for name, a, b in regs:
        sub = ev[(ev.date >= a) & (ev.date <= b)]
        rr.append(stats(sub.f20, f"{name} 20日"))
        rr.append(stats(sub.f60, f"{name} 60日"))
    res["by_regime"] = rr

    # ---------- 5. 左尾：破产概率 ----------
    res["ruin"] = {
        "p_f20_lt_-20": round(float((ev.f20 < -20).mean()) * 100, 1),
        "p_f20_lt_-30": round(float((ev.f20 < -30).mean()) * 100, 1),
        "p_f20_gt_20": round(float((ev.f20 > 20).mean()) * 100, 1),
        "p_f20_gt_40": round(float((ev.f20 > 40).mean()) * 100, 1),
        "p_f60_lt_-30": round(float((ev.f60 < -30).mean()) * 100, 1),
        "p_f60_gt_50": round(float((ev.f60 > 50).mean()) * 100, 1),
        "max_f20": round(float(ev.f20.max()), 1), "min_f20": round(float(ev.f20.min()), 1),
        "q01_f20": round(float(np.nanpercentile(ev.f20, 1)), 1),
        "q99_f20": round(float(np.nanpercentile(ev.f20, 99)), 1),
    }

    # ---------- 6. 九阳在事件集中的位置 ----------
    jy = ev[ev.symbol == "002242.SZ"][["date", "r40", "f20", "f60"]]
    res["jiuyang_events"] = [
        {"date": str(r.date.date()), "r40": round(float(r.r40), 1),
         "f20": None if r.f20 != r.f20 else round(float(r.f20), 1),
         "f60": None if r.f60 != r.f60 else round(float(r.f60), 1)}
        for _, r in jy.iterrows()
    ]

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("events:", json.dumps(res["event_count"], ensure_ascii=False))
    print("by year:", json.dumps(res["event_by_year"], ensure_ascii=False))
    print("\n=== 固定持有期赔率 ===")
    print(pd.DataFrame(res["odds_fixed_horizon"] + res["odds_baseline"]).to_string(index=False))
    print("\n=== 止盈止损网格 ===")
    print(pd.DataFrame(res["payoff_grid"])[["tp", "sl", "n", "mean", "median", "win_rate",
                                            "avg_win", "avg_loss", "odds", "need_win", "kelly"]].to_string(index=False))
    print("\n=== 分 regime ===")
    print(pd.DataFrame(res["by_regime"]).to_string(index=False))
    print("\n=== 左尾 ===")
    print(json.dumps(res["ruin"], ensure_ascii=False, indent=1))
    print("\n=== 九阳事件 ===")
    print(pd.DataFrame(res["jiuyang_events"]).to_string(index=False))


if __name__ == "__main__":
    main()
