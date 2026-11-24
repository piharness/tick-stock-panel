"""历史上最像九阳的标的：最近邻检索 + 行业相对强度是否携带信息。

两问：
  A. 「最像」= 前兆最像。以九阳 2026-09-04（启动前一周）的 7 维状态为模板，
     在全样本 211 万格上做标准化最近邻，看这些「长得一模一样」的票后来怎么了。
  B. 用户的假设：绝对量价没信息，那「相对同行业」有没有？
     定义 peer_rel = 个股 r20 − 同日同行业中位 r20，检验它是否预测 fwd_ret60。

九阳模板（2026-09-04）：r20 −4.7% / r60 −0.9% / to20 0.94% / amp20 2.9%
                          dist_hi250 −30.4% / 流通市值 66.6 亿 / 行业 小家电
随后：fwd_ret60 +260%、fwd_min60 −7.1%（几乎不回头）
"""
import json

import numpy as np
import pandas as pd

PANEL = "data/_osfe/panel.parquet"
OUT = "data/_diag_analog_jiuyang.json"
SYM = "002242.SZ"
T0 = pd.Timestamp("2026-09-04")
FEATS = ["r20", "r60", "to20", "amp20", "dist_hi250", "logmcap", "vol20"]


def main():
    d = pd.read_parquet(PANEL, columns=["symbol", "date", "industry", "r20", "r60", "to20",
                                        "amp20", "dist_hi250", "mcap_f", "vol20",
                                        "fwd_ret60", "fwd_max60", "fwd_min60", "is_st"])
    d["date"] = pd.to_datetime(d["date"])
    d["logmcap"] = np.log(d.mcap_f.clip(lower=1e7))
    d = d[d.r20.notna() & d.to20.notna() & d.dist_hi250.notna() & d.vol20.notna()]
    d = d[(d.is_st != True)]

    res = {"template_date": str(T0.date()), "n_universe": int(len(d))}

    # ---- A. 最近邻
    mu, sd = d[FEATS].mean(), d[FEATS].std()
    Z = (d[FEATS] - mu) / sd
    jy = d[(d.symbol == SYM) & (d.date == T0)]
    if len(jy) == 0:
        raise SystemExit("模板日缺失")
    jz = (jy[FEATS].iloc[0] - mu) / sd
    dist = np.sqrt(((Z - jz) ** 2).sum(axis=1))
    d = d.assign(dist=dist)
    # 排除自身与前后 60 日（避免同一段行情重复计入）
    mask = ~((d.symbol == SYM) & (d.date.between(T0 - pd.Timedelta(days=120), T0 + pd.Timedelta(days=120))))
    d = d[mask]

    res["jiuyang_template"] = {k: round(float(jy[k].iloc[0]), 4) for k in
                               ["r20", "r60", "to20", "amp20", "dist_hi250", "mcap_f", "vol20"]}
    res["jiuyang_template"]["industry"] = jy.industry.iloc[0]

    nn = d.nsmallest(200, "dist").copy()
    r = nn.fwd_ret60.dropna()
    res["nearest200"] = {
        "n_with_outcome": int(len(r)),
        "dist_median": round(float(nn.dist.median()), 2),
        "ret60_median": round(float(r.median()) * 100, 1),
        "ret60_mean": round(float(r.mean()) * 100, 1),
        "ret60_p90": round(float(r.quantile(.9)) * 100, 1),
        "ret60_max": round(float(r.max()) * 100, 1),
        "ge_100pct": int((r > 1).sum()),
        "ge_50pct": int((r > .5).sum()),
        "jiuyang_type": int(((nn.fwd_ret60 > 1) & (nn.fwd_min60 > -0.10)).sum()),
        "same_industry_share": round(float((nn.industry == jy.industry.iloc[0]).mean()) * 100, 1),
    }
    top = nn.nsmallest(20, "dist")[["symbol", "date", "industry", "dist", "r60", "to20",
                                    "dist_hi250", "fwd_ret60", "fwd_min60"]]
    res["nearest20"] = [{
        "symbol": row.symbol, "date": str(row.date.date()), "industry": row.industry,
        "dist": round(float(row.dist), 2), "r60": round(float(row.r60) * 100, 1),
        "to20": round(float(row.to20), 2), "dist_hi250": round(float(row.dist_hi250) * 100, 1),
        "fwd_ret60": None if pd.isna(row.fwd_ret60) else round(float(row.fwd_ret60) * 100, 1),
        "fwd_min60": None if pd.isna(row.fwd_min60) else round(float(row.fwd_min60) * 100, 1),
    } for _, row in top.iterrows()]

    # 基准：全样本 fwd_ret60 分布
    allr = d.fwd_ret60.dropna()
    res["baseline"] = {"n": int(len(allr)), "median": round(float(allr.median()) * 100, 1),
                       "p90": round(float(allr.quantile(.9)) * 100, 1),
                       "ge_100pct_share": round(float((allr > 1).mean()) * 100, 2)}

    # ---- B. 行业相对强度
    d = d.dropna(subset=["fwd_ret60"])
    g = d.groupby(["date", "industry"])
    d["ind_r20_med"] = g.r20.transform("median")
    d["peer_rel"] = d.r20 - d.ind_r20_med
    d["ind_ret60_med"] = g.fwd_ret60.transform("median")
    d["ex_ind"] = d.fwd_ret60 - d.ind_ret60_med

    # 分位（每日）
    d["peer_pct"] = d.groupby("date").peer_rel.rank(pct=True)
    d["ind_disp"] = g.r20.transform("std")

    tab = []
    for lo, hi, name in [(-9, .2, "Q1 最弱"), (.2, .4, "Q2"), (.4, .6, "Q3"),
                         (.6, .8, "Q4"), (.8, 9, "Q5 最强")]:
        sub = d[(d.peer_pct >= lo) & (d.peer_pct < hi)]
        tab.append({"peer_rel_bucket": name, "n": int(len(sub)),
                    "ex_ind_med": round(float(sub.ex_ind.median()) * 100, 2),
                    "ex_ind_mean": round(float(sub.ex_ind.mean()) * 100, 2),
                    "ret60_med": round(float(sub.fwd_ret60.median()) * 100, 2),
                    "p_jiuyang": round(float(((sub.fwd_ret60 > 1) & (sub.fwd_min60 > -.10)).mean()) * 100, 3),
                    "pump_rate": round(float((sub.fwd_ret60 > .5).mean()) * 100, 2)})
    res["industry_relative"] = tab

    # 「行业没动、只有它动」 vs 「行业在动」
    q = d.copy()
    q["ind_up"] = q.ind_r20_med > 0
    q["solo"] = (q.peer_pct > .8) & (~q.ind_up)
    q["with_ind"] = (q.peer_pct > .8) & (q.ind_up)
    res["solo_vs_herd"] = {
        "solo_n": int(q.solo.sum()),
        "solo_ret60_med": round(float(q[q.solo].fwd_ret60.median()) * 100, 2),
        "solo_ex_ind_med": round(float(q[q.solo].ex_ind.median()) * 100, 2),
        "solo_p_jiuyang": round(float(((q[q.solo].fwd_ret60 > 1) & (q[q.solo].fwd_min60 > -.1)).mean()) * 100, 3),
        "herd_n": int(q.with_ind.sum()),
        "herd_ret60_med": round(float(q[q.with_ind].fwd_ret60.median()) * 100, 2),
        "herd_ex_ind_med": round(float(q[q.with_ind].ex_ind.median()) * 100, 2),
        "herd_p_jiuyang": round(float(((q[q.with_ind].fwd_ret60 > 1) & (q[q.with_ind].fwd_min60 > -.1)).mean()) * 100, 3),
    }

    # 行业层面：哪个行业最容易出「九阳型」
    ev = d[(d.fwd_ret60 > 1) & (d.fwd_min60 > -.10)]
    ind_stat = (ev.groupby("industry").size() / d.groupby("industry").size() * 100).dropna()
    ind_stat = ind_stat.sort_values(ascending=False)
    res["industry_jiuyang_rate_pct"] = {str(k): round(float(v), 3) for k, v in ind_stat.head(15).items()}
    res["jiuyang_events"] = int(len(ev))
    res["jiuyang_events_per_year"] = round(len(ev) / 10.9, 1)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
