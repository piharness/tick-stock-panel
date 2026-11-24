"""启动之后：哪些「事中信息」能区分九阳与普通涨停？

用户问题从「事前找特征」改成「启动后每天暴露的信息」——这是对的方向，
因为启动后不存在「看不见」，只存在「看得见但不值钱」。

判别对象：首板启动（此前 60 个交易日无涨停）的 33,865 个事件。
观测时点：T+10 收盘（信号日 T = 首板日，T+1 开盘进场的第 10 天后）。
标签：final = fwd60 > +100%（走成九阳那一类）。

两轨并行，缺一不可：
  A 判别力：每个特征能否提高 P(final)？（这是「区别」）
  B 收益：同一特征能否提高 T+10 → T+60 的剩余收益？（这是「钱」）
项目判据：A 强 B 负是常态，必须分开报。

关键控制：cum10（前 10 日累计涨幅）是最强的判别变量，但它是同义反复。
所以先按 cum10 分档，再在档内看其他特征的增量——那才是「区别于普通涨停」。
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_diag_regime_2026/daily_panel.parquet"
OUT = "data/_diag_postignition_discriminate.json"
SYM = "002242.SZ"
JY_T = pd.Timestamp("2026-09-11")


def load():
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "high", "low", "close",
                                         "volume", "amount", "turnover_rate",
                                         "consecutive_limit_ups"])
    df["symbol"] = df["symbol"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["rn"] = g.cumcount()
    df["lu"] = (df.consecutive_limit_ups > 0).astype(np.int8)
    df["one_word"] = ((df.high == df.low) & (df.lu > 0)).astype(np.int8)
    df["cum_lu"] = g["lu"].cumsum()
    df["cum_lu_60"] = df["cum_lu"].shift(60)
    df["first_ign"] = (df.lu == 1) & (df.rn >= 60) & (df.cum_lu - df.cum_lu_60 == 1)
    df["entry"] = g["open"].shift(-1)
    df["gap1"] = df["entry"] / df["close"] - 1
    df["fwd60"] = g["close"].shift(-60) / df["entry"] - 1
    return df


def path_feats(df, ev, H=10):
    """T+1..T+H 的可观测路径特征。"""
    pos = {s: p for s, p in df.groupby("symbol", sort=False).indices.items()}
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    vol = df["volume"].to_numpy()
    lu = df["lu"].to_numpy()
    ow = df["one_word"].to_numpy()
    to = df["turnover_rate"].to_numpy()
    rn = df["rn"].to_numpy()

    out = np.full((len(ev), 10), np.nan)
    for k, (i, s, r) in enumerate(zip(ev.index, ev.symbol, ev.rn)):
        p = pos[s]
        if r + H >= len(p):
            continue
        w = p[r + 1: r + H + 1]
        ent = df.at[i, "entry"]
        if not np.isfinite(ent) or ent <= 0:
            continue
        c = close[w]
        luw = lu[w]
        out[k, 0] = c[-1] / ent - 1                       # cum
        out[k, 1] = luw.sum()                             # n_lu
        # 最长连板
        best = cur = 0
        for x in luw:
            cur = cur + 1 if x > 0 else 0
            best = max(best, cur)
        out[k, 2] = best
        out[k, 3] = to[w][luw > 0].mean() if luw.sum() > 0 else np.nan  # 涨停日换手均值
        out[k, 4] = c[-1] / c.max() - 1                   # 距期间高
        out[k, 5] = c[-1] / c.min() - 1                   # 距期间低
        half = H // 2
        out[k, 6] = vol[w[half:]].mean() / max(vol[w[:half]].mean(), 1)  # 量能趋势
        out[k, 7] = ow[w].sum()                           # 一字板数
        out[k, 8] = float(np.mean(np.diff(np.log(c)) > 0))  # 上涨日占比
        out[k, 9] = (c[-1] - ent) / (c.max() - c.min() + 1e-9)  # 收盘在区间位置
    cols = ["cum", "n_lu", "max_consec", "to_lu", "dd", "up_from_low",
            "vol_trend", "n_oneword", "up_days", "close_pos"]
    return pd.DataFrame(out, index=ev.index, columns=cols)


def bucket_report(ev, col, label, bins=5, by=None):
    d = ev.dropna(subset=[col, "rest", "final"]).copy()
    if by is not None:
        d["grp"] = d[by]
    out = []
    qs = d.groupby("grp")[col].transform(
        lambda s: pd.qcut(s.rank(method="first"), min(bins, max(2, len(s) // 200)),
                          labels=False, duplicates="drop")) if by is not None else \
        pd.qcut(d[col].rank(method="first"), min(bins, max(2, len(d) // 500)),
                labels=False, duplicates="drop")
    d["q"] = qs
    for g, sub in d.groupby(["grp", "q"] if by is not None else "q"):
        out.append({
            "group": str(g[0]) if by is not None else "",
            "q": int(g[1]) if by is not None else int(g),
            "n": int(len(sub)),
            col + "_med": round(float(sub[col].median()), 3),
            "P_final_%": round(float(sub.final.mean()) * 100, 2),
            "rest_mean_%": round(float(sub.rest.mean()) * 100, 2),
            "rest_med_%": round(float(sub.rest.median()) * 100, 2),
            "rest_win_%": round(float((sub.rest > 0).mean()) * 100, 1),
        })
    return out


def main():
    df = load()
    ev = df[df.first_ign & (df.gap1 < 0.095)].copy()
    ev = ev[ev.symbol != SYM]  # 留出九阳做样本外读数
    res = {}
    for H in (10, 20):
        feats = path_feats(df, ev, H=H)
        e = ev.join(feats)
        e = e.dropna(subset=["cum", "fwd60"])
        e["final"] = (e.fwd60 > 1.0).astype(int)
        e["rest"] = (1 + e.fwd60) / (1 + e.cum) - 1
        r = {"n_events": int(len(e)),
             "base_P_final_%": round(float(e.final.mean()) * 100, 2),
             "base_rest_mean_%": round(float(e.rest.mean()) * 100, 2)}
        r["univariate"] = {}
        for c in ["cum", "n_lu", "max_consec", "to_lu", "dd", "up_from_low",
                  "vol_trend", "n_oneword", "up_days", "close_pos"]:
            r["univariate"][c] = bucket_report(e, c, c)
        e["cum_b"] = pd.qcut(e.cum.rank(method="first"), 5, labels=False)
        r["within_cum"] = {}
        for c in ["n_lu", "max_consec", "to_lu", "dd", "vol_trend", "up_days", "close_pos"]:
            r["within_cum"][c] = bucket_report(e, c, c, bins=3, by="cum_b")
        res[f"H{H}"] = r

    # 九阳读数（样本外）
    jy = df[(df.symbol == SYM) & (df.date == JY_T)]
    if len(jy):
        jyi = jy.index
        res["jiuyang"] = {}
        for H in (10, 20):
            jf = path_feats(df, df.loc[jyi], H=H).iloc[0]
            e = ev.join(path_feats(df, ev, H=H)).dropna(subset=["cum", "fwd60"])
            pct = {}
            for c in jf.index:
                s = e[c].dropna()
                if np.isfinite(jf[c]) and len(s):
                    pct[c] = round(float((s < jf[c]).mean()) * 100, 1)
            res["jiuyang"][f"T{H}"] = {
                "value": {k: (round(float(v), 3) if np.isfinite(v) else None)
                          for k, v in jf.items()},
                "pctile": pct}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res["jiuyang"], ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
