"""第 3 阶段 · 两件事：规则的有效维度，与预注册门禁的功效。

Q1 五维条件 / 324 格邻域，真实独立维度是几维？（这是「324/324 全为正」能否算证据的前提）
Q2 预注册写死的门禁（12 个月滚动、均值>0 且 t>1）在真实日频波动下，
   通过概率是多少？80% 功效能检出的最小效应是多少？

复现：backend/.venv/Scripts/python.exe analysis/_diag_p3_gate_power.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".scratch" / "p3"
PANEL = ROOT / "data" / ".scratch" / "footprint20d" / "replay_daily.parquet"
GRIDD = ROOT / "data" / ".scratch" / "p2audit" / "threshold_grid_daily.parquet"

GRID = {
    "cum5_hi": [0.02, 0.03, 0.04, 0.05],
    "mono_lo": [0.3, 0.5, 0.7],
    "eff_lo": [0.2, 0.3, 0.4],
    "dd_lo": [-0.8, -0.6, -0.4],
    "snr_hi": [1.5, 2.0, 2.5],
}
FROZEN = (0.03, 0.5, 0.3, -0.6, 2.0)
RNG = np.random.default_rng(20261123)


def nw_t(x: np.ndarray, lag: int = 15) -> tuple[float, float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 10:
        return float("nan"), float("nan")
    mu = x.mean()
    e = x - mu
    v = e @ e / n
    for l in range(1, lag + 1):
        v += 2 * (1 - l / (lag + 1)) * (e[l:] @ e[:-l] / n)
    return float(mu), float(mu / np.sqrt(max(v, 0) / n))


def zpath(R, mkt, d):
    ex = R[d - 4:d + 1] - mkt[d - 4:d + 1, None]
    cum = np.cumsum(ex, axis=0)
    cum5 = cum[4]
    day = np.arange(1, 6, dtype=float)
    x = day - day.mean()
    y = cum - cum.mean(axis=0)
    mono = (x @ y) / np.sqrt((x @ x) * (y * y).sum(axis=0) + 1e-12)
    eff = cum5 / (np.abs(ex).sum(axis=0) + 1e-12)
    run = np.maximum.accumulate(cum, axis=0)
    dd = ((cum - run) / (np.abs(cum5) + 1e-9)).min(axis=0)
    snr = cum5 / (ex.std(axis=0) * np.sqrt(5) + 1e-9)
    return cum5, mono, eff, dd, snr


def load():
    df = pd.read_parquet(PANEL, columns=["symbol", "date", "open", "close", "ret", "basic_ok"])
    df["date"] = pd.to_datetime(df["date"])
    w = {c: df.pivot(index="date", columns="symbol", values=c).sort_index()
         for c in ["open", "close", "ret", "basic_ok"]}
    dates = pd.DatetimeIndex(w["open"].index)
    return (dates, w["open"].to_numpy(float), w["close"].to_numpy(float),
            w["ret"].to_numpy(float), w["basic_ok"].to_numpy(float) == 1)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    dates, op, cl, R, B = load()
    mkt = np.array([np.nanmean(R[i, B[i]]) if B[i].sum() > 50 else 0.0 for i in range(len(dates))])

    cells = [(a, b, c, d, e) for a in GRID["cum5_hi"] for b in GRID["mono_lo"]
             for c in GRID["eff_lo"] for d in GRID["dd_lo"] for e in GRID["snr_hi"]]
    K = 15
    rec = {name: [] for name in ("frozen_n5", "frozen_all", "relax_all")}
    cor_sum = np.zeros((5, 5))
    cor_cnt = 0
    nc = len(cells)
    inter = np.zeros(nc)
    ncell = np.zeros(nc)
    nfroz = 0
    feat_rows = []

    for d in range(5, len(dates) - K):
        if B[d].sum() < 300 or not np.isfinite(op[d + 1]).any():
            continue
        cum5, mono, eff, dd, snr = zpath(R, mkt, d)
        ok = B[d] & np.isfinite(cum5) & (eff >= -10)
        if ok.sum() < 300:
            continue
        v = cl[d + K] / op[d + 1] - 1.0
        oke = ok & np.isfinite(v)
        if oke.sum() < 300:
            continue
        ex = v - np.nanmean(v[oke])
        # 特征相关（用于有效维度）
        F = np.vstack([cum5[oke], mono[oke], eff[oke], dd[oke], snr[oke]])
        Fz = (F - F.mean(axis=1, keepdims=True)) / (F.std(axis=1, keepdims=True) + 1e-12)
        cor_sum += Fz @ Fz.T / oke.sum()
        cor_cnt += 1

        base = oke & (cum5 > 0) & (cum5 <= 0.03) & np.isfinite(ex)
        fz = base & (mono >= 0.5) & (eff >= 0.3) & (dd >= -0.6) & (snr >= 1.0) & (snr < 2.0)
        rl = base & (eff >= 0.3) & (dd >= -0.6)
        if fz.sum():
            rec["frozen_all"].append((dates[d], float(ex[fz].mean()), int(fz.sum())))
            if fz.sum() >= 5:
                rec["frozen_n5"].append((dates[d], float(ex[fz].mean()), int(fz.sum())))
        if rl.sum():
            rec["relax_all"].append((dates[d], float(ex[rl].mean()), int(rl.sum())))

        # 冻结格与 324 格的重叠（Jaccard）
        nfroz += int(fz.sum())
        for j, c in enumerate(cells):
            m = base & (cum5 <= c[0]) & (mono >= c[1]) & (eff >= c[2]) & (dd >= c[3]) & (snr < c[4])
            ncell[j] += int(m.sum())
            inter[j] += int((m & fz).sum())

    cor = cor_sum / max(cor_cnt, 1)
    names = ["cum5", "mono5", "eff5", "dd5", "snr5"]
    ev = np.linalg.eigvalsh(cor)[::-1]
    part = float(ev.sum() ** 2 / (ev ** 2).sum())
    ent = float(np.exp(-(ev / ev.sum() * np.log(ev / ev.sum() + 1e-12)).sum()))

    # 324 格日序列的相关有效秩（同一件事的另一种测量）
    X = pd.read_parquet(GRIDD).to_numpy(float)
    C = np.corrcoef(np.nan_to_num(X, nan=0.0).T)
    C = np.nan_to_num(C)
    ev2 = np.linalg.eigvalsh(C)[::-1]
    part2 = float(ev2.sum() ** 2 / (ev2 ** 2).sum())
    mean_corr = float((C[np.triu_indices_from(C, 1)]).mean())

    jac = inter / np.maximum(ncell + nfroz - inter, 1)
    tj = [cells[i] for i in range(nc) if cells[i] == FROZEN]
    jf = float(jac[[i for i in range(nc) if cells[i] == FROZEN][0]])

    # ---------- 门禁功效 ----------
    def power(daily: np.ndarray, mu_target: float, N: int, block: int = 15, nsim: int = 2000) -> float:
        x = daily[np.isfinite(daily)]
        x = x - x.mean() + mu_target
        T = x.size
        nb = int(np.ceil(N / block))
        ok = 0
        for _ in range(nsim):
            st = RNG.integers(0, T, nb)
            idx = ((st[:, None] + np.arange(block)[None, :]) % T).ravel()[:N]
            s = x[idx]
            mu, t = nw_t(s, 15)
            ok += int(mu > 0 and t > 1.0)
        return ok / nsim

    pw = {}
    series = {}
    for name, rows in rec.items():
        df = pd.DataFrame(rows, columns=["date", "ex", "n"])
        series[name] = df
        x = df.ex.to_numpy()
        pw[name] = {
            "days": int(df.date.nunique()), "avg_n": float(df.n.mean()),
            "mean_bp": float(x.mean() * 1e4), "t_nw15": nw_t(x, 15)[1],
            "sd_bp": float(x.std(ddof=1) * 1e4),
            "power": {},
        }
        for N in (252, 504, 756):
            eff = x.mean() - 0.0030
            pw[name]["power"][str(N)] = {
                "null": power(x, 0.0, N),
                "at_net30": power(x, eff, N),
                "at_gross": power(x, x.mean(), N),
            }
        # 80% 功效的 MDE（对 net30 口径）
        lo, hi = 0.0, 0.02
        for _ in range(10):
            mid = (lo + hi) / 2
            if power(x, mid, 252) < 0.8:
                lo = mid
            else:
                hi = mid
        pw[name]["mde_252_power80_bp"] = float(hi * 1e4)
        df.to_csv(OUT / f"daily_{name}.csv", index=False)

    res = {
        "feature_corr": {a: {b: round(float(cor[i, j]), 3) for j, b in enumerate(names)}
                         for i, a in enumerate(names)},
        "feature_eigs": [round(float(v), 3) for v in ev],
        "feature_participation_rank": round(part, 2),
        "feature_entropy_rank": round(ent, 2),
        "grid_mean_pair_corr": round(mean_corr, 4),
        "grid_participation_rank": round(part2, 2),
        "frozen_vs_grid_jaccard": {"frozen": round(jf, 4),
                                   "min": round(float(jac.min()), 4),
                                   "median": round(float(np.median(jac)), 4),
                                   "max": round(float(jac.max()), 4)},
        "power": pw,
    }
    json.dump(res, open(OUT / "gate_power.json", "w"), ensure_ascii=False, indent=1, default=str)

    print("== 五维特征相关 ==")
    print(pd.DataFrame(cor, index=names, columns=names).round(3).to_string())
    print(f"参与率有效秩 {part:.2f} / 熵有效秩 {ent:.2f}")
    print(f"324 格日序列：平均相关 {mean_corr:.3f}，参与率有效秩 {part2:.2f}")
    print(f"冻结格与邻域 Jaccard：自身 {jf:.3f}，min {jac.min():.3f}，median {np.median(jac):.3f}，max {jac.max():.3f}")
    print("\n== 门禁功效（12/24/36 个月，阈值 均值>0 且 NW15 t>1）==")
    for name, v in pw.items():
        print(f"-- {name}: 天数 {v['days']}, 日均信号 {v['avg_n']:.1f}, "
              f"均值 {v['mean_bp']:.1f}bp, t {v['t_nw15']:.2f}, sd {v['sd_bp']:.0f}bp")
        for N, p in v["power"].items():
            print(f"   N={N}: 零假设假通过 {p['null']:.3f} | 净30bp {p['at_net30']:.3f} | 毛 {p['at_gross']:.3f}")
        print(f"   12 个月 80% 功效 MDE = {v['mde_252_power80_bp']:.1f}bp/15d")


if __name__ == "__main__":
    main()
