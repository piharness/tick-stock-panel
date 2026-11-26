"""H8：放量下跌 vs 缩量下跌 —— 被迫卖家的痕迹（雪崩子集）。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
背景
  H2/H3/H5 三条独立测量指向同一件事：A 股边际持有人是「抄底的」，快速下跌被接住、反弹。
  但这个结论来自**总量平均**。雪崩假说认为存在一个「被迫卖家」子集：杠杆平仓、质押、赎回的人
  **卖的是量、且必须持续卖**，所以他们的下跌是「放量」的，且不反弹——价格一路阴跌。

核心判别量：**相对放量**（不是绝对换手）。
  vol_ratio = 近 5 日平均换手 / 基线换手（t−60..t−21 平均）。
  用「相对」是为了剥离「这只股票本来就高换手」：放量 = 相对它自己的常态突然放大 = 有人在甩货。

预测（三层，逐层更强）
  L1 方向：快速下跌（cum5 ≤ −5%）里，放量子集 fwd20 < 缩量子集（放量更差）。
  L2 不对称：放量对「下跌」的伤害 > 对「上涨」的伤害（被迫卖是方向性的；上涨放量是抢筹/接盘）。
  L3 持续：放量下跌的 fwd40/fwd60 仍为负（雪崩），缩量下跌反弹（抄底）。

杀死
  L1 无差异（放量下跌仍反弹）；或 L2 不对称不存在（放量只是又一个换手代理，方向无关）。

控制
  市值、波动、绝对换手、动量。置换：把 vol_ratio 在同日内打乱（放量标签变随机），重跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h8"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "data" / "kline_daily_enriched"

HORIZON = 20
COOLDOWN = 20
FAST = 0.05
VOL_RATIO = 2.0
LIMIT = 0.095
NW_LAG = 6
N_PLACEBO = 100


def load_daily(start: str = "2017-01-01"):
    """分块读取日线数据，直接构建 (date × symbol) 宽表。

    原实现用 dataset.to_table() 一次性全量加载 + 4 张 pivot_table，
    内存峰值可达 10GB+。这里改为两遍流式扫描 + pyarrow/numpy 原生向量化：
      1) 只读 symbol/date 两列，用 pc.unique 收集唯一 symbol（排除 .BJ 且 close>0）与唯一 date；
      2) 分块读取 6 列，用 Categorical/searchsorted 映射到预分配的 float32 数组。
    全程内存与最终宽表同量级（几十 MB），不再有全量长表 / pivot 中间产物驻留。
    """
    dataset = ds.dataset(SRC, format="parquet")
    start_date = pd.Timestamp(start).date()
    # 统一过滤：date>=start 且 close>0 且非北交所。null 在 filter 中被视为 false 自动排除。
    filt = (
        (ds.field("date") >= start_date)
        & (ds.field("close") > 0)
        & pc.invert(pc.ends_with(ds.field("symbol"), ".BJ"))
    )

    # ---- 第一遍：轻量收集唯一 symbol 与 date（pyarrow 向量化 unique）----
    print("      pass 1/2: collect symbols & dates ...", flush=True)
    syms = set()
    days = set()
    scanner = dataset.scanner(columns=["symbol", "date"], filter=filt, batch_size=200_000)
    for batch in scanner.to_batches():
        syms.update(pc.unique(batch.column("symbol")).to_pylist())
        days.update(pc.unique(batch.column("date").cast(pa.int32())).to_pylist())

    syms = sorted(s for s in syms if s is not None)
    days = sorted(d for d in days if d is not None)
    days_arr = np.asarray(days, dtype=np.int64)
    n, k = len(days), len(syms)

    # 预分配 float32 宽表（n×k×4 字节，远小于 pivot_table 的 float64 + 中间产物）
    O = np.full((n, k), np.nan, dtype=np.float32)
    C = np.full((n, k), np.nan, dtype=np.float32)
    A = np.full((n, k), np.nan, dtype=np.float32)
    TO = np.full((n, k), np.nan, dtype=np.float32)

    # ---- 第二遍：分块读取并向量化 scatter ----
    print("      pass 2/2: scatter into wide matrix ...", flush=True)
    cols = ["symbol", "date", "open", "close", "amount", "turnover_rate"]
    scanner2 = dataset.scanner(columns=cols, filter=filt, batch_size=200_000)
    for batch in scanner2.to_batches():
        sym_col = batch.column("symbol").to_numpy(zero_copy_only=False)  # object array of str
        day_col = batch.column("date").cast(pa.int32()).to_numpy(zero_copy_only=False)  # int32 days
        op_col = batch.column("open").to_numpy(zero_copy_only=False)
        cl_col = batch.column("close").to_numpy(zero_copy_only=False)
        am_col = batch.column("amount").to_numpy(zero_copy_only=False)
        to_col = batch.column("turnover_rate").to_numpy(zero_copy_only=False)
        cc = pd.Categorical(sym_col, categories=syms).codes
        rr = np.searchsorted(days_arr, day_col)
        good = (cc >= 0) & (rr >= 0) & (rr < n)
        if not good.any():
            continue
        rr = rr[good]
        cc = cc[good]
        O[rr, cc] = op_col[good]
        C[rr, cc] = cl_col[good]
        A[rr, cc] = am_col[good]
        t = to_col[good]
        t = np.nan_to_num(t, nan=0.0) / 100.0
        np.clip(t, 0.0, 0.9, out=t)
        TO[rr, cc] = t

    dates_index = pd.to_datetime(days_arr, unit="D")
    return (
        pd.DataFrame(O, index=dates_index, columns=syms),
        pd.DataFrame(C, index=dates_index, columns=syms),
        pd.DataFrame(A, index=dates_index, columns=syms),
        pd.DataFrame(TO, index=dates_index, columns=syms),
    )


def nw_t(x, lag: int = NW_LAG) -> float:
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


def skewness(a, axis=0):
    a = np.asarray(a, dtype=float)
    mu = a.mean(axis=axis, keepdims=True)
    d = a - mu
    m2 = (d ** 2).mean(axis=axis)
    m3 = (d ** 3).mean(axis=axis)
    return m3 / np.maximum(m2, 1e-18) ** 1.5


def build_events(C, TO, cum5):
    """快速下跌/上涨事件，附带 vol_ratio。返回 (ti, ci, dir, vol_ratio, cum5)。"""
    n, k = C.shape
    # 5 日平均换手 与 基线换手
    to5 = np.full((n, k), np.nan)
    tobase = np.full((n, k), np.nan)
    cs = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(TO, nan=0.0), axis=0)])
    for t in range(60, n):
        to5[t] = (cs[t] - cs[t - 5]) / 5
        tobase[t] = (cs[t - 21] - cs[t - 60]) / 39
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = to5 / np.maximum(tobase, 1e-9)
    ret = np.vstack([np.full((1, k), np.nan), C[1:] / C[:-1] - 1.0])
    tradable = np.isfinite(ret) & (np.abs(ret) < LIMIT)

    out = []
    for sign in (1, -1):
        mask = (cum5 >= FAST) if sign > 0 else (cum5 <= -FAST)
        mask = mask & tradable & np.isfinite(vol_ratio) & (vol_ratio > 0)
        ti, ci = np.nonzero(mask)
        vr = vol_ratio[ti, ci]
        cu = cum5[ti, ci]
        order = np.argsort(ti, kind="stable")
        ti, ci, vr, cu = ti[order], ci[order], vr[order], cu[order]
        last = np.full(k, -10**9)
        keep = np.zeros(len(ti), dtype=bool)
        for j in range(len(ti)):
            if ti[j] - last[ci[j]] >= COOLDOWN:
                keep[j] = True
                last[ci[j]] = ti[j]
        out.append((ti[keep], ci[keep], np.full(keep.sum(), sign, np.int8), vr[keep], cu[keep]))
    ti = np.concatenate([o[0] for o in out])
    ci = np.concatenate([o[1] for o in out])
    dr = np.concatenate([o[2] for o in out])
    vr = np.concatenate([o[3] for o in out])
    cu = np.concatenate([o[4] for o in out])
    return ti, ci, dr, vr, cu


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
    print("[1/4] load ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(dtype=np.float32)
    C = cl_df.to_numpy(dtype=np.float32)
    A = am_df.to_numpy(dtype=np.float32)
    TO = to_df.to_numpy(dtype=np.float32)
    n, k = C.shape
    print(f"      {n} x {k}", flush=True)

    print("[2/4] events ...", flush=True)
    cum5 = np.full((n, k), np.nan, dtype=np.float32)
    cum5[5:] = C[5:] / C[:-5] - 1.0
    ti, ci, dr, vr, cu = build_events(C, TO, cum5)
    print(f"      events={len(ti):,}  down={int((dr<0).sum()):,}  up={int((dr>0).sum()):,}", flush=True)

    print("[3/4] forward returns ...", flush=True)
    f20 = fwd_h(O, C, ti, ci, 20)
    f40 = fwd_h(O, C, ti, ci, 40)
    f60 = fwd_h(O, C, ti, ci, 60)
    ok = np.isfinite(f20)
    ti, ci, dr, vr, cu = ti[ok], ci[ok], dr[ok], vr[ok], cu[ok]
    f20, f40, f60 = f20[ok], f40[ok], f60[ok]
    print(f"      outcomes={len(ti):,}", flush=True)

    # 控制
    r = np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0])
    r = np.clip(np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0), -0.2, 0.2)
    S1 = np.vstack([np.zeros((1, k)), np.cumsum(r, axis=0)])
    S2 = np.vstack([np.zeros((1, k)), np.cumsum(r * r, axis=0)])
    TOc = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(TO, nan=0.0), axis=0)])
    Ac = np.vstack([np.zeros((1, k)), np.cumsum(np.nan_to_num(A, nan=0.0), axis=0)])
    good = ti >= 61
    tig = ti[good]; cig = ci[good]
    s1 = S1[tig, cig] - S1[tig - 20, cig]
    s2 = S2[tig, cig] - S2[tig - 20, cig]
    vol20 = np.sqrt(np.maximum(s2 / 20 - (s1 / 20) ** 2, 0.0))
    ev = pd.DataFrame({
        "date": dates[tig], "dir": dr[good], "vr": vr[good], "cum5": cu[good],
        "f20": f20[good], "f40": f40[good], "f60": f60[good],
        "vol20": vol20,
        "turn20": (TOc[tig, cig] - TOc[tig - 20, cig]) / 20,
        "size": np.log(np.maximum((Ac[tig, cig] - Ac[tig - 20, cig]) / 20, 1.0)),
        "ret20": C[tig, cig] / C[tig - 20, cig] - 1.0,
    })
    ev["hi"] = ev["vr"] >= VOL_RATIO
    ev["cell"] = np.where(ev["dir"] > 0, "up", "dn") + np.where(ev["hi"], "_hi", "_lo")
    ev["ym"] = ev["date"].values.astype("datetime64[M]")

    def monthly(ev2):
        g = ev2.groupby(["ym", "cell"]).agg(f20=("f20", "mean"), f40=("f40", "mean"), f60=("f60", "mean"), n=("f20", "size")).reset_index()
        w = g.pivot(index="ym", columns="cell", values="f20")
        w40 = g.pivot(index="ym", columns="cell", values="f40")
        w60 = g.pivot(index="ym", columns="cell", values="f60")
        for c in ["up_hi", "up_lo", "dn_hi", "dn_lo"]:
            if c not in w.columns:
                w[c] = np.nan; w40[c] = np.nan; w60[c] = np.nan
        out = pd.DataFrame(index=w.index)
        for c in ["up_hi", "up_lo", "dn_hi", "dn_lo"]:
            out[f"f20_{c}"] = w[c]; out[f"f40_{c}"] = w40[c]; out[f"f60_{c}"] = w60[c]
        out["dn_hi_minus_lo"] = w["dn_hi"] - w["dn_lo"]
        out["up_hi_minus_lo"] = w["up_hi"] - w["up_lo"]
        out["interaction"] = (w["dn_hi"] - w["dn_lo"]) - (w["up_hi"] - w["up_lo"])
        return out.reset_index()

    mon = monthly(ev)
    mon.to_parquet(OUT / "monthly.parquet", index=False)

    def summ(col):
        x = mon[col].dropna().to_numpy()
        return {"mean_bp": round(float(np.mean(x) * 1e4), 1), "nw_t": round(float(nw_t(x)), 2), "n": int(len(x))}

    summary = {
        "sample": {"events": int(len(ev)), "down": int((ev["dir"] < 0).sum()),
                   "down_hi": int(((ev["dir"] < 0) & ev["hi"]).sum()),
                   "down_hi_share": round(float(((ev["dir"] < 0) & ev["hi"]).mean() / max((ev["dir"] < 0).mean(), 1e-9)), 3),
                   "start": str(ev["date"].min().date()), "end": str(ev["date"].max().date()),
                   "months": int(ev["ym"].nunique())},
        "cells": {c: summ(f"f20_{c}") for c in ["up_hi", "up_lo", "dn_hi", "dn_lo"]},
        "L1_down": {"hi_minus_lo_bp": round(float(mon["dn_hi_minus_lo"].mean() * 1e4), 1),
                    "t": round(float(nw_t(mon["dn_hi_minus_lo"])), 2)},
        "L2_asymmetry": {"interaction_bp": round(float(mon["interaction"].mean() * 1e4), 1),
                         "t": round(float(nw_t(mon["interaction"])), 2),
                         "up_hi_minus_lo_bp": round(float(mon["up_hi_minus_lo"].mean() * 1e4), 1),
                         "t_up": round(float(nw_t(mon["up_hi_minus_lo"])), 2)},
        "L3_persistence": {"dn_hi_f40_bp": round(float(mon["f40_dn_hi"].mean() * 1e4), 1),
                           "dn_lo_f40_bp": round(float(mon["f40_dn_lo"].mean() * 1e4), 1),
                           "dn_hi_f60_bp": round(float(mon["f60_dn_hi"].mean() * 1e4), 1),
                           "dn_lo_f60_bp": round(float(mon["f60_dn_lo"].mean() * 1e4), 1)},
    }

    print("[4/4] placebo + controls ...", flush=True)
    # 连续剂量反应：f20 ~ log(vol_ratio)（分方向），比二值置换更强、更快
    ev["lvr"] = np.log(ev["vr"].clip(lower=0.05))
    def cont_reg(ev2):
        rows = []
        for ym, g in ev2.groupby("ym"):
            if len(g) < 60:
                continue
            dn = g[g["dir"] < 0]; up = g[g["dir"] > 0]
            if len(dn) < 30 or len(up) < 30:
                continue
            # 下跌侧：f20 ~ lvr + controls
            Xd = np.column_stack([zscore(dn["lvr"]), zscore(dn["vol20"]), zscore(dn["turn20"]), zscore(dn["size"]), zscore(dn["ret20"]), np.ones(len(dn))])
            yd = dn["f20"].to_numpy(float) * 1e4
            okd = np.isfinite(yd) & np.isfinite(Xd).all(axis=1)
            bd, *_ = np.linalg.lstsq(Xd[okd], yd[okd], rcond=None) if okd.sum() >= 30 else (np.full(6, np.nan), None)
            # 上涨侧
            Xu = np.column_stack([zscore(up["lvr"]), zscore(up["vol20"]), zscore(up["turn20"]), zscore(up["size"]), zscore(up["ret20"]), np.ones(len(up))])
            yu = up["f20"].to_numpy(float) * 1e4
            oku = np.isfinite(yu) & np.isfinite(Xu).all(axis=1)
            bu, *_ = np.linalg.lstsq(Xu[oku], yu[oku], rcond=None) if oku.sum() >= 30 else (np.full(6, np.nan), None)
            rows.append({"ym": ym, "b_dn": bd[0], "b_up": bu[0], "asym": bd[0] - bu[0]})
        return pd.DataFrame(rows)
    cr = cont_reg(ev).dropna()
    summary["L1_continuous"] = {"b_lvr_down_bp": round(float(cr["b_dn"].mean()), 1), "t": round(float(nw_t(cr["b_dn"])), 2), "months": int(len(cr))}
    summary["L2_asymmetry_continuous"] = {"asym_bp": round(float(cr["asym"].mean()), 1), "t": round(float(nw_t(cr["asym"])), 2)}

    # 快速向量化置换（50 次，只算 dn_hi_minus_lo 和 interaction）
    rng = np.random.default_rng(20261126)
    dates_arr = ev["date"].to_numpy()
    hi_arr = ev["hi"].to_numpy()
    dir_arr = ev["dir"].to_numpy()
    f_arr = ev["f20"].to_numpy()
    yms = ev["ym"].to_numpy()
    def quick_cells(hi, f, dr, ym):
        dn = dr < 0
        dn_hi = dn & hi
        dn_lo = dn & (~hi)
        up_hi = (~dn) & hi
        up_lo = (~dn) & (~hi)
        # 用 pandas groupby 快速求月度均值
        dd = pd.DataFrame({"ym": ym, "dn_hi": np.where(dn_hi, f, np.nan),
                           "dn_lo": np.where(dn_lo, f, np.nan),
                           "up_hi": np.where(up_hi, f, np.nan),
                           "up_lo": np.where(up_lo, f, np.nan)})
        m = dd.groupby("ym").mean()
        dn_diff = (m["dn_hi"] - m["dn_lo"]).mean()
        inter = ((m["dn_hi"] - m["dn_lo"]) - (m["up_hi"] - m["up_lo"])).mean()
        return dn_diff, inter
    true_l1, true_int = quick_cells(hi_arr, f_arr, dir_arr, yms)
    # 预计算按日分组边界（stable 排序保持组内原始顺序），
    # 避免原来每轮对每个日期做一次 O(n) 全扫描（50 × 天数 × n，极慢）。
    date_order = np.argsort(dates_arr, kind="stable")
    dates_sorted = dates_arr[date_order]
    hi_sorted = hi_arr[date_order]
    starts = np.flatnonzero(np.r_[True, dates_sorted[1:] != dates_sorted[:-1]])
    ends = np.r_[starts[1:], len(dates_sorted)]
    plac_l1, plac_int = [], []
    for _ in range(50):
        shuf_sorted = hi_sorted.copy()
        for g in range(len(starts)):
            s, e = starts[g], ends[g]
            shuf_sorted[s:e] = rng.permutation(shuf_sorted[s:e])
        shuf = np.empty_like(hi_arr)
        shuf[date_order] = shuf_sorted
        d1, d2 = quick_cells(shuf, f_arr, dir_arr, yms)
        plac_l1.append(d1); plac_int.append(d2)
    plac_l1 = np.asarray(plac_l1); plac_int = np.asarray(plac_int)
    summary["placebo"] = {
        "interaction_true_bp": round(float(true_int * 1e4), 1),
        "interaction_placebo_mean_bp": round(float(plac_int.mean() * 1e4), 1),
        "interaction_percentile": round(float((plac_int < true_int).mean() * 100), 1),
        "L1_true_bp": round(float(true_l1 * 1e4), 1),
        "L1_placebo_mean_bp": round(float(plac_l1.mean() * 1e4), 1),
        "L1_percentile": round(float((plac_l1 < true_l1).mean() * 100), 1),
    }

    # 控制回归：f20 ~ hi (within down) + controls
    def reg_down(ev2):
        rows = []
        for ym, g in ev2.groupby("ym"):
            if len(g) < 50:
                continue
            dn = g[g["dir"] < 0]
            if len(dn) < 30:
                continue
            X = np.column_stack([dn["hi"].to_numpy(float), zscore(dn["vol20"]), zscore(dn["turn20"]), zscore(dn["size"]), zscore(dn["ret20"]), np.ones(len(dn))])
            y = dn["f20"].to_numpy(float) * 1e4
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 30:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            rows.append({"ym": ym, "b_hi": b[0], "n": int(ok.sum())})
        return pd.DataFrame(rows)
    rr = reg_down(ev)
    summary["L1_controlled"] = {"b_hi_bp": round(float(rr["b_hi"].mean()), 1), "t": round(float(nw_t(rr["b_hi"])), 2), "months": int(len(rr))}

    (OUT / "h8.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def zscore(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


if __name__ == "__main__":
    main()
