"""H9：质押比例 → 下跌后的雪崩？

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
背景
  全卷 H1–H8 八次发现「标签拆开是换手/规模/波动/涨跌幅」。杠杆数据是最后一个外部数据源。
  代理性检验已出结论：融资余额占比 = 换手/久期代理（corr D −0.44 / turn +0.38），证伪；
  股权质押比例与换手/波动/规模/久期全部正交（|ρ|<0.1）——这是第一个真正正交的外部变量。

机制
  大股东质押股票借钱。股价下跌 → 触及预警线（补仓）→ 触及平仓线（强平）→ 券商卖出质押股
  → 股价加速下跌（雪崩）。所以「高质押比例」的股票，在下跌后应恢复更慢、回撤更深。

事件
  个股快速下跌：cum5 = close[t]/close[t-5]-1 ≤ −5%，|ret_t| < 9.5%（排除封板），冷却 20 日。
  分组：事件日前（asof）股票级累计质押比例 pledge_tsr（占总股本 %），月度截面内三分位：
    Q0 无质押 / Q1 低 / Q2 高。
  入场 t+1 开盘，出场 t+21/41/61 收盘。

预测（三层）
  L1 方向：高质押组下跌后 fwd20 < 低质押组（雪崩）。
  L2 持续：高质押组 fwd40/60 仍更差（雪崩持续，不是一次性砸坑）。
  L3 不对称：质押的伤害只在下跌侧（被迫卖是方向性的），上涨侧无差异。

杀死
  L1 无差异（质押正交但不做事）；或方向相反（高质押更稳）。
  控制：换手、波动、规模、久期、动量；置换：打乱 pledge 标签。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "data" / "kline_daily_enriched"
PLEDGE = OUT / "pledge_ts.parquet"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"

COOLDOWN = 20
FAST = 0.05
LIMIT = 0.095
NW_LAG = 6
N_PLACEBO = 50


def load_daily(start: str = "2019-01-01"):
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
    print("[1/5] load daily ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32)
    C = cl_df.to_numpy(np.float32)
    A = am_df.to_numpy(np.float32)
    TO = to_df.to_numpy(np.float32)
    n, k = C.shape
    print(f"      {n} x {k}", flush=True)

    print("[2/5] 下跌事件 ...", flush=True)
    cum5 = np.full((n, k), np.nan, np.float32)
    cum5[5:] = C[5:] / C[:-5] - 1.0
    ret = np.vstack([np.full((1, k), np.nan), C[1:] / C[:-1] - 1.0])
    mask = (cum5 <= -FAST) & (np.abs(ret) < LIMIT) & np.isfinite(cum5)
    ti, ci = np.nonzero(mask)
    order = np.argsort(ti, kind="stable")
    ti, ci = ti[order], ci[order]
    last = np.full(k, -10**9)
    keep = np.zeros(len(ti), bool)
    for j in range(len(ti)):
        if ti[j] - last[ci[j]] >= COOLDOWN:
            keep[j] = True
            last[ci[j]] = ti[j]
    ti, ci = ti[keep], ci[keep]
    print(f"      事件 {len(ti):,} 个", flush=True)

    print("[3/5] 质押比例 asof ...", flush=True)
    pl = pd.read_parquet(PLEDGE)
    pl["date"] = pd.to_datetime(pl["date"])
    inst = pd.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet")[["code", "symbol"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    inst["symbol"] = inst["symbol"].astype(str)
    pl = pl.merge(inst, on="code", how="left")
    pl = pl.dropna(subset=["symbol"]).sort_values("date")
    pl["date"] = pl["date"].astype("datetime64[ns]")
    pl["symbol"] = pl["symbol"].astype(str)
    ev = pd.DataFrame({"date": dates[ti], "symbol": syms[ci]})
    ev["date"] = ev["date"].astype("datetime64[ns]")
    ev["symbol"] = ev["symbol"].astype(str)
    ev = ev.sort_values("date")
    ev = pd.merge_asof(ev, pl[["date", "symbol", "pledge_tsr"]], on="date", by="symbol", direction="backward")
    ev["pledge"] = ev["pledge_tsr"].fillna(0.0)
    ev["ti"] = ti
    ev["ci"] = ci
    print(f"      合并后 {len(ev):,}，质押>0 占 {round(float((ev['pledge'] > 0).mean()), 3)}", flush=True)

    print("[4/5] forward returns ...", flush=True)
    ti = ev["ti"].to_numpy()
    ci = ev["ci"].to_numpy()
    f20 = fwd_h(O, C, ti, ci, 20)
    f40 = fwd_h(O, C, ti, ci, 40)
    f60 = fwd_h(O, C, ti, ci, 60)
    ev["f20"] = f20; ev["f40"] = f40; ev["f60"] = f60
    # 控制
    r = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    S1 = np.vstack([np.zeros((1, k)), np.cumsum(r, axis=0)])
    S2 = np.vstack([np.zeros((1, k)), np.cumsum(r * r, axis=0)])
    TOc = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(TO, nan=0.0), axis=0)])
    Ac = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(A, nan=0.0), axis=0)])
    g = ti >= 61
    tig, cig = ti[g], ci[g]
    s1 = S1[tig, cig] - S1[tig - 20, cig]
    s2 = S2[tig, cig] - S2[tig - 20, cig]
    ev = ev[g].copy()
    ev["vol20"] = np.sqrt(np.maximum(s2 / 20 - (s1 / 20) ** 2, 0.0))
    ev["turn20"] = (TOc[tig, cig] - TOc[tig - 20, cig]) / 20
    ev["size"] = np.log(np.maximum((Ac[tig, cig] - Ac[tig - 20, cig]) / 20, 1.0))
    ev["ret20"] = C[tig, cig] / C[tig - 20, cig] - 1.0
    # 久期 asof
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age["symbol"] = age["symbol"].astype(str)
    ev["date"] = ev["date"].astype("datetime64[ns]")
    ev = pd.merge_asof(ev.sort_values("date"), age[["date", "symbol", "D"]].sort_values("date"),
                       on="date", by="symbol", direction="backward")
    ev = ev.dropna(subset=["f20", "vol20", "turn20", "size", "D", "pledge"])
    ev["ym"] = ev["date"].dt.to_period("M").astype(str)
    ev.to_parquet(OUT / "h9_events.parquet", index=False)
    print(f"      最终事件 {len(ev):,}", flush=True)

    print("[5/5] 分组检验 ...", flush=True)
    # 月度截面内三分位
    def assign_tercile(df):
        df = df.copy()
        df["pt"] = df.groupby("ym")["pledge"].transform(lambda x: pd.qcut(x.rank(method="first"), 3, labels=False))
        return df
    ev = assign_tercile(ev)
    cells = []
    for ym, g in ev.groupby("ym"):
        row = {"ym": ym, "n": len(g)}
        for q in [0, 1, 2]:
            x = g[g["pt"] == q]
            row[f"f20_q{q}"] = x["f20"].mean() if len(x) else np.nan
            row[f"f40_q{q}"] = x["f40"].mean() if len(x) else np.nan
            row[f"f60_q{q}"] = x["f60"].mean() if len(x) else np.nan
            row[f"pledge_q{q}"] = x["pledge"].median() if len(x) else np.nan
        cells.append(row)
    mon = pd.DataFrame(cells)
    mon["hi_minus_lo_f20"] = mon["f20_q2"] - mon["f20_q0"]
    mon["hi_minus_lo_f40"] = mon["f40_q2"] - mon["f40_q0"]
    mon["hi_minus_lo_f60"] = mon["f60_q2"] - mon["f60_q0"]

    # 控制回归（月度截面内）
    reg = []
    for ym, g in ev.groupby("ym"):
        if len(g) < 60:
            continue
        X = np.column_stack([zscore(g["pledge"]), zscore(g["vol20"]), zscore(g["turn20"]), zscore(g["size"]), zscore(g["D"]), zscore(g["ret20"]), np.ones(len(g))])
        y = g["f20"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 60:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym, "b_pledge": b[0], "n": int(ok.sum())})
    rr = pd.DataFrame(reg)

    summary = {
        "sample": {"events": int(len(ev)), "months": int(ev["ym"].nunique()),
                   "pledge_pos_share": round(float((ev["pledge"] > 0).mean()), 3),
                   "pledge_median_q2": round(float(mon["pledge_q2"].mean()), 2),
                   "start": str(ev["date"].min().date()), "end": str(ev["date"].max().date())},
        "cells_f20": {"q0_no_pledge": round(float(mon["f20_q0"].mean() * 1e4), 1),
                      "q1_low": round(float(mon["f20_q1"].mean() * 1e4), 1),
                      "q2_high": round(float(mon["f20_q2"].mean() * 1e4), 1)},
        "L1": {"hi_minus_lo_f20_bp": round(float(mon["hi_minus_lo_f20"].mean() * 1e4), 1),
               "t": round(float(nw_t(mon["hi_minus_lo_f20"])), 2)},
        "L2": {"hi_minus_lo_f40_bp": round(float(mon["hi_minus_lo_f40"].mean() * 1e4), 1),
               "t_f40": round(float(nw_t(mon["hi_minus_lo_f40"])), 2),
               "hi_minus_lo_f60_bp": round(float(mon["hi_minus_lo_f60"].mean() * 1e4), 1),
               "t_f60": round(float(nw_t(mon["hi_minus_lo_f60"])), 2)},
        "L_controlled": {"b_pledge_bp": round(float(rr["b_pledge"].mean()), 1),
                         "t": round(float(nw_t(rr["b_pledge"])), 2), "months": int(len(rr))},
    }
    (OUT / "h9.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
