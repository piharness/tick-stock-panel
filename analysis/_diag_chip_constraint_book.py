"""约束簿（constraint book）：把「筹码」从持仓分布改成「活合约的触发价分布」。

为什么再换一次对象
------------------
卷三把筹码从 S(p) 换成 S(p,a)，仍是对「持仓」的统计。H1–H9 的失败有同一个形状：
所有统计量都是**非负、无符号**的（密度、久期、熵、中心度、比值），而收益效应是**有符号**的。
一个无符号对象只能靠估计去发现符号，于是 OOS 翻转；一个有合约规定的对象，符号在测量之前就定了。
唯一活下来的 H11/H12 正好是后者：平仓线是券商写进合约的、有日期、有强制执行的触发价。

本脚本把 H11 的事件研究升级成**状态变量**：不再问「公告日距平仓线多近」，
而是问「任意一个月末，这只股票的活合约簿里，有多大比例已经贴住触发价」。

对象
----
每条质押明细 = 一份合约：起始日 PF_START_DATE、解押日 UNFREEZE_DATE、
触发价 OPENLINE、占总股本比例 PF_TSR。
t 日的活合约 = start ≤ t 且（unfreeze > t 或 未解押且无到期日）。
  book_t      = Σ PF_TSR                      活合约占总股本 %
  near_share  = Σ PF_TSR·1{openline/close ∈ [0.8,1.2]} / book   贴线占比
  breach      = Σ PF_TSR·1{ratio ≥ 1.2} / book                 已破线占比
  wdist       = Σ PF_TSR·ratio / book                          簿加权距离
  exp90       = Σ PF_TSR·1{unfreeze ∈ (t,t+90]} / book         90 天内到期占比

检验（写于跑数之前）
--------------------
C1 剂量：book>0 的股票内，near_share 五分位 → fwd60 单调为负；控制换手/波动/规模/久期/动量后仍负。
C2 解押安慰剂（关键判别）：3 个月前贴线、现在合约已全部解除的股票，未来收益应回到「从未贴线」的水平。
   若解押组仍然跑输 → 这是趋势/动量，不是合约；约束簿理论被砍掉一半。
C3 波动（短伽马）：near_share 应预测未来 20 日已实现波动，且超过去期波动 + |ret20| + 规模 + 换手。
   合约理论说贴线股的波动是内生的；趋势理论不预测这一点。
C4 到期：near_share × exp90 的交互应为负（贴线且将到期 = 压力最大）。

杀死
----
C1 不单调或不显著；C2 解押组与贴线组同样差；C3 对波动无增量；C4 交互为零。
四条里活不到两条，约束簿只是 H11 的换名，不配做卷四。

口径边界（写死，不因结果改）
--------------------------
- 只保留能确定存活区间的合约：unfreeze 有值，或 未解押且无到期日。
  已解押但无日期的 32% 行被丢弃 → book 系统性低估；用 near_share（比例）而非绝对量做主检验。
- 2019–2020 的簿被左截断（2019 前起始且 2019 前已解押的合约不在库里）→ 面板从 2020-01 起。
- 非 ST 用当前名称、上市 ≥250 交易日、60 日均额 ≥1,500 万；北交所剔除。
- 入场 = 月末次日开盘，出场 = t+21/41/61 收盘；信号只用月末收盘与当日已知合约。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
AGE = ROOT / "data" / ".scratch" / "chip_age" / "age_panel.parquet"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
SRC = ROOT / "data" / "kline_daily_enriched"
NW_LAG = 6
START = "2020-01-01"


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
    cl = cl.reindex(op.index); am = am.reindex(op.index); to = to.reindex(op.index)
    return op, cl, am, to


def nw_t(x, lag=NW_LAG):
    x = np.asarray(x, dtype=float); x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return np.nan
    e = x - x.mean(); s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def z(x):
    x = np.asarray(x, dtype=float)
    mu, sd = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return np.clip((x - mu) / sd, -3, 3)


def main():
    print("[1/6] 日线与月末 ...", flush=True)
    op_df, cl_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32); C = cl_df.to_numpy(np.float32)
    A = am_df.to_numpy(np.float32); TO = to_df.to_numpy(np.float32)
    n, k = C.shape
    sym2idx = {s: i for i, s in enumerate(syms)}
    ym = pd.Series(dates).dt.to_period("M")
    last = pd.Series(dates).groupby(ym).max()
    mids = np.array([dates.get_loc(d) for d in last.values])
    mids = mids[(dates[mids] >= pd.Timestamp(START)) & (mids < n - 62)]
    print(f"      {n} x {k} · {len(mids)} 个月末 · {dates[mids[0]].date()} ~ {dates[mids[-1]].date()}", flush=True)

    print("[2/6] 合约 ...", flush=True)
    p = pd.read_parquet(OUT / "pledge_raw_v2.parquet").drop_duplicates()
    p["code"] = p["code"].astype(str).str.zfill(6)
    p["s"] = pd.to_datetime(p["PF_START_DATE"], errors="coerce")
    p["u"] = pd.to_datetime(p["UNFREEZE_DATE"], errors="coerce")
    p["tsr"] = pd.to_numeric(p["PF_TSR"], errors="coerce")
    p["line"] = pd.to_numeric(p["OPENLINE"], errors="coerce")
    p["open"] = p["UNFREEZE_STATE"].astype(str).eq("未解押")
    p = p.dropna(subset=["s", "tsr", "line"])
    p = p[(p["tsr"] > 0) & (p["line"] > 0)]
    p = p[p["u"].notna() | p["open"]]
    p.loc[p["u"].notna() & (p["u"] <= p["s"]), "u"] = p["s"] + pd.Timedelta(days=1)
    inst = pd.read_parquet(INST)[["code", "symbol", "name", "listing_date"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    # 同一 (股票, 持有人, 起始日, 平仓线, 到期日) 的多笔合并成一份合约
    p = p.groupby(["symbol", "idx", "HOLDER_NAME", "s", "u", "line"], dropna=False, as_index=False)["tsr"].sum()
    print(f"      合约 {len(p):,} 份 / {p['symbol'].nunique():,} 只 / 起始 {p.s.min().date()} ~ {p.s.max().date()}", flush=True)

    print("[3/6] 月末活簿 ...", flush=True)
    rows = []
    for m in mids:
        me = dates[m]
        live = (p["s"] <= me) & (p["u"].isna() | (p["u"] > me))
        q = p[live]
        if q.empty:
            continue
        px = C[m, q["idx"].to_numpy()]
        ok = np.isfinite(px) & (px > 0)
        if not ok.any():
            continue
        q = q.assign(px=px)[ok]
        q = q[q["px"] > 0]
        q["ratio"] = q["line"] / q["px"]
        q["r"] = q["ratio"].clip(0.2, 2.0)
        q["near"] = q["tsr"] * ((q["ratio"] >= 0.8) & (q["ratio"] <= 1.2))
        q["brch"] = q["tsr"] * (q["ratio"] >= 1.2)
        q["wn"] = q["tsr"] * q["r"]
        days = (q["u"] - me).dt.days
        q["exp"] = q["tsr"] * ((days > 0) & (days <= 90))
        g = q.groupby("symbol").agg(book=("tsr", "sum"), near=("near", "sum"), breach=("brch", "sum"),
                                    wnum=("wn", "sum"), expo=("exp", "sum"), ncon=("tsr", "size"))
        g["date"] = me
        g["near_share"] = g["near"] / g["book"]
        g["breach_share"] = g["breach"] / g["book"]
        g["wdist"] = g["wnum"] / g["book"]
        g["exp_share"] = g["expo"] / g["book"]
        rows.append(g.reset_index())
        if m == mids[0] or m == mids[-1]:
            print(f"      {me.date()}: {len(g):,} 只 · 中位 book {g['book'].median():.2f}% · 中位贴线 {g['near_share'].median():.2f}", flush=True)
    book = pd.concat(rows, ignore_index=True)
    book["ym"] = book["date"].dt.to_period("M").astype(str)
    print(f"      月末活簿 {len(book):,} 行", flush=True)

    print("[4/6] 结果与截面 ...", flush=True)
    # 全市场状态（含无簿股票）
    ret = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    rv20 = pd.DataFrame(ret).rolling(20).std().to_numpy()
    first = np.argmax(np.isfinite(C), axis=0)
    am20 = pd.DataFrame(A).rolling(20).mean().to_numpy()
    to20 = pd.DataFrame(TO).rolling(20).mean().to_numpy()
    am60 = pd.DataFrame(A).rolling(60).mean().to_numpy()
    name = inst.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    lst = inst.drop_duplicates("symbol").set_index("symbol")["listing_date"].to_dict()

    recs = []
    for m in mids:
        me = dates[m]
        rec = pd.DataFrame({"symbol": syms, "idx": np.arange(k)})
        rec["date"] = me
        rec["close"] = C[m]
        rec["ret20"] = C[m] / np.where(C[m - 20] > 0, C[m - 20], np.nan) - 1
        rec["ret60"] = C[m] / np.where(C[m - 60] > 0, C[m - 60], np.nan) - 1
        rec["vol20"] = rv20[m]
        rec["turn20"] = to20[m]
        rec["size"] = np.log(np.maximum(am20[m], 1.0))
        rec["amt60"] = am60[m]
        rec["f20"] = C[m + 21] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        rec["f40"] = C[m + 41] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        rec["f60"] = C[m + 61] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        rec["fv20"] = np.sqrt(np.nanmean(np.square(ret[m + 1:m + 21]), axis=0))
        rec["age"] = m - first
        rec["nm"] = [name.get(s, "") for s in syms]
        recs.append(rec)
    panel = pd.concat(recs, ignore_index=True)
    panel["ym"] = panel["date"].dt.to_period("M").astype(str)
    panel = panel.merge(book[["date", "symbol", "book", "near_share", "breach_share", "wdist", "exp_share", "ncon"]],
                        on=["date", "symbol"], how="left")
    panel["has_book"] = panel["book"].fillna(0) > 0
    for c in ["near_share", "breach_share", "wdist", "exp_share"]:
        panel[c] = panel[c].fillna(0.0)
    age = pd.read_parquet(AGE)
    age["date"] = pd.to_datetime(age["date"])
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    panel = panel.merge(age[["date", "symbol", "D"]], on=["date", "symbol"], how="left")
    panel["st"] = panel["nm"].fillna("").str.contains("ST")
    panel["ok"] = (~panel["st"]) & (panel["age"] >= 250) & (panel["amt60"] >= 1.5e7) & panel["close"].gt(0)
    # 3 个月前的贴线状态（解押安慰剂）
    past = panel[["date", "symbol", "near_share", "has_book"]].copy()
    past["date"] = past["date"] + pd.offsets.MonthEnd(0)
    p3 = past.copy()
    p3["date"] = p3["date"] + pd.DateOffset(months=3)
    p3 = p3.rename(columns={"near_share": "near_l3", "has_book": "book_l3"})
    panel = panel.merge(p3, on=["date", "symbol"], how="left")
    panel.to_parquet(OUT / "constraint_book_panel.parquet", index=False)
    print(f"      面板 {len(panel):,} 行 · 有效 {int(panel['ok'].sum()):,} · 有簿 {int((panel['ok'] & panel['has_book']).sum()):,}", flush=True)

    print("[5/6] C1 剂量 ...", flush=True)
    v = panel[panel["ok"] & panel["has_book"] & panel["f60"].notna()].copy()
    v = v[v.groupby("ym")["near_share"].transform("std") > 0.02]
    v["q"] = v.groupby("ym")["near_share"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    qm = v.dropna(subset=["q"]).groupby(["ym", "q"]).agg(f20=("f20", "mean"), f60=("f60", "mean"),
                                                         n=("f60", "size")).reset_index()
    piv = qm.pivot(index="ym", columns="q", values="f60")
    for c in range(5):
        if c not in piv:
            piv[c] = np.nan
    spread = piv[4] - piv[0]
    reg = []
    for ym_, g in v.groupby("ym"):
        if len(g) < 80:
            continue
        X = np.column_stack([z(g["near_share"]), z(g["breach_share"]), z(g["vol20"]), z(g["turn20"]),
                             z(g["size"]), z(g["D"]), z(g["ret20"]), z(g["ret60"]), np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 80:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym_, "b_near": b[0], "b_breach": b[1], "n": int(ok.sum())})
    rr = pd.DataFrame(reg)
    c1 = {
        "n": int(len(v)), "months": int(v["ym"].nunique()), "stocks": int(v["symbol"].nunique()),
        "q_f60_bp": {f"Q{i+1}": round(float(piv[i].mean() * 1e4), 1) for i in range(5)},
        "spread_bp": round(float(spread.mean() * 1e4), 1), "t": round(float(nw_t(spread)), 2),
        "positive_months": round(float((spread > 0).mean()), 2),
        "controlled_b_near_bp": round(float(rr["b_near"].mean()), 1),
        "controlled_t": round(float(nw_t(rr["b_near"])), 2),
        "controlled_b_breach_bp": round(float(rr["b_breach"].mean()), 1),
        "controlled_t_breach": round(float(nw_t(rr["b_breach"])), 2),
    }
    print("      " + json.dumps(c1, ensure_ascii=False), flush=True)

    print("[6/6] C2 解押 / C3 波动 / C4 到期 ...", flush=True)
    # C2：四组
    w = panel[panel["ok"] & panel["f60"].notna()].copy()
    w["grp"] = np.where(w["has_book"] & (w["near_share"] >= 0.5), "live_near",
               np.where(w["has_book"] & (w["near_share"] < 0.2), "live_far",
               np.where((~w["has_book"]) & w["book_l3"].fillna(False) & (w["near_l3"].fillna(0) >= 0.5), "released_near",
               np.where(~w["has_book"], "clean", "mid"))))
    gm = w.groupby(["ym", "grp"]).agg(f60=("f60", "mean"), ret60=("ret60", "mean"), n=("f60", "size")).reset_index()
    piv2 = gm.pivot(index="ym", columns="grp", values="f60")
    cnt = gm.pivot(index="ym", columns="grp", values="n")
    cells = {g: {"f60_bp": round(float(piv2[g].mean() * 1e4), 1), "ret60_pct": round(float(w[w["grp"] == g]["ret60"].mean() * 100), 1),
                 "n_months": int(piv2[g].notna().sum()), "avg_stocks": int(cnt[g].mean()) if g in cnt else 0}
             for g in piv2.columns}
    diff_rel = (piv2.get("released_near") - piv2.get("live_near")) if "released_near" in piv2 else None
    diff_rel_far = (piv2.get("released_near") - piv2.get("live_far")) if "released_near" in piv2 else None
    diff_rel_clean = (piv2.get("released_near") - piv2.get("clean")) if ("released_near" in piv2 and "clean" in piv2) else None
    c2 = {"cells": cells,
          "released_minus_live_near_bp": round(float(diff_rel.mean() * 1e4), 1) if diff_rel is not None else None,
          "t_rel_live": round(float(nw_t(diff_rel)), 2) if diff_rel is not None else None,
          "released_minus_live_far_bp": round(float(diff_rel_far.mean() * 1e4), 1) if diff_rel_far is not None else None,
          "t_rel_far": round(float(nw_t(diff_rel_far)), 2) if diff_rel_far is not None else None,
          "released_minus_clean_bp": round(float(diff_rel_clean.mean() * 1e4), 1) if diff_rel_clean is not None else None,
          "t_rel_clean": round(float(nw_t(diff_rel_clean)), 2) if diff_rel_clean is not None else None}
    print("      " + json.dumps(c2, ensure_ascii=False), flush=True)

    # C3：未来波动
    vv = panel[panel["ok"] & panel["has_book"] & panel["fv20"].notna()].copy()
    reg3 = []
    for ym_, g in vv.groupby("ym"):
        if len(g) < 80:
            continue
        X = np.column_stack([z(g["near_share"]), z(g["vol20"]), z(np.abs(g["ret20"])), z(g["turn20"]),
                             z(g["size"]), np.ones(len(g))])
        y = g["fv20"].to_numpy(float)
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 80:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg3.append({"ym": ym_, "b_near": b[0], "b_vol": b[1], "n": int(ok.sum())})
    r3 = pd.DataFrame(reg3)
    c3 = {"b_near": round(float(r3["b_near"].mean() * 100), 3), "t": round(float(nw_t(r3["b_near"])), 2),
          "b_vol": round(float(r3["b_vol"].mean() * 100), 3), "t_vol": round(float(nw_t(r3["b_vol"])), 2),
          "months": int(len(r3)), "note": "b_near 单位：near_share 每 +1sd，未来20日波动 +b 个百分点"}
    print("      " + json.dumps(c3, ensure_ascii=False), flush=True)

    # C4：贴线 × 90 天内到期
    v4 = panel[panel["ok"] & panel["has_book"] & panel["f60"].notna() & (panel["exp_share"] > 0)].copy()
    reg4 = []
    for ym_, g in v4.groupby("ym"):
        if len(g) < 60:
            continue
        X = np.column_stack([z(g["near_share"]), z(g["exp_share"]), z(g["near_share"] * g["exp_share"]),
                             z(g["vol20"]), z(g["turn20"]), z(g["size"]), z(g["D"]), np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 60:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg4.append({"ym": ym_, "b_int": b[2], "b_near": b[0], "b_exp": b[1], "n": int(ok.sum())})
    r4 = pd.DataFrame(reg4)
    c4 = {"b_interaction_bp": round(float(r4["b_int"].mean()), 1), "t": round(float(nw_t(r4["b_int"])), 2),
          "b_near_bp": round(float(r4["b_near"].mean()), 1), "t_near": round(float(nw_t(r4["b_near"])), 2),
          "b_exp_bp": round(float(r4["b_exp"].mean()), 1), "t_exp": round(float(nw_t(r4["b_exp"])), 2),
          "months": int(len(r4))}
    print("      " + json.dumps(c4, ensure_ascii=False), flush=True)

    # 描述：总量曲线
    agg = panel[panel["ok"]].groupby("ym").agg(n=("symbol", "size"),
                                               with_book=("has_book", "sum"),
                                               book_sum=("book", "sum"),
                                               near_med=("near_share", "median")).reset_index()
    out = {"meta": {"panel_start": str(dates[mids[0]].date()), "panel_end": str(dates[mids[-1]].date()),
                    "contracts": int(len(p)), "stocks": int(p["symbol"].nunique()),
                    "note": "book 单位=占总股本%；near_share∈[0,1]"},
           "C1": c1, "C2": c2, "C3": c3, "C4": c4,
           "aggregate": agg.round(4).to_dict(orient="records")}
    (OUT / "constraint_book.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[done] constraint_book.json", flush=True)


if __name__ == "__main__":
    main()
