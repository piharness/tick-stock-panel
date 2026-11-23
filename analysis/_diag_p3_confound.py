"""第 3 阶段 · 混杂检验：温和 ramp 的超额是不是「低流动性/低关注度溢价」换皮。

上一阶段从未做这个对照。项目自己的归因结论是：成交额/换手越强，20 日超额越差，
且唯一为正的长仓是流动性溢价。那么本条信号必须先证明它在流动性分层内仍然存在。

方法：同日、同 15 日窗口、按事前 ADV20 五分位做双重排序。
  raw     = 信号 ex15 − 全池等权 bench
  within  = 信号 ex15 − 同一流动性分位的全池均值   ← 扣掉流动性溢价后剩下的
复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_confound.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_p3_frontier import build_signals, load, nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "p3"
K = 15
NQ = 5


def main() -> None:
    dates, syms, op, cl, R, B, A, mkt = load()
    dates = pd.DatetimeIndex(dates)
    # 事前 ADV20（不含当日）
    amt = pd.DataFrame(A, index=dates)
    adv20 = amt.rolling(20, min_periods=15).mean().shift(1).to_numpy(float)

    sig = build_signals(dates, B, R, mkt)
    key = {(int(d), int(a)) for d, a in zip(sig.di, sig.ai)}

    rows = []
    daily = []
    for d in range(25, len(dates) - K):
        if B[d].sum() < 300:
            continue
        v = cl[d + K] / op[d + 1] - 1.0
        ok = B[d] & np.isfinite(v) & np.isfinite(adv20[d])
        if ok.sum() < 400:
            continue
        idx = np.flatnonzero(ok)
        bench = v[idx].mean()
        q = pd.qcut(pd.Series(adv20[d, idx]).rank(method="first"), NQ, labels=False).to_numpy()
        qmean = np.array([v[idx][q == k].mean() for k in range(NQ)])
        ex = v[idx] - bench
        is_sig = np.array([(d, a) in key for a in idx])
        if is_sig.any():
            w_all = ex[is_sig] - qmean[q[is_sig]]
            # 连续控制：ex15 ~ 1 + z + z^2 + sig 的横截面回归，取 sig 系数（Fama-MacBeth）
            z = np.log(adv20[d, idx])
            z = (z - z.mean()) / (z.std() + 1e-12)
            Xm = np.column_stack([np.ones(idx.size), z, z ** 2, is_sig.astype(float)])
            beta, *_ = np.linalg.lstsq(Xm, ex, rcond=None)
            daily.append({"date": dates[d], "n": int(is_sig.sum()),
                          "raw": float(ex[is_sig].mean()),
                          "within": float(w_all.mean()),
                          "fm_sig": float(beta[3])})
        for k in range(NQ):
            m = (q == k)
            rows.append({
                "date": dates[d], "q": k, "univ_mean_bp": qmean[k] * 1e4,
                "univ_n": int(m.sum()), "sig_n": int((m & is_sig).sum()),
                "raw_bp": (ex[m & is_sig].mean() * 1e4) if (m & is_sig).any() else np.nan,
                "within_bp": ((ex[m & is_sig] - qmean[k]).mean() * 1e4) if (m & is_sig).any() else np.nan,
                "adv20_yi": float(np.median(adv20[d, idx][m]) / 1e8),
            })
    e = pd.DataFrame(rows)
    e.to_csv(OUT / "confound_daily.csv", index=False)

    tab = []
    for k in range(NQ):
        g = e[(e.q == k) & e.raw_bp.notna()]
        if not len(g):
            continue
        mu_r, t_r = nw_t(g.raw_bp.to_numpy() / 1e4)
        mu_w, t_w = nw_t(g.within_bp.to_numpy() / 1e4)
        tab.append({"q": k + 1, "median_adv_yi": round(float(g.adv20_yi.median()), 2),
                    "signals": int(g.sig_n.sum()), "days": len(g),
                    "univ_mean_bp": float(g.univ_mean_bp.mean()),
                    "raw_bp": mu_r * 1e4, "t_raw": t_r,
                    "within_bp": mu_w * 1e4, "t_within": t_w})
    tab = pd.DataFrame(tab)

    dl = pd.DataFrame(daily)
    mu_r, t_r = nw_t(dl.raw.to_numpy())
    mu_w, t_w = nw_t(dl.within.to_numpy())
    mu_f, t_f = nw_t(dl.fm_sig.to_numpy())
    overall = {"signals": int(dl.n.sum()), "days": len(dl),
               "raw_bp": mu_r * 1e4, "t_raw": t_r,
               "within_bp": mu_w * 1e4, "t_within": t_w,
               "fm_alpha_bp": mu_f * 1e4, "t_fm": t_f}

    # 时间稳定性：前/后半段（逐日聚合后）
    half = []
    for name, m in (("2018-2022", dl.date.dt.year <= 2022), ("2023-2026", dl.date.dt.year > 2022)):
        gg = dl[m]
        if len(gg) < 50:
            continue
        mm, tt = nw_t(gg.within.to_numpy())
        mr, tr = nw_t(gg.raw.to_numpy())
        mf, tf = nw_t(gg.fm_sig.to_numpy())
        half.append({"period": name, "raw_bp": mr * 1e4, "t_raw": tr,
                     "within_bp": mm * 1e4, "t_within": tt,
                     "fm_alpha_bp": mf * 1e4, "t_fm": tf, "days": len(gg)})

    res = {"overall": overall, "by_quintile": tab.to_dict("records"), "halves": half}
    json.dump(res, open(OUT / "confound.json", "w"), ensure_ascii=False, indent=1, default=str)
    print(pd.DataFrame(tab).round(1).to_string(index=False))
    print("\noverall:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in overall.items()})
    print("halves:", pd.DataFrame(half).round(1).to_string(index=False))


if __name__ == "__main__":
    main()
