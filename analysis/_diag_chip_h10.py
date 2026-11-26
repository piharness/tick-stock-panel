"""H10：补充质押（被迫补仓）→ 雪崩，还是反弹？

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
背景
  「平仓线触发」需要存续期 + 前复权 + 无噪声平仓线，直接做不可靠（openline/close 中位 0.64，
  但 53% 落在 [0.4,0.9] 之外，含除权未调整、口径混杂）。
  换一个更干净、更本质的「被迫」信号：**补充质押**。股价下跌触及预警线后，大股东被迫加押更多股票
  来避免平仓。所以「补充质押公告」本身就是「股价已经跌到触发大股东被迫行动」的可观测证据，
  且不依赖噪声平仓线。

机制（两个对立假说）
  雪崩假说：补充质押 = 大股东资金链断裂、被强平前兆 → 后续继续跌。
  抄底假说：补充质押 = 股价已跌到位、触发线已现 → 超跌反弹（A 股边际持有人是抄底的）。

事件
  补充质押：PF_REASON 含「补充质押/补充担保/追加质押」等被迫补仓字样（11,875 笔，去重到股票-日）。
  对照：同期「非补充」的新增质押事件（大股东主动质押，非被迫）。
  入场：公告日后第一个交易日开盘；出场 t+21/41/61 收盘。

预测（三层）
  L1 方向：补充质押组 fwd20 < 新增质押组（被迫的更差）。
  L2 持续：fwd40/60 仍更差（雪崩持续）。
  L3 绝对：补充质押组 fwd20 是否 < 0（继续跌）—— 若是，雪崩存在；若为正，反弹。

杀死
  L1 无差异或反向（补充质押组不更差）；或 L3 补充组 fwd20 > 0（反弹）。
  控制：换手、波动、规模、久期、事件前动量（补充组本就发生在下跌后）。
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

    print("[2/5] 事件（补充质押 vs 新增质押）...", flush=True)
    p = pd.read_parquet(PLEDGE)
    p["reason"] = p["PF_REASON"].astype(str)
    p["is_buchong"] = p["reason"].str.contains("补充质押|补充担保|补充保证金|追加质押|追加担保|补充股份质押", na=False)
    inst = pd.read_parquet(INST)[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    # 去重到 股票-日（同日多股东合并为一条）
    p["notice"] = p["notice"].astype("datetime64[ns]")
    p = p.sort_values("notice").drop_duplicates(subset=["symbol", "notice", "is_buchong"])
    # 对齐到公告日后第一个交易日
    date_arr = dates.values
    p["tidx"] = np.searchsorted(date_arr, p["notice"].to_numpy(), side="right")
    p = p[(p["tidx"] >= 61) & (p["tidx"] < n - 62)]
    print(f"      补充 {int(p['is_buchong'].sum()):,}  新增 {int((~p['is_buchong']).sum()):,}", flush=True)

    print("[3/5] forward returns ...", flush=True)
    bu = p[p["is_buchong"]].copy()
    nu = p[~p["is_buchong"]].copy()
    def outcomes(df):
        ti = df["tidx"].to_numpy(); ci = df["idx"].to_numpy()
        df = df.copy()
        df["f20"] = fwd_h(O, C, ti, ci, 20)
        df["f40"] = fwd_h(O, C, ti, ci, 40)
        df["f60"] = fwd_h(O, C, ti, ci, 60)
        df["date"] = dates[ti]
        return df
    bu = outcomes(bu); nu = outcomes(nu)
    ev = pd.concat([bu.assign(grp="buchong"), nu.assign(grp="new")], ignore_index=True)

    print("[4/5] controls ...", flush=True)
    r = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    S1 = np.vstack([np.zeros((1, k)), np.cumsum(r, axis=0)])
    S2 = np.vstack([np.zeros((1, k)), np.cumsum(r * r, axis=0)])
    TOc = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(TO, nan=0.0), axis=0)])
    Ac = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(A, nan=0.0), axis=0)])
    tig = ev["tidx"].to_numpy(); cig = ev["idx"].to_numpy()
    s1 = S1[tig, cig] - S1[tig - 20, cig]
    s2 = S2[tig, cig] - S2[tig - 20, cig]
    ev["vol20"] = np.sqrt(np.maximum(s2 / 20 - (s1 / 20) ** 2, 0.0))
    ev["turn20"] = (TOc[tig, cig] - TOc[tig - 20, cig]) / 20
    ev["size"] = np.log(np.maximum((Ac[tig, cig] - Ac[tig - 20, cig]) / 20, 1.0))
    ev["ret20"] = C[tig, cig] / C[tig - 20, cig] - 1.0
    ev["ret60"] = C[tig, cig] / C[tig - 60, cig] - 1.0
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age["symbol"] = age["symbol"].astype(str)
    ev["date"] = ev["date"].astype("datetime64[ns]")
    age["date"] = age["date"].astype("datetime64[ns]")
    ev["symbol"] = ev["symbol"].astype(str)
    ev = pd.merge_asof(ev.sort_values("date"), age[["date", "symbol", "D"]].sort_values("date"),
                       on="date", by="symbol", direction="backward")
    ev = ev.dropna(subset=["f20", "vol20", "turn20", "size", "D", "ret20", "ret60"])
    ev["ym"] = ev["date"].dt.to_period("M").astype(str)
    ev.to_parquet(OUT / "h10_events.parquet", index=False)
    print(f"      最终事件 {len(ev):,}", flush=True)

    print("[5/5] 检验 ...", flush=True)
    def monthly(ev2):
        g = ev2.groupby(["ym", "grp"]).agg(f20=("f20", "mean"), f40=("f40", "mean"), f60=("f60", "mean"),
                                           n=("f20", "size")).reset_index()
        w20 = g.pivot(index="ym", columns="grp", values="f20")
        w40 = g.pivot(index="ym", columns="grp", values="f40")
        w60 = g.pivot(index="ym", columns="grp", values="f60")
        out = pd.DataFrame(index=w20.index)
        out["bu_f20"] = w20["buchong"] if "buchong" in w20 else np.nan
        out["new_f20"] = w20["new"] if "new" in w20 else np.nan
        out["bu_f40"] = w40["buchong"] if "buchong" in w40 else np.nan
        out["new_f40"] = w40["new"] if "new" in w40 else np.nan
        out["bu_f60"] = w60["buchong"] if "buchong" in w60 else np.nan
        out["new_f60"] = w60["new"] if "new" in w60 else np.nan
        out["bu_minus_new_f20"] = out["bu_f20"] - out["new_f20"]
        out["bu_minus_new_f40"] = out["bu_f40"] - out["new_f40"]
        out["bu_minus_new_f60"] = out["bu_f60"] - out["new_f60"]
        return out.reset_index()

    mon = monthly(ev)
    mon.to_parquet(OUT / "h10_monthly.parquet", index=False)

    # 控制回归（月度截面内）：f20 ~ is_buchong + controls
    reg = []
    for ym, g in ev.groupby("ym"):
        if len(g) < 30:
            continue
        X = np.column_stack([(g["grp"] == "buchong").to_numpy(float),
                             zscore(g["vol20"]), zscore(g["turn20"]), zscore(g["size"]), zscore(g["D"]),
                             zscore(g["ret20"]), zscore(g["ret60"]), np.ones(len(g))])
        y = g["f20"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 30:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym, "b_buchong": b[0], "b_ret20": b[5], "b_ret60": b[6], "n": int(ok.sum())})
    rr = pd.DataFrame(reg)

    summary = {
        "sample": {"buchong": int((ev["grp"] == "buchong").sum()), "new": int((ev["grp"] == "new").sum()),
                   "months": int(ev["ym"].nunique()),
                   "start": str(ev["date"].min().date()), "end": str(ev["date"].max().date())},
        "event_before": {
            "bu_ret20": round(float(ev[ev["grp"] == "buchong"]["ret20"].mean() * 100), 2),
            "new_ret20": round(float(ev[ev["grp"] == "new"]["ret20"].mean() * 100), 2),
            "bu_ret60": round(float(ev[ev["grp"] == "buchong"]["ret60"].mean() * 100), 2),
            "new_ret60": round(float(ev[ev["grp"] == "new"]["ret60"].mean() * 100), 2),
        },
        "cells_f20": {"buchong": round(float(mon["bu_f20"].mean() * 1e4), 1),
                      "new": round(float(mon["new_f20"].mean() * 1e4), 1)},
        "L1": {"bu_minus_new_f20_bp": round(float(mon["bu_minus_new_f20"].mean() * 1e4), 1),
               "t": round(float(nw_t(mon["bu_minus_new_f20"])), 2)},
        "L2": {"bu_minus_new_f40_bp": round(float(mon["bu_minus_new_f40"].mean() * 1e4), 1),
               "t_f40": round(float(nw_t(mon["bu_minus_new_f40"])), 2),
               "bu_minus_new_f60_bp": round(float(mon["bu_minus_new_f60"].mean() * 1e4), 1),
               "t_f60": round(float(nw_t(mon["bu_minus_new_f60"])), 2)},
        "L3_absolute": {"bu_f20_bp": round(float(mon["bu_f20"].mean() * 1e4), 1),
                        "bu_f40_bp": round(float(mon["bu_f40"].mean() * 1e4), 1),
                        "bu_f60_bp": round(float(mon["bu_f60"].mean() * 1e4), 1)},
        "controlled": {"b_buchong_bp": round(float(rr["b_buchong"].mean()), 1),
                       "t": round(float(nw_t(rr["b_buchong"])), 2), "months": int(len(rr))},
    }
    (OUT / "h10.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
