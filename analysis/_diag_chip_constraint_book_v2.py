"""存量簿 v2：把测量缺口单独做一版，不用 E1–E5 的解释来兜底。

v1 的三个口径问题（按影响排序）
--------------------------------
G1 平仓线没复权：`OPENLINE` 是质押日的**原始价**，而日线 `close` 是**前复权价**。
   2019 年起始的合约，前复权因子中位 0.90；2015 年起始约 0.71。v1 用 raw_line / adj_close，
   老合约的距离被系统性放大 10–40% —— 足以把「贴线」推成「已穿」或「远」。
G2 入场用了合约起始日，不是公告日：`PF_START_DATE` 早于 `NOTICE_DATE`，市场在公告前不知道这份合约。
   v1 在 [start, notice) 区间用了未来信息。
G3 到期日语义混用：`已解押` 的 `UNFREEZE_DATE` 是实际解押日；`未解押` 的是计划到期日，可能展期。
   v1 对未解押合约也在计划日丢弃，且丢掉「已解押但无日期」的 32% 行。

本脚本把三个缺口拆成可开关的变体，逐一重跑 C1（near/wdist → f60）与 C3（→ 波动）：

  v1        entry=start, adj=False, extend=False, impute=None   （复现旧结果）
  v2        entry=notice, adj=True,  extend=True,  impute=None   （全修）
  v2a       entry=notice, adj=False, extend=True,  impute=None   （只修入场）
  v2b       entry=start,  adj=True,  extend=True,  impute=None   （只修复权）
  v2c       entry=notice, adj=True,  extend=True,  impute=365    （补缺失解押日）

判据（写死）
------------
- 若 v2 的受控 near/wdist 仍 |t|<2 且符号不稳 → 存量零不是口径问题，理论侧的解释成立。
- 若 v2 出现稳定负号（t≤−2，且 v2a/v2b 至少一个同号）→ v1 的零是测量缺口，强版本复活。
- 若只有 v2b（复权）翻负 → 缺口主要是 G1；若只有 v2a（入场）翻负 → 缺口主要是 G2。
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
    t = d.to_table(columns=["symbol", "date", "open", "close", "raw_close", "amount", "turnover_rate"],
                   filter=ds.field("date") >= pd.Timestamp(start).date())
    df = t.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.9)
    op = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    rc = df.pivot_table(index="date", columns="symbol", values="raw_close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="turnover_rate", aggfunc="last").sort_index()
    return op, cl.reindex(op.index), rc.reindex(op.index), am.reindex(op.index), to.reindex(op.index)


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


def build_contracts(p, dates):
    """给每份合约算：入场 idx、复权后的平仓线、到期日语义。"""
    p = p.copy()
    p["s"] = pd.to_datetime(p["PF_START_DATE"], errors="coerce")
    p["notice"] = pd.to_datetime(p["NOTICE_DATE"], errors="coerce")
    p["u"] = pd.to_datetime(p["UNFREEZE_DATE"], errors="coerce")
    p = p.dropna(subset=["s", "notice", "OPENLINE", "PF_TSR"])
    p = p[(p["OPENLINE"] > 0) & (p["PF_TSR"] > 0)]
    p["open_state"] = p["UNFREEZE_STATE"].astype(str).eq("未解押")
    # 不在这里丢弃「已解押但无日期」：留给变体决定（v1 丢、v2c 补 365 天）
    first = dates[0]
    # 起始日早于日线起点：数据左截断，无法知道真实公告日；按「2019 年初已在簿」处理，
    # 而不是用后来的补充质押公告日（那会把老合约推迟到 2026 年才入簿）。
    p["entry"] = np.where(p["s"].to_numpy() < first.to_datetime64(),
                          first.to_datetime64(),
                          np.maximum(p["s"].to_numpy(), p["notice"].to_numpy()))
    p["si"] = np.where(p["s"].to_numpy() < first.to_datetime64(), 0,
                       np.searchsorted(dates.values, p["s"].values, side="right") - 1)
    p["ni"] = np.searchsorted(dates.values, p["notice"].values, side="right") - 1
    p["ei"] = np.searchsorted(dates.values, p["entry"], side="right") - 1
    p = p[(p["ni"] >= 0) & (p["ei"] >= 0)]
    return p


def variant_book(p, dates, C, RC, sym2idx, mids, entry, adj, extend, impute):
    rows = []
    for m in mids:
        me = dates[m]
        live = (p["ei"] <= m)
        has_u = p["u"].notna()
        if not extend:
            # v1：有到期日按到期日；未解押但无日期一直活
            live &= (has_u & (p["u"] > me)) | ((~has_u) & p["open_state"])
        else:
            # v2：未解押一直活；已解押按实际解押日
            live &= p["open_state"] | (has_u & (p["u"] > me))
            if impute is not None:
                # 已解押但无日期：按 notice + impute 天补寿命
                live |= (~p["open_state"]) & (~has_u) & ((p["notice"] + pd.Timedelta(days=impute)) > me)
        q = p[live]
        if q.empty:
            continue
        px = C[m, q["idx"].to_numpy()]
        ok = np.isfinite(px) & (px > 0)
        if not ok.any():
            continue
        q = q.assign(px=px)[ok]
        if adj:
            si = q["si"].to_numpy()
            ni = q["ni"].to_numpy()
            fac = np.ones(len(q))
            good_s = (si >= 0) & (si < len(dates))
            fac[good_s] = (C[si[good_s], q["idx"].to_numpy()[good_s]] /
                           np.maximum(RC[si[good_s], q["idx"].to_numpy()[good_s]], 1e-9))
            bad = ~np.isfinite(fac) | (fac <= 0)
            if bad.any():
                fac[bad] = (C[ni[bad], q["idx"].to_numpy()[bad]] /
                            np.maximum(RC[ni[bad], q["idx"].to_numpy()[bad]], 1e-9))
            fac = np.clip(np.where(np.isfinite(fac) & (fac > 0), fac, 1.0), 0.05, 5.0)
            line_adj = q["OPENLINE"].to_numpy() * fac
        else:
            line_adj = q["OPENLINE"].to_numpy()
        q["ratio"] = line_adj / q["px"].to_numpy()
        q["tsr_n"] = q["PF_TSR"] * ((q["ratio"] >= 0.8) & (q["ratio"] <= 1.2))
        q["tsr_b"] = q["PF_TSR"] * (q["ratio"] >= 1.2)
        q["tsr_w"] = q["PF_TSR"] * np.clip(q["ratio"], 0.2, 2.0)
        g = q.groupby("symbol").agg(book=("PF_TSR", "sum"), near=("tsr_n", "sum"),
                                    breach=("tsr_b", "sum"), wnum=("tsr_w", "sum"), n=("PF_TSR", "size"))
        g["date"] = me
        g["near_share"] = g["near"] / g["book"].clip(lower=1e-9)
        g["breach_share"] = g["breach"] / g["book"].clip(lower=1e-9)
        g["wdist"] = g["wnum"] / g["book"].clip(lower=1e-9)
        rows.append(g.reset_index())
    b = pd.concat(rows, ignore_index=True)
    b["ym"] = b["date"].dt.to_period("M").astype(str)
    return b


def main():
    print("[1/6] load ...", flush=True)
    op_df, cl_df, rc_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index)
    syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32); C = cl_df.to_numpy(np.float32)
    RC = rc_df.to_numpy(np.float32); A = am_df.to_numpy(np.float32); TO = to_df.to_numpy(np.float32) / 100.0
    n, k = C.shape
    sym2idx = {s: i for i, s in enumerate(syms)}
    ym = pd.Series(dates).dt.to_period("M")
    last = pd.Series(dates).groupby(ym).max()
    mids = np.array([dates.get_loc(d) for d in last.values])
    mids = mids[(dates[mids] >= pd.Timestamp(START)) & (mids < n - 62)]
    print(f"      {n} x {k} · {len(mids)} 个月末", flush=True)

    p = pd.read_parquet(OUT / "pledge_raw_v2.parquet").drop_duplicates()
    p["code"] = p["code"].astype(str).str.zfill(6)
    p["PF_TSR"] = pd.to_numeric(p["PF_TSR"], errors="coerce")
    p["OPENLINE"] = pd.to_numeric(p["OPENLINE"], errors="coerce")
    inst = pd.read_parquet(INST)[["code", "symbol", "name"]]
    inst["code"] = inst["code"].astype(str).str.zfill(6); inst["symbol"] = inst["symbol"].astype(str)
    p = p.merge(inst, on="code", how="left").dropna(subset=["symbol"])
    p = p[p["symbol"].isin(sym2idx)]
    p["idx"] = p["symbol"].map(sym2idx)
    p = build_contracts(p, dates)
    print(f"      合约 {len(p):,} / {p['symbol'].nunique():,} 只", flush=True)

    # 结果与控制（一次备好）
    ret = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    rv20 = pd.DataFrame(ret).rolling(20).std().to_numpy()
    first = np.argmax(np.isfinite(C), axis=0)
    am20 = pd.DataFrame(A).rolling(20).mean().to_numpy()
    am60 = pd.DataFrame(A).rolling(60).mean().to_numpy()
    to20 = pd.DataFrame(TO).rolling(20).mean().to_numpy()
    age = pd.read_parquet(AGE); age["date"] = pd.to_datetime(age["date"])
    age["D"] = age["age_mean"] + age["old_share"] * 500.0
    age = age[["date", "symbol", "D"]]
    nmap = inst.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()

    recs = []
    for m in mids:
        r = pd.DataFrame({"symbol": syms, "date": dates[m], "close": C[m]})
        r["ret20"] = C[m] / np.where(C[m - 20] > 0, C[m - 20], np.nan) - 1
        r["ret60"] = C[m] / np.where(C[m - 60] > 0, C[m - 60], np.nan) - 1
        r["vol20"] = rv20[m]; r["turn20"] = to20[m]
        r["size"] = np.log(np.maximum(am20[m], 1.0)); r["amt60"] = am60[m]
        r["f20"] = C[m + 21] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        r["f60"] = C[m + 61] / np.where(O[m + 1] > 0, O[m + 1], np.nan) - 1
        r["fv20"] = np.sqrt(np.nanmean(np.square(ret[m + 1:m + 21]), axis=0))
        r["age"] = m - first
        recs.append(r)
    base = pd.concat(recs, ignore_index=True)
    base = base.merge(age, on=["date", "symbol"], how="left")
    base["nm"] = base["symbol"].map(nmap).fillna("")
    base["ok"] = (~base["nm"].str.contains("ST")) & (base["age"] >= 250) & (base["amt60"] >= 1.5e7) & (base["close"] > 0)
    base["ym"] = base["date"].dt.to_period("M").astype(str)
    print(f"      基准面板 {len(base):,} · 有效 {int(base['ok'].sum()):,}", flush=True)

    variants = {
        "v1": dict(entry="start", adj=False, extend=False, impute=None),
        "v2": dict(entry="notice", adj=True, extend=True, impute=None),
        "v2a": dict(entry="notice", adj=False, extend=True, impute=None),
        "v2b": dict(entry="start", adj=True, extend=True, impute=None),
        "v2c": dict(entry="notice", adj=True, extend=True, impute=365),
        "v2i90": dict(entry="notice", adj=True, extend=True, impute=90),
        "v2i180": dict(entry="notice", adj=True, extend=True, impute=180),
        "v2i540": dict(entry="notice", adj=True, extend=True, impute=540),
        "v2i730": dict(entry="notice", adj=True, extend=True, impute=730),
    }
    out = {"meta": {"months": len(mids), "contracts": int(len(p)), "stocks": int(p["symbol"].nunique()),
                    "note": "v1=旧口径；v2=公告日入场+平仓线复权+未解押展期；v2a=只修入场；v2b=只修复权；v2c=补缺失解押日"}}
    for name, v in variants.items():
        print(f"[run] {name} {v}", flush=True)
        b = variant_book(p, dates, C, RC, sym2idx, mids, **v)
        d = base.merge(b[["date", "symbol", "book", "near_share", "breach_share", "wdist", "n"]],
                       on=["date", "symbol"], how="left")
        d["has_book"] = d["book"].fillna(0) > 0
        for c in ["near_share", "breach_share", "wdist"]:
            d[c] = d[c].fillna(0.0)
        s = d[d["ok"] & d["has_book"] & d["f60"].notna()].copy()
        # 控制回归
        reg = []
        for ym_, g in s.groupby("ym"):
            if len(g) < 80:
                continue
            X = np.column_stack([z(g["near_share"]), z(g["wdist"]), z(g["breach_share"]), z(g["vol20"]),
                                 z(g["turn20"]), z(g["size"]), z(g["D"]), z(g["ret20"]), z(g["ret60"]), np.ones(len(g))])
            y = g["f60"].to_numpy(float) * 1e4
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 80:
                continue
            bb, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            reg.append({"ym": ym_, "b_near": bb[0], "b_w": bb[1], "b_b": bb[2]})
        r = pd.DataFrame(reg)
        # 波动（全样本 + 下跌子样本）
        vv = d[d["ok"] & d["has_book"] & d["fv20"].notna()].copy()
        vv["shock"] = vv["ret20"] < vv.groupby("ym")["ret20"].transform(lambda x: x.quantile(0.2))
        rv = []
        for ym_, g in vv.groupby("ym"):
            if len(g) < 80:
                continue
            X = np.column_stack([z(g["near_share"]), z(g["vol20"]), z(np.abs(g["ret20"])), z(g["turn20"]),
                                 z(g["size"]), np.ones(len(g))])
            y = g["fv20"].to_numpy(float)
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 80:
                continue
            bb, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            rv.append(bb[0])
        rvs = []
        for ym_, g in vv[vv["shock"]].groupby("ym"):
            if len(g) < 40:
                continue
            X = np.column_stack([z(g["near_share"]), z(g["vol20"]), z(g["turn20"]), z(g["size"]), np.ones(len(g))])
            y = g["fv20"].to_numpy(float)
            ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
            if ok.sum() < 40:
                continue
            bb, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
            rvs.append(bb[0])
        out[name] = {
            "n": int(len(s)), "months": int(s["ym"].nunique()), "stocks": int(s["symbol"].nunique()),
            "b_near_bp": round(float(r["b_near"].mean()), 1), "t_near": round(float(nw_t(r["b_near"])), 2),
            "b_wdist_bp": round(float(r["b_w"].mean()), 1), "t_wdist": round(float(nw_t(r["b_w"])), 2),
            "b_breach_bp": round(float(r["b_b"].mean()), 1), "t_breach": round(float(nw_t(r["b_b"])), 2),
            "vol_b_near_pp": round(float(np.mean(rv) * 100), 3), "vol_t_near": round(float(nw_t(pd.Series(rv))), 2),
            "shock_vol_b_near_pp": round(float(np.mean(rvs) * 100), 3), "shock_vol_t": round(float(nw_t(pd.Series(rvs))), 2),
        }
        print("      " + json.dumps(out[name], ensure_ascii=False), flush=True)

    (OUT / "constraint_book_v2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[done] constraint_book_v2.json", flush=True)


if __name__ == "__main__":
    main()
