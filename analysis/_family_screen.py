"""状态变量族批量筛查：一次性过统一闸门，不做单假设单报告。

设计要点（预注册 `docs/state-family-screen-prereg.md`）：
  - 候选族 32 个 × 两尾 = 64 个检验，穷举、不因结果增减
  - **邻居图只算一次**（同日 × 同行业，6 维标准化特征 8 近邻），全族共用 ⇒
    64 个检验的对照口径完全一致，避免"换个匹配就跑出正数"
  - 主闸门是**正交化残差**：候选对 7 个核心变量（剔除自身）取残差后再取五分位
  - 族级多重检验用 max-t 置换（每次置换取族内最大 Δ），不是逐条 t 检验

用法：python analysis/_family_screen.py [--reps 500] [--perm 100]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_osfe_pump import agg, base_sample, boot_ci, load_panel  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_osfe"

ZC = ["log_mcap", "r20", "r60", "to20", "amp20", "dist_hi250"]
CORE = ["profit", "amp20", "to20", "log_mcap", "r20", "r60", "dist_hi250"]

FAMILY = [
    # 位置
    "r5", "r20", "r60", "r250", "dist_ma20", "dist_ma60", "dist_ma120", "dist_ma250",
    "dist_hi60", "dist_hi250", "dist_lo250", "pos_range250",
    # 波动 / 形状
    "amp20", "amp60", "vol20", "skew20", "body20", "gap20", "updn_vol20", "updays20",
    # 量能
    "to5", "to20", "to60", "to_trend", "turnover_cv20", "amt_share20", "n_bigup60",
    # 筹码
    "os", "os20", "fe", "fe20", "profit", "near_mass", "cost_gap",
]
K_NN = 8
K_USE = 3


def within_q(x: np.ndarray, dpos: np.ndarray) -> np.ndarray:
    s = pd.Series(x)
    r = s.groupby(dpos).rank(pct=True) * 5
    return np.ceil(r).clip(1, 5).to_numpy()


def residualize(df: pd.DataFrame, col: str) -> np.ndarray:
    """对 CORE 中除自身外的变量做逐日 OLS，返回残差。"""
    ctrl = [c for c in CORE if c != col]
    X = df[["z_" + c if ("z_" + c) in df.columns else c for c in ctrl]].to_numpy(float)
    y = df[col].to_numpy(float)
    out = np.full(len(df), np.nan)
    for _, idx in df.groupby("dpos", sort=False).indices.items():
        A = np.column_stack([np.ones(len(idx)), X[idx]])
        b, *_ = np.linalg.lstsq(A, y[idx], rcond=None)
        out[idx] = y[idx] - A @ b
    return out


def build_nn(df: pd.DataFrame, dims: list[str], k: int = K_NN) -> np.ndarray:
    """同日 × 同行业内，dims 维标准化特征的 k 近邻（不含自身）。

    dims 必须**剔除候选自身**：拿换手率去匹配换手率，等于问「换手相同的情况下
    换手有没有用」，估出来的不是我们要的东西。
    """
    Z = df[[f"z_{c}" for c in dims]].to_numpy(dtype=np.float32)
    codes, _ = pd.factorize(df["industry"], sort=False)
    nn = np.full((len(df), k), -1, dtype=np.int32)
    for d, idx in df.groupby("dpos", sort=False).indices.items():
        cd = codes[idx]
        for ind in np.unique(cd):
            g = idx[cd == ind]
            if len(g) < 2:
                continue
            A = Z[g]
            D = A @ A.T
            sq = np.diag(D).copy()
            D = sq[:, None] + sq[None, :] - 2.0 * D
            np.fill_diagonal(D, np.inf)
            kk = min(k, len(g) - 1)
            part = np.argpartition(D, kk - 1, axis=1)[:, :kk]
            buf = np.full((len(g), k), -1, dtype=np.int32)
            buf[:, :kk] = g[part]
            nn[g] = buf
    return nn


def pairs_from_nn(nn: np.ndarray, treat: np.ndarray, eligible: np.ndarray):
    """处理组行号 + 每行前 3 个「合法且非处理」的近邻。"""
    ev = np.flatnonzero(treat & eligible)
    if len(ev) == 0:
        return None
    nb = nn[ev]
    ok = (nb >= 0) & eligible[np.maximum(nb, 0)] & ~treat[np.maximum(nb, 0)]
    order = np.argsort(~ok, axis=1, kind="stable")[:, :K_USE]
    ctrl = np.take_along_axis(nb, order, axis=1)
    good = np.take_along_axis(ok, order, axis=1).all(axis=1)
    return ev[good], ctrl[good]


def delta_by_date(df: pd.DataFrame, pairs, y: np.ndarray, reps: int):
    ev, ctrl = pairs
    yc = y[ctrl].mean(axis=1)
    d = y[ev] - yc
    keep = np.isfinite(d)
    ev, d = ev[keep], d[keep]
    dpos = df["dpos"].to_numpy()[ev]
    nd = int(df["dpos"].max()) + 1
    s = np.bincount(dpos, weights=d, minlength=nd)
    n = np.bincount(dpos, minlength=nd)
    m, cnt = agg(s, n)
    (lo, hi), _, _ = boot_ci(s, n, 12, reps=reps)
    return m, lo, hi, cnt


def bh_q(p: np.ndarray) -> np.ndarray:
    m = len(p)
    o = np.argsort(p)
    q = np.empty(m)
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = o[rank]
        prev = min(prev, p[i] * m / (rank + 1))
        q[i] = prev
    return q


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=500)
    ap.add_argument("--perm", type=int, default=100)
    a = ap.parse_args()

    df = load_panel()
    df = base_sample(df)
    # 只用基础样本内的列做标准化（log_adv 已在 load_panel 里）
    for c in ZC:
        s = df.groupby("dpos", sort=False)[c]
        df[f"z_{c}"] = np.clip((df[c] - s.transform("mean"))
                               / s.transform("std").replace(0, 1.0), -3, 3)
    df = df.sort_values(["dpos", "symbol"]).reset_index(drop=True)
    df["dpos"] = df["dpos"].astype(int)
    df["adv20_q"] = within_q(df["adv20"].to_numpy(float), df["dpos"].to_numpy())
    print(f"base rows={len(df):,} dates={df['dpos'].nunique():,} "
          f"symbols={df['symbol'].nunique():,}", flush=True)

    y = df["pump30"].to_numpy()
    print(f"pump30 base={y.mean():.4%}", flush=True)

    print("building NN graphs (per-candidate dims minus self) ...", flush=True)
    cov_ok = np.isfinite(df[[f"z_{c}" for c in ZC]].to_numpy()).all(axis=1)
    graph_cache: dict[tuple, np.ndarray] = {}

    def get_nn(cand: str) -> np.ndarray:
        dims = tuple(c for c in ZC if c != cand)
        if dims not in graph_cache:
            graph_cache[dims] = build_nn(df, list(dims))
            print(f"  graph dims={dims} (n={len(graph_cache)})", flush=True)
        return graph_cache[dims]

    print(f"covariate-complete rows={cov_ok.sum():,}", flush=True)

    dpos = df["dpos"].to_numpy()
    adv_q = df["adv20_q"].to_numpy()
    year = df["date"].dt.year.to_numpy()

    rows = []
    masks = {}
    for cand in FAMILY:
        if cand not in df.columns:
            print(f"  !! missing {cand}", flush=True)
            continue
        x = df[cand].to_numpy(float)
        ok = np.isfinite(x) & cov_ok
        nn = get_nn(cand)
        # 对 CORE 中**除自身外**的变量取残差：对 profit 也是合法的
        resid = residualize(df, cand)
        valid_r = ok & np.isfinite(resid)
        q_raw = within_q(np.where(ok, x, np.nan), dpos)
        q_res = within_q(np.where(valid_r, resid, np.nan), dpos) if valid_r.any() else None

        for tail in (5, 1):
            t_raw = ok & (q_raw == tail)
            p_raw = pairs_from_nn(nn, t_raw, ok)
            if p_raw is None or len(p_raw[0]) < 200:
                continue
            m, lo, hi, n_ev = delta_by_date(df, p_raw, y, a.reps)
            rec = {"cand": cand, "tail": "Q5" if tail == 5 else "Q1",
                   "n_ev": n_ev, "d_raw": 100 * m, "raw_lo": 100 * lo, "raw_hi": 100 * hi}
            if q_res is not None:
                t_res = valid_r & (q_res == tail)
                p_res = pairs_from_nn(nn, t_res, valid_r)
                if p_res is not None and len(p_res[0]) >= 200:
                    mr, lor, hir, nr = delta_by_date(df, p_res, y, a.reps)
                    rec.update(d_res=100 * mr, res_lo=100 * lor, res_hi=100 * hir,
                               n_res=nr)
                    masks[(cand, tail)] = p_res
                    ev = p_res[0]
                    big = adv_q[ev] >= 3
                    if big.sum() >= 100:
                        dd = y[ev][big] - y[p_res[1]][big].mean(axis=1)
                        rec["d_top3adv"] = 100 * float(np.nanmean(dd))
                    d_des = y[ev][year[ev] <= 2021] - y[p_res[1]][year[ev] <= 2021].mean(axis=1)
                    d_oos = y[ev][year[ev] >= 2022] - y[p_res[1]][year[ev] >= 2022].mean(axis=1)
                    rec["d_design"] = 100 * float(np.nanmean(d_des)) if len(d_des) else np.nan
                    rec["d_oos"] = 100 * float(np.nanmean(d_oos)) if len(d_oos) else np.nan
                    rec["se_res"] = (rec["res_hi"] - rec["res_lo"]) / 3.92
            rows.append(rec)
            print(f"  {cand:14s} {rec['tail']} n={rec['n_ev']:>7,} "
                  f"raw={rec['d_raw']:+6.2f} res={rec.get('d_res', float('nan')):+6.2f} "
                  f"top3={rec.get('d_top3adv', float('nan')):+6.2f}", flush=True)

    res = pd.DataFrame(rows)
    res["z"] = res["d_res"] / res["se_res"].replace(0, np.nan)
    from math import erfc
    res["p"] = [erfc(abs(z) / np.sqrt(2)) if np.isfinite(z) else np.nan for z in res["z"]]
    res["q_bh"] = bh_q(res["p"].fillna(1.0).to_numpy())

    # ---- max-t 族级置换 ----
    print(f"\nmax-t 置换 {a.perm} 次（族内最大 Δ_res）...", flush=True)
    rng = np.random.default_rng(21)
    keys = list(masks.keys())
    perm_max = []
    for _ in range(a.perm):
        yp = y.copy()
        for _, idx in df.groupby("dpos", sort=False).indices.items():
            yp[idx] = yp[idx][rng.permutation(len(idx))]
        best = -np.inf
        for k in keys:
            ev, ctrl = masks[k]
            d = yp[ev] - yp[ctrl].mean(axis=1)
            best = max(best, float(np.nanmean(d)))
        perm_max.append(100 * best)
    perm_max = np.array(perm_max)
    best_obs = float(res["d_res"].max())
    p_fw = (1 + (perm_max >= best_obs).sum()) / (1 + a.perm)
    print(f"族内最大 Δ_res 实测={best_obs:+.2f}pp，置换零分布 p95={np.percentile(perm_max,95):+.2f} "
          f"max={perm_max.max():+.2f} ⇒ 族级 p={p_fw:.3f}")

    # ---- 裁决 ----
    res["G1"] = (res["d_res"] >= 1.5) & (res["res_lo"] > 0)
    res["G2"] = res.get("d_top3adv", np.nan) >= 1.5
    res["G3"] = (res["d_design"] >= 0) & (res["d_oos"] >= 0) & res["d_design"].notna()
    res["G4"] = res["p"] < 0.05
    res["verdict"] = np.where(res[["G1", "G2", "G3"]].all(axis=1) & res["G4"],
                             "存活", np.where(res["G1"], "统计可见·不可执行/不稳", "归零"))
    res = res.sort_values("d_res", ascending=False)
    show = ["cand", "tail", "n_ev", "d_raw", "d_res", "res_lo", "res_hi",
            "d_top3adv", "d_design", "d_oos", "p", "q_bh", "verdict"]
    print("\n" + res[show].to_string(index=False, float_format=lambda v: f"{v:+.2f}"))

    res.to_csv(OUT / "family_screen.csv", index=False)
    (OUT / "family_screen.json").write_text(json.dumps({
        "n_tests": int(len(res)),
        "family_max_d_res_pp": best_obs,
        "perm_max_p95_pp": float(np.percentile(perm_max, 95)),
        "family_p": float(p_fw),
        "survivors": res.loc[res["verdict"] == "存活", "cand"].tolist(),
        "visible_but_not_executable": res.loc[
            res["verdict"] == "统计可见·不可执行/不稳", "cand"].tolist(),
    }, indent=2, ensure_ascii=False))
    print(f"\n→ {OUT/'family_screen.csv'}  /  {OUT/'family_screen.json'}")


if __name__ == "__main__":
    main()
