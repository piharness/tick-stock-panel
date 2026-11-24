"""首次涨停「启动」之后：能不能事前筛，和「波澜但始终向上」是否延续。

原问：如何找到九阳股份这种「9-24 前波动平稳 → 到 11-24 涨幅夸张 → 中途看似结束但一直向上」。

先把个案读对（见 docs 输出），再拆成两个可证伪的问题：

  Q1 事前可筛？  首次涨停（此前 60 个交易日无涨停）发生后，
                 首板日「换手放大倍数 / 板上封单形态 / 位置」能否区分后续 40 日大涨？
  Q2 韧性延续？  启动后第 20 个交易日回看：浮盈多少、距期间高点回撤多少，
                 是否预测「第 20 → 60 个交易日」的收益？

口径：
  - 收益一律 close 复权价，入场 = T+1 开盘（信号日收盘确认）
  - T+1 开盘涨停（跳空 ≥9.5%）视为买不到，剔除并单独报告占比
  - 所有 shift 走 groupby（按 symbol 内第 N 个交易日），不假设日历
  - 基准 = 同日全市场等权（同买入口径）
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_ignition_resilience.json"
COLS = ["symbol", "date", "open", "high", "low", "close", "amount",
        "turnover_rate", "consecutive_limit_ups"]


def load():
    df = pd.read_parquet(PANEL, columns=COLS)
    df["symbol"] = df["symbol"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["rn"] = g.cumcount()
    df["r1"] = g.close.pct_change()
    df["lu"] = (df.consecutive_limit_ups > 0).astype(np.int8)

    # 首次启动：T 日涨停，且 T-59..T 之间只有这 1 次涨停
    df["cum_lu"] = g["lu"].cumsum()
    df["cum_lu_60"] = g["cum_lu"].shift(60)
    first = (df.lu == 1) & (df.rn >= 60) & (df.cum_lu - df.cum_lu_60 == 1)
    df["first_ign"] = first

    # 换手放大：当日 / 前 5 日均值（前 5 日不含当日）
    df["to_ma5_prev"] = g["turnover_rate"].transform(
        lambda s: s.shift(1).rolling(5).mean())
    df["to_surge"] = df["turnover_rate"] / df["to_ma5_prev"]

    # 位置：距 250 日高 / 低
    df["hi250"] = g["close"].transform(lambda s: s.rolling(250, min_periods=120).max())
    df["lo250"] = g["close"].transform(lambda s: s.rolling(250, min_periods=120).min())
    df["dist_hi250"] = df["close"] / df["hi250"] - 1
    df["dist_lo250"] = df["close"] / df["lo250"] - 1

    # 入场与前瞻收益（组内第 N 个交易日）
    df["entry"] = g["open"].shift(-1)
    df["gap1"] = df["entry"] / df["close"] - 1
    for h in (5, 20, 40, 60):
        df[f"fwd{h}"] = g["close"].shift(-h) / df["entry"] - 1
    return df


def window_stats(df, ev):
    """对每个事件取 T+1..T+20 的 close/low，算浮盈、最大回撤、是否创新高。"""
    pos = {s: p for s, p in df.groupby("symbol", sort=False).indices.items()}
    close = df["close"].to_numpy()
    low = df["low"].to_numpy()
    rn = df["rn"].to_numpy()
    out = np.full((len(ev), 4), np.nan)
    for k, (i, s, r) in enumerate(zip(ev.index, ev.symbol, ev.rn)):
        p = pos[s]
        a, b = r + 1, r + 20
        if b >= len(p):
            continue
        w = p[a:b + 1]
        c = close[w]
        ent = df.at[i, "entry"]
        if not np.isfinite(ent):
            continue
        out[k, 0] = c[-1] / ent - 1                    # gain20
        out[k, 1] = low[w].min() / ent - 1             # mae20
        out[k, 2] = c[-1] / c.max() - 1                # dd20（距期间最高收盘）
        out[k, 3] = (c.max() / ent - 1)                # mfe20
    return pd.DataFrame(out, index=ev.index,
                        columns=["gain20", "mae20", "dd20", "mfe20"])


def desc(s, name, n_dec=1):
    s = s.dropna()
    if len(s) == 0:
        return {"bucket": name, "n": 0}
    return {
        "bucket": name, "n": int(len(s)),
        "mean": round(float(s.mean()) * 100, n_dec),
        "median": round(float(s.median()) * 100, n_dec),
        "p10": round(float(s.quantile(.10)) * 100, n_dec),
        "p90": round(float(s.quantile(.90)) * 100, n_dec),
        "win": round(float((s > 0).mean()) * 100, 1),
        "gt50": round(float((s > .5).mean()) * 100, 2),
        "gt100": round(float((s > 1.0).mean()) * 100, 2),
    }


def main():
    df = load()
    res = {}

    ev = df[df.first_ign].copy()
    res["n_first_ign"] = int(len(ev))
    # 可交易：T+1 开盘跳空 < 9.5%
    tradable = ev.gap1 < 0.095
    res["n_tradable"] = int(tradable.sum())
    res["pct_untradable"] = round(float((~tradable).mean()) * 100, 2)
    ev = ev[tradable].copy()
    res["n_events"] = int(len(ev))
    res["n_symbols"] = int(ev.symbol.nunique())
    res["span"] = [str(ev.date.min().date()), str(ev.date.max().date())]

    # ---- Q1：事前可筛？换手放大倍数分档
    ev["surge_b"] = pd.cut(ev.to_surge, [-np.inf, 1.5, 3, 6, 10, np.inf],
                           labels=["<1.5", "1.5-3", "3-6", "6-10", ">10"])
    res["Q1_turnover_surge"] = [
        desc(ev.loc[ev.surge_b == b, "fwd40"], str(b)) for b in ev.surge_b.cat.categories]

    # 位置分档
    ev["pos_b"] = pd.cut(ev.dist_hi250, [-np.inf, -.5, -.3, -.15, -.05, np.inf],
                         labels=["<-50%", "-50~-30%", "-30~-15%", "-15~-5%", ">-5%"])
    res["Q1_position"] = [
        desc(ev.loc[ev.pos_b == b, "fwd40"], str(b)) for b in ev.pos_b.cat.categories]

    # 与全市场同期基准比较（同日等权，用事件日对齐）
    mkt = df.dropna(subset=["fwd40"]).groupby("date")["fwd40"].mean()
    ev["ex40"] = ev.fwd40 - ev.date.map(mkt)
    res["Q1_overall"] = desc(ev.ex40, "excess40_vs_mkt")
    res["Q1_overall_raw"] = desc(ev.fwd40, "raw_fwd40")

    # 按年
    ev["yr"] = ev.date.dt.year
    res["Q1_by_year"] = {
        int(y): desc(g2.fwd40, str(y)) for y, g2 in ev.groupby("yr") if len(g2) >= 100}

    # ---- Q2：韧性（T+20 可观测）是否预测 T+20→T+60
    ws = window_stats(df, ev)
    ev = ev.join(ws)
    ev["fwd40b"] = (1 + df.loc[ev.index, "fwd60"]) / (1 + ev.gain20) - 1  # 从 T+20 收盘起算

    ev["dd_b"] = pd.cut(ev.dd20, [-np.inf, -.30, -.20, -.10, -.04, np.inf],
                        labels=["<-30%", "-30~-20%", "-20~-10%", "-10~-4%", ">-4%"])
    res["Q2_drawdown_vs_next40"] = [
        desc(ev.loc[ev.dd_b == b, "fwd40b"], str(b)) for b in ev.dd_b.cat.categories]

    ev["gain_b"] = pd.cut(ev.gain20, [-np.inf, 0, .2, .5, 1.0, np.inf],
                          labels=["<0", "0-20%", "20-50%", "50-100%", ">100%"])
    res["Q2_gain_vs_next40"] = [
        desc(ev.loc[ev.gain_b == b, "fwd40b"], str(b)) for b in ev.gain_b.cat.categories]

    # 绝对口径：入场后一直持有到 T+60，与「T+20 卖」对比
    ev["hold60"] = df.loc[ev.index, "fwd60"]
    hold = []
    for b in ev.gain_b.cat.categories:
        sub = ev[ev.gain_b == b]
        hold.append({
            "bucket": str(b), "n": int(sub.fwd40b.notna().sum()),
            "sell_at_20": round(float(sub.gain20[sub.fwd40b.notna()].median()) * 100, 1),
            "hold_to_60_mean": round(float(sub.hold60.dropna().mean()) * 100, 1),
            "hold_to_60_median": round(float(sub.hold60.dropna().median()) * 100, 1),
            "hold_to_60_win": round(float((sub.hold60.dropna() > 0).mean()) * 100, 1),
        })
    res["Q2_sell_vs_hold"] = hold

    # ---- 负控：全样本（不限首板）同一条件，看反转是不是首板特有
    allq = df.dropna(subset=["fwd20", "fwd40"]).copy()
    allq["gain_b"] = pd.cut(allq.fwd20, [-np.inf, 0, .2, .5, 1.0, np.inf],
                            labels=["<0", "0-20%", "20-50%", "50-100%", ">100%"])
    allq["nxt40"] = (1 + allq.fwd40) / (1 + allq.fwd20) - 1
    res["NEGCTRL_all_days_gain_vs_next40"] = [
        desc(allq.loc[allq.gain_b == b, "nxt40"], str(b)) for b in allq.gain_b.cat.categories]

    # 首板 vs 全样本：同 gain 档的差值（事件内均值 − 全样本均值）
    cmp = []
    for b in allq.gain_b.cat.categories:
        a = ev.loc[ev.gain_b == b, "fwd40b"].dropna()
        c = allq.loc[allq.gain_b == b, "nxt40"].dropna()
        if len(a) > 30 and len(c) > 30:
            cmp.append({"bucket": str(b), "n_ev": int(len(a)),
                        "ev_mean": round(float(a.mean()) * 100, 1),
                        "all_mean": round(float(c.mean()) * 100, 1),
                        "diff": round(float(a.mean() - c.mean()) * 100, 1)})
    res["NEGCTRL_ignition_minus_all"] = cmp

    # 交叉：浮盈为正 × 回撤浅
    alive = ev[(ev.gain20 > 0)]
    cross = []
    for db in alive.dd_b.cat.categories:
        sub = alive[alive.dd_b == db]
        cross.append(desc(sub.fwd40b, f"gain>0 & dd {db}"))
    res["Q2_cross"] = cross

    # ---- 九阳读数
    jy = df[(df.symbol == "002242.SZ") & (df.date == "2026-09-11")]
    if len(jy):
        r = jy.iloc[0]
        res["jiuyang_20260911"] = {
            "close": float(r.close), "turnover_rate": round(float(r.turnover_rate), 2),
            "to_surge": round(float(r.to_surge), 2),
            "dist_hi250": round(float(r.dist_hi250) * 100, 1),
            "dist_lo250": round(float(r.dist_lo250) * 100, 1),
            "entry": round(float(r.entry), 2),
            "fwd20": round(float(r.fwd20) * 100, 1) if np.isfinite(r.fwd20) else None,
            "fwd40": round(float(r.fwd40) * 100, 1) if np.isfinite(r.fwd40) else None,
        }
        # 同档（换手放大 >3 倍）在首板样本里的分位
        sub = ev[ev.to_surge > 3].fwd40.dropna()
        res["jiuyang_20260911"]["pctile_in_surge_gt3"] = (
            round(float((sub < r.fwd40).mean()) * 100, 1) if np.isfinite(r.fwd40) else None)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
