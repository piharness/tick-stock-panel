"""F2 大样本重跑：机构专用卖方深折价。

原 F2 的口径是「过去 60 日无大宗 + 当日最小折价那笔的卖方类型」，
把机构专用卖方压到 138 件。本脚本换成**大样本口径**：

  事件 = 任意大宗日（不再要求「60 日首次」），同股票 20 交易日冷却；
  类型优先级：机构专用卖方深折价 > 营业部卖方深折价 > 溢价 > 其他。
  区间 2012-01 ~ 2026-11（2005-2011 大宗覆盖太薄）。
  深折价 = 当日任一 Block 的 seller 含「机构专用」且 premium_doc ≤ −5%；
  营业部深折价 = seller 含「证券/营业部/总部/分公司」且 ≤ −5%；
  溢价 = 任一 Block 的 premium_doc ≥ +2%。

检验
----
R1 计数：四类事件数、按年分布。
R2 原始：月聚簇 f60 均值；inst_deep − other、broker_deep − other、inst_deep − broker_deep；NW t。
R3 控制：月截面 f60 ~ inst_deep + broker_deep + prem + ret20/60 + vol20 + turn20 + size + D_proxy；
   月均系数 + NW t。
R4 匹配：每个 inst_deep 事件，在同月、同 ret20 五分位、同 size 五分位、同 turn20 五分位里
   抽 3 个 other 事件；比较 f60；月聚簇 t。
R5 分时代：2012-2018 vs 2019-2026。

判据（写死）
------------
- R1 若 inst_deep < 500 件 → 样本仍不够，不裁决。
- R2/R3 若 inst_deep 系数 ≤ −100bp 且 |t| ≥ 2、且 broker_deep 不同号 → 符号可识别（负）。
- R4 匹配后仍负且 |t| ≥ 2 → 不是「跌得多/规模小」的组成效应。
- 若 inst_deep 与 broker_deep 同号，或匹配后归零 → 仍不可识别，F2 结论不变。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
INST = ROOT / "data" / "instruments" / "instruments.parquet"
SRC = ROOT / "data" / "kline_daily_enriched"
BT = ROOT / "data" / "block_trade"
NW_LAG = 6
START = "2012-01-01"


def load_daily(start=START):
    d = ds.dataset(SRC, format="parquet")
    t = d.to_table(columns=["symbol", "date", "open", "close", "raw_close", "amount", "turnover_rate"],
                   filter=ds.field("date") >= pd.Timestamp(start).date()).to_pandas()
    df = t
    df["date"] = pd.to_datetime(df["date"]); df["symbol"] = df["symbol"].astype("string")
    df = df.dropna(subset=["symbol", "date", "close"])
    df = df[(df["close"] > 0) & (~df["symbol"].str.endswith(".BJ"))]
    df["to"] = (df["turnover_rate"].fillna(0) / 100).clip(0, 0.9)
    op = df.pivot_table(index="date", columns="symbol", values="open", aggfunc="last").sort_index()
    cl = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    rc = df.pivot_table(index="date", columns="symbol", values="raw_close", aggfunc="last").sort_index()
    am = df.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()
    to = df.pivot_table(index="date", columns="symbol", values="to", aggfunc="last").sort_index()
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


def main():
    print("[1/5] 日线 ...", flush=True)
    op_df, cl_df, rc_df, am_df, to_df = load_daily()
    dates = pd.DatetimeIndex(op_df.index); syms = np.asarray(op_df.columns)
    O = op_df.to_numpy(np.float32); C = cl_df.to_numpy(np.float32); RC = rc_df.to_numpy(np.float32)
    A = am_df.to_numpy(np.float32); TO = to_df.to_numpy(np.float32)
    n, k = C.shape
    sym2idx = {s: i for i, s in enumerate(syms)}
    print(f"      {n} x {k} · {dates[0].date()} ~ {dates[-1].date()}", flush=True)

    print("[2/5] 大宗类型矩阵 ...", flush=True)
    b = ds.dataset(BT, format="parquet", partitioning="hive").to_table(
        columns=["date", "symbol", "sec_type", "premium_doc", "seller"],
        filter=ds.field("date") >= pd.Timestamp(START).date()).to_pandas()
    b = b[b["sec_type"] == "EQA"].copy()
    b["symbol"] = b["symbol"].astype(str)
    b = b[b["symbol"].isin(sym2idx)]
    b["date"] = pd.to_datetime(b["date"])
    b["premium_doc"] = pd.to_numeric(b["premium_doc"], errors="coerce")
    b["seller"] = b["seller"].astype(str)
    b = b.dropna(subset=["date", "symbol", "premium_doc"]).drop_duplicates()

    def stype(s):
        if "机构专用" in s:
            return "institution"
        if ("证券" in s) or ("营业部" in s) or ("总部" in s) or ("分公司" in s):
            return "broker"
        return "company"

    b["stype"] = b["seller"].map(stype)
    b["inst_deep"] = (b["stype"] == "institution") & (b["premium_doc"] <= -0.05)
    b["broker_deep"] = (b["stype"] == "broker") & (b["premium_doc"] <= -0.05)
    b["prem"] = b["premium_doc"] >= 0.02
    g = b.groupby(["date", "symbol"]).agg(inst_deep=("inst_deep", "max"), broker_deep=("broker_deep", "max"),
                                          prem=("prem", "max")).reset_index()
    print(f"      股票-日 {len(g):,} · inst_deep {int(g['inst_deep'].sum()):,} · broker_deep {int(g['broker_deep'].sum()):,}", flush=True)

    B_ANY = np.zeros((n, k), dtype=bool); ID = np.zeros((n, k), dtype=bool)
    BD = np.zeros((n, k), dtype=bool); PR = np.zeros((n, k), dtype=bool)
    ti = np.searchsorted(dates.values, g["date"].values, side="left")
    ci = g["symbol"].map(sym2idx).to_numpy()
    ok = (ti >= 0) & (ti < n)
    ti, ci = ti[ok], ci[ok]
    B_ANY[ti, ci] = True
    ID[ti, ci] = g["inst_deep"].to_numpy()[ok]
    BD[ti, ci] = g["broker_deep"].to_numpy()[ok]
    PR[ti, ci] = g["prem"].to_numpy()[ok]

    print("[3/5] 20 日冷却 + 类型 ...", flush=True)
    idx = np.arange(n, dtype=np.int32)[:, None]
    last = np.maximum.accumulate(np.where(B_ANY, idx, -1000000), axis=0)
    prev = np.vstack([np.full((1, k), -1000000, dtype=np.int32), last[:-1]])
    keep = B_ANY & ((idx - prev) > 20)
    code = np.zeros((n, k), dtype=np.int8)
    code[keep] = 4  # other
    code[keep & PR] = 3
    code[keep & BD] = 2
    code[keep & ID] = 1  # 优先级最高
    print(f"      keep {int((code > 0).sum()):,} · inst {int((code==1).sum()):,} · broker {int((code==2).sum()):,} · prem {int((code==3).sum()):,} · other {int((code==4).sum()):,}", flush=True)

    print("[4/5] 结果 + 控制 ...", flush=True)
    ret = np.clip(np.nan_to_num(np.vstack([np.zeros((1, k)), C[1:] / C[:-1] - 1.0]), nan=0.0), -0.2, 0.2)
    rv20 = pd.DataFrame(ret).rolling(20).std().to_numpy()
    am20 = pd.DataFrame(A).rolling(20).mean().to_numpy()
    am60 = pd.DataFrame(A).rolling(60).mean().to_numpy()
    to20 = pd.DataFrame(TO).rolling(20).mean().to_numpy()
    first_valid = np.argmax(np.isfinite(C), axis=0)
    inst = pd.read_parquet(INST)[["symbol", "name"]]
    nmap = inst.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()

    ev = []
    for t, i in zip(*np.nonzero(code > 0)):
        if t < 60 or t + 61 >= n:
            continue
        nm = nmap.get(syms[i], "")
        if "ST" in nm or (t - first_valid[i]) < 250 or am60[t, i] < 1.5e7:
            continue
        ev.append({
            "date": dates[t], "symbol": syms[i], "code": int(code[t, i]),
            "raw_close": RC[t, i],
            "f20": C[t + 21, i] / O[t + 1, i] - 1 if O[t + 1, i] > 0 else np.nan,
            "f60": C[t + 61, i] / O[t + 1, i] - 1 if O[t + 1, i] > 0 else np.nan,
            "ret20": C[t, i] / C[t - 20, i] - 1 if C[t - 20, i] > 0 else np.nan,
            "ret60": C[t, i] / C[t - 60, i] - 1 if C[t - 60, i] > 0 else np.nan,
            "vol20": rv20[t, i], "turn20": to20[t, i],
            "size": np.log(max(am20[t, i], 1.0)),
            "D": 1.0 / max(to20[t, i], 0.001),
        })
    e = pd.DataFrame(ev).dropna(subset=["f60", "ret20", "ret60", "vol20", "turn20", "size"])
    e["ym"] = e["date"].dt.to_period("M").astype(str)
    e["grp"] = e["code"].map({1: "inst_deep", 2: "broker_deep", 3: "prem", 4: "other"})
    e.to_parquet(OUT / "block_type_big_events.parquet", index=False)
    print(f"      样本 {len(e):,} · {e['grp'].value_counts().to_dict()}", flush=True)

    # R1
    by_year = e.groupby([e["date"].dt.year, "grp"]).size().unstack(fill_value=0)
    r1 = {str(y): {c: int(by_year.loc[y, c]) for c in by_year.columns} for y in by_year.index}

    # R2 原始
    mon = e.groupby(["ym", "grp"])["f60"].mean().unstack()
    cnt = e.groupby(["ym", "grp"]).size().unstack(fill_value=0)
    r2 = {"means_bp": {c: round(float(mon[c].mean() * 1e4), 1) for c in mon.columns},
          "months": {c: int(mon[c].notna().sum()) for c in mon.columns}}
    for a, bname in [("inst_deep", "other"), ("broker_deep", "other"), ("inst_deep", "broker_deep")]:
        if a in mon and bname in mon:
            d = (mon[a] - mon[bname]).dropna()
            r2[f"{a}_minus_{bname}"] = {"bp": round(float(d.mean() * 1e4), 1),
                                        "t": round(float(nw_t(d)), 2), "months": int(len(d))}

    # R3 控制
    reg = []
    for ym_, g in e.groupby("ym"):
        if len(g) < 30:
            continue
        X = np.column_stack([(g["grp"] == "inst_deep").to_numpy(float), (g["grp"] == "broker_deep").to_numpy(float),
                             (g["grp"] == "prem").to_numpy(float), z(g["ret20"]), z(g["ret60"]), z(g["vol20"]),
                             z(g["turn20"]), z(g["size"]), z(np.log1p(g["D"])), np.ones(len(g))])
        y = g["f60"].to_numpy(float) * 1e4
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 30:
            continue
        bb, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        reg.append({"ym": ym_, "b_inst": bb[0], "b_broker": bb[1], "b_prem": bb[2]})
    r = pd.DataFrame(reg)
    r3 = {"months": int(len(r)),
          "b_inst_bp": round(float(r["b_inst"].mean()), 1), "t_inst": round(float(nw_t(r["b_inst"])), 2),
          "b_broker_bp": round(float(r["b_broker"].mean()), 1), "t_broker": round(float(nw_t(r["b_broker"])), 2),
          "b_prem_bp": round(float(r["b_prem"].mean()), 1), "t_prem": round(float(nw_t(r["b_prem"])), 2)}

    # R4 匹配（同月 × ret20 五分位 × size 五分位 × turn20 五分位，1:3）
    e["rq"] = e.groupby("ym")["ret20"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    e["sq"] = e.groupby("ym")["size"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    e["tq"] = e.groupby("ym")["turn20"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    treat = e[e["grp"] == "inst_deep"]
    ctrl = e[e["grp"] == "other"]
    rows = []
    for _, r0 in treat.iterrows():
        pool = ctrl[(ctrl["ym"] == r0["ym"]) & (ctrl["rq"] == r0["rq"]) & (ctrl["sq"] == r0["sq"]) & (ctrl["tq"] == r0["tq"])]
        if len(pool) == 0:
            continue
        pick = pool.sample(min(3, len(pool)), random_state=int(abs(hash((r0["date"].value, r0["symbol"]))) % 2**31))
        rows.append({"ym": r0["ym"], "f_t": r0["f60"], "f_c": pick["f60"].mean()})
    m = pd.DataFrame(rows)
    r4 = {"matched_n": int(len(m)), "months": int(m["ym"].nunique()) if len(m) else 0}
    if len(m):
        m["d"] = m["f_t"] - m["f_c"]
        r4["diff_bp"] = round(float(m["d"].mean() * 1e4), 1)
        r4["t"] = round(float(nw_t(m.groupby("ym")["d"].mean())), 2)

    # R5 分时代
    r5 = {}
    for lo, hi in [("2012", "2018"), ("2019", "2026")]:
        s = e[(e["date"].dt.year >= int(lo)) & (e["date"].dt.year <= int(hi))]
        if s.empty:
            continue
        mm = s.groupby(["ym", "grp"])["f60"].mean().unstack()
        if "inst_deep" in mm and "other" in mm:
            d = (mm["inst_deep"] - mm["other"]).dropna()
            r5[f"{lo}-{hi}"] = {"bp": round(float(d.mean() * 1e4), 1), "t": round(float(nw_t(d)), 2),
                                "n_inst": int((s["grp"] == "inst_deep").sum())}

    out = {"meta": {"start": str(e["date"].min().date()), "end": str(e["date"].max().date()),
                    "events": int(len(e)), "groups": e["grp"].value_counts().to_dict(),
                    "cooldown": 20, "note": "任意大宗日 + 20 日冷却；类型优先级 inst_deep > broker_deep > prem > other"},
           "R1_by_year": r1, "R2_raw": r2, "R3_controlled": r3, "R4_matched": r4, "R5_era": r5}
    (OUT / "block_trade_type_big.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
