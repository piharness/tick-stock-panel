"""收敛裁决：dist_lo250 的 +1.87pp 概率增量，折成钱还剩多少。

按 `docs/state-family-converge.md` 的 E1–E6 逐条判。不做新搜索：
处理组固定为「dist_lo250 正交化残差 Q5」，与筛查里通过 G1–G4 的那一格完全一致。

用法：python analysis/_converge_distlo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_osfe_pump import agg, base_sample, boot_ci, load_panel  # noqa: E402
from _family_screen import (CORE, ZC, build_nn, pairs_from_nn,  # noqa: E402
                            residualize, within_q)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_osfe"
L = 12
COST = 0.005  # 双边 0.5%


def main() -> None:
    df = load_panel()
    df = base_sample(df)
    for c in ZC:
        s = df.groupby("dpos", sort=False)[c]
        df[f"z_{c}"] = np.clip((df[c] - s.transform("mean"))
                               / s.transform("std").replace(0, 1.0), -3, 3)
    df = df.sort_values(["dpos", "symbol"]).reset_index(drop=True)
    df["dpos"] = df["dpos"].astype(int)
    df["adv20_q"] = within_q(df["adv20"].to_numpy(float), df["dpos"].to_numpy())
    dpos = df["dpos"].to_numpy()
    adv_q = df["adv20_q"].to_numpy()
    year = df["date"].dt.year.to_numpy()
    print(f"base rows={len(df):,} dates={df['dpos'].nunique():,}", flush=True)

    nn = build_nn(df, ZC)  # dist_lo250 不在 ZC 里 ⇒ 用标准 6 维图
    cov_ok = np.isfinite(df[[f"z_{c}" for c in ZC]].to_numpy()).all(axis=1)

    def test(cand: str, tail: int = 5, label: str = "pump30"):
        x = df[cand].to_numpy(float)
        ok = np.isfinite(x) & cov_ok
        r = residualize(df, cand)
        v = ok & np.isfinite(r)
        q = within_q(np.where(v, r, np.nan), dpos)
        treat = v & (q == tail)
        p = pairs_from_nn(nn, treat, v)
        y = df[label].to_numpy(float) if label in df.columns else None
        if y is None:
            return None
        return treat, p, y

    def delta(p, y, subset=None, win=None):
        ev, ctrl = p
        if win is not None:
            y = np.clip(y, win[0], win[1])
        if subset is not None:
            ev, ctrl = ev[subset[ev]], ctrl[subset[ev]]
        d = y[ev] - y[ctrl].mean(axis=1)
        keep = np.isfinite(d)
        ev, d = ev[keep], d[keep]
        dd = dpos[ev]
        nd = int(dpos.max()) + 1
        s = np.bincount(dd, weights=d, minlength=nd)
        n = np.bincount(dd, minlength=nd)
        m, cnt = agg(s, n)
        (lo, hi), _, _ = boot_ci(s, n, L, reps=500)
        return 100 * m, 100 * lo, 100 * hi, cnt

    print("\n=== 主判据对象：dist_lo250 残差 Q5 ===", flush=True)
    base = test("dist_lo250", 5, "pump30")
    if base is None or base[1] is None:
        print("no pairs")
        return
    treat, pairs, _ = base
    print(f"events={len(pairs[0]):,}, treated cells={int(treat.sum()):,}")

    res = {}
    print("\n-- 概率 / 幅度 / 收益 / 左尾（处理组 − 孪生 的配对差）--")
    print("   收益类同时给：全样本均值 / 1–99% 截尾均值 / 配对中位数")
    for lab in ["pump30", "pump20", "pump50", "fast30", "pump30_40", "pump30_80",
                "fwd_max60", "fwd_ret60", "fwd_ret60_oc", "fwd_min60",
                "fwd_ret40", "fwd_ret80"]:
        if lab not in df.columns:
            continue
        y = df[lab].to_numpy(float)
        m, lo, hi, n = delta(pairs, y)
        is_ret = lab.startswith(("fwd_ret", "fwd_min"))
        rec = {"delta": m, "lo": lo, "hi": hi, "n": n}
        if is_ret:
            w = np.nanpercentile(y, [1, 99])
            mt, lt, ht, _ = delta(pairs, y, win=w)
            ev, ctrl = pairs
            dd = y[ev] - y[ctrl].mean(axis=1)
            med = 100 * float(np.nanmedian(dd))
            rec.update(trim=mt, trim_lo=lt, trim_hi=ht, median=med, win=w.tolist())
            print(f"{lab:14s} 均值={m:+7.2f}pp [{lo:+.2f},{hi:+.2f}]  "
                  f"截尾={mt:+6.2f}pp [{lt:+.2f},{ht:+.2f}]  中位={med:+6.2f}pp")
        else:
            unit = "pp(max)" if "max" in lab else "pp(概率)"
            print(f"{lab:14s} Δ={m:+7.3f} {unit:9s} [{lo:+.3f}, {hi:+.3f}]  n={n:,}")
        res[lab] = rec

    print("\n-- 容量（收益侧，1–99% 截尾）--")
    y_oc = df["fwd_ret60_oc"].to_numpy(float)
    w_oc = np.nanpercentile(y_oc, [1, 99])
    cap = {}
    for q in range(1, 6):
        sub = adv_q == q
        m, lo, hi, n = delta(pairs, y_oc, subset=sub, win=w_oc)
        cap[f"q{q}"] = {"delta": m, "lo": lo, "hi": hi, "n": n}
        print(f"ADV Q{q}: ΔE[ret60_oc]={m:+.2f}pp [{lo:+.2f}, {hi:+.2f}] n={n:,}")
    m3, l3, h3, n3 = delta(pairs, y_oc, subset=adv_q >= 3, win=w_oc)
    print(f"top-3 ADV 合并: {m3:+.2f}pp [{l3:+.2f}, {h3:+.2f}] n={n3:,}")
    res["ret60_oc_top3adv"] = {"delta": m3, "lo": l3, "hi": h3, "n": n3}

    print("\n-- 稳健：替代构造（120/200/300 日低点）--")
    var = {}
    for c in ["dist_lo120", "dist_lo200", "dist_lo250", "dist_lo300"]:
        t = test(c, 5, "pump30")
        if t is None:
            continue
        _, p2, _ = t
        if p2 is None or len(p2[0]) == 0:
            print(f"{c:12s} 无有效配对，跳过")
            continue
        mp, _, _, np_ = delta(p2, df["pump30"].to_numpy(float))
        mr, lr, hr, nr = delta(p2, y_oc, win=w_oc)
        var[c] = {"pump": mp, "ret": mr}
        print(f"{c:12s} Δpump={mp:+.2f}pp  ΔE[ret_oc]={mr:+.2f}pp n={np_:,}")

    print("\n-- 稳健：分段（截尾）--")
    for nm, msk in {"2016-2021": year <= 2021, "2022-2026": year >= 2022}.items():
        mr, lr, hr, nr = delta(pairs, y_oc, subset=msk, win=w_oc)
        print(f"{nm}: ΔE[ret_oc]={mr:+.2f}pp [{lr:+.2f}, {hr:+.2f}] n={nr:,}")

    # 收益去哪了：把「拉升组」与「没拉升组」分开看
    print("\n-- 分解：多出来的拉升值不值 --")
    y_oc0 = df["fwd_ret60_oc"].to_numpy(float)
    pk = df["pump30"].to_numpy(float)
    ev0, c0_ = pairs
    tp, cp = pk[ev0] == 1, pk[c0_] == 1
    print(f"处理组: P(pump)={tp.mean():.3f}  E[ret|拉升]={100*np.nanmean(y_oc0[ev0][tp]):+.1f}%  "
          f"E[ret|未拉升]={100*np.nanmean(y_oc0[ev0][~tp]):+.1f}%  "
          f"E[ret]={100*np.nanmean(y_oc0[ev0]):+.1f}%")
    print(f"对照组: P(pump)={cp.mean():.3f}  E[ret|拉升]={100*np.nanmean(y_oc0[c0_][cp]):+.1f}%  "
          f"E[ret|未拉升]={100*np.nanmean(y_oc0[c0_][~cp]):+.1f}%  "
          f"E[ret]={100*np.nanmean(y_oc0[c0_]):+.1f}%")

    # ---- 经济换算（用截尾口径，避免单笔 10 倍走势主导结论）----
    d_ret = res["fwd_ret60_oc"].get("trim", res["fwd_ret60_oc"]["delta"])
    d_raw = res["fwd_ret60_oc"]["delta"]
    net = d_ret - 100 * COST
    ann = net * 252 / 60
    print("\n=== 经济换算（次日开盘进场，60 日持有）===")
    print(f"单次毛超额 截尾={d_ret:+.2f}pp（全样本 {d_raw:+.2f}pp）"
          f" → 扣 {100*COST:.1f}pp 双边 → 净 {net:+.2f}pp")
    print(f"常年部署（周转 252/60≈4.2 次/年）⇒ 年化 ≈ {ann:+.1f}%")
    print(f"ΔP(pump30)={res['pump30']['delta']:+.2f}pp，"
          f"每多一次拉升对应的收益 = {d_ret/max(res['pump30']['delta'],1e-9):+.2f}pp")

    gates = {
        "E1_ret60>=1.0": (d_ret >= 1.0) and res["fwd_ret60"].get(
            "trim_lo", res["fwd_ret60"]["lo"]) > 0,
        "E2_ret60_oc>=0.5": d_ret >= 0.5,
        "E3_left_tail>=-1.0": res["fwd_min60"].get("trim", res["fwd_min60"]["delta"]) >= -1.0,
        "E4_net>=0.5": net >= 0.5,
        "E5_variants>=3of4": sum(1 for v in var.values() if v["ret"] > 0) >= 3,
        "E6_top3adv>=0.5": m3 >= 0.5,
    }
    print("\n=== 裁决 ===")
    for k, v in gates.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print("  ⇒ " + ("值得单独做纸面跟踪" if all(gates.values()) else "关闭整条线"))

    (OUT / "converge_distlo.json").write_text(json.dumps(
        {"outcomes": res, "capacity": cap, "variants": var,
         "top3adv": {"delta": m3, "lo": l3, "hi": h3, "n": n3},
         "net_pp": net, "annualized_pct": ann, "gates": gates}, indent=2,
        ensure_ascii=False))
    print(f"\n→ {OUT/'converge_distlo.json'}")


if __name__ == "__main__":
    main()
