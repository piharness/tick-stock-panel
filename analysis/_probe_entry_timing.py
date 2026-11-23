"""进场时点探针：信号日收盘 / 次日开盘 / 次日收盘 / 第三日开盘，哪一个形状最好。

背景问题（2026-11-23）：159530 在 t+1 盘中 +8%，账户规则却是「t+1 收盘买」。
如果进场日的涨幅已经吃掉 20 日均值，那么规则不是「滞后」，是**进场时点选错了**。

口径（与 holding-and-exit.md 一致，只改进场价，不改判定与退出）：
- 事件：候选段首日 t；同行业 60 交易日冷却（复用 experiments 的定义）。
- 载体：信号日 F6 选出的同行业最大 ETF。
- 退出：固定在 t+21 收盘（现行规则的退出日），所有进场变体卖在同一天，
  这样比较的是「同一段行情，进场价不同」，不是「持有期不同」。
- 相对收益：减去同行业等权净值同期收益；t 按自然月聚簇，N 是月不是事件。

用法：python analysis/_probe_entry_timing.py
输出：data/etf_watch/_probe_entry_timing.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import etf_watch  # noqa: E402
import _diag_industry_etf_asym as core  # noqa: E402
import etf_watch_experiments as EX  # noqa: E402

OUT = ROOT / "data/etf_watch/_probe_entry_timing.json"


def price_frame_with_open(d: pd.DataFrame, symbols: set[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    sub = d[d["symbol"].isin(symbols)]
    for sym, grp in sub.groupby("symbol", sort=False):
        f = grp.set_index("date")[["open", "close", "close_adj", "hi_adj", "lo_adj", "hi250"]].sort_index()
        f = f[~f.index.duplicated(keep="last")]
        # 与 hi_adj/lo_adj 同一折算：open_adj = open × (close_adj / close)
        k = f["close_adj"] / f["close"].where(f["close"] > 0)
        f["open_adj"] = f["open"] * k
        out[str(sym)] = f
    return out


def monthly_t(x: pd.DataFrame, col: str) -> tuple[float | None, int]:
    if x.empty:
        return None, 0
    mo = x.groupby("ym")[col].mean()
    if len(mo) <= 6 or mo.std(ddof=1) == 0:
        return None, len(mo)
    return float(mo.mean() / (mo.std(ddof=1) / np.sqrt(len(mo)))), len(mo)


def summarize(x: pd.DataFrame) -> dict:
    if x.empty:
        return {"n": 0}
    t_rel, months = monthly_t(x, "rel")
    return {
        "n": int(len(x)),
        "months": months,
        "abs_bp": round(float(x["ret"].mean()) * 1e4, 1),
        "abs_med_bp": round(float(x["ret"].median()) * 1e4, 1),
        "rel_bp": round(float(x["rel"].mean()) * 1e4, 1),
        "rel_med_bp": round(float(x["rel"].median()) * 1e4, 1),
        "rel_t_month": round(t_rel, 2) if t_rel is not None else None,
        "win": round(float((x["ret"] > 0).mean()) * 100, 1),
        "rel_win": round(float((x["rel"] > 0).mean()) * 100, 1),
        "p10_bp": round(float(np.nanpercentile(x["ret"], 10)) * 1e4, 1),
    }


def main() -> None:
    etf, _ = etf_watch.fresh_etf()
    idx, _ = etf_watch.fresh_index()
    d, _ = core.load(etf_raw=etf)
    m = EX.scan_history(d, idx)

    mats = EX.daily_matrices(m)
    status, dd, sym_mat = mats["status"], mats["dd"], mats["symbol"]
    sessions = list(status.index)
    pos = {t: i for i, t in enumerate(sessions)}
    ev = EX.build_events(status, dd)

    ctx = etf_watch.build_context(d, idx, sessions=10 ** 6)
    nav = ctx.nav
    symbols = {str(s) for s in sym_mat.stack().dropna().unique()}
    prices = price_frame_with_open(d, symbols)

    recs: list[dict] = []
    for _, r in ev.iterrows():
        i = int(pos[r["date"]])
        if i + 22 >= len(sessions):
            continue
        sym = EX._carrier_of(r, sym_mat)
        px = prices.get(sym) if sym else None
        if px is None:
            continue
        t0, t1, t2 = sessions[i], sessions[i + 1], sessions[i + 2]
        exit_ = sessions[i + 21]
        if exit_ not in px.index:
            continue
        need = {t0: "close_adj", t1: "close_adj", t2: "close_adj"}
        if any(t not in px.index or not np.isfinite(px.at[t, c]) for t, c in need.items()):
            continue
        if not np.isfinite(px.at[t1, "open_adj"]) or not np.isfinite(px.at[t2, "open_adj"]):
            continue
        ind = r["industry"]
        if ind not in nav.columns or t0 not in nav.index or exit_ not in nav.index:
            continue
        dd1 = float(dd.at[t1, ind]) if (t1 in dd.index and ind in dd.columns) else np.nan
        b0 = float(nav.at[t0, ind])
        b1 = float(nav.at[exit_, ind])
        if not np.isfinite(b0) or not np.isfinite(b1) or b0 == 0:
            continue

        entries = {
            "t0_close": float(px.at[t0, "close_adj"]),
            "t1_open": float(px.at[t1, "open_adj"]),
            "t1_close": float(px.at[t1, "close_adj"]),
            "t2_open": float(px.at[t2, "open_adj"]),
        }
        # 限价单形状：min(信号收盘 +3%, 区间上沿价)；开盘低于限价按开盘成交，
        # 盘中回落到限价按限价成交，否则放弃。一个订单同时实现「不追高」与「出区间不买」。
        limit_adj = min(float(px.at[t0, "close_adj"]) * 1.03, float(px.at[t0, "hi250"]) * 0.65)
        # 简化版：限价 = 信号收盘 +3%（不依赖 hi250/k，少一个数据依赖）
        limit3 = float(px.at[t0, "close_adj"]) * 1.03
        if entries["t1_open"] <= limit3:
            fill3, how3 = entries["t1_open"], "open"
        elif np.isfinite(px.at[t1, "lo_adj"]) and float(px.at[t1, "lo_adj"]) <= limit3:
            fill3, how3 = limit3, "limit"
        else:
            fill3, how3 = None, "skip"
        if entries["t1_open"] <= limit_adj:
            fill, how = entries["t1_open"], "open"
        elif np.isfinite(px.at[t1, "lo_adj"]) and float(px.at[t1, "lo_adj"]) <= limit_adj:
            fill, how = limit_adj, "limit"
        else:
            fill, how = None, "skip"
        base = float(px.at[exit_, "close_adj"])
        rec = {
            "date": r["date"], "ym": r["ym"], "industry": ind, "symbol": sym,
            "limit_adj": limit_adj, "fill": fill, "fill_how": how,
            "limit3": limit3, "fill3": fill3, "fill_how3": how3,
            "t0": t0, "t1": t1, "t2": t2, "exit": exit_,
            "t0_close": entries["t0_close"],
            "t1_open": entries["t1_open"],
            "t1_close": entries["t1_close"],
            "t2_open": entries["t2_open"],
            "exit_close": base,
            "t1_move_from_t0": entries["t1_close"] / entries["t0_close"] - 1,
            "t1_open_move": entries["t1_open"] / entries["t0_close"] - 1,
            "t1_intraday": entries["t1_close"] / entries["t1_open"] - 1,
            "t2_open_move": entries["t2_open"] / entries["t1_close"] - 1,
            "base": b1 / b0 - 1,
            "dd_t1": dd1,
            "out_of_band": bool(np.isfinite(dd1) and dd1 > -0.35),
        }
        for name, p0 in entries.items():
            rec[f"ret_{name}"] = base / p0 - 1
            rec[f"rel_{name}"] = (base / p0 - 1) - (b1 / b0 - 1)
        if fill:
            rec["ret_limit"] = base / fill - 1
            rec["rel_limit"] = (base / fill - 1) - (b1 / b0 - 1)
        else:
            rec["ret_limit"] = np.nan
            rec["rel_limit"] = np.nan
        if fill3:
            rec["ret_limit3"] = base / fill3 - 1
            rec["rel_limit3"] = (base / fill3 - 1) - (b1 / b0 - 1)
        else:
            rec["ret_limit3"] = np.nan
            rec["rel_limit3"] = np.nan
        recs.append(rec)

    x = pd.DataFrame(recs)

    variants = {}
    for name in ("t0_close", "t1_open", "t1_close", "t2_open"):
        variants[name] = summarize(x.rename(columns={f"ret_{name}": "ret", f"rel_{name}": "rel"}))
    filled = x[x.fill_how != "skip"].rename(columns={"ret_limit": "ret", "rel_limit": "rel"})
    variants["t1_open_limit"] = summarize(filled)
    variants["t1_open_limit"].update({
        "skipped": int((x.fill_how == "skip").sum()),
        "filled_at_open": int((x.fill_how == "open").sum()),
        "filled_at_limit": int((x.fill_how == "limit").sum()),
    })
    filled3 = x[x.fill_how3 != "skip"].rename(columns={"ret_limit3": "ret", "rel_limit3": "rel"})
    variants["t1_open_limit3"] = summarize(filled3)
    variants["t1_open_limit3"].update({
        "skipped": int((x.fill_how3 == "skip").sum()),
        "filled_at_open": int((x.fill_how3 == "open").sum()),
        "filled_at_limit": int((x.fill_how3 == "limit").sum()),
    })

    # 进场日移动的分布：这是「等一天」的价格成本
    def dist(col: str) -> dict:
        s = x[col].dropna()
        return {
            "mean_bp": round(float(s.mean()) * 1e4, 1),
            "med_bp": round(float(s.median()) * 1e4, 1),
            "p10_bp": round(float(np.percentile(s, 10)) * 1e4, 1),
            "p90_bp": round(float(np.percentile(s, 90)) * 1e4, 1),
            "p95_bp": round(float(np.percentile(s, 95)) * 1e4, 1),
            "max_bp": round(float(s.max()) * 1e4, 1),
            "min_bp": round(float(s.min()) * 1e4, 1),
            "share_gt_3pct": round(float((s > 0.03).mean()) * 100, 1),
            "share_gt_5pct": round(float((s > 0.05).mean()) * 100, 1),
            "share_lt_neg3pct": round(float((s < -0.03).mean()) * 100, 1),
        }

    # 按进场日涨幅分桶：追高到底有没有代价
    buckets = []
    for label, lo, hi in (("<= -3%", -9, -0.03), ("-3% ~ 0", -0.03, 0.0),
                          ("0 ~ +3%", 0.0, 0.03), ("+3% ~ +6%", 0.03, 0.06),
                          ("> +6%", 0.06, 9)):
        g = x[(x.t1_move_from_t0 > lo) & (x.t1_move_from_t0 <= hi)]
        cell = summarize(g.rename(columns={"ret_t1_close": "ret", "rel_t1_close": "rel"}))
        cell["bucket"] = label
        buckets.append(cell)

    # 相关与回归：进场日涨幅 → 之后 20 日相对收益（从 t1 收盘起算）
    y = x["rel_t1_close"].to_numpy(float)
    z = x["t1_move_from_t0"].to_numpy(float)
    ok = np.isfinite(y) & np.isfinite(z)
    corr = float(pd.Series(z[ok]).rank().corr(pd.Series(y[ok]).rank()))
    slope = intercept = None
    if ok.sum() > 5:
        slope, intercept = np.polyfit(z[ok], y[ok], 1)

    # 进场日是否已经修复出区间（dd > -35%）：这是「买在修复之后」的直接检验
    band_rows = []
    for label, mask in (("still_in_band", ~x.out_of_band), ("out_of_band", x.out_of_band)):
        g = x[mask]
        cell = summarize(g.rename(columns={"ret_t1_close": "ret", "rel_t1_close": "rel"}))
        cell["bucket"] = label
        band_rows.append(cell)

    # 今日个案：159530，t0=2026-11-20，t1=2026-11-23
    today = {}
    sym = "159530.SZ"
    px = prices.get(sym)
    if px is not None and pd.Timestamp("2026-11-20") in px.index and pd.Timestamp("2026-11-23") in px.index:
        t0p = float(px.at[pd.Timestamp("2026-11-20"), "close_adj"])
        t1o = float(px.at[pd.Timestamp("2026-11-23"), "open_adj"])
        t1c = float(px.at[pd.Timestamp("2026-11-23"), "close_adj"])
        today = {"symbol": sym, "t0_close": round(t0p, 4), "t1_open": round(t1o, 4),
                 "t1_close": round(t1c, 4), "t1_move": round(t1c / t0p - 1, 4),
                 "t1_open_move": round(t1o / t0p - 1, 4)}

    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "window": [str(ev.date.min().date()), str(ev.date.max().date())],
            "events": int(len(x)), "months": int(x.ym.nunique()),
            "exit": "固定在 t+21 收盘；只改进场价，不改判定与退出",
            "today_note": "t0=2026-11-20 收盘，t1=2026-11-23；t1_close 用本地日线（若未落盘则为 NaN）",
        },
        "variants": variants,
        "entry_move": {
            "t1_open_vs_t0_close": dist("t1_open_move"),
            "t1_close_vs_t0_close": dist("t1_move_from_t0"),
            "t1_close_vs_t1_open": dist("t1_intraday"),
            "t2_open_vs_t1_close": dist("t2_open_move"),
        },
        "by_entry_day_move": buckets,
        "by_band_at_entry": band_rows,
        "corr_entry_move_vs_fwd_rel": round(corr, 3),
        "regression": {"slope": round(float(slope), 3) if slope is not None else None,
                       "intercept_bp": round(float(intercept) * 1e4, 1) if intercept is not None else None},
        "today": today,
        "events_detail": [
            {
                "date": str(r.date.date()), "industry": r.industry, "symbol": r.symbol,
                "t1_move_bp": round(float(r.t1_move_from_t0) * 1e4, 1),
                "t1_open_move_bp": round(float(r.t1_open_move) * 1e4, 1),
                "t1_intraday_bp": round(float(r.t1_intraday) * 1e4, 1),
                "ret_t1_close_bp": round(float(r.ret_t1_close) * 1e4, 1),
                "rel_t1_close_bp": round(float(r.rel_t1_close) * 1e4, 1),
                "ret_t1_open_bp": round(float(r.ret_t1_open) * 1e4, 1),
                "rel_t1_open_bp": round(float(r.rel_t1_open) * 1e4, 1),
                "dd_t1": round(float(r.dd_t1), 4) if np.isfinite(r.dd_t1) else None,
                "out_of_band": bool(r.out_of_band),
                "fill_how": r.fill_how,
                "fill_how3": r.fill_how3,
            }
            for r in x.itertuples()
        ],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=float))


if __name__ == "__main__":
    main()
