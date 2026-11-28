"""H13b：同一跌幅下，贴线还有没有增量？——最近邻匹配

H13 已显示：ratio 与 drift 同时进回归，ratio 从 +240bp/sd（t 6.93）掉到 +47bp/sd（t 0.83）。
但回归是线性的，且 ratio 与 drift 是同一枚硬币的两面（ratio ≈ 折算/(1+drift)），
线性控制不足以回答「同样跌了 30%，贴线的那只和远离平仓线的那只，未来 60 日是否不同」。

本脚本做匹配对照（与本仓库 #19 同一方法）：
  处理组 = near（ratio_c > 0.8）
  对照组 = far（ratio_c < 0.7），同月，特征最近邻
  匹配维 = drift_c（强制接近）、ret60、ret250、vol20、turn20、size、D
  估计 = 配对差 f60，按月聚簇 NW t；同时报告匹配前后协变量平衡。

输出：data/.scratch/leverage/h13b_match.json
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
FEATS = ["drift_c", "ret60", "ret250", "vol20", "turn20", "size", "D"]
# 匹配距离里 drift 权重高，其余等权；drift 另设硬门槛
W = {"drift_c": 4.0, "ret60": 1.0, "ret250": 0.5, "vol20": 1.0, "turn20": 1.0, "size": 0.5, "D": 0.5}
MAX_DRIFT_GAP = 0.05


def match_one_month(s: pd.DataFrame, caliper: float) -> pd.DataFrame:
    tr = s[s["ratio_c"] > 0.8]
    ct = s[s["ratio_c"] < 0.7]
    if len(tr) == 0 or len(ct) < 3:
        return pd.DataFrame()
    z = {}
    for c in FEATS:
        sd = s[c].std()
        z[c] = (s[c] - s[c].mean()) / (sd if sd and np.isfinite(sd) else 1.0)
    Z = pd.DataFrame(z)
    rows = []
    used: set[int] = set()
    for i, r in tr.iterrows():
        cand = ct[ct.index != i]
        if cand.empty:
            continue
        gap = (cand["drift_c"] - r["drift_c"]).abs()
        cand = cand[gap <= MAX_DRIFT_GAP]
        if cand.empty:
            continue
        dist = sum(W[c] * (Z.loc[cand.index, c] - Z.loc[i, c]) ** 2 for c in FEATS)
        dist = dist[~dist.index.isin(used)]
        if dist.empty or dist.min() > caliper:
            continue
        j = dist.idxmin()
        used.add(j)
        rows.append({"ym": r["ym"], "i": i, "j": j, "f60_i": r["f60"], "f60_j": ct.loc[j, "f60"], "d": r["f60"] - ct.loc[j, "f60"]})
    return pd.DataFrame(rows)


def balance(d: pd.DataFrame, m: pd.DataFrame) -> pd.DataFrame:
    a = d.loc[m["i"], FEATS].mean()
    b = d.loc[m["j"], FEATS].mean()
    sd = d[FEATS].std()
    return pd.DataFrame({"near": a, "far": b, "diff_sd": (a - b) / sd})


def main() -> None:
    ev = load()
    d = ev.dropna(subset=["f60", "ratio_c", "drift_c", *FEATS]).copy()
    out: dict = {"events": int(len(d)), "months": int(d["ym"].nunique())}

    for caliper in (1.5, 0.8):
        parts = [match_one_month(s, caliper) for _, s in d.groupby("ym")]
        m = pd.concat([p for p in parts if len(p)], ignore_index=True)
        if m.empty:
            out[f"caliper_{caliper}"] = {"matched": 0}
            continue
        by_month = m.groupby("ym")["d"].mean()
        out[f"caliper_{caliper}"] = {
            "matched": int(len(m)),
            "coverage": round(len(m) / max(int((d["ratio_c"] > 0.8).sum()), 1), 3),
            "bp": round(float(m["d"].mean() * 1e4), 1),
            "t": round(nw_t(by_month.to_numpy()), 2),
            "months": int(len(by_month)),
            "win": round(float((by_month > 0).mean()), 3),
        }
        if caliper == 0.8:
            out["balance"] = json.loads(balance(d, m).round(3).to_json(orient="index"))

    # 分层：drift 更深的一组，贴线是否更不重要
    d["dq"] = d.groupby("ym")["drift_c"].transform(lambda x: pd.qcut(x.rank(method="first"), 3, labels=False))
    strata = []
    for q in range(3):
        s = d[d["dq"] == q]
        parts = [match_one_month(x, 0.8) for _, x in s.groupby("ym")]
        parts = [p for p in parts if len(p)]
        if not parts:
            continue
        m = pd.concat(parts, ignore_index=True)
        if m.empty:
            continue
        bm = m.groupby("ym")["d"].mean()
        strata.append({"drift_q": q, "matched": int(len(m)), "bp": round(float(m["d"].mean() * 1e4), 1), "t": round(nw_t(bm.to_numpy()), 2)})
    out["strata"] = strata

    (OUT / "h13b_match.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
