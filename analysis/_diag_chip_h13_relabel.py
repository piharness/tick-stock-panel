"""H13：贴线（ratio_c）是不是「跌幅」的另一个名字？

问题
----
卷五把 ratio_c = 平仓线/股价 读成一张「应付账单」，near−far f60 +525bp。
但 ratio_c 在给定折算率时是 drift_c（质押以来涨跌）的单调变换；
而 §5D 自己已经承认：三条轴里只有「余额（drift_c）」在动（668bp），
位置 100bp、能力 45bp。

如果 near−far 在 drift 桶内归零，那么「未付账单」不是新变量，
只是「买跌得多的」的另一个标签——与本仓库已关闭的短期反转同源。

检验（三层，全部月聚簇 NW t）
1. 双重排序：每月先按 drift_c 分三/五档，再在档内比 near(>0.8) vs far(<0.7)。
2. 月截面回归：f60 ~ z(ratio_c) + z(drift_c) + 控制（ret250/ret60/vol20/turn20/size/D）。
   共线时 ratio 的增量系数就是「账单」的独立信息。
3. 剂量反应：档内 ratio 五分位的 f60 是否单调。

输出：data/.scratch/leverage/h13_relabel.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_audit_fix import load, nw_t  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / ".scratch" / "leverage"


def month_diff(d: pd.DataFrame, mask_a: pd.Series, mask_b: pd.Series, col: str = "f60") -> dict:
    g = pd.DataFrame({"ym": d["ym"], "a": np.where(mask_a, 1.0, np.nan), "b": np.where(mask_b, 1.0, np.nan), col: d[col]})
    w = g.groupby("ym").apply(lambda x: x.loc[x["a"].eq(1), col].mean() - x.loc[x["b"].eq(1), col].mean(), include_groups=False)
    w = w.dropna()
    return {"bp": round(float(w.mean() * 1e4), 1), "t": round(nw_t(w.to_numpy()), 2), "months": int(len(w)), "n_a": int(mask_a.sum()), "n_b": int(mask_b.sum())}


def main() -> None:
    ev = load()
    d = ev.dropna(subset=["f60", "ratio_c", "drift_c", "ret250", "ret60", "vol20", "turn20", "size", "D"]).copy()
    out: dict = {"events": int(len(d)), "months": int(d["ym"].nunique())}

    near = d["ratio_c"] > 0.8
    far = d["ratio_c"] < 0.7
    out["pooled"] = month_diff(d, near, far)

    # 1) 双重排序：drift 三档 / 五档内 near−far
    out["within_drift"] = {}
    for k in (3, 5):
        d["dq"] = d.groupby("ym")["drift_c"].transform(lambda x: pd.qcut(x.rank(method="first"), k, labels=False))
        rows = []
        for q in range(k):
            s = d[d["dq"] == q]
            r = month_diff(s, s["ratio_c"] > 0.8, s["ratio_c"] < 0.7)
            r["drift_mid"] = round(float(s["drift_c"].median() * 100), 1)
            rows.append(r)
        out["within_drift"][f"q{k}"] = rows
    d.drop(columns=["dq"], inplace=True)

    # 2) 月截面回归：ratio 与 drift 同时进入
    cols = ["f60", "ratio_c", "drift_c", "ret250", "ret60", "vol20", "turn20", "size", "D"]
    for c in cols[1:]:
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

    out["reg"] = {
        "ratio_only": run(["ratio_c"]),
        "drift_only": run(["drift_c"]),
        "both": run(["ratio_c", "drift_c"]),
        "both_full": run(["ratio_c", "drift_c", "ret250", "ret60", "vol20", "turn20", "size", "D"]),
    }

    # 3) 档内剂量反应：drift 三档 × ratio 五分位
    d["dq"] = d.groupby("ym")["drift_c"].transform(lambda x: pd.qcut(x.rank(method="first"), 3, labels=False))
    d["rq"] = d.groupby(["ym", "dq"])["ratio_c"].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=False))
    dose = []
    for q in range(3):
        s = d[d["dq"] == q]
        g = s.groupby(["ym", "rq"])["f60"].mean().unstack()
        row = {"drift_q": q, "drift_mid": round(float(s["drift_c"].median() * 100), 1)}
        for rq in range(5):
            row[f"r{rq}"] = round(float(g[rq].mean() * 1e4), 1)
        dose.append(row)
    out["dose"] = dose

    # 4) 样本外味道：按年看 within-drift 的 near−far
    d["year"] = pd.to_datetime(d["notice"]).dt.year
    by = []
    for y, s in d.groupby("year"):
        if len(s) < 200:
            continue
        r = month_diff(s, s["ratio_c"] > 0.8, s["ratio_c"] < 0.7)
        by.append({"year": int(y), **r})
    out["by_year"] = by

    (OUT / "h13_relabel.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
