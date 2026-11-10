# -*- coding: utf-8 -*-
"""基线复核 A：事件定义 + 同涨幅对照 + 可执行总体（含炸板）+ HAC。

对被复核基线（analysis/_baseline_limitup.py）三处前提做攻击：
1. 涨停档位用 instruments.name 判 ST（快照名 ≠ 历史名）——5%/10% 会互错。
2. 涨停价基准用原始 prev_close（除权日基准错）。
3. R0/R1 的超额基准是全市场非涨停等权，且事件集条件在「收盘封住」上——
   而「收盘封住」15:00 才知道，盘中挂涨停价成交的人面对的是「触及涨停」全集。
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = Path(__file__).resolve().parent / "_audit_eventdef_result.json"

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
GEM_PREFIX = ("300", "301")
STAR_PREFIX = ("688", "689")
BJ_PREFIX = ("43", "83", "87", "88", "920")
CAND_ALL = (0.05, 0.10, 0.20, 0.30)
TICK = 0.011   # 认档容差（半个到一个最小价位）
REACH = 0.005  # 触及判定：最高价不得低于涨停价超过一个价位的一半


def board_pct(code, name):
    """旧口径：名称判 ST。"""
    is_st = "ST" in str(name).upper()
    if code.startswith(MAIN_PREFIX):
        return 0.05 if is_st else 0.10
    if code.startswith(GEM_PREFIX) or code.startswith(STAR_PREFIX):
        return 0.20
    if code.startswith(BJ_PREFIX):
        return 0.30
    return np.nan


def allowed_L(code):
    if code.startswith(MAIN_PREFIX):
        return (0.05, 0.10)
    if code.startswith(GEM_PREFIX) or code.startswith(STAR_PREFIX):
        return (0.20,)
    if code.startswith(BJ_PREFIX):
        return (0.30,)
    return ()


def nw_t(s, lag=5):
    """Newey-West HAC，(均值bp, t, IR, n)。"""
    x = pd.Series(s).dropna().to_numpy(float)
    n = len(x)
    if n < 30:
        return None
    m = x.mean()
    u = x - m
    g0 = (u @ u) / n
    var = g0
    for l in range(1, lag + 1):
        var += 2 * (1 - l / (lag + 1)) * ((u[l:] @ u[:-l]) / n)
    se = np.sqrt(max(var, 1e-18) / n)
    return (round(m * 1e4, 2), round(m / se, 2), round(m / np.sqrt(g0) * np.sqrt(242), 3), n)


def main() -> int:
    t0 = time.time()
    fs = sorted((DATA / "kline_daily").glob("date=*/part.parquet"))
    cols = ["symbol", "date", "open", "high", "low", "close", "amount"]
    df = pd.concat([pq.ParquetFile(f).read(columns=cols).to_pandas() for f in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    print(f"panel rows={len(df):,} sym={df.symbol.nunique()} {time.time()-t0:.0f}s", flush=True)

    ins = pq.ParquetFile(DATA / "instruments" / "instruments.parquet").read().to_pandas()
    nm = dict(zip(ins["symbol"], ins["name"]))
    ex = pd.read_parquet(DATA / "adj_factor" / "all.parquet")[["symbol", "trade_date", "ex_factor"]]
    ex["trade_date"] = pd.to_datetime(ex["trade_date"])

    g = df.groupby("symbol", sort=False)
    df["prev_raw"] = g["close"].shift(1)
    df["nxt_date"] = g["date"].shift(-1)
    df["nxt_open"] = g["open"].shift(-1)
    df["nxt_close"] = g["close"].shift(-1)
    df["nxt2_date"] = g["date"].shift(-2)
    df["nxt2_open"] = g["open"].shift(-2)

    def exmap(dates):
        v = ex.set_index(["symbol", "trade_date"])["ex_factor"].reindex(
            pd.MultiIndex.from_arrays([df["symbol"], dates])).to_numpy()
        return np.where(np.isfinite(v), v, 1.0)

    exT, ex1, ex2 = exmap(df["date"]), exmap(df["nxt_date"]), exmap(df["nxt2_date"])
    df["adj_prev"] = df["prev_raw"] / exT
    df["ret"] = df["close"] / df["adj_prev"] - 1.0

    ap = df["adj_prev"].to_numpy()
    hi = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    cl = df["close"].to_numpy()
    op = df["open"].to_numpy()
    code = df["symbol"].str.slice(0, 6)

    # 板块约束的档位识别：用最高价挑它落在哪个涨停价上
    n = len(df)
    best_d = np.full(n, np.inf)
    best_L = np.full(n, np.nan)
    best_exp = np.full(n, np.nan)
    for L in CAND_ALL:
        alw = np.array([L in allowed_L(c) for c in code], bool)
        e = np.round(ap * (1 + L), 2)
        d = np.abs(hi - e)
        upd = alw & (d < best_d)
        best_d[upd] = d[upd]
        best_L[upd] = L
        best_exp[upd] = e[upd]
    df["L"] = best_L
    df["lim_px"] = best_exp
    df["touch"] = np.isfinite(best_exp) & (best_d <= TICK) & (hi >= best_exp - REACH)
    df["lu_px"] = df["touch"] & (np.abs(cl - best_exp) <= TICK)
    df["fill_window"] = df["touch"] & ((df["lim_px"] - lo) > REACH)
    df["sealed"] = df["lu_px"] & ((cl - lo) <= REACH)

    # 旧口径：名称判 ST + 原始 prev_close（复现被复核对象）
    df["pct_name"] = np.array([board_pct(c, nm.get(s)) for c, s in zip(code, df["symbol"])], float)
    lp_old = np.round(df["prev_raw"].to_numpy() * (1 + df["pct_name"].to_numpy()), 2)
    df["lu_old"] = (np.abs(cl - lp_old) <= 0.005) & np.isfinite(df["pct_name"])

    gap1 = (df["nxt_date"] - df["date"]).dt.days
    gap2 = (df["nxt2_date"] - df["nxt_date"]).dt.days
    df["ok_win"] = (gap1 <= 5) & (gap2 <= 5)
    df["r0"] = df["nxt_open"] * ex1 / df["close"] - 1.0
    df["r1"] = df["nxt2_open"] * ex2 / df["nxt_open"] - 1.0
    df["r0_lim"] = df["nxt_open"] * ex1 / df["lim_px"] - 1.0
    df["r0_low"] = df["nxt_open"] * ex1 / df["low"] - 1.0

    # 连板高度（对收盘封住的涨停日计数）
    lu = df["lu_px"].to_numpy()
    sym = df["symbol"].to_numpy()
    cnt = np.zeros(len(df), int)
    run = 0
    for i in range(len(df)):
        if i and sym[i] == sym[i - 1] and lu[i]:
            run = run + 1 if lu[i - 1] else 1
        elif lu[i]:
            run = 1
        else:
            run = 0
        cnt[i] = run
    df["board_num"] = cnt

    filt = df["ok_win"] & df["nxt2_open"].notna()

    # ---------- A. 事件定义分歧 ----------
    ev_o = df[df["lu_old"] & filt]
    ev_p = df[df["lu_px"] & filt]
    o = set(zip(ev_o["symbol"], ev_o["date"]))
    p = set(zip(ev_p["symbol"], ev_p["date"]))
    ex_extra = ev_p[~ev_p["lu_old"]]
    div = {
        "n_old": len(ev_o), "n_px": len(ev_p),
        "n_extra_missed_by_old": len(p - o),
        "n_extra_rate": round(len(p - o) / max(len(p), 1), 4),
        "n_old_only": len(o - p),
        "n_old_only_rate": round(len(o - p) / max(len(o), 1), 4),
        "extra_by_L": {f"{k:.2f}": int(v) for k, v in ex_extra["L"].value_counts().sort_index().items()},
        "extra_name_has_ST": int(ex_extra["symbol"].map(lambda s: "ST" in str(nm.get(s)).upper()).sum()),
    }
    print("A 事件定义:", json.dumps(div, ensure_ascii=False), flush=True)

    # ---------- B. 主口径（价格口径 vs 旧口径），HAC ----------
    base = df[df["r1"].notna() & ~df["lu_px"] & df["ok_win"]]
    mkt_r0 = base.groupby("date")["r0"].mean()
    mkt_r1 = base.groupby("date")["r1"].mean()

    def blk(sub, label):
        if len(sub) < 50:
            return {"label": label, "n_ev": int(len(sub)), "insufficient": True}
        s0 = sub.groupby("date")["r0"].mean()
        s1 = sub.groupby("date")["r1"].mean()
        e0 = (s0 - mkt_r0).dropna()
        e1 = (s1 - mkt_r1).dropna()
        t0_, t1_ = nw_t(e0), nw_t(e1)
        return {
            "label": label, "n_ev": int(len(sub)), "n_days": int(len(s0)),
            "r0_abs_bp": round(float(sub["r0"].mean()) * 1e4, 2),
            "r0_ex_bp": t0_[0] if t0_ else None,
            "r0_ex_nw_t": t0_[1] if t0_ else None,
            "r1_abs_bp": round(float(sub["r1"].mean()) * 1e4, 2),
            "r1_ex_bp": t1_[0] if t1_ else None,
            "r1_ex_nw_t": t1_[1] if t1_ else None,
            "r1_ex_ir": t1_[2] if t1_ else None,
            "r1_median_pct": round(float(sub["r1"].median()) * 100, 2),
        }

    lu_p = df[df["lu_px"] & filt].copy()
    nxt_lu = df.set_index(["symbol", "date"])["lu_px"]
    lu_p["t1_at_limit"] = [
        bool(nxt_lu.get((s, d), False) and abs(x - y) <= TICK)
        for s, d, x, y in zip(lu_p["symbol"], lu_p["nxt_date"], lu_p["nxt_open"], lu_p["nxt_close"])
    ]

    main_out = {
        "px_all": blk(lu_p, "价格口径-全部"),
        "px_buyable": blk(lu_p[~lu_p["t1_at_limit"]], "价格口径-T+1可买"),
        "old_all": blk(df[df["lu_old"] & filt], "旧口径-全部"),
    }

    # ---------- C. 同涨幅对照（主板，按 T 日涨幅分箱） ----------
    c = df[filt].copy()
    c["main_board"] = c["symbol"].str.slice(0, 6).str.startswith(MAIN_PREFIX)
    edges = [-9, -0.02, 0.0, 0.02, 0.04, 0.06, 0.08, 0.095, 9]
    labels = ["<-2%", "-2~0", "0~2", "2~4", "4~6", "6~8", "8~9.5", ">=9.5"]
    ctrl = {}
    for lab, a, b in zip(labels, edges[:-1], edges[1:]):
        sel = c["main_board"] & (c["ret"] > a) & (c["ret"] <= b)
        if lab == ">=9.5":
            sel = c["main_board"] & c["lu_px"]
        ctrl[lab] = blk(c[sel], lab)

    # ---------- D. 连板高度 ----------
    board = {}
    hb = pd.cut(lu_p["board_num"], [0, 1, 2, 3, 99], labels=["首板", "2板", "3板", ">=4板"])
    for k, sub in lu_p.groupby(hb):
        board[str(k)] = blk(sub, str(k))

    # ---------- E. 档位分层 ----------
    by_L = {f"L={L:.2f}": blk(sub, f"L={L:.2f}") for L, sub in lu_p.groupby("L")}

    # ---------- F. 封板形态 × 连板高度 ----------
    cross = {}
    for seal, s1 in [("封死", lu_p[lu_p["sealed"]]), ("开过板", lu_p[~lu_p["sealed"]])]:
        for h, s2 in s1.groupby(hb):
            b = blk(s2, f"{seal}|{h}")
            b["share_of_all"] = round(len(s2) / len(lu_p), 4)
            cross[f"{seal}|{h}"] = b

    # ---------- G. 可执行总体：触及涨停（含炸板） ----------
    touch_out = {}
    for lab, sel in [("触及全部", df["touch"]),
                     ("有成交窗口", df["fill_window"]),
                     ("窗口|封住", df["fill_window"] & df["lu_px"]),
                     ("窗口|炸板", df["fill_window"] & ~df["lu_px"]),
                     ("触及|无窗口(一字)", df["touch"] & ~df["fill_window"])]:
        sub = df[sel & filt & df["nxt_open"].notna()]
        if len(sub) < 50:
            touch_out[lab] = {"n_ev": int(len(sub)), "insufficient": True}
            continue
        s0 = sub.groupby("date")["r0_lim"].mean()
        s1 = sub.groupby("date")["r1"].mean()
        g0 = sub.groupby("date")["r0_low"].mean()
        a0, a1 = nw_t(s0), nw_t(s1)
        touch_out[lab] = {
            "n_ev": int(len(sub)), "n_days": int(len(s0)),
            "r0_lim_bp": round(float(sub["r0_lim"].mean()) * 1e4, 2),
            "r0_lim_nw_t": a0[1] if a0 else None,
            "r0_low_bp": round(float(sub["r0_low"].mean()) * 1e4, 2),
            "r1_abs_bp": round(float(sub["r1"].mean()) * 1e4, 2),
            "r1_nw_t": a1[1] if a1 else None,
        }
    w = df["fill_window"] & filt
    nz = int((w & ~df["lu_px"]).sum())
    ncz = int((w & df["lu_px"]).sum())
    touch_out["炸板占可成交触及比"] = round(nz / max(nz + ncz, 1), 4)

    # ---------- H. 手算抽验 ----------
    chk = []
    for lab, sub in [("炸板", df[df["fill_window"] & ~df["lu_px"] & filt]),
                     ("封住", df[df["fill_window"] & df["lu_px"] & filt])]:
        for _, r in sub.sample(min(3, len(sub)), random_state=11).iterrows():
            chk.append({"grp": lab, "symbol": r["symbol"], "T": str(r["date"].date()),
                        "adj_prev": round(float(r["adj_prev"]), 3), "L": float(r["L"]),
                        "lim_px": round(float(r["lim_px"]), 2), "high": round(float(r["high"]), 2),
                        "low": round(float(r["low"]), 2), "close": round(float(r["close"]), 2),
                        "open_T1": round(float(r["nxt_open"]), 2),
                        "r0_lim_pct": round(float(r["r0_lim"]) * 100, 2),
                        "r0_recalc_pct": round(float(r["nxt_open"] / r["lim_px"] - 1) * 100, 2)})

    out = {"A_eventdef": div, "B_main": main_out, "C_control": ctrl, "D_board": board,
           "E_by_L": by_L, "F_cross": cross, "G_touch": touch_out, "H_sample": chk,
           "panel": {"rows": int(len(df)), "date_min": str(df.date.min().date()),
                     "date_max": str(df.date.max().date())},
           "defs": {"TICK": TICK, "REACH": REACH, "nw_lag": 5}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def show(dic, keys=("n_ev", "r0_ex_bp", "r0_ex_nw_t", "r1_ex_bp", "r1_ex_nw_t", "r1_median_pct")):
        for k, v in dic.items():
            if v.get("insufficient"):
                print(f"  {k:<14} n={v['n_ev']} 不足")
            else:
                print(f"  {k:<14} n={v['n_ev']:>6} r0ex={v['r0_ex_bp']:>7} "
                      f"(t={v['r0_ex_nw_t']:>6}) r1ex={v['r1_ex_bp']:>7} (t={v['r1_ex_nw_t']:>6}) "
                      f"med={v['r1_median_pct']}%")

    print("\nB 主口径")
    show(main_out)
    print("\nC 主板同涨幅对照")
    show(ctrl)
    print("\nD 连板高度")
    show(board)
    print("\nE 档位")
    show(by_L)
    print("\nF 封板 × 高度")
    show(cross)
    print("\nG 可执行总体")
    for k, v in touch_out.items():
        if isinstance(v, dict):
            print(f"  {k:<18} n={v.get('n_ev')} r0lim={v.get('r0_lim_bp')} "
                  f"(t={v.get('r0_lim_nw_t')}) r0low={v.get('r0_low_bp')} r1={v.get('r1_abs_bp')}")
        else:
            print(f"  {k} = {v}")
    print(f"\nelapsed {time.time()-t0:.0f}s -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
