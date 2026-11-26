"""H4：年龄离散度 → 冲击后的恢复速度。

预注册（写于跑数之前，不因结果改）
--------------------------------------------------
为什么换轴
  原始 H4 用 age_entropy。实测 age_entropy 与 age_mean 的 Spearman = 0.989、
  与 20 日换手 = -0.921：几何型年龄分布下熵 ≈ log(均值)，它不是第二个轴，
  只是久期的另一种写法。用它跑恢复 = 把 H1 重跑一遍。
  所以主轴改为**尺度无关**的离散度：
      disp = q90 / q50        （年龄分位比；几何分布下 ≈ 3.32，越大越分散）
  实测 disp 与 age_mean 的 Spearman 仅 -0.161，与换手 +0.356。
  次轴 excess_H = age_entropy - log(age_mean)（超额熵），与久期 -0.457，作稳健性。

冲击
  个股**特质**冲击：exc_t = r_t - mkt_t（mkt 为等权全市场），
  exc_t <= -2 * sd60(exc) 且 exc_t <= -3%。
  用特质而非原始收益，避免把「市场普跌」当成个股冲击（那会让同日所有股票共享一个冲击）。
  市况冲击（次口径）：mkt_t <= -1.5% 的全部交易日。

恢复
  半恢复位 = (C_{t-1} + C_t) / 2（冲击前收盘与冲击日收盘的中点）。
  从 t+1 起算，首次 C >= 半恢复位的天数；60 日截尾。
  主结果 rec20 = 20 日内半恢复（二元）；次结果 rec40 / rec60 / half_days（截尾）。
  全恢复（回到 C_{t-1}）只作参考：真重估的股票本就不该回去。

判据
  主：月度截面回归 rec20 ~ z(disp) + z(D) + 冲击幅度 + z(vol20) + z(beta) + z(ret20)
      + z(turn20) + z(logamt20)；z(disp) 系数 NW(6) |t| >= 2 且符号为「高 disp 恢复快」。
  次：disp 五分位 Q5-Q1 的 rec20 差 >= 5pp，且 half_days 中位快 >= 30%。
  再：在冲击幅度三分位内做同样对比（排除「冲击更深所以更慢」的机械解释）。
  市况冲击口径必须同号。

杀死
  控制后不显著；或符号相反；或 disp 的效应完全由 D/规模/波动解释（系数掉到 1/3 以下）；
  或市况冲击口径反向。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "chip_h4"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "data" / "kline_daily_enriched"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"

COOLDOWN = 20
REC_MAX = 60
SHOCK_SD = 2.0
SHOCK_FLOOR = -0.03
MKT_SHOCK = -0.015
NW_LAG = 6


def load_daily(start: str = "2017-01-01"):
    dataset = ds.dataset(SRC, format="parquet")
    table = dataset.to_table(
        columns=["symbol", "date", "open", "close", "amount", "turnover_rate"],
        filter=ds.field("date") >= pd.Timestamp(start).date(),
    )
    df = table.to_pandas()
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


def zscore(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def recovery(ti: np.ndarray, ci: np.ndarray, C: np.ndarray, pre: np.ndarray):
    """半恢复与全恢复的天数（1..60），未恢复记 61。"""
    n = len(ti)
    half = 0.5 * (pre + C[ti, ci])
    full = pre.copy()
    d_half = np.full(n, REC_MAX + 1, dtype=np.int16)
    d_full = np.full(n, REC_MAX + 1, dtype=np.int16)
    for d in range(1, REC_MAX + 1):
        t2 = ti + d
        ok = t2 < C.shape[0]
        if not ok.any():
            break
        sel = np.nonzero(ok)[0]
        c = C[t2[sel], ci[sel]]
        fin = c[np.isfinite(c)]
        # 用向量化比较，长度对齐
        hit_h = np.zeros(len(sel), dtype=bool)
        hit_f = np.zeros(len(sel), dtype=bool)
        good = np.isfinite(c)
        hit_h[good] = c[good] >= half[sel][good]
        hit_f[good] = c[good] >= full[sel][good]
        upd_h = (d_half[sel] > REC_MAX) & hit_h
        upd_f = (d_full[sel] > REC_MAX) & hit_f
        if upd_h.any():
            d_half[sel[upd_h]] = d
        if upd_f.any():
            d_full[sel[upd_f]] = d
    return d_half, d_full


def main() -> None:
    print("[1/5] load ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(dtype=np.float32)
    C = cl_df.to_numpy(dtype=np.float32)
    A = am_df.to_numpy(dtype=np.float32)
    TO = to_df.to_numpy(dtype=np.float32)
    n, k = C.shape
    print(f"      {n} x {k}", flush=True)

    print("[2/5] returns, market, rolling sd60 of excess ...", flush=True)
    r = np.full((n, k), np.nan, dtype=np.float32)
    r[1:] = C[1:] / C[:-1] - 1.0
    mkt = np.nanmean(r, axis=1)
    exc = r - mkt[:, None]
    sd60 = np.full((n, k), np.nan, dtype=np.float32)
    lo = 60
    for t in range(lo, n):
        w = exc[t - lo:t]
        with np.errstate(all="ignore"):
            sd60[t] = np.nanstd(w, axis=0)
    print(f"      market ann vol = {np.nanstd(mkt) * np.sqrt(252):.3f}", flush=True)

    print("[3/5] shock events ...", flush=True)
    with np.errstate(all="ignore"):
        shock = (exc <= -SHOCK_SD * sd60) & (exc <= SHOCK_FLOOR) & np.isfinite(exc)
    ti, ci = np.nonzero(shock)
    order = np.argsort(ti, kind="stable")
    ti, ci = ti[order], ci[order]
    last = np.full(k, -10**9)
    keep = np.zeros(len(ti), dtype=bool)
    for j in range(len(ti)):
        t, i = ti[j], ci[j]
        if t - last[i] >= COOLDOWN:
            keep[j] = True
            last[i] = t
    ti, ci = ti[keep], ci[keep]
    print(f"      events={len(ti):,}  days={len(np.unique(ti)):,}  symbols={len(np.unique(ci)):,}", flush=True)

    print("[4/5] recovery ...", flush=True)
    pre = C[ti - 1, ci]
    d_half, d_full = recovery(ti, ci, C, pre)
    ret_t = r[ti, ci]
    ev = pd.DataFrame({
        "date": dates[ti], "symbol": syms[ci],
        "ret": ret_t, "exc": exc[ti, ci],
        "pre": pre, "post": C[ti, ci],
        "drop": C[ti, ci] / pre - 1.0,
        "half_days": d_half.astype(np.int16),
        "full_days": d_full.astype(np.int16),
        "rec20": (d_half <= 20).astype(np.float32),
        "rec40": (d_half <= 40).astype(np.float32),
        "rec60": (d_half <= REC_MAX).astype(np.float32),
        "full20": (d_full <= 20).astype(np.float32),
        "full60": (d_full <= REC_MAX).astype(np.float32),
    })
    # 事件层控制：只用事件日前的信息
    ret20 = np.full(len(ti), np.nan)
    vol20 = np.full(len(ti), np.nan)
    beta = np.full(len(ti), np.nan)
    turn20 = np.full(len(ti), np.nan)
    amt20 = np.full(len(ti), np.nan)
    for t in np.unique(ti):
        sel = np.nonzero(ti == t)[0]
        if t < 65:
            continue
        idx = ci[sel]
        ret20[sel] = C[t, idx] / C[t - 20, idx] - 1.0
        w = exc[t - 20:t, idx]
        vol20[sel] = np.nanstd(w, axis=0)
        m = mkt[t - 60:t]
        e = exc[t - 60:t, idx]
        mv = np.nanvar(m)
        beta[sel] = np.nanmean((e - np.nanmean(e, axis=0)) * (m - np.nanmean(m))[:, None], axis=0) / max(mv, 1e-12)
        turn20[sel] = np.nanmean(TO[t - 20:t, idx], axis=0)
        amt20[sel] = np.nanmean(A[t - 20:t, idx], axis=0)
    ev["ret20"] = ret20
    ev["vol20"] = vol20
    ev["beta"] = beta
    ev["turn20"] = turn20
    ev["logamt20"] = np.log(np.maximum(amt20, 1.0))
    ev["down_gap"] = ev["drop"]

    print("[5/5] merge age panel + tests ...", flush=True)
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"]).astype("datetime64[ns]")
    age = age[age["q50"] > 0].copy()
    age["disp"] = (age["q90"] / age["q50"]).clip(1.0, 20.0)
    age["excess_H"] = age["age_entropy"] - np.log(age["age_mean"].clip(lower=1.0))
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    ag = age[["date", "symbol", "disp", "excess_H", "D", "age_mean", "age_entropy", "young20"]].copy()
    ag = ag.sort_values("date")
    ev["date"] = ev["date"].astype("datetime64[ns]")
    ag["symbol"] = ag["symbol"].astype(str)
    ev["symbol"] = ev["symbol"].astype(str)
    ev = ev.sort_values("date")
    ev = pd.merge_asof(ev, ag, on="date", by="symbol", direction="backward")
    ev = ev.dropna(subset=["disp", "D", "ret20", "vol20", "beta", "turn20", "logamt20", "half_days"])
    ev = ev[np.isfinite(ev["disp"]) & np.isfinite(ev["beta"])]
    ev.to_parquet(OUT / "events.parquet", index=False)
    print(f"      merged={len(ev):,}  dates={ev['date'].nunique()}  symbols={ev['symbol'].nunique()}", flush=True)

    corr = {
        "disp_vs_D": round(float(ev[["disp", "D"]].corr(method="spearman").iloc[0, 1]), 3),
        "disp_vs_turn20": round(float(ev[["disp", "turn20"]].corr(method="spearman").iloc[0, 1]), 3),
        "disp_vs_vol20": round(float(ev[["disp", "vol20"]].corr(method="spearman").iloc[0, 1]), 3),
        "disp_vs_logamt20": round(float(ev[["disp", "logamt20"]].corr(method="spearman").iloc[0, 1]), 3),
        "excessH_vs_D": round(float(ev[["excess_H", "D"]].corr(method="spearman").iloc[0, 1]), 3),
    }

    def run(ev2: pd.DataFrame, xcol: str):
        reg = []
        for dt, g in ev2.groupby("date"):
            if len(g) < 60:
                continue
            y = g["rec20"].to_numpy(float)
            X = np.column_stack([
                zscore(g[xcol]), zscore(g["D"]), zscore(g["drop"]), zscore(g["vol20"]),
                zscore(g["beta"]), zscore(g["ret20"]), zscore(g["turn20"]), zscore(g["logamt20"]),
                np.ones(len(g)),
            ])
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 60 or y[ok].std() == 0:
                continue
            b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            reg.append({"date": dt, "b_x": b[0], "b_D": b[1], "n": int(ok.sum())})
        return pd.DataFrame(reg)

    reg = run(ev, "disp")
    reg.to_parquet(OUT / "regression.parquet", index=False)
    main_res = {
        "b_disp": round(float(reg["b_x"].mean()), 4),
        "t_disp": round(float(nw_t(reg["b_x"])), 2),
        "b_D": round(float(reg["b_D"].mean()), 4),
        "t_D": round(float(nw_t(reg["b_D"])), 2),
        "months": int(len(reg)),
    }

    # 五分位（月度内）
    qrows = []
    for dt, g in ev.groupby("date"):
        if len(g) < 100:
            continue
        try:
            g = g.copy()
            g["q"] = pd.qcut(g["disp"], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        for qi, x in g.groupby("q"):
            qrows.append({"date": dt, "q": int(qi), "n": len(x),
                          "rec20": x["rec20"].mean(), "rec40": x["rec40"].mean(),
                          "rec60": x["rec60"].mean(),
                          "half_days": x["half_days"].median(),
                          "half_mean": x["half_days"].mean(),
                          "drop": x["drop"].mean()})
    q = pd.DataFrame(qrows)
    q.to_parquet(OUT / "quintiles.parquet", index=False)
    qm = q.groupby("q")[["rec20", "rec40", "rec60", "half_days", "half_mean", "drop", "n"]].mean()
    spread_rows = []
    for dt, g in q.groupby("date"):
        w = g.set_index("q")
        if 0 in w.index and 4 in w.index:
            spread_rows.append({"date": dt,
                                "rec20": w.loc[4, "rec20"] - w.loc[0, "rec20"],
                                "rec40": w.loc[4, "rec40"] - w.loc[0, "rec40"],
                                "rec60": w.loc[4, "rec60"] - w.loc[0, "rec60"],
                                "half_days": w.loc[4, "half_days"] - w.loc[0, "half_days"]})
    sp = pd.DataFrame(spread_rows)
    sp.to_parquet(OUT / "spread.parquet", index=False)

    # 冲击幅度三分位内的对比（排除「冲击更深→恢复更慢」的机械解释）
    cond = []
    for dt, g in ev.groupby("date"):
        if len(g) < 150:
            continue
        try:
            g = g.copy()
            g["dt3"] = pd.qcut(g["drop"], 3, labels=False, duplicates="drop")
            g["dq"] = pd.qcut(g["disp"], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        for d3, x in g.groupby("dt3"):
            a = x[x["dq"] == 4]
            b = x[x["dq"] == 0]
            if len(a) > 5 and len(b) > 5:
                cond.append({"date": dt, "dt3": int(d3),
                             "rec20": a["rec20"].mean() - b["rec20"].mean(),
                             "half_days": a["half_days"].median() - b["half_days"].median(),
                             "drop": x["drop"].mean()})
    cd = pd.DataFrame(cond)

    # 次轴：超额熵
    reg2 = run(ev, "excess_H")
    sec = {
        "b_excessH": round(float(reg2["b_x"].mean()), 4),
        "t_excessH": round(float(nw_t(reg2["b_x"])), 2),
        "months": int(len(reg2)),
    }

    # 市况冲击口径
    mkt_shock_days = [t for t in range(65, n) if mkt[t] <= MKT_SHOCK]
    mk_rows = []
    for t in mkt_shock_days:
        sub = ev[ev["date"] == dates[t]]
        if len(sub) < 100:
            continue
        try:
            sub = sub.copy()
            sub["q"] = pd.qcut(sub["disp"], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        a = sub[sub["q"] == 4]
        b = sub[sub["q"] == 0]
        if len(a) > 10 and len(b) > 10:
            mk_rows.append({"date": dates[t], "rec20": a["rec20"].mean() - b["rec20"].mean(),
                            "half_days": a["half_days"].median() - b["half_days"].median(),
                            "n": len(sub)})
    mk = pd.DataFrame(mk_rows)

    summary = {
        "sample": {
            "events": int(len(ev)), "dates": int(ev["date"].nunique()),
            "symbols": int(ev["symbol"].nunique()),
            "start": str(ev["date"].min().date()), "end": str(ev["date"].max().date()),
            "mkt_shock_days": int(len(mk)),
        },
        "corr": corr,
        "main": main_res,
        "secondary_axis": sec,
        "quintiles": {str(i): {c: round(float(qm.loc[i, c]), 4) for c in
                               ["rec20", "rec40", "rec60", "half_days", "half_mean", "drop"]}
                      for i in qm.index},
        "spread": {
            "rec20_pp": round(float(sp["rec20"].mean() * 100), 2),
            "t_rec20": round(float(nw_t(sp["rec20"])), 2),
            "rec40_pp": round(float(sp["rec40"].mean() * 100), 2),
            "t_rec40": round(float(nw_t(sp["rec40"])), 2),
            "half_days": round(float(sp["half_days"].mean()), 2),
            "t_half": round(float(nw_t(sp["half_days"])), 2),
        },
        "within_drop_tercile": {
            str(int(d3)): {
                "rec20_pp": round(float(x["rec20"].mean() * 100), 2),
                "t_rec20": round(float(nw_t(x["rec20"])), 2),
                "half_days": round(float(x["half_days"].mean()), 2),
                "t_half": round(float(nw_t(x["half_days"])), 2),
                "mean_drop": round(float(x["drop"].mean()), 4),
            } for d3, x in cd.groupby("dt3")
        },
        "market_shock": {
            "rec20_pp": round(float(mk["rec20"].mean() * 100), 2) if len(mk) else np.nan,
            "t_rec20": round(float(nw_t(mk["rec20"])), 2) if len(mk) else np.nan,
            "half_days": round(float(mk["half_days"].mean()), 2) if len(mk) else np.nan,
            "t_half": round(float(nw_t(mk["half_days"])), 2) if len(mk) else np.nan,
            "days": int(len(mk)),
        },
    }

    q1, q5 = summary["quintiles"]["0"], summary["quintiles"]["4"]
    faster = (q1["half_days"] / q5["half_days"]) if q5["half_days"] > 0 else np.nan
    summary["verdict"] = {
        "direction": "high_disp_faster" if summary["spread"]["rec20_pp"] > 0 else "high_disp_slower",
        "reg_pass": bool(main_res["b_disp"] > 0 and main_res["t_disp"] >= 2),
        "spread_pass": bool(summary["spread"]["rec20_pp"] >= 5 and summary["spread"]["t_rec20"] >= 2),
        "speedup_ratio_q1_over_q5": round(float(faster), 3),
        "speedup_30pct": bool(faster >= 1.3),
        "mkt_shock_same_sign": bool(np.sign(summary["market_shock"]["rec20_pp"]) ==
                                    np.sign(summary["spread"]["rec20_pp"])),
        "secondary_axis_same_sign": bool(np.sign(sec["b_excessH"]) == np.sign(main_res["b_disp"])),
    }
    (OUT / "h4.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
