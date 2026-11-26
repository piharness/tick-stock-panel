"""H11：股价距质押平仓线的距离 → 强平雪崩？

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
背景
  H10 发现「补充质押」不是「被迫」的好代理：它事件前动量是正的，且距预警线<20% 的占比（36%）
  反而低于新增质押（46%）。「补充/新增」是标签，不是触发。
  真正可观测的「被迫」信号是**股价距平仓线的物理距离**：ratio = openline / close（公告日）。
  ratio > 0.8 = 股价距平仓线 < 20%，接近强平；ratio ∈ (0.4, 0.7) = 安全距离。

机制（两个对立假说）
  雪崩假说：股价接近平仓线 → 大股东无力补仓 → 券商强平 → 加速下跌。
  抄底假说：股价接近平仓线 = 已跌到位 → 超跌反弹（A 股边际持有人是抄底的，H3/H8/H9）。

事件
  质押公告（openline/close 落在 [0.3, 1.2] 的合理样本，剔除除权噪声），去重到股票-日。
  入场：公告日后第一个交易日开盘；出场 t+21/41/61。

预测（三层）
  L1 方向：near 组（ratio>0.8）fwd20 < far 组（0.4<ratio<0.7）。
  L2 持续：fwd40/60 仍更差。
  L3 绝对：near 组 fwd20 是否 < 0（继续跌）—— 若是雪崩存在，若为正反弹。

杀死
  L1 无差异或反向；或 L3 near 组 fwd20 > 0。
  控制：换手、波动、规模、久期、事件前动量（near 组本就发生在下跌后）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
PLEDGE = OUT / "pledge_raw.parquet"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
SRC = ROOT / "data" / "kline_daily_enriched"

COOLDOWN = 20
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

    print("[2/5] 事件（距平仓线距离）...", flush=True)
    p = pd.read_parquet(PLEDGE)
    p = p.dropna(subset=["openline", "close"])
    p = p[(p["openline"] > 0) & (p["close"] > 0)]
    p["ratio"] = p["openline"] / p["close"]
    p = p[(p["ratio"] >= 0.3) & (p["ratio"] <= 1.2)]  # 剔除除权/口径噪声
    inst = pd.read_parquet(INST)[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    p["notice"] = p["notice"].astype("datetime64[ns]")
    p = p.sort_values("notice").drop_duplicates(subset=["symbol", "notice"])
    # 分组：near（距平仓线<20%）、far（安全 30-60%）
    p["grp"] = np.where(p["ratio"] > 0.8, "near", np.where(p["ratio"] < 0.7, "far", "mid"))
    date_arr = dates.values
    p["tidx"] = np.searchsorted(date_arr, p["notice"].to_numpy(), side="right")
    p = p[(p["tidx"] >= 61) & (p["tidx"] < n - 62)]
    print(f"      near {int((p['grp']=='near').sum()):,}  far {int((p['grp']=='far').sum()):,}  mid {int((p['grp']=='mid').sum()):,}", flush=True)

    print("[3/5] forward returns ...", flush=True)
    def outcomes(df):
        ti = df["tidx"].to_numpy(); ci = df["idx"].to_numpy()
        df = df.copy()
        df["f20"] = fwd_h(O, C, ti, ci, 20)
        df["f40"] = fwd_h(O, C, ti, ci, 40)
        df["f60"] = fwd_h(O, C, ti, ci, 60)
        df["date"] = dates[ti]
        return df
    p = outcomes(p)

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
    p["ret20"] = C[tig, cig] / C[tig - 20, cig] - 1.0
    p["ret60"] = C[tig, cig] / C[tig - 60, cig] - 1.0
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age["symbol"] = age["symbol"].astype(str)
    p["date"] = p["date"].astype("datetime64[ns]")
    p["symbol"] = p["symbol"].astype(str)
    p = pd.merge_asof(p.sort_values("date"), age[["date", "symbol", "D"]].sort_values("date"),
                      on="date", by="symbol", direction="backward")
    p = p.dropna(subset=["f20", "vol20", "turn20", "size", "D", "ret20", "ret60"])
    p["ym"] = p["date"].dt.to_period("M").astype(str)
    p.to_parquet(OUT / "h11_events.parquet", index=False)
    print(f"      最终事件 {len(p):,}", flush=True)

    print("[5/5] 检验 ...", flush=True)
    p = p[p["grp"] != "mid"]
    def monthly(df):
        g = df.groupby(["ym", "grp"]).agg(f20=("f20", "mean"), f40=("f40", "mean"), f60=("f60", "mean")).reset_index()
        w20 = g.pivot(index="ym", columns="grp", values="f20")
        w40 = g.pivot(index="ym", columns="grp", values="f40")
        w60 = g.pivot(index="ym", columns="grp", values="f60")
        out = pd.DataFrame(index=w20.index)
        for c, w in [("f20", w20), ("f40", w40), ("f60", w60)]:
            out[f"near_{c}"] = w["near"] if "near" in w else np.nan
            out[f"far_{c}"] = w["far"] if "far" in w else np.nan
            out[f"near_minus_far_{c}"] = out[f"near_{c}"] - out[f"far_{c}"]
        return out.reset_index()
    mon = monthly(p)
    mon.to_parquet(OUT / "h11_monthly.parquet", index=False)

    reg = []
    for ym, g in p.groupby("ym"):
        if len(g) < 30:
            continue
        X = np.column_stack([(g["grp"] == "near").to_numpy(float),
                             zscore(g["vol20"]), zscore(g["turn20"]), zscore(g["size"]), zscore(g["D"]),
                             zscore(g["ret20"]), zscore(g["ret60"]), np.ones(len(g))])
        y = g["f20"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 30:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym, "b_near": b[0], "n": int(ok.sum())})
    rr = pd.DataFrame(reg)

    summary = {
        "sample": {"near": int((p["grp"] == "near").sum()), "far": int((p["grp"] == "far").sum()),
                   "months": int(p["ym"].nunique()),
                   "start": str(p["date"].min().date()), "end": str(p["date"].max().date())},
        "event_before": {
            "near_ret20": round(float(p[p["grp"] == "near"]["ret20"].mean() * 100), 2),
            "far_ret20": round(float(p[p["grp"] == "far"]["ret20"].mean() * 100), 2),
            "near_ret60": round(float(p[p["grp"] == "near"]["ret60"].mean() * 100), 2),
            "far_ret60": round(float(p[p["grp"] == "far"]["ret60"].mean() * 100), 2),
        },
        "cells_f20": {"near": round(float(mon["near_f20"].mean() * 1e4), 1),
                      "far": round(float(mon["far_f20"].mean() * 1e4), 1)},
        "L1": {"near_minus_far_f20_bp": round(float(mon["near_minus_far_f20"].mean() * 1e4), 1),
               "t": round(float(nw_t(mon["near_minus_far_f20"])), 2)},
        "L2": {"near_minus_far_f40_bp": round(float(mon["near_minus_far_f40"].mean() * 1e4), 1),
               "t_f40": round(float(nw_t(mon["near_minus_far_f40"])), 2),
               "near_minus_far_f60_bp": round(float(mon["near_minus_far_f60"].mean() * 1e4), 1),
               "t_f60": round(float(nw_t(mon["near_minus_far_f60"])), 2)},
        "L3_absolute": {"near_f20_bp": round(float(mon["near_f20"].mean() * 1e4), 1),
                        "near_f40_bp": round(float(mon["near_f40"].mean() * 1e4), 1),
                        "near_f60_bp": round(float(mon["near_f60"].mean() * 1e4), 1)},
        "controlled": {"b_near_bp": round(float(rr["b_near"].mean()), 1),
                       "t": round(float(nw_t(rr["b_near"])), 2), "months": int(len(rr))},
    }
    (OUT / "h11.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
