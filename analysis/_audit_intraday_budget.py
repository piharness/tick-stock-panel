"""分钟级杠杆的「数据预算」判定：免费窗口能拒绝什么，买数据买到的是什么。

背景（B 段已定）：
  日线层级回封可预测（OOS AUC 0.668），但 EV = -75.7bp（t=-2.84），
  且赔率随筛选同向恶化 ⇒ 任何阈值都跨不过自己的平衡点。
  唯一未被否决的杠杆是分钟级：开板时长 / 封单撤单率 / 排队位置。

本脚本只回答一个问题，且要求它可红：
  为了把 EV 从 -75.7bp 推到 0，需要多少「交易日 × 事件」的数据？
  以及这个量能不能在现有数据源上免费拿到？

四段：
  A 事件包络     —— 免费窗口（源侧 40 交易日上限）内到底有多少个可成交达板事件
  B 本地覆盖     —— 已有分钟样本覆盖了多少个这类事件（覆盖率为 0 时任何标定都无意义）
  C 门槛分解     —— -75.7bp 到 0 的三条充分路径，以及在 T+1 下哪几条物理可达
  D 功效/MDE     —— 给定每日入选数 k 与两次筛的选择重叠率 rho，40 天能分辨多大的提升

可红测试（null calibration）：
  用同一选择率做随机筛选，实测「子集均值 − 总体均值」的日度序列 sd，
  必须与解析式 sd*sqrt(2(1-rho)/k) 在 ±25% 内一致；否则 MDE 表整体作废。

口径与 _audit_reseal_predict.py 逐位一致（TICK/REACH/allowed_L/ok_win/fill_window）。
输出：_audit_intraday_budget_result.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = Path(__file__).resolve().parent / "_audit_intraday_budget_result.json"
RESEAL = Path(__file__).resolve().parent / "_audit_reseal_result.json"

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
GEM_PREFIX = ("300", "301")
STAR_PREFIX = ("688", "689")
BJ_PREFIX = ("43", "83", "87", "88", "920")
CAND_ALL = (0.05, 0.10, 0.20, 0.30)
TICK = 0.011
REACH = 0.005

MINUTE_MAX_COUNT = 9600          # tdxquant 单标的 count 模式实测上限
MINUTE_BARS_PER_DAY = 240
FREE_TDAYS = MINUTE_MAX_COUNT // MINUTE_BARS_PER_DAY   # = 40
SETTINGS_CLAMP = 30              # settings.py: max(1, min(30, minute_sync_days))
COST_BP = 20.0
Z_POWER = 1.959964 + 0.841621    # 双侧 5% / 功效 80%
SEL_RATE = 0.0371                # B 段 pooled share_selected


def allowed_L(code: str):
    if code.startswith(MAIN_PREFIX):
        return (0.05, 0.10)
    if code.startswith(GEM_PREFIX) or code.startswith(STAR_PREFIX):
        return (0.20,)
    if code.startswith(BJ_PREFIX):
        return (0.30,)
    return ()


def nw_t(s, lag=5):
    x = pd.Series(s).dropna().to_numpy(float)
    n = len(x)
    if n < 30:
        return None
    m = x.mean()
    u = x - m
    var = (u @ u) / n
    for l in range(1, lag + 1):
        var += 2 * (1 - l / (lag + 1)) * ((u[l:] @ u[:-l]) / n)
    se = np.sqrt(max(var, 1e-18) / n)
    return round(m * 1e4, 2), round(m / se, 2), n


def build_universe():
    """与 B 段同一条代码路径构造可成交触及样本。"""
    fs = sorted((DATA / "kline_daily").glob("date=*/part.parquet"))
    cols = ["symbol", "date", "open", "high", "low", "close", "amount"]
    df = pd.concat([pq.ParquetFile(f).read(columns=cols).to_pandas() for f in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)

    ex = pd.read_parquet(DATA / "adj_factor" / "all.parquet")[["symbol", "trade_date", "ex_factor"]]
    ex["trade_date"] = pd.to_datetime(ex["trade_date"])

    g = df.groupby("symbol", sort=False)
    df["prev_raw"] = g["close"].shift(1)
    df["nxt_date"] = g["date"].shift(-1)
    df["nxt_open"] = g["open"].shift(-1)
    df["nxt2_date"] = g["date"].shift(-2)
    df["nxt2_open"] = g["open"].shift(-2)

    def exmap(dates):
        v = ex.set_index(["symbol", "trade_date"])["ex_factor"].reindex(
            pd.MultiIndex.from_arrays([df["symbol"], dates])).to_numpy()
        return np.where(np.isfinite(v), v, 1.0)

    exT, ex1 = exmap(df["date"]), exmap(df["nxt_date"])
    df["adj_prev"] = df["prev_raw"] / exT

    ap, hi, lo, cl = (df[c].to_numpy() for c in ("adj_prev", "high", "low", "close"))
    code = df["symbol"].str.slice(0, 6)
    n = len(df)
    best_d = np.full(n, np.inf)
    best_exp = np.full(n, np.nan)
    for L in CAND_ALL:
        alw = np.array([L in allowed_L(c) for c in code], bool)
        e = np.round(ap * (1 + L), 2)
        d = np.abs(hi - e)
        upd = alw & (d < best_d)
        best_d[upd] = d[upd]
        best_exp[upd] = e[upd]

    df["lim_px"] = best_exp
    df["touch"] = np.isfinite(best_exp) & (best_d <= TICK) & (hi >= best_exp - REACH)
    df["lu_px"] = df["touch"] & (np.abs(cl - best_exp) <= TICK)
    df["fill_window"] = df["touch"] & ((df["lim_px"] - lo) > REACH)
    gap1 = (df["nxt_date"] - df["date"]).dt.days
    gap2 = (df["nxt2_date"] - df["nxt_date"]).dt.days
    df["ok_win"] = (gap1 <= 5) & (gap2 <= 5)
    df["r0_lim"] = df["nxt_open"] * ex1 / df["lim_px"] - 1.0

    filt = df["ok_win"] & df["nxt2_open"].notna()
    u = df[filt & df["fill_window"] & df["nxt_open"].notna()].copy()
    u["y"] = u["lu_px"].astype(int)
    return df, u.reset_index(drop=True)


def minute_coverage(events: pd.DataFrame):
    """已有分钟样本（40 标的 × 21 天）覆盖了多少个事件。"""
    files = sorted((DATA / "kline_minute").glob("date=*/part.parquet"))
    if not files:
        return {"files": 0, "symbols": 0, "days": 0, "matched_events": 0, "note": "无分钟数据"}
    m = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    syms = set(m["symbol"].unique())
    days = set(pd.to_datetime(m["datetime"]).dt.normalize().unique())
    cov = events[events["symbol"].isin(syms) & events["date"].isin(days)]
    bars = m.groupby("symbol").size()
    per_day = m.groupby(pd.to_datetime(m["datetime"]).dt.normalize()).size()
    trange = m.groupby(pd.to_datetime(m["datetime"]).dt.normalize())["datetime"].agg(["min", "max"])
    truncated = trange[trange["max"].dt.strftime("%H:%M") < "14:55"]
    return {
        "files": len(files),
        "symbols": len(syms),
        "days": len(days),
        "bars_total": int(len(m)),
        "bars_per_symbol_median": int(bars.median()),
        "days_truncated_before_close": [str(d.date()) for d in truncated.index],
        "matched_events": int(len(cov)),
        "matched_event_symbols": sorted(cov["symbol"].unique().tolist()),
    }


def decompose(pooled: dict):
    """从当前 EV 推到 0（毛）/ +20bp（净）的三条充分路径。"""
    hit, seal, brk = pooled["hit"], pooled["seal_bp"], pooled["break_bp"]
    out = {"now": {"hit": hit, "seal_bp": seal, "break_bp": brk,
                   "ev_bp": pooled["ev_bp"], "nw_t": pooled["nw_t"]}}
    for tag, c in (("gross", 0.0), ("net20", COST_BP)):
        hit_need = (c - brk) / (seal - brk)
        brk_need = -(c + hit * seal) / (1 - hit)
        seal_need = (c - (1 - hit) * brk) / hit
        out[tag] = {
            "cost_bp": c,
            "A_hit": {"need_hit": round(hit_need, 4), "delta_pp": round((hit_need - hit) * 100, 2)},
            "B_break": {"need_break_bp": round(brk_need, 1), "delta_bp": round(brk_need - brk, 1)},
            "C_seal": {"need_seal_bp": round(seal_need, 1), "delta_bp": round(seal_need - seal, 1)},
        }
    out["t1_constraint"] = ("A 股 T+1：以涨停价成交后当日不可卖出 ⇒ B/C 不能在成交后取得；"
                            "只能通过「不成交」实现，而那是 A 的互补面")
    return out


def rho_crit(k: float, target: float, sd: float, T: int) -> float:
    """T*(rho)=T 的临界重叠率：高于它则免费窗口够用，低于它必须加天数。"""
    return 1 - (T * target ** 2 * k) / (Z_POWER ** 2 * 2 * sd ** 2)


def var_sel(k: float, nday: float, sd: float) -> float:
    """k 只子集均值与当日总体均值之差（同日配对）的方差 = sd^2*(1-k/n)/k。"""
    return sd ** 2 * max(1.0 - k / max(nday, 1e-9), 1e-9) / max(k, 1e-9)


def mde(k: float, rho: float, T: int, sd: float, nday: float) -> dict:
    """两次筛选（日线 vs 日线+分钟）在 T 天内的配对差分可分辨下限。

    近似口径：两个筛选各取 k 只、集合重叠率 rho ⇒ 差分方差 = 2(1-rho)·var_sel。
    """
    var_day = 2 * (1 - rho) * var_sel(k, nday, sd)
    se = np.sqrt(var_day / T)
    return {"se_bp": round(float(se), 2), "mde_bp": round(float(Z_POWER * se), 2)}


def t_star(k: float, rho: float, target: float, sd: float, nday: float) -> float:
    var_day = 2 * (1 - rho) * var_sel(k, nday, sd)
    return float((Z_POWER ** 2) * var_day / target ** 2)


def null_calibration(u: pd.DataFrame, sd_raw: float, k: int, seed: int = 20261110) -> dict:
    """随机筛选（无信息）下「子集均值 − 总体均值」的日度 sd，实测 vs 解析式。

    这是本脚本的可红测试：解析式偏了，整张 MDE 表作废。
    """
    rng = np.random.default_rng(seed)
    days = sorted(u["date"].unique())
    diff, nday = [], []
    for d in days:
        s = u.loc[u["date"] == d, "r0_lim"].to_numpy()
        if len(s) < k + 1:
            continue
        sel = rng.choice(len(s), size=k, replace=False)
        diff.append(s[sel].mean() - s.mean())
        nday.append(len(s))
    diff = np.asarray(diff)
    emp = float(diff.std(ddof=1) * 1e4)
    ana = float(np.sqrt(var_sel(k, float(np.mean(nday)), sd_raw)) * 1e4)
    lo, hi = 0.75 * ana, 1.25 * ana
    return {
        "k": k, "n_days": int(len(diff)),
        "emp_sd_bp": round(emp, 1), "analytic_sd_bp": round(ana, 1),
        "ratio": round(emp / ana, 3), "within_25pct": bool(lo <= emp <= hi),
    }


def rho_crit(k: float, target: float, sd: float, T: int, nday: float) -> float:
    """T*(rho)=T 的临界重叠率：高于它则免费窗口够用，低于它必须加天数。"""
    return 1 - (T * target ** 2) / (Z_POWER ** 2 * 2 * var_sel(k, nday, sd))


def main() -> int:
    t0 = time.time()
    df, u = build_universe()
    print(f"universe n={len(u):,} days={u.date.nunique()} hit={u.y.mean():.4f}", flush=True)

    days = np.array(sorted(u["date"].unique()))
    free_days = set(days[-FREE_TDAYS:])
    u_free = u[u["date"].isin(free_days)]

    per_day = u.groupby("date").size()
    sd_raw = float(u["r0_lim"].std())
    sd_trade = sd_raw * 1e4
    d_all = u.groupby("date")["r0_lim"].mean()
    d_free = u_free.groupby("date")["r0_lim"].mean()

    def arm(s):
        t = nw_t(s.groupby("date")["r0_lim"].mean())
        return {"n": int(len(s)), "n_days": int(s["date"].nunique()),
                "hit": round(float(s["y"].mean()), 4),
                "ev_bp": round(float(s["r0_lim"].mean()) * 1e4, 2),
                "nw_t": t[1] if t else None,
                "seal_bp": round(float(s.loc[s["y"] == 1, "r0_lim"].mean()) * 1e4, 1) if s["y"].any() else None,
                "break_bp": round(float(s.loc[s["y"] == 0, "r0_lim"].mean()) * 1e4, 1) if (s["y"] == 0).any() else None}

    envelope = {
        "full": arm(u),
        "free_window": dict(arm(u_free), **{
            "start": str(pd.Timestamp(min(free_days)).date()),
            "end": str(pd.Timestamp(max(free_days)).date()),
            "events_per_day_mean": round(float(u_free.groupby("date").size().mean()), 1),
            "events_per_day_min": int(u_free.groupby("date").size().min()),
            "events_per_day_max": int(u_free.groupby("date").size().max()),
            "distinct_symbols": int(u_free["symbol"].nunique()),
            "pull_requests": int(u_free["symbol"].nunique()),
            "pull_note": "单标的一次 count=9600 覆盖 40 交易日 ⇒ 请求数 = 标的数",
        }),
        "all": {"events_per_day_mean": round(float(per_day.mean()), 1),
                "median": int(per_day.median()), "min": int(per_day.min()), "max": int(per_day.max())},
        "sd_trade_bp": round(sd_trade, 1),
        "sd_daily_mean_bp_full": round(float(d_all.std()) * 1e4, 1),
        "sd_daily_mean_bp_free": round(float(d_free.std()) * 1e4, 1),
        "free_tdays": FREE_TDAYS,
        "settings_clamp_tdays": SETTINGS_CLAMP,
    }

    pooled = json.loads(RESEAL.read_text(encoding="utf-8"))["pooled"]
    dec = decompose(pooled)

    ks = [round(float(per_day.mean()) * SEL_RATE, 1), 5, 11, 25]
    ks = sorted(set(ks))
    rhos = [0.0, 0.5, 0.8, 0.95]
    nday = float(u_free.groupby("date").size().mean())
    target = -pooled["ev_bp"] + COST_BP     # 从 -75.7 推到 净 +20bp
    power_grid = {}
    for k in ks:
        power_grid[f"k={k}"] = {
            f"rho={r}": dict(mde(k, r, FREE_TDAYS, sd_trade, nday),
                             t_star_days=round(t_star(k, r, target, sd_trade, nday), 1))
            for r in rhos
        }

    calib = [null_calibration(u_free, sd_raw, k=int(max(2, round(float(u_free.groupby('date').size().mean()) * SEL_RATE))))]
    cover = minute_coverage(u)

    # 容量/价值：全仓轮动时 年毛 = EV × 交易日数（与 k 无关）
    value = {
        "trades_per_day_model_sel": round(float(pooled["n"]) / pooled["n_days"], 2),
        "annual_gross_pct_at_ev": {f"{e}bp": round(e * 242 / 100, 2) for e in (10, 20, 50, 100)},
        "note": "EV 是单日持仓口径 ⇒ 每日全仓轮动的年毛收益 = EV×242，与每日几只无关；"
                "故决策是纯符号判定，不是幅度判定。",
    }

    verdict = {
        "target_shift_bp": round(target, 1),
        "provider_ceiling_tdays": FREE_TDAYS,
        "settings_clamp_tdays": SETTINGS_CLAMP,
        "margin": {
            f"k={k}": {"rho_crit": round(rho_crit(k, target, sd_trade, FREE_TDAYS, nday), 3),
                       "t_star_rho0": round(t_star(k, 0.0, target, sd_trade, nday), 1),
                       "t_star_rho50": round(t_star(k, 0.5, target, sd_trade, nday), 1),
                       "t_star_rho80": round(t_star(k, 0.8, target, sd_trade, nday), 1)}
            for k in ks
        },
        "calibration_ok": all(c["within_25pct"] for c in calib),
        "verdict_note": ("免费窗口（40 交易日）够不够用，取决于分钟筛与日线筛的选择重叠率 rho；"
                         "rho 只能在免费窗口上测出来 ⇒ 先跑免费 40 天，再决定买多少天"),
    }

    out = {
        "meta": {"generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "elapsed_s": round(time.time() - t0, 1),
                 "defs": {"TICK": TICK, "REACH": REACH, "universe": "touch & fill_window & ok_win",
                          "buy_px": "limit_up", "sell": "T+1 open", "cost_bp": COST_BP,
                          "sel_rate": SEL_RATE, "z_power": round(Z_POWER, 4)}},
        "envelope": envelope,
        "minute_coverage": cover,
        "decompose": dec,
        "power": {"ks": ks, "rhos": rhos, "T": FREE_TDAYS, "sd_trade_bp": round(sd_trade, 1),
                  "target_bp": round(target, 1), "grid": power_grid, "calibration": calib},
        "value": value,
        "verdict": verdict,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"envelope": envelope["free_window"], "coverage": cover["matched_events"],
                      "decompose_net20": dec["net20"]["A_hit"], "verdict": verdict,
                      "calib": calib, "power": power_grid["k=5"]}, ensure_ascii=False, indent=1), flush=True)
    print(f"written {OUT.name} in {time.time()-t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
