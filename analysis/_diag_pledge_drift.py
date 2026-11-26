"""预注册冻结实现：质押存续期累计涨跌 → 未来收益。

对应 docs/structural-edge/prereg-pledge-drift.md（pledge-drift-2026-11-26）。
冻结规格：信号 pledge_drift、过滤、rank 三分位、fwd60 主指标、月聚簇、design/holdout 先行检验。
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
HOLD_CUT = "2024-01-01"


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

    print("[2/5] 事件 + 冻结过滤 ...", flush=True)
    # ST（按当前名称）+ 上市≥250日
    inst = pd.read_parquet(INST)[["code", "symbol", "name"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    inst["symbol"] = inst["symbol"].astype(str)
    is_st = inst[inst["name"].astype(str).str.contains("ST", na=False)]["symbol"].tolist()
    p = pd.read_parquet(PLEDGE)
    p = p.dropna(subset=["OPENLINE", "CLOSE_FORWARD_ADJPRICE", "CLOSE_FORWARD_ADJPRICE_TODAY"])
    p = p[(p["OPENLINE"] > 0) & (p["CLOSE_FORWARD_ADJPRICE"] > 0) & (p["CLOSE_FORWARD_ADJPRICE_TODAY"] > 0)]
    p["zhesuan"] = p["OPENLINE"] / p["CLOSE_FORWARD_ADJPRICE"]
    p["drift"] = p["CLOSE_FORWARD_ADJPRICE_TODAY"] / p["CLOSE_FORWARD_ADJPRICE"] - 1.0
    p = p[(p["zhesuan"] >= 0.3) & (p["zhesuan"] <= 0.9)]
    p = p[(p["drift"] >= -0.5) & (p["drift"] <= 0.5)]
    p = p.merge(inst[["code", "symbol"]], on="code", how="left").dropna(subset=["symbol"])
    p = p[~p["symbol"].isin(is_st)]
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    p["notice"] = p["notice"].astype("datetime64[ns]")
    p = p.sort_values("notice").drop_duplicates(subset=["symbol", "notice"])
    date_arr = dates.values
    p["tidx"] = np.searchsorted(date_arr, p["notice"].to_numpy(), side="right")
    # 上市≥250日：该 symbol 在日线里的第一条记录距今≥250
    first_idx = pd.Series({s: i for i, s in enumerate(syms)})
    # 用每个 symbol 首次非 NaN 的日期
    first_seen = pd.DataFrame({"symbol": syms, "first": np.argmax(np.isfinite(C), axis=0)})
    first_seen["symbol"] = first_seen["symbol"].astype(str)
    p = p.merge(first_seen, on="symbol", how="left")
    p = p[(p["tidx"] >= 61) & (p["tidx"] - p["first"] >= 250) & (p["tidx"] < n - 62)]
    print(f"      事件 {len(p):,}", flush=True)

    print("[3/5] 指标 ...", flush=True)
    ti = p["tidx"].to_numpy(); ci = p["idx"].to_numpy()
    p["f20"] = fwd_h(O, C, ti, ci, 20)
    p["f40"] = fwd_h(O, C, ti, ci, 40)
    p["f60"] = fwd_h(O, C, ti, ci, 60)
    p["date"] = dates[ti]

    print("[4/5] 控制 ...", flush=True)
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
    # 60日均成交额≥1500万
    amt60 = (Ac[tig, cig] - Ac[tig - 60, cig]) / 60
    p = p[amt60 >= 1.5e7]
    tig = p["tidx"].to_numpy(); cig = p["idx"].to_numpy()
    p["ret60"] = C[tig, cig] / C[tig - 60, cig] - 1.0
    p["ret250"] = np.where(tig >= 250, C[tig, cig] / C[tig - 250, cig] - 1.0, np.nan)
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age["symbol"] = age["symbol"].astype(str)
    p["date"] = p["date"].astype("datetime64[ns]")
    p["symbol"] = p["symbol"].astype(str)
    p = pd.merge_asof(p.sort_values("date"), age[["date", "symbol", "D"]].sort_values("date"),
                      on="date", by="symbol", direction="backward")
    p = p.dropna(subset=["f20", "f60", "vol20", "turn20", "size", "D", "ret60", "ret250"])
    p["ym"] = p["date"].dt.to_period("M").astype(str)
    p.to_parquet(OUT / "pledge_drift_events.parquet", index=False)
    print(f"      最终事件 {len(p):,}（含成交额过滤）", flush=True)

    print("[5/5] design/holdout ...", flush=True)
    # rank 三分位（月度截面内）
    p["q"] = p.groupby("ym")["drift"].transform(lambda x: pd.qcut(x.rank(method="first"), 3, labels=False))

    def run(seg):
        rows = []
        for ym, g in seg.groupby("ym"):
            q2 = g[g["q"] == 2]["f60"].mean()
            q0 = g[g["q"] == 0]["f60"].mean()
            rows.append({"ym": ym, "d": q2 - q0, "n": len(g),
                         "f20_d": g[g["q"] == 2]["f20"].mean() - g[g["q"] == 0]["f20"].mean()})
        m = pd.DataFrame(rows).dropna(subset=["d"])
        # 控制回归
        reg = []
        for ym, g in seg.groupby("ym"):
            if len(g) < 40:
                continue
            X = np.column_stack([zscore(g["drift"]), zscore(g["ret250"]), zscore(g["ret60"]),
                                 zscore(g["vol20"]), zscore(g["turn20"]), zscore(g["size"]), zscore(g["D"]), np.ones(len(g))])
            y = g["f60"].to_numpy(float) * 1e4
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 40:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            reg.append({"ym": ym, "b_drift": b[0]})
        r = pd.DataFrame(reg)
        return {
            "d_mean_bp": round(float(m["d"].mean() * 1e4), 1),
            "d_t": round(float(nw_t(m["d"])), 2),
            "f20_d_mean_bp": round(float(m["f20_d"].mean() * 1e4), 1),
            "positive_months": round(float((m["d"] > 0).mean()), 3),
            "b_drift_ctrl_bp": round(float(r["b_drift"].mean()), 1),
            "b_drift_ctrl_t": round(float(nw_t(r["b_drift"])), 2),
            "months": int(len(m)),
            "events": int(seg.groupby("ym")["f60"].count().sum()),
        }

    design = run(p[p["date"] < HOLD_CUT])
    holdout = run(p[p["date"] >= HOLD_CUT])
    full = run(p)
    summary = {"design_2019_2023": design, "holdout_2024_2026": holdout, "full": full,
               "sample": {"events": int(len(p)), "months": int(p["ym"].nunique()),
                          "drift_median": round(float(p["drift"].median() * 100), 2),
                          "start": str(p["date"].min().date()), "end": str(p["date"].max().date())}}
    (OUT / "pledge_drift.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
