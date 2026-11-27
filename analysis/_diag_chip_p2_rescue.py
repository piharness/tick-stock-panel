"""P2：救助可达性 → 贴线反弹（国资 vs 自然人控制）。

预注册见 docs/structural-edge/prereg-rescue-p2.md（写于跑数之前）。

信号：drift_c = 公告日复权收盘 / 质押起始日复权收盘 − 1；ratio_c = 折算 / (1 + drift_c)。
分组：第一大流通股东 —— 国资关键词 = soe；自然人（is_org=0）= person；其余剔除。
判据：组内 spread = mean(f60[Q2]) − mean(f60[Q0])；Δ = spread_soe − spread_person < 0 且 NW t ≤ −2。
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
EVENTS = OUT / "pledge_drift_events.parquet"
KLINE = ROOT / "data" / "kline_daily_enriched"
TOP_FLOAT = sorted(glob.glob(str(ROOT / "data" / "shareholder" / "top_float" / "*.parquet")))[0]

STATE = re.compile(
    r"国有资产|国资委|财政局|财政部|国务院|人民政府|管委会|中央汇金|中国证券金融|国新|诚通|国家电网|南方电网"
    r"|中国石油|中国石化|中海油|中石油|中石化|中国移动|中国电信|中国联通|中国烟草|中国铁路|国铁|华润|招商局"
    r"|中粮|保利|中船|兵器|航天|航空工业|中国电子|中电科|中核|中广核|大唐|华能|华电|国家电投|三峡|中车"
    r"|中国建筑|中国铁建|中国交建|中国电建|中国能建|中国化学|中国黄金|中国铝业|五矿|中钢|中煤|神华|国家能源"
    r"|中远海运|中国国航|东方航空|南方航空|中国商飞|中国船舶|中国重工|中国中冶|华侨城|中国旅游"
    r"|上海国资|北京国资|广东国资|江苏国资|浙江国资|山东国资|四川国资|湖北国资|湖南国资|河南国资|河北国资"
    r"|福建国资|安徽国资|江西国资|陕西国资|山西国资|辽宁国资|吉林国资|黑龙江国资|云南国资|贵州国资|广西国资"
    r"|甘肃国资|青海国资|宁夏国资|新疆国资|内蒙古国资|西藏国资|海南国资|天津国资|重庆国资"
    r"|城投|城建投资|交通投资|水务集团|燃气集团|公共交通|地铁集团|机场集团|港口集团|高速公路"
)
NOMINEE = re.compile(r"香港中央结算|中央结算|中国证券金融|中央汇金")
FIN = re.compile(r"基金|保险|信托|证券|资产管理|社保|QFII|银行")


def nw_t(x: np.ndarray, lag: int = 6) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan")
    e = x - x.mean()
    s = float((e * e).mean())
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * float((e[l:] * e[:-l]).mean())
    se = np.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se) if se > 0 else float("nan")


def ownership() -> pd.DataFrame:
    tf = pd.read_parquet(TOP_FLOAT, columns=["code", "period_end", "holder_name", "rank", "is_org"])
    tf = tf[tf["rank"] == 1].sort_values("period_end").groupby("code").tail(1).copy()
    org = pd.to_numeric(tf["is_org"], errors="coerce").fillna(1).astype(int)

    def cls(name: str, is_org: int) -> str:
        n = str(name)
        if NOMINEE.search(n) or FIN.search(n):
            return "other"
        if STATE.search(n):
            return "soe"
        if is_org == 0:
            return "person"
        return "other"

    tf["own"] = [cls(n, o) for n, o in zip(tf["holder_name"], org)]
    return tf[["code", "own", "holder_name"]]


def load_events() -> pd.DataFrame:
    ev = pd.read_parquet(EVENTS)
    ev = ev[ev["f60"].notna()].copy()
    ev["notice"] = pd.to_datetime(ev["NOTICE_DATE"])
    ev["start"] = pd.to_datetime(ev["PF_START_DATE"])
    ev["code"] = ev["symbol"].str.slice(0, 6)
    syms = ev["symbol"].unique().tolist()
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "close"],
        filter=(ds.field("date") >= pd.Timestamp("2015-01-01").date()) & ds.field("symbol").isin(syms),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    A = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    di = A.index
    n = di.searchsorted(ev["notice"].values)
    s = di.searchsorted(ev["start"].values)
    ok = (s >= 0) & (n < len(di)) & (s < n)
    ev = ev[ok].reset_index(drop=True)
    n, s = n[ok], s[ok]
    j = A.columns.get_indexer(ev["symbol"])
    M = A.values
    ev["drift_c"] = M[n, j] / M[s, j] - 1.0
    ev["ratio_c"] = ev["zhesuan"] / (1.0 + ev["drift_c"])
    ev = ev[ev["drift_c"].between(-0.9, 3.0)].copy()
    ev = ev.merge(ownership(), on="code", how="left")
    ev["own"] = ev["own"].fillna("other")
    return ev


def spread_series(d: pd.DataFrame, sig: str, y: str = "f60") -> pd.Series:
    x = d.dropna(subset=[sig, y]).copy()
    if len(x) < 50:
        return pd.Series(dtype=float)
    x = x[x.groupby("ym")[sig].transform("size") >= 6].copy()
    x["q"] = x.groupby("ym")[sig].transform(lambda s: pd.qcut(s.rank(method="first"), 3, labels=[0, 1, 2]).astype(float))
    g = x.groupby(["ym", "q"])[y].mean().unstack()
    if 0 not in g.columns or 2 not in g.columns:
        return pd.Series(dtype=float)
    return (g[2] - g[0]).dropna()


def month_z(d: pd.DataFrame, col: str) -> pd.Series:
    return d.groupby("ym")[col].transform(lambda x: (x - x.mean()) / x.std())


def main() -> None:
    ev = load_events()
    out: dict = {
        "events": int(len(ev)),
        "symbols": int(ev["symbol"].nunique()),
        "groups": {k: {"events": int(v), "symbols": int(ev[ev.own == k]["symbol"].nunique())} for k, v in ev["own"].value_counts().items()},
    }

    g = {k: ev[ev["own"] == k] for k in ["soe", "person"]}
    res = {}
    for k, d in g.items():
        s = spread_series(d, "drift_c")
        res[k] = {
            "months": int(len(s)),
            "spread": round(float(s.mean() * 1e4), 1),
            "t": round(nw_t(s.to_numpy()), 2),
            "f60_deep": round(float(d[d["drift_c"] <= d["drift_c"].quantile(0.33)]["f60"].mean() * 1e4), 1),
            "f60_shallow": round(float(d[d["drift_c"] >= d["drift_c"].quantile(0.67)]["f60"].mean() * 1e4), 1),
            "size_med": round(float(d["size"].median()), 2),
            "vol_med": round(float(d["vol20"].median()), 4),
        }
    out["groupsDetail"] = res
    diff = (pd.DataFrame({"soe": spread_series(g["soe"], "drift_c"), "person": spread_series(g["person"], "drift_c")}).dropna())
    out["diff"] = {
        "months": int(len(diff)),
        "delta": round(float((diff["soe"] - diff["person"]).mean() * 1e4), 1),
        "t": round(nw_t((diff["soe"] - diff["person"]).to_numpy()), 2),
    }

    # 月截面交互：f60 ~ z(drift_c) * soe + 控制
    d = ev[ev["own"].isin(["soe", "person"])].dropna(subset=["f60", "drift_c", "size", "vol20", "turn20", "D", "ret60", "ret250"]).copy()
    d["z"] = month_z(d, "drift_c")
    d["soe"] = (d["own"] == "soe").astype(float)
    d["zi"] = d["z"] * d["soe"]
    for c in ["size", "vol20", "turn20", "D", "ret60", "ret250"]:
        d["z_" + c] = month_z(d, c)

    def run(xs: list[str]) -> list[dict]:
        co = []
        for _, gg in d.groupby("ym"):
            if len(gg) < 30:
                continue
            X = np.column_stack([np.ones(len(gg))] + [gg[c].values for c in xs])
            b = np.linalg.lstsq(X, gg["f60"].values, rcond=None)[0]
            co.append(b[1:])
        co = np.array(co)
        return [{"var": c, "bp": round(float(co[:, i].mean() * 1e4), 1), "t": round(nw_t(co[:, i]), 2)} for i, c in enumerate(xs)]

    out["interSimple"] = run(["z", "soe", "zi"])
    out["interFull"] = run(["z", "soe", "zi", "z_size", "z_vol20", "z_turn20", "z_D", "z_ret60", "z_ret250"])

    # size 三分位内
    d["sz"] = pd.qcut(d["size"].rank(method="first"), 3, labels=["小", "中", "大"])
    layers = []
    for k, sub in d.groupby("sz", observed=True):
        a = spread_series(sub[sub.own == "soe"], "drift_c")
        b = spread_series(sub[sub.own == "person"], "drift_c")
        s = pd.DataFrame({"soe": a, "person": b}).dropna()
        layers.append({"layer": str(k), "months": int(len(s)), "soe": round(float(s["soe"].mean() * 1e4), 1) if len(s) else None,
                       "person": round(float(s["person"].mean() * 1e4), 1) if len(s) else None,
                       "delta": round(float((s["soe"] - s["person"]).mean() * 1e4), 1) if len(s) else None,
                       "t": round(nw_t((s["soe"] - s["person"]).to_numpy()), 2) if len(s) else None})
    out["sizeLayers"] = layers

    # 存量簿层：has_book 的股票-月，wdist 五分位 Q5−Q1
    bp = pd.read_parquet(OUT / "constraint_book_panel.parquet")
    bp = bp[(bp["has_book"]) & (bp["ok"]) & bp["f60"].notna()].copy()
    bp["code"] = bp["symbol"].str.slice(0, 6)
    bp = bp.merge(ownership()[["code", "own"]], on="code", how="left")
    bp["own"] = bp["own"].fillna("other")
    bp = bp[bp["own"].isin(["soe", "person"])].copy()
    book = {"rows": int(len(bp)), "groups": {k: int(v) for k, v in bp["own"].value_counts().items()}, "symbols": {k: int(v) for k, v in bp.groupby("own")["symbol"].nunique().items()}}
    ser = {}
    for k, dd in bp.groupby("own"):
        dd = dd[dd.groupby("ym")["wdist"].transform("size") >= 8].copy()
        dd["q"] = dd.groupby("ym")["wdist"].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(float))
        g = dd.groupby(["ym", "q"])["f60"].mean().unstack()
        if 1 in g.columns and 5 in g.columns:
            s5 = (g[5] - g[1]).dropna()
            ser[k] = s5
            book[k] = {"months": int(len(s5)), "q5q1": round(float(s5.mean() * 1e4), 1), "t": round(nw_t(s5.to_numpy()), 2)}
    if len(ser) == 2:
        dd = pd.DataFrame({"soe": ser["soe"], "person": ser["person"]}).dropna()
        book["diff"] = {"months": int(len(dd)), "delta": round(float((dd["soe"] - dd["person"]).mean() * 1e4), 1),
                        "t": round(nw_t((dd["soe"] - dd["person"]).to_numpy()), 2)}
    out["book"] = book

    # 全市场对照：ret60 交互（没有质押簿的筛选）
    mk = pd.read_parquet(OUT / "constraint_book_panel.parquet")
    mk = mk[mk["ok"] & mk["f60"].notna()].copy()
    mk["code"] = mk["symbol"].str.slice(0, 6)
    mk = mk.merge(ownership()[["code", "own"]], on="code", how="left")
    mk["own"] = mk["own"].fillna("other")
    mk = mk[mk["own"].isin(["soe", "person"])].dropna(subset=["f60", "ret60", "size", "vol20", "turn20", "D"]).copy()
    mk["z"] = month_z(mk, "ret60")
    mk["soe"] = (mk["own"] == "soe").astype(float)
    mk["zi"] = mk["z"] * mk["soe"]
    for c in ["size", "vol20", "turn20", "D"]:
        mk["z_" + c] = month_z(mk, c)
    co = []
    for _, gg in mk.groupby("ym"):
        if len(gg) < 50:
            continue
        X = np.column_stack([np.ones(len(gg)), gg["z"], gg["soe"], gg["zi"], gg["z_size"], gg["z_vol20"], gg["z_turn20"], gg["z_D"]])
        co.append(np.linalg.lstsq(X, gg["f60"].values, rcond=None)[0][1:])
    co = np.array(co)
    out["market"] = {"rows": int(len(mk)), "zi": round(float(co[:, 2].mean() * 1e4), 1), "t": round(nw_t(co[:, 2]), 2)}

    (OUT / "p2_rescue.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
