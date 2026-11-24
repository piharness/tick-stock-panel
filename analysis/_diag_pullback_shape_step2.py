"""强势股回调：形状到底值不值钱。

问题重构：
  原问「回调时最佳的上车形状」= 在问「哪个形状之后涨的概率高」——这是概率不是钱。
  改问：在 r20>=30% 的强势股里，回调日买入 vs 上涨日买入，T+1 开盘进场的 N 日超额差多少；
        差异是否被「形状」进一步解释，还是全被「回调」这一个事实吃掉。

对照组三层：
  A 同股同期「上涨日」（同一段拉升里的非回调日）——最干净的形状对照
  B 同日全市场等权（市场基准）
  C 同日同池（r20 同分位）等权

口径硬约束：
  - 信号在 T 日收盘确认 ⇒ T+1 开盘买入，避免用 T 收盘价自欺
  - 剔除 T+1 开盘即涨停（买不到）
  - 按日聚合 + 按年分解；事件扎堆时 N 是段数不是天数
"""
import json
import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_pullback_shape_step2.json"
HORIZONS = [1, 3, 5, 10, 20]
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
    df["dma20"] = (df.close / df.ma20 - 1) * 100
    # 未来收益：T+1 开盘买
    df["buy"] = g.open.shift(-1)
    for h in HORIZONS:
        df[f"sell{h}"] = g.close.shift(-h)
        df[f"f{h}"] = (df[f"sell{h}"] / df["buy"] - 1) * 100
    # 连续下跌天数
    dn = (df.r1 < 0).astype(int)
    df["dn_run"] = dn * 0 + dn.groupby([df.symbol, (dn == 0).cumsum()]).cumcount() + dn
    df["lim_up_t1"] = (df.raw_close.shift(-1) / df.raw_close - 1) > 0.095
    return df


def agg_daily(ev, col):
    """按信号日聚合再求均值 —— 事件扎堆时防止单日主导"""
    d = ev.dropna(subset=[col])
    per = d.groupby("date")[col].mean()
    if len(per) < 3:
        return np.nan, np.nan, np.nan, len(per)
    m = per.mean()
    se = per.std(ddof=1) / np.sqrt(len(per))
    return m, m / se if se > 0 else np.nan, per.median(), len(per)


def main():
    df = load()
    res = {}
    # 基准：每日全市场等权（同一买入口径）
    base = {}
    for h in HORIZONS:
        base[h] = df.groupby("date")[f"f{h}"].mean()
    base_df = pd.DataFrame(base)
    res["base_all"] = {f"f{h}": round(float(base_df[h].mean()), 3) for h in HORIZONS}

    # ---------- 样本 ----------
    d = df[(df.n >= 250) & (df.raw_close > 2)].copy()
    d = d[d.date >= "2016-02-01"]
    for h in HORIZONS:
        d[f"ex{h}"] = d[f"f{h}"] - d.date.map(base_df[h])

    strong = d[d.r20 >= 30].copy()          # 强势拉升中
    strong["is_pb"] = strong.r1 < 0         # 当日回调
    res["pool"] = {
        "n_rows": int(len(strong)),
        "n_symbols": int(strong.symbol.nunique()),
        "n_pullback": int(strong.is_pb.sum()),
        "n_days": int(strong.date.nunique()),
        "share_pullback": round(float(strong.is_pb.mean()), 4),
    }

    # ---------- 1. 回调日 vs 上涨日（同池、同股） ----------
    tab = []
    for h in HORIZONS:
        for grp, sub in [("pullback", strong[strong.is_pb]), ("upday", strong[~strong.is_pb])]:
            m, t, med, nd = agg_daily(sub, f"ex{h}")
            raw_m, _, _, _ = agg_daily(sub, f"f{h}")
            tab.append({"h": h, "grp": grp, "n": int(sub[f"ex{h}"].notna().sum()),
                        "days": nd, "raw": round(raw_m, 2), "ex": round(m, 2),
                        "t_daily": round(t, 2), "ex_med": round(med, 2)})
    res["pullback_vs_upday"] = tab

    # ---------- 2. 形状分解（只在回调日内） ----------
    pb = strong[strong.is_pb].copy()
    pb["depth"] = pd.cut(pb.r1, [-11, -9, -7, -5, -3, -1.5, 0],
                         labels=["-11~-9", "-9~-7", "-7~-5", "-5~-3", "-3~-1.5", "-1.5~0"])
    pb["volb"] = pd.cut(pb.vr, [0, 0.6, 0.8, 1.0, 1.5, 99],
                        labels=["<0.6", "0.6-0.8", "0.8-1.0", "1.0-1.5", ">1.5"])
    pb["posb"] = pd.cut(pb.cl_pos, [-0.01, 0.2, 0.4, 0.6, 0.8, 1.01],
                        labels=["0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"])
    pb["runb"] = pd.cut(pb.dn_run, [0, 1, 2, 3, 99], labels=["1天", "2天", "3天", "4天+"])
    pb["mab"] = pd.cut(pb.dma10, [-99, -5, -2, 0, 2, 99],
                       labels=["破MA10>5%", "-5~-2%", "-2~0%", "0~+2%", ">+2%"])

    def by(col, h=5):
        rows = []
        for k, sub in pb.dropna(subset=[col]).groupby(col, observed=True):
            m, t, med, nd = agg_daily(sub, f"ex{h}")
            rows.append({"bucket": str(k), "n": int(len(sub)), "days": nd,
                         "ex": round(m, 2), "t_daily": round(t, 2), "ex_med": round(med, 2)})
        return rows

    res["shape_h5"] = {c: by(c, 5) for c in ["depth", "volb", "posb", "runb", "mab"]}
    res["shape_h10"] = {c: by(c, 10) for c in ["depth", "volb", "posb", "runb", "mab"]}

    # ---------- 3. 多维交叉：缩量 + 守MA10 + 收在下半区 ----------
    pb["combo"] = np.select(
        [(pb.vr < 0.8) & (pb.dma10 > 0) & (pb.cl_pos < 0.5),
         (pb.vr < 0.8) & (pb.dma10 > 0),
         (pb.vr >= 1.5) & (pb.dma10 < 0)],
        ["A_缩量+守MA10+弱收", "B_缩量+守MA10", "C_放量+破MA10"],
        default="other")
    rows = []
    for k, sub in pb.groupby("combo"):
        for h in [3, 5, 10]:
            m, t, med, nd = agg_daily(sub, f"ex{h}")
            rows.append({"combo": k, "h": h, "n": int(len(sub)), "days": nd,
                         "ex": round(m, 2), "t_daily": round(t, 2), "ex_med": round(med, 2)})
    res["combo"] = rows

    # ---------- 4. 分年（h=5，回调 vs 上涨） ----------
    yr = []
    for y, sub in strong.groupby(strong.date.dt.year):
        for grp, s2 in [("pb", sub[sub.is_pb]), ("up", sub[~sub.is_pb])]:
            m, _, _, nd = agg_daily(s2, "ex5")
            yr.append({"y": int(y), "grp": grp, "n": int(len(s2)), "days": nd,
                       "ex": round(m, 2) if m == m else None})
    res["by_year_h5"] = yr

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: res[k] for k in ["pool", "base_all"]}, ensure_ascii=False, indent=1))
    print("\n--- 回调 vs 上涨 ---")
    print(pd.DataFrame(res["pullback_vs_upday"]).to_string(index=False))
    for c in ["depth", "volb", "posb", "runb", "mab"]:
        print(f"\n--- {c} (h=5) ---")
        print(pd.DataFrame(res["shape_h5"][c]).to_string(index=False))
    print("\n--- combo ---")
    print(pd.DataFrame(res["combo"]).to_string(index=False))


if __name__ == "__main__":
    main()
