"""控制权变更（民营 → 国资）事件研究：现金 vs 表决权。

问题：国资拿控制权，付钱的和不付钱的，市场定价是否不同？
  现金组（协议转让/司法拍卖/定增）承担了「面值 − 市价」的余额；
  表决权组（委托/放弃）只拿控制权，不承担余额。
  若卷五的「未付账单」读法成立，现金组的事后反弹应更强。

口径：
  事件日 = 控制权变更公告日；入场 = 次日开盘；出场 = 第 k 个交易日收盘（k=20/40/60）
  超额 = 个股收益 − 全市场等权收益（同一入场/出场日）
  显著性 = 按月聚簇的 Newey-West t（事件在时间上聚集，不能按个股独立算）

输出：
  data/.scratch/state/control_change_event.json
  data/.scratch/state/control_change_event.csv（逐事件）
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "state"
HORIZONS = [20, 40, 60, 120]


def nw_t(x: np.ndarray, lag: int = 6) -> float:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return np.nan
    e = x - x.mean()
    s = (e * e).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (e[l:] * e[:-l]).mean()
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else np.nan


def build_panel():
    d = ds.dataset(str(ROOT / "data" / "kline_daily_enriched"), format="parquet")
    t = d.to_table(columns=["symbol", "date", "open", "close"]).to_pandas()
    t["date"] = pd.to_datetime(t["date"])
    O = t.pivot_table(index="date", columns="symbol", values="open").sort_index()
    C = t.pivot_table(index="date", columns="symbol", values="close").sort_index()
    return O, C


def main() -> None:
    ev = pd.read_parquet(OUT / "control_change_A.parquet")
    ev["date"] = pd.to_datetime(ev["date"])
    inst = pd.read_parquet(ROOT / "data" / "instruments" / "instruments.parquet", columns=["code", "symbol"])
    inst["code"] = inst["code"].astype(str).str.zfill(6)
    ev["code"] = ev["code"].astype(str).str.zfill(6)
    ev = ev.merge(inst.drop_duplicates("code"), on="code", how="left")
    print(f"代码映射成功 {ev['symbol'].notna().sum()}/{len(ev)}")
    ev["cls"] = np.where(
        ev["method"].isin(["协议转让", "司法拍卖", "非公开发行", "定向增发", "间接收购"]), "cash",
        np.where(ev["method"].isin(["表决权委托", "表决权放弃"]), "voting", "unknown"),
    )

    # 对照组：同一批控制权变更公告里，买方为民营的事件
    cc = pd.read_parquet(OUT / "control_change_events.parquet")
    cc["date"] = pd.to_datetime(cc["date"])
    cc["code"] = cc["code"].astype(str).str.zfill(6)
    cc = cc.merge(inst.drop_duplicates("code"), on="code", how="left")
    ctrl = cc[cc["new_type"].isin(["corp", "person"])].copy()
    ctrl["cls"] = "private"
    ctrl["method"] = ctrl["method"].astype("string")
    ev = pd.concat([
        ev.assign(symbol=ev["symbol"]),
        ctrl[["date", "code", "symbol", "name", "cls", "method", "prior_ctrl", "new_ctrl"]],
    ], ignore_index=True)

    O, C = build_panel()
    dates = O.index
    # 全市场等权：日收益的截面均值，再累乘
    mkt = C.pct_change(fill_method=None).mean(axis=1, skipna=True)
    mkt_cum = (1 + mkt.fillna(0)).cumprod()

    rows = []
    for r in ev.itertuples():
        if not isinstance(r.symbol, str) or r.symbol not in C.columns:
            continue
        i0 = dates.searchsorted(r.date)
        ent = i0 + 1
        while ent < len(dates) and not np.isfinite(O.iat[ent, O.columns.get_loc(r.symbol)]):
            ent += 1
        if ent >= len(dates) - 5:
            continue
        j = O.columns.get_loc(r.symbol)
        o = O.iat[ent, j]
        if not np.isfinite(o) or o <= 0:
            continue
        rec = {"date": r.date, "code": r.code, "name": r.name, "cls": r.cls, "method": r.method,
               "prior_ctrl": r.prior_ctrl, "new_ctrl": r.new_ctrl, "entry": dates[ent].date()}
        # 事件前 60 日涨跌（截至入场前一日）
        p0 = ent - 60
        rec["pre60"] = float(C.iat[ent - 1, j] / C.iat[p0, j] - 1) if p0 >= 0 and np.isfinite(C.iat[p0, j]) else np.nan
        for k in HORIZONS:
            ex = ent + k - 1
            if ex >= len(dates):
                rec[f"r{k}"] = np.nan
                rec[f"car{k}"] = np.nan
                continue
            c = C.iat[ex, j]
            if not np.isfinite(c):
                rec[f"r{k}"] = np.nan
                rec[f"car{k}"] = np.nan
                continue
            rr = c / o - 1
            mm = mkt_cum.iat[ex] / mkt_cum.iat[ent - 1] - 1
            rec[f"r{k}"] = float(rr)
            rec[f"car{k}"] = float(rr - mm)
        rows.append(rec)

    e = pd.DataFrame(rows)
    e.to_csv(OUT / "control_change_event.csv", index=False, encoding="utf-8-sig")

    def agg(g: pd.DataFrame, label: str) -> dict:
        out = {"group": label, "n": len(g)}
        for k in HORIZONS:
            x = g[f"car{k}"].dropna()
            if len(x) < 5:
                out[f"car{k}"] = None
                out[f"t{k}"] = None
                continue
            m = g.dropna(subset=[f"car{k}"]).assign(ym=g["date"].dt.to_period("M"))
            bym = m.groupby("ym")[f"car{k}"].mean()
            out[f"car{k}"] = round(float(x.mean() * 100), 2)
            out[f"med{k}"] = round(float(x.median() * 100), 2)
            out[f"win{k}"] = round(float((x > 0).mean() * 100), 1)
            out[f"t{k}"] = round(nw_t(bym.to_numpy()), 2)
            out[f"months{k}"] = int(len(bym))
        x = g["pre60"].dropna()
        out["pre60"] = round(float(x.mean() * 100), 2) if len(x) else None
        return out

    groups = [agg(e[e.cls == c], c) for c in ["cash", "voting", "unknown", "private"]]
    allg = agg(e[e.cls != "private"], "state_all")

    # 现金 − 表决权：同月配对差
    wide = e[e.cls != "private"].dropna(subset=["car60"]).assign(ym=e["date"].dt.to_period("M")).pivot_table(
        index="ym", columns="cls", values="car60", aggfunc="mean")
    diff = {}
    if {"cash", "voting"}.issubset(wide.columns):
        dd = (wide["cash"] - wide["voting"]).dropna()
        diff = {"car60": round(float(dd.mean() * 100), 2), "t": round(nw_t(dd.to_numpy()), 2), "months": int(len(dd))}

    ws = e.dropna(subset=["car60"]).assign(ym=e["date"].dt.to_period("M")).pivot_table(
        index="ym", columns="cls", values="car60", aggfunc="mean")
    sp = {}
    if {"cash", "private"}.issubset(ws.columns):
        d1 = (ws["cash"] - ws["private"]).dropna()
        sp = {"label": "现金 − 民营", "car60": round(float(d1.mean() * 100), 2), "t": round(nw_t(d1.to_numpy()), 2), "months": int(len(d1))}
    if {"voting", "private"}.issubset(ws.columns):
        d2 = (ws["voting"] - ws["private"]).dropna()
        sp2 = {"label": "表决权 − 民营", "car60": round(float(d2.mean() * 100), 2), "t": round(nw_t(d2.to_numpy()), 2), "months": int(len(d2))}
    else:
        sp2 = {}

    # 分年（state vs private，f60）
    yr = {}
    for name, g in [("state", e[e.cls != "private"]), ("private", e[e.cls == "private"])]:
        r = g.dropna(subset=["car60"]).groupby(g["date"].dt.year)["car60"].agg(["mean", "size"])
        r = r[r["size"] >= 5]
        yr[name] = {int(k): [round(float(v["mean"] * 100), 1), int(v["size"])] for k, v in r.iterrows()}

    res = {
        "events": int(len(ev)),
        "usable": int(len(e)),
        "coverage": {k: round(float(e[f"car{k}"].notna().mean()), 3) for k in HORIZONS},
        "all": allg,
        "groups": groups,
        "cash_minus_voting_f60": diff,
        "vs_private": [x for x in [sp, sp2] if x],
        "by_year": yr,
    }
    (OUT / "control_change_event.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"事件 {len(ev)} · 可用 {len(e)} · 覆盖 " + " ".join(f"f{k} {res['coverage'][k]:.0%}" for k in HORIZONS))
    hdr = f"{'组':<8}{'n':>5}{'pre60':>9}" + "".join(f"{'f'+str(k):>9}{'t':>7}{'胜率':>7}" for k in HORIZONS)
    print(hdr)
    for g in [allg] + groups:
        line = f"{g['group']:<8}{g['n']:>5}{g['pre60'] if g['pre60'] is not None else float('nan'):>9.1f}"
        for k in HORIZONS:
            v = g.get(f"car{k}")
            t = g.get(f"t{k}")
            w = g.get(f"win{k}")
            line += f"{v if v is not None else float('nan'):>9.1f}" + (f"{t:>7.2f}" if t is not None else f"{'—':>7}") + (f"{w:>7.1f}" if w is not None else f"{'—':>7}")
        print(line)
    if diff:
        print(f"\n现金 − 表决权 · f60：{diff['car60']:+.1f}pp · t {diff['t']} · {diff['months']} 个月")
    print("\n分年 f60（state / private，n）：")
    for y in sorted(set(res["by_year"]["state"]) | set(res["by_year"]["private"])):
        a = res["by_year"]["state"].get(y)
        b = res["by_year"]["private"].get(y)
        print(f"  {y}  state {a[0]:>7.1f} ({a[1]:>2})   private {b[0]:>7.1f} ({b[1]:>2})" if a and b else
              f"  {y}  state {a}   private {b}")
    for x in res["vs_private"]:
        print(f"{x['label']} · f60：{x['car60']:+.1f}pp · t {x['t']} · {x['months']} 个月")


if __name__ == "__main__":
    main()
