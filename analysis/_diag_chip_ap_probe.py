"""卷五首测：把「发起」拆成三条可分离的轴——位置（年龄）/ 能力（可质押）/ 余额（浮亏）。

背景
----
卷四的唯一活口是质押公告贴线（H11），H12 把它拆成「折算（券商定价）」与「涨幅（质押后涨跌）」：
折算无效、涨幅强有效。卷五提出：定价的不是约束的位置，是「尚未支付的余额」。
若成立，则同一批事件里，三条轴对**未来收益**的解释力应当分离：

  A 位置轴：合约年龄（公告日 − 质押起始日）。越老 = 越接近典型 1–3 年期限的终点。
  B 能力轴：累计质押比例（已质押/总股本）五分位。越高 = 可抵押物越少。
  C 余额轴：质押以来涨跌（公告日价 / 质押日价 − 1）。

本脚本只做描述性分组，不新增判据，不改预注册。输出供
frontend/src/pages/chip/data.ts 的 AP_PROBE 使用。

诚实标注（读之前先看）
--------------------
1. 年龄可观测、不外生，但它只是「剩余期限」的粗代理（合约期限未披露）。
   UNFREEZE_DATE 是**结果**不是条款（股价恢复会被提前解押），只作副检验，主表不用。
2. 质押比例是「已质押/总股本」，不是「剩余可质押/流通市值」，只是能力的粗代理。
3. 全部为探索性（H11/H12 已看过全样本），不是样本外证据。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "leverage"
EVENTS = OUT / "pledge_drift_events.parquet"


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


def add(d: pd.DataFrame, axis: str, bucket: str, mask: pd.Series) -> dict:
    s = d[mask]
    g = s.groupby("ym")["exc60"].mean()
    q = s.groupby(["ym", "q"])["exc60"].mean().unstack()
    spread = (q[2] - q[0]).dropna() if (0 in q.columns and 2 in q.columns) else pd.Series(dtype=float)
    return {
        "axis": axis,
        "bucket": bucket,
        "n": int(len(s)),
        "mean": round(float(g.mean() * 1e4), 1),
        "meanT": round(nw_t(g.to_numpy()), 2),
        "spread": round(float(spread.mean() * 1e4), 1) if len(spread) else None,
        "spreadT": round(nw_t(spread.to_numpy()), 2) if len(spread) else None,
    }


def main() -> None:
    d = pd.read_parquet(EVENTS)
    d["notice"] = pd.to_datetime(d["NOTICE_DATE"])
    d["start"] = pd.to_datetime(d["PF_START_DATE"])
    d["unf"] = pd.to_datetime(d["UNFREEZE_DATE"])
    d["age"] = (d["notice"] - d["start"]).dt.days
    d["ttl"] = (d["unf"] - d["notice"]).dt.days
    d = d[(d["f60"].notna()) & (d["drift"].between(-0.5, 0.5)) & (d["age"] >= 0)].copy()
    d["exc60"] = d["f60"] - d.groupby("ym")["f60"].transform("mean")
    d["q"] = d.groupby("ym")["drift"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 3, labels=[0, 1, 2]).astype(float)
    )

    rows: list[dict] = []

    # A 位置轴：合约年龄（可观测）
    for label, lo, hi in [("<0.5 年", 0, 180), ("0.5–1 年", 180, 365), ("1–1.5 年", 365, 540),
                          ("1.5–2 年", 540, 730), (">2 年", 730, 10**6)]:
        rows.append(add(d, "age", label, (d["age"] >= lo) & (d["age"] < hi)))

    # B 能力轴：累计质押比例五分位
    d["pq"] = d.groupby("ym")["ACCUM_PLEDGE_TSR"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(float)
    )
    for k in [1, 2, 3, 4, 5]:
        rows.append(add(d, "pledge", f"Q{k}", d["pq"] == k))

    # C 余额轴：质押以来涨跌
    d["rb"] = pd.cut(
        d["drift"], [-0.5, -0.25, -0.10, 0.0, 0.10, 0.5],
        labels=["<−25%", "−25~−10%", "−10~0%", "0~+10%", ">+10%"],
    )
    for label in ["<−25%", "−25~−10%", "−10~0%", "0~+10%", ">+10%"]:
        rows.append(add(d, "drift", label, d["rb"] == label))

    # 副检验：距解押日（内生，只作对照）
    for label, mask in [("<3 月", d["ttl"] < 90), ("3–12 月", (d["ttl"] >= 90) & (d["ttl"] < 365)),
                        (">1 年", d["ttl"] >= 365), ("未解押", d["ttl"].isna())]:
        rows.append(add(d, "ttl", label, mask))

    def rng(axis: str, key: str = "mean") -> float:
        v = [r[key] for r in rows if r["axis"] == axis and r[key] is not None]
        return round(max(v) - min(v), 1)

    out = {
        "asOf": str(d["notice"].max().date()),
        "events": int(len(d)),
        "months": int(d["ym"].nunique()),
        "window": f"{d['notice'].min().date()} ~ {d['notice'].max().date()}",
        "rows": rows,
        "range": {a: rng(a) for a in ["age", "pledge", "drift", "ttl"]},
    }
    (OUT / "ap_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
