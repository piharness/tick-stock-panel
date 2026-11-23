"""第 3 阶段 · 决策层：把候选策略放回账户，而不是放进真空。

审计的对照组是 basic_ok 等权（+2.36%/年），但账户实际的机会成本是
JB0（腿1 + 债券 B&H，7.87% / −6.91%）。同一个 8.65% 在这两个对照组下结论相反。
这里只做算术：CRRA 确定性等价 + 袖套权重 + 盈亏平衡置信度。

复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_decision.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "p3"
NAV = ROOT / "data" / ".scratch" / "p2audit" / "portfolio_nav.csv"

WIN = ("2019-12-02", "2026-11-20")
ACC = {"J0": {"cagr": 0.0663, "mdd": -0.0711}, "JB0": {"cagr": 0.0787, "mdd": -0.0691}}
ACC_SD = 0.068          # 账户逐年收益标准差（J0 yearly 8 个观测），作为 σ 的代理
CAND_ALPHA = 0.0627     # 审计的 CAPM alpha


def crra_ce(r: np.ndarray, gamma: float, per_year: int = 12) -> float:
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    if gamma == 1.0:
        ce = float(np.exp(np.mean(np.log1p(r))) - 1)
    else:
        u = np.mean((1 + r) ** (1 - gamma))
        ce = float(u ** (1 / (1 - gamma)) - 1)
    return (1 + ce) ** per_year - 1


def main() -> None:
    nav = pd.read_csv(NAV, index_col=0, parse_dates=True).loc[WIN[0]:WIN[1]]
    ret = nav.strategy.pct_change().dropna()
    m = (1 + ret).resample("ME").prod() - 1
    m = m.iloc[1:] if m.iloc[0] == 0 else m
    ann = (1 + ret).prod() ** (252 / len(ret)) - 1
    vol = ret.std(ddof=1) * np.sqrt(252)
    eq = (1 + ret).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())

    rows = {"candidate": {"cagr": ann, "vol": vol, "mdd": mdd,
                          "ce": {f"g{g}": crra_ce(m, g) for g in (1.0, 2.0, 3.0, 5.0)}}}

    # 账户 CE（用 σ 代理，CAGR 取 JB0/J0）
    for k, v in ACC.items():
        rows[k] = {"cagr": v["cagr"], "vol": ACC_SD,
                   "ce": {f"g{g}": v["cagr"] - 0.5 * g * ACC_SD ** 2 for g in (1.0, 2.0, 3.0, 5.0)}}

    # 袖套：w 配候选，其余 JB0；ρ 未知，给出 0 / 0.3 / 0.5
    blend = {}
    for rho in (0.0, 0.3, 0.5):
        out = []
        for w in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0):
            mu = w * ann + (1 - w) * ACC["JB0"]["cagr"]
            var = (w * vol) ** 2 + ((1 - w) * ACC_SD) ** 2 + 2 * w * (1 - w) * rho * vol * ACC_SD
            sd = np.sqrt(var)
            out.append({"w": w, "mu": mu, "sd": sd,
                        "ce_g1": mu - 0.5 * 1 * var, "ce_g3": mu - 0.5 * 3 * var,
                        "ce_g5": mu - 0.5 * 5 * var})
        blend[f"rho{rho}"] = out

    # 盈亏平衡置信度：在给定 w 下，α 为真的先验概率 p 使 E[CE] = CE(账户)
    be = {}
    for w in (0.1, 0.2, 0.3):
        ce0 = ACC["JB0"]["cagr"] - 0.5 * 3 * ACC_SD ** 2
        for rho in (0.0, 0.3):
            var = (w * vol) ** 2 + ((1 - w) * ACC_SD) ** 2 + 2 * w * (1 - w) * rho * vol * ACC_SD
            pen = 0.5 * 3 * var
            mu_real = w * ann + (1 - w) * ACC["JB0"]["cagr"]
            mu_null = w * (ann - CAND_ALPHA) + (1 - w) * ACC["JB0"]["cagr"]
            up, dn = mu_real - pen - ce0, mu_null - pen - ce0
            be[f"w{w}_rho{rho}"] = float(-dn / (up - dn)) if up > dn else float("nan")

    res = {"window": WIN, "months": int(len(m)), "rows": rows,
           "blend": blend, "breakeven_confidence_g3": be,
           "account_sd_proxy": ACC_SD}
    json.dump(res, open(OUT / "decision.json", "w"), ensure_ascii=False, indent=1)

    print(f"窗口 {WIN[0]}→{WIN[1]}  {len(m)} 个月")
    print(f"候选：CAGR {ann*100:.2f}%  vol {vol*100:.2f}%  MDD {mdd*100:.2f}%")
    print("\n== 确定性等价（年化）==")
    tab = pd.DataFrame(rows).T
    print(tab.to_string())
    for rho, out in blend.items():
        print(f"\n== 袖套 w × 候选（ρ={rho}）==")
        print(pd.DataFrame(out).round(4).to_string(index=False))
    print("\n== γ=3 下使袖套优于账户的盈亏平衡「α 为真」概率 ==")
    print(json.dumps({k: round(v, 3) for k, v in be.items()}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
