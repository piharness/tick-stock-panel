"""买点之后：持有期限、退出规则、事件簇预算 —— 三个先注册再跑的测量。

背景：120 日回放只给了 6 个行业、1 个事件簇（11/13–11/20），任何「几日后收益」都
不能从那里推。本脚本把同一条判定（`etf_watch.scan`）在**全部历史**上逐日重跑，
再对同一批事件做三组测量：

- EXP-01 持有期限：H = 3/5/10/20/40/60，收益、相对、胜率、p10、MAE/MFE。
- EXP-02 退出规则：固定持有 / 候选失效 / 位置修复 / 止盈止损，全部「收盘知道、次日收盘执行」。
- EXP-03 事件簇：同一天多行业触发按**一笔预算**算，看组合收益与相关性。

三条纪律（和前两份脚本一致，这里再写一遍）：
1. 判定只调 `etf_watch.scan`，不复制 F1/F2/F3/F6；本脚本只做测量。
2. 事件按「首次进入候选 + 同行业 60 交易日冷却」定义；t 值按**自然月**聚簇，
   因为同月多行业是同一波行情（研究段 51 个事件只对应 26 个独立月）。
3. 收益是事后评价：先定 status，再读之后的价格；任何收益列都不回写判定。

用法：
    python analysis/etf_watch_experiments.py              # 有缓存则直接用
    python analysis/etf_watch_experiments.py --rebuild    # 强制重扫全历史（约 3 分钟）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402  （判定唯一产地）
import _diag_industry_etf_asym as core  # noqa: E402

OUT = ROOT / "data/etf_watch/experiments.json"
CACHE = ROOT / "data/etf_watch/_scan_matrix.parquet"

HORIZONS = (3, 5, 10, 20, 40, 60)
COOLDOWN = 60          # 同行业冷却，与 etf_watch.COOLDOWN 一致
MAX_HOLD = 60          # 退出实验的最长持有
CLUSTER_GAP = 5        # 事件簇允许的间隔（交易日内无候选则断开）
EXIT_TP, EXIT_SL = 0.08, -0.05
EXIT_DD = -0.20        # 「位置修复」：dd 回到 −20% 以上


# ---------------------------------------------------------------- 逐日扫描
def scan_history(d: pd.DataFrame, idx: pd.DataFrame, rebuild: bool = False) -> pd.DataFrame:
    """全历史逐日 scan → 长表 (date, industry, status, dd, vol_pct, best_symbol, amt20)。

    缓存只按「行情覆盖到哪一天」失效：缓存末日 < 行情末日就重建。
    2026-11-23 的事故：缓存止于 11-20、行情已到 11-23，账户照旧读缓存，
    却把 asof 写成 11-23 —— 于是同一份快照里，日期是新的、信号是旧的。
    """
    end = pd.Timestamp(d["date"].max())
    if CACHE.exists() and not rebuild:
        m = pd.read_parquet(CACHE)
        m["date"] = pd.to_datetime(m["date"])
        if m["date"].max() >= end:
            print(f"[cache] {CACHE.relative_to(ROOT)}  {len(m)} 行 "
                  f"{m.date.min().date()}→{m.date.max().date()}")
            return m
        print(f"[stale] 缓存止于 {m.date.max().date()}，行情已到 {end.date()}：重建逐日扫描（约 3 分钟）")

    ctx = etf_watch.build_context(d, idx, sessions=10 ** 6)
    days = [t for t in ctx.sessions if t in ctx.regime.index]
    print(f"逐日扫描 {len(days)} 个交易日（{days[0].date()} → {days[-1].date()}）…")
    rows: list[dict] = []
    for n, day in enumerate(days, 1):
        payload, _ = etf_watch.scan(day, d, idx, [], ctx=ctx)
        state = payload["market"]["state"]
        for x in payload["industries"]:
            rows.append({
                "date": day, "industry": x["industry"], "status": x["status"],
                "dd": x["dd"], "vol_pct": x["vol_pct"],
                "best_symbol": x["best_symbol"], "amt20": x["amt20"],
                "state": state,
            })
        if n % 300 == 0:
            print(f"  {n}/{len(days)}  {day.date()}")
    m = pd.DataFrame(rows)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    m.to_parquet(CACHE, index=False)
    print(f"[done] 写入 {CACHE.relative_to(ROOT)}  {len(m)} 行")
    return m


def daily_matrices(m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """长表 → 日期 × 行业矩阵（status / dd / best_symbol）。"""
    status = m.pivot(index="date", columns="industry", values="status").sort_index()
    dd = m.pivot(index="date", columns="industry", values="dd").sort_index()
    sym = m.pivot(index="date", columns="industry", values="best_symbol").sort_index()
    return {"status": status, "dd": dd, "symbol": sym}


# ---------------------------------------------------------------- 事件
def build_events(status: pd.DataFrame, dd: pd.DataFrame) -> pd.DataFrame:
    """候选段的第一个交易日 = 事件；同行业 60 交易日冷却（与 V1 一致）。"""
    cand = status.eq("CANDIDATE")
    enter = etf_watch.run_starts(cand)
    sessions = list(status.index)
    pos = {t: i for i, t in enumerate(sessions)}
    out: list[dict] = []
    for ind in cand.columns:
        last = -10 ** 9
        for t in status.index[enter[ind].fillna(False)]:
            i = pos[t]
            if i - last < COOLDOWN:
                continue
            last = i
            out.append({"date": t, "industry": ind, "i": i,
                        "dd": float(dd.at[t, ind]) if np.isfinite(dd.at[t, ind]) else np.nan})
    ev = pd.DataFrame(out).sort_values(["date", "industry"]).reset_index(drop=True)
    ev["ym"] = ev.date.dt.to_period("M").astype(str)
    return ev


# ---------------------------------------------------------------- 价格
def price_frame(d: pd.DataFrame, symbols: set[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    sub = d[d["symbol"].isin(symbols)]
    for sym, grp in sub.groupby("symbol", sort=False):
        f = grp.set_index("date")[["open", "close", "close_adj", "hi_adj", "lo_adj"]].sort_index()
        f = f[~f.index.duplicated(keep="last")]
        k = f["close_adj"] / f["close"].where(f["close"] > 0)
        f["open_adj"] = f["open"] * k
        out[str(sym)] = f
    return out


def _carrier_of(ev_row, sym_mat: pd.DataFrame) -> str | None:
    try:
        s = sym_mat.at[ev_row["date"], ev_row["industry"]]
    except KeyError:
        return None
    return str(s) if isinstance(s, str) else None


def _path(px: pd.DataFrame, sessions: list[pd.Timestamp], entry_i: int, hold: int):
    """进场日（含）之后 hold 个交易日的收盘/盘中序列；缺价返回 None。"""
    end_i = min(entry_i + hold, len(sessions) - 1)
    w = px.reindex(sessions[entry_i:end_i + 1])
    if w["close_adj"].isna().all():
        return None
    return w


# ---------------------------------------------------------------- EXP-01
def exp_horizons(ev: pd.DataFrame, sessions, prices, sym_mat, nav) -> dict:
    base = nav.mean(axis=1)
    recs: list[dict] = []
    for _, r in ev.iterrows():
        sym = _carrier_of(r, sym_mat)
        px = prices.get(sym) if sym else None
        i = int(r["i"])
        if px is None or i + 1 >= len(sessions):
            continue
        entry = sessions[i + 1]
        if entry not in px.index or not np.isfinite(px.at[entry, "close_adj"]):
            continue
        e0 = float(px.at[entry, "close_adj"])
        b0 = float(base.get(entry, np.nan))
        if not np.isfinite(b0):
            continue
        for h in HORIZONS:
            j = i + 1 + h
            if j >= len(sessions):
                continue
            exit_ = sessions[j]
            if exit_ not in px.index or not np.isfinite(px.at[exit_, "close_adj"]):
                continue
            r_abs = float(px.at[exit_, "close_adj"]) / e0 - 1
            r_base = float(base.get(exit_, np.nan)) / b0 - 1
            w = px.reindex(sessions[i + 2:j + 1])
            lo = w["lo_adj"].min() if len(w) else np.nan
            hi = w["hi_adj"].max() if len(w) else np.nan
            recs.append({
                "date": r["date"], "ym": r["ym"], "industry": r["industry"], "h": h,
                "ret": r_abs, "base": r_base, "rel": r_abs - r_base,
                "mae": (float(lo) / e0 - 1) if np.isfinite(lo) else np.nan,
                "mfe": (float(hi) / e0 - 1) if np.isfinite(hi) else np.nan,
                "dd": r["dd"],
            })
    x = pd.DataFrame(recs)
    if x.empty:
        return {"note": "无可用事件"}
    out = {"events": int(x[["date", "industry"]].drop_duplicates().shape[0]),
           "months": int(x.ym.nunique()), "horizons": {}}
    for h in HORIZONS:
        g = x[x.h == h]
        if g.empty:
            continue
        mo = g.groupby("ym")[["ret", "rel", "base"]].mean()
        monthly = mo["rel"]
        t_month = (monthly.mean() / (monthly.std(ddof=1) / np.sqrt(len(monthly)))
                   if len(monthly) > 6 and monthly.std(ddof=1) > 0 else None)
        # beta 校正：高波篮子天然在反弹里跑赢，rel 不等于 alpha。用月均做 OLS，别用事件数做 N。
        beta = alpha = t_alpha = None
        if len(mo) > 6 and mo["base"].std(ddof=1) > 0:
            beta = float(np.polyfit(mo["base"], mo["ret"], 1)[0])
            alpha_s = mo["ret"] - beta * mo["base"]
            alpha = float(alpha_s.mean())
            t_alpha = float(alpha / (alpha_s.std(ddof=1) / np.sqrt(len(alpha_s))))
        out["horizons"][f"h{h}"] = {
            "n": int(len(g)),
            "months": int(monthly.shape[0]),
            "ev_bp": round(float(g.ret.mean()) * 1e4, 1),
            "ev_bp_month": round(float(mo["ret"].mean()) * 1e4, 1),
            "med_bp": round(float(g.ret.median()) * 1e4, 1),
            "med_bp_month": round(float(mo["ret"].median()) * 1e4, 1),
            "rel_bp": round(float(g.rel.mean()) * 1e4, 1),
            "rel_bp_month": round(float(mo["rel"].mean()) * 1e4, 1),
            "base_bp_month": round(float(mo["base"].mean()) * 1e4, 1),
            "beta": round(beta, 2) if beta is not None else None,
            "alpha_bp": round(alpha * 1e4, 1) if alpha is not None else None,
            "t_alpha": round(t_alpha, 2) if t_alpha is not None else None,
            "t_month": round(float(t_month), 2) if t_month is not None else None,
            "win": round(float((g.ret > 0).mean()) * 100, 1),
            "rel_win": round(float((g.rel > 0).mean()) * 100, 1),
            "p10_bp": round(float(np.nanpercentile(g.ret, 10)) * 1e4, 1),
            "mae_med_bp": round(float(np.nanmedian(g.mae)) * 1e4, 1),
            "mfe_med_bp": round(float(np.nanmedian(g.mfe)) * 1e4, 1),
        }
    x["year"] = pd.to_datetime(x.date).dt.year
    out["by_year"] = [
        {"year": int(y), "n": int(len(g)),
         "ev_bp": round(float(g.ret.mean()) * 1e4, 1),
         "rel_bp": round(float(g.rel.mean()) * 1e4, 1),
         "win": round(float((g.ret > 0).mean()) * 100, 1)}
        for y, g in x[x.h == 20].groupby("year") if len(g) >= 8
    ]
    return out


# ---------------------------------------------------------------- EXP-02
def _hold_return(px: pd.DataFrame, sessions, entry_i: int, exit_i: int) -> float | None:
    a, b = sessions[entry_i], sessions[exit_i]
    if a not in px.index or b not in px.index:
        return None
    pa, pb = float(px.at[a, "close_adj"]), float(px.at[b, "close_adj"])
    if not (np.isfinite(pa) and np.isfinite(pb)):
        return None
    return pb / pa - 1


def exp_exits(ev: pd.DataFrame, status, dd, sessions, prices, sym_mat) -> dict:
    """四种退出，全部「收盘观察到、次日收盘执行」，最长 60 日。

    - hold5/10/20：基线
    - status：某日 status 不再是 CANDIDATE ⇒ 次日收盘走
    - recover：某日 dd > −20%（位置修复）⇒ 次日收盘走
    - stop：某日收盘收益 ≥ +8% 或 ≤ −5% ⇒ 次日收盘走
    """
    cand = status.eq("CANDIDATE")
    rows: list[dict] = []
    for _, r in ev.iterrows():
        sym = _carrier_of(r, sym_mat)
        px = prices.get(sym) if sym else None
        i = int(r["i"])
        if px is None or i + 1 >= len(sessions):
            continue
        entry_i = i + 1
        if sessions[entry_i] not in px.index:
            continue
        e0 = float(px.at[sessions[entry_i], "close_adj"])
        if not np.isfinite(e0):
            continue
        ind = r["industry"]
        last_i = min(entry_i + MAX_HOLD, len(sessions) - 1)
        px_path = px.reindex(sessions[entry_i:last_i + 1])

        def walk(rule: str) -> tuple[int, str]:
            for k in range(1, last_i - entry_i + 1):
                obs_i = entry_i + k                  # 观察日：这天的收盘才知道信号
                act_i = min(obs_i + 1, last_i)       # 执行日：次日收盘
                obs = sessions[obs_i]
                if rule == "status" and not bool(cand.at[obs, ind]):
                    return act_i, "status"
                if rule == "recover" and np.isfinite(dd.at[obs, ind]) and dd.at[obs, ind] > EXIT_DD:
                    return act_i, "recover"
                if rule == "stop":
                    cur = px_path["close_adj"].iloc[k]
                    if np.isfinite(cur) and cur / e0 - 1 >= EXIT_TP:
                        return act_i, "tp"
                    if np.isfinite(cur) and cur / e0 - 1 <= EXIT_SL:
                        return act_i, "sl"
            return last_i, "timeout"

        rec = {"date": r["date"], "ym": r["ym"], "industry": ind}
        for h in (5, 10, 20):
            j = entry_i + h
            rec[f"hold{h}"] = _hold_return(px, sessions, entry_i, j) if j <= last_i else None
        for rule in ("status", "recover", "stop"):
            j, why = walk(rule)
            rec[f"{rule}_ret"] = _hold_return(px, sessions, entry_i, j)
            rec[f"{rule}_days"] = int(j - entry_i)
            rec[f"{rule}_why"] = why
        rows.append(rec)
    x = pd.DataFrame(rows)

    def stat(col: str, days: str | None = None) -> dict:
        s = x[col].dropna()
        if s.empty:
            return {"n": 0}
        monthly = x.dropna(subset=[col]).groupby("ym")[col].mean()
        t = (monthly.mean() / (monthly.std(ddof=1) / np.sqrt(len(monthly)))
             if len(monthly) > 6 and monthly.std(ddof=1) > 0 else None)
        d = {"n": int(len(s)), "ev_bp": round(float(s.mean()) * 1e4, 1),
             "ev_bp_month": round(float(monthly.mean()) * 1e4, 1),
             "med_bp": round(float(s.median()) * 1e4, 1),
             "win": round(float((s > 0).mean()) * 100, 1),
             "p10_bp": round(float(np.percentile(s, 10)) * 1e4, 1),
             "t_month": round(float(t), 2) if t is not None else None}
        if days:
            d["avg_days"] = round(float(x[days].mean()), 1)
        return d

    return {
        "common": {
            "n": int(len(x)),
            "hold5": stat("hold5", None),
            "hold10": stat("hold10"),
            "hold20": stat("hold20"),
            "status": stat("status_ret", "status_days"),
            "recover": stat("recover_ret", "recover_days"),
            "stop": stat("stop_ret", "stop_days"),
        },
        "stop_reasons": x["stop_why"].value_counts().to_dict() if "stop_why" in x else {},
        "status_reasons": x["status_why"].value_counts().to_dict() if "status_why" in x else {},
    }


# ---------------------------------------------------------------- EXP-03
def exp_day_k(status, nav, sessions) -> dict:
    """按「当天有几个候选」分组：k=1 / k=2 / k>=3，等权买入当天全部候选。

    这才是「六个行业同时触发该不该加总仓位」的直接读数；按天取样必然重叠，
    所以 t 仍按自然月聚簇，且不把 n 当独立样本。
    """
    cand = status.eq("CANDIDATE")
    cnt = cand.sum(axis=1)
    pos = {t: i for i, t in enumerate(sessions)}
    recs: list[dict] = []
    for t in cnt[cnt > 0].index:
        i = pos[t]
        if i + 1 >= len(sessions):
            continue
        inds = [c for c in status.columns if bool(cand.at[t, c])]
        entry = sessions[i + 1]
        sub = nav[inds]
        if entry not in sub.index:
            continue
        norm = sub / sub.loc[entry]
        port = norm.mean(axis=1)          # 等权组合净值（行业净值口径，不含 ETF 选择）
        rec = {"date": t, "ym": str(t)[:7], "k": len(inds),
               "members": ",".join(inds)}
        for h in (5, 10, 20):
            j = i + 1 + h
            if j >= len(sessions):
                continue
            exit_ = sessions[j]
            if exit_ not in port.index:
                continue
            rec[f"ret{h}"] = float(port.loc[exit_] - 1)
            win = port.loc[entry:exit_]
            rec[f"mae{h}"] = float(win.min() - 1)
            rec[f"mfe{h}"] = float(win.max() - 1)
        recs.append(rec)
    x = pd.DataFrame(recs)
    if x.empty:
        return {"note": "无候选日"}
    out = {"days": int(len(x)), "months": int(x.ym.nunique()), "by_k": {}}
    for label, mask in {"k=1": x.k == 1, "k=2": x.k == 2, "k>=3": x.k >= 3}.items():
        g = x[mask]
        if g.empty:
            out["by_k"][label] = None
            continue
        cell = {"n_days": int(len(g)), "n_months": int(g.ym.nunique())}
        for h in (5, 10, 20):
            col = f"ret{h}"
            if col not in g:
                continue
            s = g[col].dropna()
            if s.empty:
                continue
            monthly = g.dropna(subset=[col]).groupby("ym")[col].mean()
            t = (monthly.mean() / (monthly.std(ddof=1) / np.sqrt(len(monthly)))
                 if len(monthly) > 6 and monthly.std(ddof=1) > 0 else None)
            cell[f"h{h}"] = {
                "n": int(len(s)),
                "ret_bp": round(float(s.mean()) * 1e4, 1),
                "med_bp": round(float(s.median()) * 1e4, 1),
                "win": round(float((s > 0).mean()) * 100, 1),
                "p10_bp": round(float(np.percentile(s, 10)) * 1e4, 1),
                "mae_med_bp": round(float(g[f"mae{h}"].median()) * 1e4, 1),
                "t_month": round(float(t), 2) if t is not None else None,
            }
        out["by_k"][label] = cell
    return out


def exp_clusters(ev: pd.DataFrame, status, nav, prices, sym_mat, sessions) -> dict:
    """同一天的多个候选 = 一笔预算：组合收益、相关性、组合最大不利波动。"""
    cnt = status.eq("CANDIDATE").sum(axis=1)
    active = cnt[cnt > 0].index
    if len(active) == 0:
        return {"note": "无候选日"}
    pos = {t: i for i, t in enumerate(sessions)}
    clusters: list[list[pd.Timestamp]] = []
    cur = [active[0]]
    for t in active[1:]:
        if pos[t] - pos[cur[-1]] <= CLUSTER_GAP:
            cur.append(t)
        else:
            clusters.append(cur)
            cur = [t]
    clusters.append(cur)

    base = nav.mean(axis=1)
    recs = []
    for cl in clusters:
        start = cl[0]
        i = pos[start]
        inds = [c for c in status.columns if status.at[start, c] == "CANDIDATE"]
        if not inds or i + 1 >= len(sessions):
            continue
        entry = sessions[i + 1]
        rec = {"start": str(start.date()), "days": len(cl), "k": len(inds),
               "max_k": int(cnt.loc[cl].max()), "industries": inds}
        for h in (5, 10, 20):
            j = i + 1 + h
            if j >= len(sessions):
                continue
            exit_ = sessions[j]
            rets = []
            for ind in inds:
                sym = str(sym_mat.at[start, ind])
                px = prices.get(sym)
                if px is None or entry not in px.index or exit_ not in px.index:
                    continue
                a, b = float(px.at[entry, "close_adj"]), float(px.at[exit_, "close_adj"])
                if np.isfinite(a) and np.isfinite(b):
                    rets.append(b / a - 1)
            if rets:
                rec[f"ret{h}"] = float(np.mean(rets))
                rec[f"n{h}"] = len(rets)
                rec[f"rel{h}"] = float(np.mean(rets)) - (float(base.get(exit_, np.nan)) / float(base.get(entry, np.nan)) - 1)
        # 簇内相关性（行业净值日收益）
        if len(inds) >= 2 and len(cl) >= 5:
            sub = nav.loc[cl[0]:cl[-1], inds].pct_change().dropna()
            if len(sub) >= 3:
                c = sub.corr().to_numpy()
                rec["avg_corr"] = float(np.nanmean(c[np.triu_indices_from(c, k=1)]))
        recs.append(rec)
    x = pd.DataFrame(recs)
    if x.empty:
        return {"clusters": 0}

    def agg(mask, h):
        g = x[mask].dropna(subset=[f"ret{h}"]) if f"ret{h}" in x else pd.DataFrame()
        if g.empty:
            return None
        return {"n": int(len(g)),
                "ret_bp": round(float(g[f"ret{h}"].mean()) * 1e4, 1),
                "rel_bp": round(float(g[f"rel{h}"].dropna().mean()) * 1e4, 1) if f"rel{h}" in g else None,
                "win": round(float((g[f"ret{h}"] > 0).mean()) * 100, 1)}

    out = {
        "clusters": int(len(x)),
        "k_dist": {int(k): int(v) for k, v in x["k"].value_counts().sort_index().items()},
        "avg_corr": round(float(x["avg_corr"].mean()), 3) if "avg_corr" in x else None,
        "corr_n": int(x["avg_corr"].notna().sum()) if "avg_corr" in x else 0,
        "by_k": {},
    }
    for label, mask in {"k<=1": x.k <= 1, "k=2": x.k == 2, "k>=3": x.k >= 3}.items():
        out["by_k"][label] = {f"h{h}": agg(mask, h) for h in (5, 10, 20)}
    out["sample"] = x.tail(8)[["start", "k", "max_k", "days", "avg_corr", "ret20"]].astype(object).to_dict("records")
    return out


# ---------------------------------------------------------------- 入口
def main() -> None:
    ap = argparse.ArgumentParser(description="买点之后：持有期限 / 退出 / 事件簇")
    ap.add_argument("--rebuild", action="store_true", help="强制重扫全历史")
    args = ap.parse_args()

    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = scan_history(d, idx, rebuild=args.rebuild)

    mats = daily_matrices(m)
    status, dd, sym_mat = mats["status"], mats["dd"], mats["symbol"]
    sessions = list(status.index)
    ev = build_events(status, dd)
    print(f"事件 {len(ev)} 个，独立月 {ev.ym.nunique()} 个 "
          f"（{ev.date.min().date()} → {ev.date.max().date()}）")

    ctx = etf_watch.build_context(d, idx, sessions=10 ** 6)
    nav = ctx.nav
    symbols = {str(s) for s in sym_mat.stack().dropna().unique()}
    prices = price_frame(d, symbols)

    hz = exp_horizons(ev, sessions, prices, sym_mat, nav)
    ex = exp_exits(ev, status, dd, sessions, prices, sym_mat)
    cl = exp_clusters(ev, status, nav, prices, sym_mat, sessions)
    dk = exp_day_k(status, nav, sessions)

    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rule_version": "F1/F2/F3/F6 @ 2026-11-22",
            "window": [str(ev.date.min().date()), str(ev.date.max().date())],
            "events": int(len(ev)),
            "months": int(ev.ym.nunique()),
            "definitions": {
                "event": "候选段首日；同行业 60 交易日冷却",
                "entry": "t+1 收盘（载体=当日 F6 选出的最大 ETF）",
                "rel": "减去全行业等权净值的同期收益",
                "t": "按自然月聚簇后对月均值做 t，不用事件数做 N",
                "exit_exec": "收盘观察、次日收盘执行，避免同日盘中路径假设",
            },
            "caveats": [
                "行业分类用当前名称映射、ETF 池为当前在市 ⇒ 生存/前视偏差未消除",
                "60 日收益在窗口尾部不足 60 日的样本被丢弃 ⇒ 近期事件在长周期上偏少",
            ],
        },
        "exp01_horizons": hz,
        "exp02_exits": ex,
        "exp03_clusters": cl,
        "exp03_day_k": dk,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    print("\n=== EXP-01 持有期限（载体 ETF，t+1 收盘进场，相对全行业等权）===")
    if "horizons" in hz:
        rows = []
        for k, v in hz["horizons"].items():
            rows.append({"H": k, "n": v["n"], "months": v["months"],
                         "ev_bp": v["ev_bp"], "月均bp": v["ev_bp_month"],
                         "rel月bp": v["rel_bp_month"], "beta": v["beta"],
                         "alpha": v["alpha_bp"], "t_a": v["t_alpha"], "t_rel": v["t_month"],
                         "win": v["win"], "p10_bp": v["p10_bp"], "mae_bp": v["mae_med_bp"]})
        print(pd.DataFrame(rows).to_string(index=False))
    print("\n=== EXP-02 退出规则（次日收盘执行）===")
    print(pd.DataFrame(ex["common"]).T.to_string())
    print("\n=== EXP-03 事件簇 ===")
    print(f"簇 {cl.get('clusters')} 个 · k 分布 {cl.get('k_dist')} · "
          f"簇内平均相关 {cl.get('avg_corr')}（n={cl.get('corr_n')}）")
    for lab, v in cl.get("by_k", {}).items():
        for h, s in v.items():
            if s:
                print(f"  {lab:5s} {h:4s} n={s['n']:3d} ret {s['ret_bp']:+7.1f}bp "
                      f"rel {s['rel_bp']:+7.1f}bp win {s['win']:.0f}%")

    print("\n=== EXP-03b 按当日候选数分组（等权买入当天全部候选）===")
    for lab, cell in (dk.get("by_k") or {}).items():
        if not cell:
            continue
        line = f"  {lab:5s} {cell['n_days']:4d} 天 / {cell['n_months']:3d} 月"
        for h in (5, 10, 20):
            s = cell.get(f"h{h}")
            if s:
                line += f" | {h}日 {s['ret_bp']:+7.1f}bp t={s['t_month']}"
        print(line)
    print(f"\n写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
