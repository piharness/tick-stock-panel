"""买点择时：启动时点的市场状态，能否改善后续 ~90 个交易日的 mu/sigma。

触发问题（2026-11-15）：用户问「2026-11-16 到 2027 Q1 末是否存在相对更好的买点，
提供收益、降低波动」。这不是一个能点预测的问题，但可以拆成三个可判定的：

  Q1 择时有效性：按启动日的状态（市场波动率分位 / 距 250 日高点回撤）分组，
     后续 H=90 日的 S1（IR / mu / sigma）是否有系统性差异？差异是否样本外仍成立？
     若没有差异 ⇒「买点」不存在，任何一天启动的期望相同。
  Q2 波动管理：不选买点，而是每天按过去 20 日已实现波动调杠杆（Moreira-Muir 型）。
     若波动率与预期收益负相关，IR 会上升；若只是等比缩放，IR 不变（只降 sigma 不降 mu）。
  Q3 季节：11 月—次年 3 月是否有月均收益上的系统性偏离（A 股「春季躁动」的传言）。
     用月分组，报每月均值与 t，不用「窗口」这种事后切法。

口径：
  - S1 = 可实现形状（底仓 1 天 + overlay），mom20 top20，gap<=-1%，20bp 成本，
    年化分母 208.4，与 `_diag_product_nav` 逐位一致（parity 断言）。
  - 市场状态只用 t 日及之前的数据：V20 = 全市场等权日收益过去 20 日的 realized vol
    （年化）；DD250 = 等权指数距过去 250 日最高点的回撤。
  - 前视防护：forward 窗口从 t+1 起算，状态 t 日收盘可得 ⇒ 决策在 t 日收盘、执行在 t+1。
  - 重叠窗口的 CI 用**按年 block bootstrap**（同年窗口一起抽），否则 CI 会窄得离谱。
  - 样本外：state 与分组边界用 <=2024 估，2025-01 之后单独报。

只读分区。ASCII 输出。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _diag_ir_lower_frontier as irf  # noqa: E402

OUT = Path(__file__).resolve().parent / "_diag_entry_timing_result.json"
GAP_MAX = -0.01
COST_BP = 20.0
P = irf.DAYS_PER_YEAR          # 208.4
H = 90                          # forward 窗口（交易日，约 4.5 个月）
SEED = 20261115
TARGET_VOL = 0.05
SPLIT = "2025-01-01"            # 样本外起点


def build_s1() -> tuple[np.ndarray, list[str], np.ndarray]:
    """S1 日序列（相对 AUM，bp）。与 _diag_product_nav 同一算法。"""
    Pp = irf.panel()
    dates, s_map, d_map = Pp["dates"], Pp["s_map"], Pp["d_map"]
    c, o = Pp["c"], Pp["o"]
    tradable, I1, T = Pp["tradable"], Pp["I1"], Pp["T"]
    s1, days, s2 = [], [], []
    for r in Pp["picks"]:
        i0 = d_map[str(r["date"])]
        if i0 + 2 >= T:
            continue
        idx = np.array([s_map[x] for x in r["symbols"] if x in s_map], dtype=np.int64)
        if idx.size < 3:
            continue
        idx = idx[tradable[i0][idx] & tradable[i0 + 1][idx]]
        if idx.size < 3:
            continue
        gp = o[i0 + 1][idx] / c[i0][idx] - 1.0
        i1 = I1[i0][idx]
        rb = c[i0 + 1][idx] / c[i0][idx] - 1.0
        ok = np.isfinite(gp) & np.isfinite(i1) & np.isfinite(rb)
        if ok.sum() < 3:
            continue
        gp, i1, rb = gp[ok], i1[ok], rb[ok]
        N = gp.size
        sel = gp <= GAP_MAX
        frac = sel.sum() / N
        base = float(rb.mean())
        ov = float(i1[sel].sum()) / N if sel.any() else 0.0
        s1.append((base + ov) / (1.0 + frac))
        s2.append(base)
        days.append(str(dates[i0 + 1]))
    return np.array(s1) * 1e4, days, np.array(s2) * 1e4


def market_series() -> tuple[np.ndarray, list[str]]:
    """全市场等权日收益与等权指数（T-1 长度，对齐 dates[1:]）。"""
    Pp = irf.panel()
    c, dates = Pp["c"], Pp["dates"]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = c[1:] / c[:-1] - 1.0
    r = np.where(np.isfinite(r) & (np.abs(r) < 0.5), r, np.nan)
    cnt = np.sum(np.isfinite(r), axis=1)
    tot = np.nansum(r, axis=1)
    rr = np.where(cnt >= 200, tot / np.maximum(cnt, 1), np.nan)
    return rr, [str(d) for d in dates[1:]]


def fwd_stats(x: np.ndarray, i: int, h: int) -> tuple[float, float, float] | None:
    w = x[i + 1:i + 1 + h]
    if w.size < h or not np.isfinite(w).all():
        return None
    sd = float(w.std(ddof=1))
    if sd <= 0:
        return None
    m = float(w.mean())
    return m / sd * math.sqrt(P), m, sd * math.sqrt(P) / 100.0


def block_ci(vals: np.ndarray, years: np.ndarray, B: int = 2000, seed: int = SEED):
    rng = np.random.default_rng(seed)
    uy = np.unique(years)
    out = np.empty(B)
    for b in range(B):
        pick = rng.choice(uy, size=uy.size, replace=True)
        idx = np.concatenate([np.flatnonzero(years == y) for y in pick])
        out[b] = vals[idx].mean()
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def main() -> None:
    t0 = time.time()
    A1, days, A2 = build_s1()
    rr, mdays = market_series()
    m_idx = {d: i for i, d in enumerate(mdays)}

    n = A1.size
    # 状态（t 日收盘可得）
    v20 = np.full(n, np.nan)
    dd250 = np.full(n, np.nan)
    for k, d in enumerate(days):
        j = m_idx.get(d)
        if j is None or j < 21:
            continue
        seg = rr[max(0, j - 20):j]
        seg = seg[np.isfinite(seg)]
        if seg.size >= 15:
            v20[k] = float(seg.std(ddof=1)) * math.sqrt(242) * 100
        lo = max(0, j - 250)
        window = np.nan_to_num(rr[lo:j + 1], nan=0.0)
        if window.size >= 60:
            eq = np.cumprod(1.0 + window)
            dd250[k] = (eq[-1] / eq.max() - 1.0) * 100

    # forward 90 日
    fwd_ir = np.full(n, np.nan)
    fwd_mu = np.full(n, np.nan)
    fwd_sd = np.full(n, np.nan)
    for i in range(n - H):
        st = fwd_stats(A1, i, H)
        if st is None:
            continue
        fwd_ir[i], fwd_mu[i], fwd_sd[i] = st

    ok = np.isfinite(v20) & np.isfinite(dd250) & np.isfinite(fwd_ir)
    years = np.array([d[:4] for d in days])
    ins = ok & (np.array(days) < SPLIT)
    oos = ok & (np.array(days) >= SPLIT)

    # 分组边界只用样本内估
    qs = np.percentile(v20[ins], [20, 40, 60, 80])
    bins = np.digitize(v20, qs)
    qd = np.percentile(dd250[ins], [20, 40, 60, 80])
    dbins = np.digitize(dd250, qd)

    def group_table(mask, b, edges, label):
        rows = []
        for g in range(5):
            m = mask & (b == g)
            if m.sum() < 30:
                rows.append({"group": g, "n": int(m.sum())})
                continue
            lo, hi = block_ci(fwd_ir[m], years[m])
            rows.append({
                "group": g,
                "n": int(m.sum()),
                "v_lo": round(float(edges[g - 1]), 2) if g > 0 else None,
                "v_hi": round(float(edges[g]), 2) if g < 4 else None,
                "fwd_ir_mean": round(float(fwd_ir[m].mean()), 2),
                "fwd_ir_ci95_block_year": [round(lo, 2), round(hi, 2)],
                "fwd_mu_bp": round(float(fwd_mu[m].mean()), 1),
                "fwd_sd_bp": round(float(fwd_sd[m].mean()), 1),
                "fwd_ir_median": round(float(np.median(fwd_ir[m])), 2),
            })
        return {"axis": label, "rows": rows}

    grp_v = group_table(ins, bins, qs, "V20_market_realized_vol_pct")
    grp_d = group_table(ins, dbins, qd, "DD250_drawdown_from_250d_high_pct")

    grp_v_oos = group_table(oos, bins, qs, "V20 (OOS >=2025)")
    grp_d_oos = group_table(oos, dbins, qd, "DD250 (OOS >=2025)")

    # 单调性检验：分组均值是否随状态单调
    def mono(rows):
        v = [r.get("fwd_ir_mean") for r in rows if r.get("fwd_ir_mean") is not None]
        if len(v) < 3:
            return None
        d = np.diff(v)
        return {"n_groups": len(v), "values": v,
                "monotone_up": bool((d > 0).all()),
                "monotone_down": bool((d < 0).all()),
                "sign_flips": int(np.sum(np.sign(d[1:]) != np.sign(d[:-1]))) if d.size > 1 else 0}

    # 当前读数
    last = np.flatnonzero(np.isfinite(v20) & np.isfinite(dd250))[-1]
    cur = {
        "as_of": days[last],
        "v20_pct_annualized": round(float(v20[last]), 2),
        "v20_percentile_in_sample": round(float((v20[ins] <= v20[last]).mean() * 100), 1),
        "dd250_pct": round(float(dd250[last]), 2),
        "dd250_percentile_in_sample": round(float((dd250[ins] <= dd250[last]).mean() * 100), 1),
        "s1_last_20d_mean_bp": round(float(A1[last - 20:last].mean()), 2),
        "s1_last_20d_sd_bp": round(float(A1[last - 20:last].std(ddof=1)), 1),
        "target_window": "2026-11-16 .. 2027-03-31",
        "trading_days_in_window_est": 90,
    }

    # 波动管理（Moreira-Muir）：t-1 的 20 日 realized vol 定杠杆
    tgt = TARGET_VOL * 1e4 / math.sqrt(P)     # 目标日波动（bp）
    vt = np.full(n, np.nan)
    lev = np.full(n, np.nan)
    for i in range(20, n):
        s = float(A1[i - 20:i].std(ddof=1))
        if s > 0:
            lev[i] = min(tgt / s, 2.0)        # 上限 2 倍，防止小样本爆炸
            vt[i] = lev[i] * A1[i]
    vm = np.isfinite(vt) & np.isfinite(A1)
    vt_ir = float(vt[vm].mean() / vt[vm].std(ddof=1) * math.sqrt(P))
    raw_ir = float(A1[vm].mean() / A1[vm].std(ddof=1) * math.sqrt(P))
    vt_net = float((vt[vm].mean() - COST_BP * float(np.nanmean(lev[vm])))
                   / vt[vm].std(ddof=1) * math.sqrt(P))
    volmgmt = {
        "target_vol_pct": TARGET_VOL * 100,
        "raw_ir_gross": round(raw_ir, 2),
        "volmanaged_ir_gross": round(vt_ir, 2),
        "raw_ir_net20": round(float((A1[vm].mean() - COST_BP) / A1[vm].std(ddof=1) * math.sqrt(P)), 2),
        "volmanaged_ir_net20": round(vt_net, 2),
        "avg_leverage": round(float(np.nanmean(lev[vm])), 3),
        "lev_p05": round(float(np.nanpercentile(lev[vm], 5)), 3),
        "lev_p95": round(float(np.nanpercentile(lev[vm], 95)), 3),
        "volmanaged_sigma_nat_pct": round(float(vt[vm].std(ddof=1)) * math.sqrt(P) / 100, 2),
        "reading": ("IR 之差 = 波动率择时的 alpha。若是 0，vol targeting 只换 sigma 不换 IR；"
                    "为正说明高波动期收益/波动比更差，值得降杠杆"),
    }

    # 季节：月分组（不做窗口切分）
    mon = np.array([int(d[5:7]) for d in days])
    seas = []
    for m in range(1, 13):
        msk = mon == m
        if msk.sum() < 50:
            continue
        mm = A1[msk]
        sd = float(mm.std(ddof=1))
        seas.append({
            "month": m, "n_days": int(msk.sum()),
            "mean_gross_bp": round(float(mm.mean()), 2),
            "sd_bp": round(sd, 1),
            "t": round(float(mm.mean()) / (sd / math.sqrt(mm.size)), 2),
            "ir_gross": round(float(mm.mean()) / sd * math.sqrt(P), 2),
        })

    # 目标窗口本身的季节：11 月下半 + 12 + 1 + 2 + 3 月
    win_mask = ((mon == 11) & (np.array([int(d[8:10]) for d in days]) >= 16)) | \
               np.isin(mon, [12, 1, 2, 3])
    win_s = A1[win_mask]
    win_stat = {
        "n_days": int(win_mask.sum()),
        "mean_gross_bp": round(float(win_s.mean()), 2),
        "sd_bp": round(float(win_s.std(ddof=1)), 1),
        "ir_gross": round(float(win_s.mean()) / float(win_s.std(ddof=1)) * math.sqrt(P), 2),
        "ir_net20": round(float(win_s.mean() - COST_BP) / float(win_s.std(ddof=1))
                          * math.sqrt(P), 2),
        "vs_all_ir_gross": round(raw_ir, 2),
        "note": "同一策略在目标日历窗与全年的对比；不是预测，是历史上这一段的表现",
    }

    res = {
        "purpose": "启动时点的市场状态能否改善后续 90 日的 mu/sigma；波动管理是否有效",
        "caliber": ("S1 可实现形状；H=90 交易日；状态 t 日收盘可得；forward 从 t+1 起；"
                    "分组边界只用 <=2024 估；CI 按年 block bootstrap"),
        "parity": {
            "s1_n_days": int(n),
            "s1_mean_gross_bp": round(float(A1.mean()), 2),
            "expect_from_product_nav": 40.82,
            "diff_bp": round(float(A1.mean()) - 40.82, 2),
            "s1_ir_net20": round(float((A1.mean() - COST_BP) / A1.std(ddof=1) * math.sqrt(P)), 2),
        },
        "n_forward_windows": {"in_sample": int(ins.sum()), "out_sample": int(oos.sum())},
        "group_by_vol": grp_v,
        "group_by_drawdown": grp_d,
        "group_by_vol_oos": grp_v_oos,
        "group_by_drawdown_oos": grp_d_oos,
        "monotonicity": {"vol": mono(grp_v["rows"]), "drawdown": mono(grp_d["rows"]),
                         "vol_oos": mono(grp_v_oos["rows"]),
                         "drawdown_oos": mono(grp_d_oos["rows"])},
        "current_reading": cur,
        "vol_management": volmgmt,
        "seasonality_by_month": seas,
        "target_window_history": win_stat,
        "elapsed_s": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[written] {OUT} elapsed={res['elapsed_s']}s")


if __name__ == "__main__":
    main()
