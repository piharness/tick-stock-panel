# -*- coding: utf-8 -*-
"""达板事件基线：全市场日线口径，零成本本地计算。

判据见 analysis/_prereg_baseline_limitup.md（跑前提交，跑后不得改动）。

设计要点：
1. 事件源 = 全市场日线算涨停，不用龙虎榜（龙虎榜是异动榜，欠采样且带选择性偏差）。
2. 涨停价按板块 + ST 状态精确计算，不靠"涨幅接近 10%"近似。
3. kline_daily 是未复权原始价，跨除权日用 adj_factor 的 ex_factor 还原。
4. 三个口径同时报，因为"打板"和"接力"是两个不同策略：
   R0 = 开盘(T+1)/收盘(T)-1   打板自身（T 日涨停价买入，隔夜卖出）——假设能排到队，是乐观上界
   R1 = 开盘(T+2)/开盘(T+1)-1 接力（T+1 开盘买，持有 1 日）——本次主口径
   R2 = 收盘(T+1)/开盘(T+1)-1 日内（T+1 开盘买，当日收盘卖）
5. 可成交性单独标记：T+1 开盘即涨停（一字/开盘板）视为买不进。
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
OUT_JSON = Path(__file__).resolve().parent / "_baseline_limitup_result.json"

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
GEM_PREFIX = ("300", "301")
STAR_PREFIX = ("688", "689")
BJ_PREFIX = ("43", "83", "87", "88", "920")
COST_BP = 15.0


def board_pct(code: str, name: str) -> float:
    is_st = "ST" in str(name).upper()
    if code.startswith(MAIN_PREFIX):
        return 0.05 if is_st else 0.10
    if code.startswith(GEM_PREFIX) or code.startswith(STAR_PREFIX):
        return 0.20
    if code.startswith(BJ_PREFIX):
        return 0.30
    return np.nan


def board_name(code: str) -> str:
    if code.startswith(MAIN_PREFIX):
        return "main"
    if code.startswith(GEM_PREFIX):
        return "gem"
    if code.startswith(STAR_PREFIX):
        return "star"
    if code.startswith(BJ_PREFIX):
        return "bj"
    return "unknown"


def load_panel() -> pd.DataFrame:
    fs = sorted((DATA / "kline_daily").glob("date=*/part.parquet"))
    df = pd.concat([pq.ParquetFile(f).read().to_pandas() for f in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)


def load_names() -> dict:
    ins = pq.ParquetFile(DATA / "instruments" / "instruments.parquet").read().to_pandas()
    return dict(zip(ins["symbol"], ins["name"]))


def load_ex_events() -> pd.DataFrame:
    df = pq.ParquetFile(DATA / "adj_factor" / "all.parquet").read().to_pandas()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df[["symbol", "trade_date", "ex_factor"]].copy()


def stats(d_ev: pd.Series, d_mkt: pd.Series, label: str) -> dict:
    d = d_ev.to_frame("ev").join(d_mkt.rename("mkt"), how="inner").dropna()
    n = len(d)
    if n < 20:
        return {"label": label, "n_days": int(n), "insufficient": True}
    ex = d["ev"] - d["mkt"]

    def one(s):
        m, sd = float(s.mean()), float(s.std(ddof=1))
        return (round(m * 1e4, 2), round(m / sd * np.sqrt(n), 2), round(m / sd * np.sqrt(242), 3))

    ev_m, ev_t, ev_ir = one(d["ev"])
    ex_m, ex_t, ex_ir = one(ex)
    return {
        "label": label, "n_days": int(n),
        "abs_bp": ev_m, "abs_t": ev_t, "abs_ir": ev_ir,
        "ex_bp": ex_m, "ex_t": ex_t, "ex_ir": ex_ir,
        "ex_winrate": round(float((ex > 0).mean()), 4),
        "net_ex_bp": round(ex_m - COST_BP, 2),
    }


def main() -> int:
    t0 = time.time()
    out = {}

    def log(m):
        print(m, flush=True)

    log("[1/6] 读日线全量 ...")
    df = load_panel()
    log(f"      rows={len(df):,} symbols={df.symbol.nunique()} "
        f"{df.date.min().date()}->{df.date.max().date()} {time.time()-t0:.1f}s")

    names = load_names()
    ex = load_ex_events()
    ex_key = ex.set_index(["symbol", "trade_date"])["ex_factor"]

    code = df["symbol"].str.slice(0, 6)
    name_map = df["symbol"].map(names)
    df["pct"] = np.array([board_pct(c, n) for c, n in zip(code, name_map)], float)
    df["is_st"] = np.array(["ST" in str(n).upper() for n in name_map])
    df["board"] = np.array([board_name(c) for c in code])

    log("[2/6] 前向/后向字段 ...")
    g = df.groupby("symbol", sort=False)
    for src, dst in [("close", "prev_close"), ("date", "nxt_date"), ("open", "nxt_open"),
                     ("close", "nxt_close"), ("high", "nxt_high"), ("low", "nxt_low"),
                     ("date", "nxt2_date"), ("open", "nxt2_open")]:
        df[dst] = g[src].shift(1 if dst in ("prev_close",) else -1)
    # nxt2_* 需要再 shift 一次
    df["nxt2_date"] = g["date"].shift(-2)
    df["nxt2_open"] = g["open"].shift(-2)

    lp = np.round(df["prev_close"].to_numpy() * (1 + df["pct"].to_numpy()), 2)
    df["is_limit_up"] = df["pct"].notna().to_numpy() & (np.abs(df["close"].to_numpy() - lp) <= 0.005)
    # 全天封死：当日最低价即涨停价 => 盘中无低于涨停价的成交，尾盘挂单几乎排不到
    df["sealed_all_day"] = df["is_limit_up"] & (df["low"].to_numpy() >= lp - 0.005)

    # T+1 涨停价：若 T+1 为除权日，基准价是 close/ex_factor
    ex1 = ex_key.reindex(pd.MultiIndex.from_arrays([df["symbol"], df["nxt_date"]])).to_numpy()
    df["ex1"] = np.where(np.isfinite(ex1), ex1, 1.0)
    lp1 = np.round(df["close"].to_numpy() / df["ex1"].to_numpy() * (1 + df["pct"].to_numpy()), 2)
    df["t1_open_at_limit"] = df["nxt_open"].to_numpy() >= (lp1 - 0.005)

    ex2 = ex_key.reindex(pd.MultiIndex.from_arrays([df["symbol"], df["nxt2_date"]])).to_numpy()
    df["ex2"] = np.where(np.isfinite(ex2), ex2, 1.0)

    gap1 = (df["nxt_date"] - df["date"]).dt.days
    gap2 = (df["nxt2_date"] - df["nxt_date"]).dt.days
    df["contiguous"] = (gap1 <= 5) & (gap2 <= 5)

    log("[3/6] 三口径收益（复权）...")
    df["r0"] = (df["nxt_open"] / df["close"]) * df["ex1"] - 1.0
    df["r1"] = (df["nxt2_open"] / df["nxt_open"]) * df["ex2"] - 1.0
    df["r2"] = (df["nxt_close"] / df["nxt_open"]) - 1.0
    df["gap"] = (df["nxt_open"] * df["ex1"] / df["close"]) - 1.0

    log("[4/6] 诊断：全市场涨停 vs 龙虎榜 ...")
    d0 = pd.Timestamp("2025-11-13")
    lu_all = set(df.loc[(df["date"] == d0) & df["is_limit_up"], "symbol"].str.slice(0, 6))
    dth = json.loads((DATA / "dragon_tiger" / "date=2025-11-13.json").read_text(encoding="utf-8"))
    items = dth["all"]["stock_items"]
    dt_tick = {it["ticker"] for it in items}
    dt_lu = {it["ticker"] for it in items
             if np.isfinite(board_pct(it["ticker"], it.get("name", "")))
             and abs(it.get("change", 0) - board_pct(it["ticker"], it.get("name", ""))) <= 0.004}
    diagnostics = {
        "date": "2025-11-13",
        "market_limit_up": len(lu_all),
        "dt_rows": len(items),
        "dt_unique": len(dt_tick),
        "dt_limit_up_by_change": len(dt_lu),
        "recall": round(len(lu_all & dt_tick) / max(len(lu_all), 1), 4),
        "precision": round(len(dt_lu & lu_all) / max(len(dt_lu), 1), 4),
        "dt_limit_up_missed_by_kline": sorted(dt_lu - lu_all)[:10],
    }
    log("      " + json.dumps(diagnostics, ensure_ascii=False))

    log("[5/6] 聚合 ...")
    base = df[df["r1"].notna() & ~df["is_limit_up"] & df["contiguous"]]
    mkt = {c: base.groupby("date")[c].mean() for c in ("r0", "r1", "r2")}

    ev = df[df["is_limit_up"] & df["contiguous"] & df["r1"].notna()].copy()
    ev = ev[ev["nxt_date"].notna() & ev["nxt2_date"].notna()]
    log(f"      events={len(ev):,} days={ev.date.nunique()} "
        f"unbuyable_share={ev['t1_open_at_limit'].mean():.3f}")

    groups = {
        "all": ev,
        "buyable": ev[~ev["t1_open_at_limit"]],
        "unbuyable": ev[ev["t1_open_at_limit"]],
    }
    agg_out = {}
    for gname, sub in groups.items():
        agg_out[gname] = {
            "n_events": int(len(sub)),
            "gap_mean_pct": round(float(sub["gap"].mean()) * 100, 3),
            "gap_median_pct": round(float(sub["gap"].median()) * 100, 3),
            "r0": stats(sub.groupby("date")["r0"].mean(), mkt["r0"], f"{gname}-R0打板隔夜"),
            "r1": stats(sub.groupby("date")["r1"].mean(), mkt["r1"], f"{gname}-R1接力"),
            "r2": stats(sub.groupby("date")["r2"].mean(), mkt["r2"], f"{gname}-R2日内"),
        }

    # 分层稳健性（只对 R1）
    layers = {}
    ev["seal"] = np.where(ev["sealed_all_day"], "全天封死", "当日开过板")
    ev["quarter"] = ev["date"].dt.to_period("Q").astype(str)
    for key, col in [("board", "board"), ("is_st", "is_st"), ("quarter", "quarter"), ("seal", "seal")]:
        d = {}
        for k, sub in ev.groupby(col):
            s1 = stats(sub.groupby("date")["r1"].mean(), mkt["r1"], f"{key}={k}")
            s0 = stats(sub.groupby("date")["r0"].mean(), mkt["r0"], f"{key}={k}-r0")
            d[str(k)] = {"n_days": s1.get("n_days"), "n_ev": int(len(sub)),
                         "r1_ex": s1.get("ex_bp"), "r1_t": s1.get("ex_t"), "r1_ir": s1.get("ex_ir"),
                         "r0_ex": s0.get("ex_bp"), "r0_t": s0.get("ex_t"), "r0_ir": s0.get("ex_ir")}
        layers[key] = d

    # 交叉分层：T 日封板形态 × T+1 可成交性（决定性的一格）
    ev["cell"] = (ev["seal"] + "|" + np.where(ev["t1_open_at_limit"], "T+1开盘涨停", "T+1可买"))
    layers["seal_x_tradable"] = {}
    for k, sub in ev.groupby("cell"):
        s0 = stats(sub.groupby("date")["r0"].mean(), mkt["r0"], k)
        s1 = stats(sub.groupby("date")["r1"].mean(), mkt["r1"], k)
        layers["seal_x_tradable"][str(k)] = {
            "n_ev": int(len(sub)), "n_days": s1.get("n_days"),
            "r0_ex": s0.get("ex_bp"), "r0_t": s0.get("ex_t"),
            "r1_ex": s1.get("ex_bp"), "r1_t": s1.get("ex_t"), "r1_ir": s1.get("ex_ir"),
            "r1_median_pct": round(float(sub["r1"].median()) * 100, 2),
            "r1_winrate": round(float((sub["r1"] > 0).mean()), 4),
        }

    # 流动性分层（按 T 日成交额）
    ev["amt_bucket"] = pd.qcut(ev["amount"], 4, labels=["Q1低", "Q2", "Q3", "Q4高"], duplicates="drop")
    layers["liquidity"] = {}
    for k, sub in ev.groupby("amt_bucket", observed=True):
        s1 = stats(sub.groupby("date")["r1"].mean(), mkt["r1"], f"amt={k}")
        s0 = stats(sub.groupby("date")["r0"].mean(), mkt["r0"], f"amt={k}-r0")
        layers["liquidity"][str(k)] = {"n_days": s1.get("n_days"), "n_ev": int(len(sub)),
                                       "r1_ex": s1.get("ex_bp"), "r1_t": s1.get("ex_t"),
                                       "r1_ir": s1.get("ex_ir"),
                                       "r0_ex": s0.get("ex_bp"), "r0_t": s0.get("ex_t"),
                                       "r0_ir": s0.get("ex_ir")}

    # 抽样明细（人工可核）
    samp = ev.sample(min(12, len(ev)), random_state=20261110)[
        ["symbol", "date", "prev_close", "close", "nxt_date", "nxt_open", "nxt_close",
         "nxt2_date", "nxt2_open", "gap", "r0", "r1", "r2", "t1_open_at_limit"]]
    samp_out = []
    for _, r in samp.iterrows():
        samp_out.append({
            "symbol": r["symbol"], "T": str(r["date"].date()),
            "prev_close": round(float(r["prev_close"]), 2),
            "close_T": round(float(r["close"]), 2),
            "open_T1": round(float(r["nxt_open"]), 2),
            "close_T1": round(float(r["nxt_close"]), 2),
            "open_T2": round(float(r["nxt2_open"]), 2),
            "gap_pct": round(float(r["gap"]) * 100, 2),
            "r0_pct": round(float(r["r0"]) * 100, 2),
            "r1_pct": round(float(r["r1"]) * 100, 2),
            "r2_pct": round(float(r["r2"]) * 100, 2),
            "unbuyable": bool(r["t1_open_at_limit"]),
        })

    out = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "panel": {"rows": int(len(df)), "symbols": int(df.symbol.nunique()),
                  "date_min": str(df.date.min().date()), "date_max": str(df.date.max().date())},
        "diagnostics": diagnostics,
        "aggregates": agg_out,
        "per_trade": {g: {
            "r0_median_pct": round(float(s["r0"].median()) * 100, 2),
            "r0_winrate": round(float((s["r0"] > 0).mean()), 4),
            "r1_median_pct": round(float(s["r1"].median()) * 100, 2),
            "r1_winrate": round(float((s["r1"] > 0).mean()), 4),
        } for g, s in groups.items()},
        "layers": layers,
        "sample": samp_out,
        "defs": {"r0": "open(T+1)/close(T)-1 复权", "r1": "open(T+2)/open(T+1)-1 复权",
                 "r2": "close(T+1)/open(T+1)-1", "cost_bp": COST_BP},
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log("[6/6] -> " + str(OUT_JSON))

    for g, v in agg_out.items():
        log(f"  [{g}] n={v['n_events']} gap中位={v['gap_median_pct']}%")
        for r in ("r0", "r1", "r2"):
            s = v[r]
            if s.get("insufficient"):
                log(f"    {r}: n_days={s['n_days']} 样本不足")
            else:
                log(f"    {r}: abs={s['abs_bp']}bp(t={s['abs_t']}) "
                    f"ex={s['ex_bp']}bp(t={s['ex_t']},IR={s['ex_ir']}) "
                    f"net={s['net_ex_bp']}bp win={s['ex_winrate']}")
    log(f"  elapsed {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
