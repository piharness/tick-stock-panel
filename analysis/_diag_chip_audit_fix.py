"""审计修正：把质押事件的端点从「提取日」改回「公告日」，重跑 H11/H12 与三轴。

背景（2026-11-27 审计）
--------------------
`pledge_drift_events.parquet` 的 `drift` 用的是东财字段
`CLOSE_FORWARD_ADJPRICE_TODAY`，它**不是公告日价，是提取日（2026-11）的最新价**。
证据：
  - drift 与「最新价/质押日价−1」的中位绝对差 = 0.000；与「公告日价/质押日价−1」= 0.270；
  - 同一股票在 2020/2023/2024 三个公告日的 CLOSE_PRICE 完全相同（000661：65.65）；
  - 000661 在 2020-01-10 的 raw_close = 496.00，而 CLOSE_PRICE = 65.65。

因此旧 H11（ratio = 平仓线/最新价）、H12（涨幅 = 最新价/质押日价−1）、
预注册 pledge-drift、卷五三轴全部含公告日之后的行情，作废。

本脚本用日线面板重算：
  drift_c = adj_close(公告日) / adj_close(质押起始日) − 1      # 公告日收盘可观测
  ratio_c = 折算 / (1 + drift_c) = 平仓线 × 起始日复权因子 / 公告日复权价
入场沿用事件表：公告日后第一个交易日开盘（事件表已算好 f20/f40/f60）。

输出：data/.scratch/leverage/audit_fix.json（供 frontend/src/pages/chip/data.ts 的 AUDIT_* 使用）
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
EVENTS = OUT / "pledge_drift_events.parquet"
KLINE = ROOT / "data" / "kline_daily_enriched"


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


def load() -> pd.DataFrame:
    ev = pd.read_parquet(EVENTS)
    ev = ev[ev["f60"].notna()].copy()
    ev["notice"] = pd.to_datetime(ev["NOTICE_DATE"])
    ev["start"] = pd.to_datetime(ev["PF_START_DATE"])
    syms = ev["symbol"].unique().tolist()
    d = ds.dataset(KLINE, format="parquet")
    t = d.to_table(
        columns=["symbol", "date", "close", "raw_close"],
        filter=(ds.field("date") >= pd.Timestamp("2015-01-01").date()) & ds.field("symbol").isin(syms),
    )
    px = t.to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    A = px.pivot_table(index="date", columns="symbol", values="close").sort_index()
    P = px.pivot_table(index="date", columns="symbol", values="raw_close").sort_index()
    di = A.index
    pn = di.searchsorted(ev["notice"].values)
    ps = di.searchsorted(ev["start"].values)
    ok = (pn < len(di)) & (ps >= 0) & (ps < pn)
    ev = ev[ok].reset_index(drop=True)
    pn, ps = pn[ok], ps[ok]
    j = A.columns.get_indexer(ev["symbol"])
    An = A.values[pn, j]
    As = A.values[ps, j]
    ev["drift_c"] = An / As - 1.0
    ev["ratio_c"] = ev["zhesuan"] / (1.0 + ev["drift_c"])
    # 审计证据：旧 drift 与「最新价/起点−1」的差
    last = A.values[-1, j]
    ev["drift_to_last"] = last / As - 1.0
    return ev[ev["drift_c"].between(-0.9, 3.0)].copy()


def month_mean(d: pd.DataFrame, col: str, val: str = "f60") -> tuple[float, float, int]:
    g = d.groupby("ym")[val].mean().dropna()
    return float(g.mean() * 1e4), nw_t(g.to_numpy()), int(len(g))


def bucket(d: pd.DataFrame, col: str, edges: list[float], labels: list[str]) -> list[dict]:
    x = d.dropna(subset=[col, "f60"]).copy()
    x["b"] = pd.cut(x[col], edges, labels=labels)
    g = x.groupby(["ym", "b"], observed=True)["f60"].mean().unstack()
    out = []
    for k in labels:
        s = g[k].dropna()
        sub = x[x["b"] == k]
        row = {
            "bucket": str(k),
            "n": int(len(sub)),
            "mean": round(float(s.mean() * 1e4), 1),
            "t": round(nw_t(s.to_numpy()), 2),
        }
        for h in ["f20", "f40", "f60"]:
            gh = x.groupby(["ym", "b"])[h].mean().unstack()[k].dropna()
            row[h] = round(float(gh.mean() * 1e4), 1)
        out.append(row)
    return out


def main() -> None:
    ev = load()
    out: dict = {
        "asOf": str(ev["notice"].max().date()),
        "events": int(len(ev)),
        "months": int(ev["ym"].nunique()),
        "audit": {
            "medianAbsDiff_latest": round(float((ev["drift"] - ev["drift_to_last"]).abs().median()), 6),
            "medianAbsDiff_notice": round(float((ev["drift"] - ev["drift_c"]).abs().median()), 6),
            "corr_old_new": round(float(ev[["drift", "drift_c"]].corr().iloc[0, 1]), 3),
        },
    }

    # H11：八桶 + near−far
    out["ratioBuckets"] = bucket(
        ev, "ratio_c", [0, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.3],
        ["≤0.4", "0.4–0.5", "0.5–0.6", "0.6–0.7", "0.7–0.8", "0.8–0.9", "0.9–1.0", ">1.0"],
    )
    near = ev[ev["ratio_c"] > 0.8]
    far = ev[ev["ratio_c"] < 0.7]
    for h in ["f20", "f40", "f60"]:
        g = pd.DataFrame({"ym": ev["ym"], "grp": np.where(ev["ratio_c"] > 0.8, "near", np.where(ev["ratio_c"] < 0.7, "far", "mid")), h: ev[h]})
        w = g.groupby(["ym", "grp"])[h].mean().unstack()
        diff = (w["near"] - w["far"]).dropna()
        out.setdefault("nearFar", {})[h] = {"bp": round(float(diff.mean() * 1e4), 1), "t": round(nw_t(diff.to_numpy()), 2)}

    # H12 / 控制回归：月截面标准化系数
    cols = ["f60", "zhesuan", "drift_c", "ratio_c", "ret250", "ret60", "vol20", "turn20", "size", "D", "ym"]
    d = ev.dropna(subset=cols).copy()
    for c in cols[1:-1]:
        d["z_" + c] = d.groupby("ym")[c].transform(lambda x: (x - x.mean()) / x.std())

    def run(xs: list[str]) -> list[dict]:
        co = []
        for _, g in d.groupby("ym"):
            if len(g) < 20:
                continue
            X = np.column_stack([np.ones(len(g))] + [g["z_" + c].values for c in xs])
            b = np.linalg.lstsq(X, g["f60"].values, rcond=None)[0]
            co.append(b[1:])
        co = np.array(co)
        return [{"var": c, "bp": round(float(co[:, i].mean() * 1e4), 1), "t": round(nw_t(co[:, i]), 2)} for i, c in enumerate(xs)]

    out["regSimple"] = run(["zhesuan", "drift_c"])
    out["regFull"] = run(["zhesuan", "drift_c", "ret250", "ret60", "vol20", "turn20", "size", "D"])
    out["regRatioSimple"] = run(["ratio_c"])
    out["regRatioFull"] = run(["ratio_c", "ret250", "ret60", "vol20", "turn20", "size", "D"])

    # 三轴（修正后）
    d["age"] = (pd.to_datetime(d["notice"]) - pd.to_datetime(d["start"])).dt.days
    d["q"] = d.groupby("ym")["drift_c"].transform(lambda x: pd.qcut(x.rank(method="first"), 3, labels=[0, 1, 2]).astype(float))
    axes = []
    for label, m in [("<0.5 年", d.age < 180), ("0.5–1 年", (d.age >= 180) & (d.age < 365)), ("1–1.5 年", (d.age >= 365) & (d.age < 540)),
                     ("1.5–2 年", (d.age >= 540) & (d.age < 730)), (">2 年", d.age >= 730)]:
        s = d[m]
        g = s.groupby(["ym", "q"])["f60"].mean().unstack()
        diff = (g[2] - g[0]).dropna()
        axes.append({"axis": "age", "bucket": label, "n": int(len(s)), "spread": round(float(diff.mean() * 1e4), 1), "t": round(nw_t(diff.to_numpy()), 2)})
    d["pq"] = d.groupby("ym")["ACCUM_PLEDGE_TSR"].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(float))
    for k in [1, 2, 3, 4, 5]:
        s = d[d["pq"] == k]
        g = s.groupby(["ym", "q"])["f60"].mean().unstack()
        diff = (g[2] - g[0]).dropna()
        axes.append({"axis": "pledge", "bucket": f"Q{k}", "n": int(len(s)), "spread": round(float(diff.mean() * 1e4), 1), "t": round(nw_t(diff.to_numpy()), 2)})
    axes.extend([{"axis": "drift", **r} for r in bucket(ev, "drift_c", [-0.9, -0.25, -0.1, 0, 0.1, 3], ["<−25%", "−25~−10%", "−10~0%", "0~+10%", ">+10%"])])
    out["axes"] = axes

    # 分年
    out["byYear"] = []
    for y, g in d.groupby(pd.to_datetime(d["notice"]).dt.year):
        if len(g) < 200:
            continue
        X = np.column_stack([np.ones(len(g)), g["z_drift_c"].values])
        b = np.linalg.lstsq(X, g["f60"].values, rcond=None)[0]
        out["byYear"].append({"year": int(y), "n": int(len(g)), "bp": round(float(b[1] * 1e4), 0)})

    # H11 汇总（修正后）
    near = ev[ev["ratio_c"] > 0.8]
    far = ev[ev["ratio_c"] < 0.7]
    out["h11"] = {
        "n": int(len(ev)),
        "months": int(ev["ym"].nunique()),
        "near": int(len(near)),
        "far": int(len(far)),
        "nearF20": round(float(near["f20"].mean() * 1e4), 1),
        "nearF40": round(float(near["f40"].mean() * 1e4), 1),
        "nearF60": round(float(near["f60"].mean() * 1e4), 1),
        "farF60": round(float(far["f60"].mean() * 1e4), 1),
        "corrRatioAccum": round(float(ev["ratio_c"].rank().corr(ev["ACCUM_PLEDGE_TSR"].rank())), 2),
    }
    mids = [0.2, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95, 1.05]
    xs = np.array(mids)
    ys = np.array([r["f60"] for r in out["ratioBuckets"]])
    if np.isfinite(ys).all():
        out["h11"]["spearmanF60"] = round(float(pd.Series(xs).rank().corr(pd.Series(ys).rank())), 3)
        out["h11"]["spearmanF20"] = round(float(pd.Series(xs).rank().corr(pd.Series([r["f20"] for r in out["ratioBuckets"]]).rank())), 3)

    (OUT / "audit_fix.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
