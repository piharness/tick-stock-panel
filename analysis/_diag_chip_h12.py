"""H12：拆因果——雪崩（股价跌幅）vs 券商定价（平仓线折算）。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
背景
  H11 找到 ratio = 平仓线/股价 与未来收益单调（Spearman −0.976），但 near 组事件前动量是正的，
  无法区分两种读法：
    雪崩：股价跌到平仓线 → 强平卖压 → 阴跌。
    券商定价：平仓线定得高（券商对高风险股定价）→ 这些股票本来就差。
  本脚本用「质押日前复权价」把 ratio 拆成两个分量：
    P_start = CLOSE_FORWARD_ADJPRICE（质押日前复权价）
    P_today = CLOSE_FORWARD_ADJPRICE_TODAY（公告日前复权价）
    折算 = openline / P_start         （平仓线定得多高，券商定价分量）
    涨幅 = P_today / P_start − 1      （质押后股价涨跌，雪崩分量）
    ratio = 折算 / (1 + 涨幅)

预测（对立假说）
  雪崩假说：控制折算后，涨幅越低（跌越深）→ fwd 越差（涨幅系数为正）。
  券商定价假说：控制涨幅后，折算越高 → fwd 越差（折算系数为负）。

杀死
  两个分量都不显著（H11 的信号是二者的某种混合，无法归因）；或符号与预测相反。

控制：换手、波动、规模、久期。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
PLEDGE = OUT / "pledge_raw_v2.parquet"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
SRC = ROOT / "data" / "kline_daily_enriched"

NW_LAG = 6


def load_daily(start="2019-01-01"):
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "open", "close", "amount", "turnover_rate"],
                   filter=ds.field("date") >= pd.Timestamp(start).date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.9)
    op = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="to", aggfunc="last").sort_index()
    cl = cl.reindex(op.index)
    am = am.reindex(op.index)
    to = to.reindex(op.index)
    return op, cl, am, to


def nw_t(x, lag=NW_LAG):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return np.nan
    e = x - x.mean()
    s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def zscore(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def fwd_h(O, C, ti, ci, H):
    out = np.full(len(ti), np.nan)
    for t in np.unique(ti):
        sel = np.nonzero(ti == t)[0]
        idx = ci[sel]
        if t + H + 2 > C.shape[0]:
            continue
        p0 = O[t + 1, idx].astype(np.float64)
        p = C[t + 1:t + H + 2, idx].astype(np.float64)
        good = np.isfinite(p0) & (p0 > 0) & np.isfinite(p).all(axis=0)
        if good.any():
            out[sel[good]] = p[-1, good] / p0[good] - 1.0
    return out


def main():
    print("[1/5] load ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32)
    C = cl_df.to_numpy(np.float32)
    A = am_df.to_numpy(np.float32)
    TO = to_df.to_numpy(np.float32)
    n, k = C.shape
    sym2idx = {s: i for i, s in enumerate(syms)}
    print(f"      {n} x {k}", flush=True)

    print("[2/5] 拆分两个分量 ...", flush=True)
    p = pd.read_parquet(PLEDGE)
    p = p.dropna(subset=["OPENLINE", "CLOSE_FORWARD_ADJPRICE", "CLOSE_FORWARD_ADJPRICE_TODAY"])
    p = p[(p["OPENLINE"] > 0) & (p["CLOSE_FORWARD_ADJPRICE"] > 0) & (p["CLOSE_FORWARD_ADJPRICE_TODAY"] > 0)]
    p["zhesuan"] = p["OPENLINE"] / p["CLOSE_FORWARD_ADJPRICE"]          # 券商定价
    p["zhangfu"] = p["CLOSE_FORWARD_ADJPRICE_TODAY"] / p["CLOSE_FORWARD_ADJPRICE"] - 1.0  # 股价变动
    # 合理区间
    p = p[(p["zhesuan"] >= 0.3) & (p["zhesuan"] <= 0.9)]
    p = p[(p["zhangfu"] >= -0.5) & (p["zhangfu"] <= 0.5)]
    inst = pd.read_parquet(INST)[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    p["notice"] = p["notice"].astype("datetime64[ns]")
    p = p.sort_values("notice").drop_duplicates(subset=["symbol", "notice"])
    date_arr = dates.values
    p["tidx"] = np.searchsorted(date_arr, p["notice"].to_numpy(), side="right")
    p = p[(p["tidx"] >= 61) & (p["tidx"] < n - 62)]
    print(f"      事件 {len(p):,}；折算中位 {p['zhesuan'].median():.3f}，涨幅中位 {p['zhangfu'].median()*100:.1f}%", flush=True)

    print("[3/5] forward returns ...", flush=True)
    ti = p["tidx"].to_numpy(); ci = p["idx"].to_numpy()
    p["f20"] = fwd_h(O, C, ti, ci, 20)
    p["f40"] = fwd_h(O, C, ti, ci, 40)
    p["f60"] = fwd_h(O, C, ti, ci, 60)
    p["date"] = dates[ti]

    print("[4/5] controls ...", flush=True)
    r = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    S1 = np.vstack([np.zeros((1, k)), np.cumsum(r, axis=0)])
    S2 = np.vstack([np.zeros((1, k)), np.cumsum(r * r, axis=0)])
    TOc = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(TO, nan=0.0), axis=0)])
    Ac = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(A, nan=0.0), axis=0)])
    tig = p["tidx"].to_numpy(); cig = p["idx"].to_numpy()
    s1 = S1[tig, cig] - S1[tig - 20, cig]
    s2 = S2[tig, cig] - S2[tig - 20, cig]
    p["vol20"] = np.sqrt(np.maximum(s2 / 20 - (s1 / 20) ** 2, 0.0))
    p["turn20"] = (TOc[tig, cig] - TOc[tig - 20, cig]) / 20
    p["size"] = np.log(np.maximum((Ac[tig, cig] - Ac[tig - 20, cig]) / 20, 1.0))
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age["symbol"] = age["symbol"].astype(str)
    p["date"] = p["date"].astype("datetime64[ns]")
    p["symbol"] = p["symbol"].astype(str)
    p = pd.merge_asof(p.sort_values("date"), age[["date", "symbol", "D"]].sort_values("date"),
                      on="date", by="symbol", direction="backward")
    p = p.dropna(subset=["f20", "vol20", "turn20", "size", "D"])
    p["ym"] = p["date"].dt.to_period("M").astype(str)
    p.to_parquet(OUT / "h12_events.parquet", index=False)
    print(f"      最终事件 {len(p):,}", flush=True)

    print("[5/5] 交叉回归 ...", flush=True)
    # 月度截面回归：f20 ~ 折算 + 涨幅 + controls
    reg = []
    for ym, g in p.groupby("ym"):
        if len(g) < 40:
            continue
        X = np.column_stack([zscore(g["zhesuan"]), zscore(g["zhangfu"]),
                             zscore(g["vol20"]), zscore(g["turn20"]), zscore(g["size"]), zscore(g["D"]),
                             np.ones(len(g))])
        y = g["f20"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 40:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym, "b_zhesuan": b[0], "b_zhangfu": b[1], "n": int(ok.sum())})
    rr = pd.DataFrame(reg)

    # 双变量剂量反应：在涨幅三分位内看折算，在折算三分位内看涨幅
    def tercile(df, col):
        df = df.copy()
        df[col + "_t"] = df.groupby("ym")[col].transform(lambda x: pd.qcut(x.rank(method="first"), 3, labels=False))
        return df
    p2 = tercile(tercile(p, "zhesuan"), "zhangfu")
    dose_zhesuan = {}
    for t in [0, 1, 2]:
        g = p2[p2["zhangfu_t"] == t]
        m = g.groupby("ym").apply(lambda x: x[x["zhesuan_t"] == 2]["f60"].mean() - x[x["zhesuan_t"] == 0]["f60"].mean(), include_groups=False)
        dose_zhesuan[f"涨幅第{t}分位"] = {"zhesuan高-低f60_bp": round(float(m.mean() * 1e4), 1), "t": round(float(nw_t(m)), 2)}
    dose_zhangfu = {}
    for t in [0, 1, 2]:
        g = p2[p2["zhesuan_t"] == t]
        m = g.groupby("ym").apply(lambda x: x[x["zhangfu_t"] == 2]["f60"].mean() - x[x["zhangfu_t"] == 0]["f60"].mean(), include_groups=False)
        dose_zhangfu[f"折算第{t}分位"] = {"涨幅高-低f60_bp": round(float(m.mean() * 1e4), 1), "t": round(float(nw_t(m)), 2)}

    summary = {
        "sample": {"events": int(len(p)), "months": int(p["ym"].nunique()),
                   "zhesuan_median": round(float(p["zhesuan"].median()), 3),
                   "zhangfu_median_pct": round(float(p["zhangfu"].median() * 100), 2),
                   "start": str(p["date"].min().date()), "end": str(p["date"].max().date())},
        "corr": {"zhesuan_vs_zhangfu": round(float(p[["zhesuan", "zhangfu"]].corr(method="spearman").iloc[0, 1]), 3),
                 "zhesuan_vs_f60": round(float(p[["zhesuan", "f60"]].corr(method="spearman").iloc[0, 1]), 3),
                 "zhangfu_vs_f60": round(float(p[["zhangfu", "f60"]].corr(method="spearman").iloc[0, 1]), 3)},
        "regression": {
            "b_zhesuan_bp": round(float(rr["b_zhesuan"].mean()), 1),
            "t_zhesuan": round(float(nw_t(rr["b_zhesuan"])), 2),
            "b_zhangfu_bp": round(float(rr["b_zhangfu"].mean()), 1),
            "t_zhangfu": round(float(nw_t(rr["b_zhangfu"])), 2),
            "months": int(len(rr)),
        },
        "dose_zhesuan_within_zhangfu": dose_zhesuan,
        "dose_zhangfu_within_zhesuan": dose_zhangfu,
    }
    (OUT / "h12.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
