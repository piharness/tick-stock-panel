"""产品净值口径：把 L1 从「增量 alpha」还原成一条可执行的净值序列。

触发问题（2026-11-15）：上一轮查出 σ 的分母没有唯一定义。
  回合口径 IR_net = 4.25（分母 = 实际投入的回合资本）
  `_diag_l1_annualize` 的 `ann_on_total_equity_inventory_equal` = 收益/2 = 2.15（分母未定义）
两者差 2 倍，而 μ≥10% 的余量在这两格之间从「松 2.1 倍」翻成「松 7%」。
本脚本用一条真实的、满足 T+1 的持仓路径，把分母钉死。

  为什么是 T+1：A 股当日买入不能当日卖出。L1 的信号是「T 日开盘买、T 日收盘卖」，
  所以收盘卖出的必须是**此前已持有**的股数。可执行的记账主体因此有三层，不是一层。

三个形状（同一批 picks、同一批日子，只改记账主体）：

  S0 `overlay_only`   只持有被选中的 M 只，open→close。**不可执行**，只作上界。
                      分母 = 回合资本（M/N 份）。这是 annualize 的 121.5bp 口径。
  S1 `base1d_overlay` 底仓 = mom20 top20 等权，**持有 1 天**（T-1 收盘买、T 日收盘卖）；
                      T 日开盘对 gap<=-1% 的加仓 1/N 份，收盘连同底仓一起卖出。
                      分母 = 日内最大占用 = 1 + M/N 份。**这是可实现的形状。**
  S2 `base1d_only`    只有底仓，无 overlay。分母 = 1 份。是「mom20 日频」的基准。

记账恒等式（必须逐位成立，脚本里断言）：
  S1 的 P&L = Σ(1/N)·(close_T/close_{T-1} − 1) + Σ_{gap<=-1%}(1/N)·(close_T/open_T − 1)
            = Σ(1/N)·(gap + i1) + Σ_sel(1/N)·i1
  分母 1 + M/N 是「T 日开盘那一刻的最大占用」：底仓 1 份还没卖，加仓又买进来。
  收盘卖掉 M/N 份后回到 1 份。用隔夜占用（1 份）当分母会系统性高估 IR 1+M/N 倍。

成本：每天全换手 ⇒ 买入金额 = 分母 ⇒ 相对 AUM 恰为一次往返，用 20bp（与 annualize 同）。

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

_N: list[int] = []

OUT = Path(__file__).resolve().parent / "_diag_product_nav_result.json"
GAP_MAX = -0.01
COST_BP = 20.0
PERIODS = irf.DAYS_PER_YEAR  # 208.4 有信号回合/年
A_STAR = 21_938_325.0        # _diag_l1_capacity s=3.52% 的每回合投入上限
SIGMA_TARGET = 0.05
MU_FLOOR = 0.10


def lower(ir: float, n: int) -> float:
    return ir - 1.959964 * math.sqrt(PERIODS * (1.0 + ir * ir / (2.0 * PERIODS)) / n)


def stats(x: np.ndarray, tag: str, denom_note: str) -> dict:
    sd = float(x.std(ddof=1))
    m = float(x.mean())
    ir = m / sd * math.sqrt(PERIODS) if sd > 0 else float("nan")
    ir_net = (m - COST_BP) / sd * math.sqrt(PERIODS) if sd > 0 else float("nan")
    return {
        "shape": tag,
        "denominator": denom_note,
        "n_days": int(x.size),
        "mean_gross_bp": round(m, 2),
        "mean_net20_bp": round(m - COST_BP, 2),
        "sd_bp": round(sd, 1),
        "sigma_nat_pct": round(sd * math.sqrt(PERIODS) / 1e4 * 100, 2),
        "ir_gross": round(ir, 2),
        "ir_net20": round(ir_net, 2),
        "lower_net_own_n": round(lower(ir_net, x.size), 2),
        "ann_net_pct": round((m - COST_BP) * PERIODS / 1e4 * 100, 1),
    }


def main() -> None:
    t0 = time.time()
    P = irf.panel()
    dates, s_map, d_map = P["dates"], P["s_map"], P["d_map"]
    c, o = P["c"], P["o"]
    tradable, I1 = P["tradable"], P["I1"]
    T = P["T"]

    s0, s1, s2, overlay_aum = [], [], [], []
    cache: list[tuple[np.ndarray, np.ndarray, np.ndarray, str]] = []
    gap_all, gap_sel, i1_all, i1_sel_mean = [], [], [], []
    global _N
    mfrac, days, _Ns = [], [], []
    n_missing = 0

    for r in P["picks"]:
        i0 = d_map[str(r["date"])]
        if i0 + 2 >= T:
            continue
        syms = [x for x in r["symbols"] if x in s_map]
        idx = np.array([s_map[x] for x in syms], dtype=np.int64)
        ok = tradable[i0][idx] & tradable[i0 + 1][idx]
        idx = idx[ok]
        if idx.size < 3:
            n_missing += 1
            continue
        N = int(idx.size)
        gp = o[i0 + 1][idx] / c[i0][idx] - 1.0
        i1 = I1[i0][idx]
        rb = c[i0 + 1][idx] / c[i0][idx] - 1.0
        good = np.isfinite(gp) & np.isfinite(i1) & np.isfinite(rb)
        if good.sum() < 3:
            n_missing += 1
            continue
        gp, i1, rb = gp[good], i1[good], rb[good]
        N = int(gp.size)

        is_sel = gp <= GAP_MAX
        M = int(is_sel.sum())
        frac = M / N

        # 恒等式自检：rb == (1+gp)*(1+i1) - 1
        recon = (1.0 + gp) * (1.0 + i1) - 1.0
        assert float(np.max(np.abs(recon - rb))) < 1e-9, "gap/i1/rbase 不自洽"

        pnl_overlay = float(i1[is_sel].sum()) / N if M else 0.0
        pnl_base = float(rb.mean())
        denom = 1.0 + frac

        # S0：overlay 自己的资金（M/N 份），报「相对回合资本」；M=0 时无定义，不入表
        if M:
            s0.append(float(i1[is_sel].mean()))
        # S1/S2 对**所有**日子成立：产品每天都持有底仓，overlay 只在有信号的日子发生。
        # 若按 M>=3 过滤，每个 gap 阈值会筛出不同的日子集合，底仓均值随之变化
        # （gap=-3% 只剩 2079 天，那些天底仓亏 -74.55bp）——那是选择效应，不是阈值效应。
        s1.append((pnl_base + pnl_overlay) / denom)
        s2.append(pnl_base)
        overlay_aum.append(pnl_overlay)

        cache.append((gp, i1, rb, str(dates[i0 + 1])))
        gap_all.append(float(gp.mean()))
        if M:
            gap_sel.append(float(gp[is_sel].mean()))
            i1_sel_mean.append(float(i1[is_sel].mean()))
        i1_all.append(float(i1.mean()))
        mfrac.append(frac)
        _Ns.append(N)
        days.append(str(dates[i0 + 1]))

    def arr(v):
        return np.array(v, dtype=float) * 1e4

    A0 = arr(s0)                     # 只有信号日（与 annualize 对拍用）
    A1, A2 = arr(s1), arr(s2)        # 产品口径 = 全部信号日
    Aov = arr(overlay_aum)           # overlay 对 AUM 的增量 P&L（无信号日 = 0）
    fr = np.array(mfrac)

    rows = [
        stats(A0, "S0_overlay_only", "回合资本 M/N 份（不可执行，上界；仅有信号日）"),
        stats(Aov, "S0_overlay_on_AUM", "总 AUM（overlay 占 M/N；全部日子）"),
        stats(A1, "S1_base1d_overlay", "日内最大占用 1+M/N 份（可实现；全部日子）"),
        stats(A2, "S2_base1d_only", "底仓 1 份（无 overlay；全部日子）"),
    ]

    # sigma=5% 三角（用 S1，唯一可实现的形状）
    s1s = stats(A1, "S1", "1+M/N")
    lev = SIGMA_TARGET / (s1s["sigma_nat_pct"] / 100.0)
    aum_cap = A_STAR / lev
    mu = s1s["ann_net_pct"] * lev
    ann_profit = aum_cap * mu / 100.0
    tri = {
        "shape": "S1",
        "sigma_nat_pct": s1s["sigma_nat_pct"],
        "leverage": round(lev, 4),
        "aum_cap_cny": round(aum_cap, 0),
        "ann_profit_cny": round(ann_profit, 0),
        "mu_pct": round(mu, 2),
        "ir_net": s1s["ir_net20"],
        "mu_over_floor": round(mu / (MU_FLOOR * 100), 2),
        "note": "AUM 上限 = A_star / L；每天买入金额 = AUM（底仓全换 + 加仓）",
    }

    # gap 网格：M/N 从 0 到 1 的整条 IR 曲线（决定「最优形状」的那一格）
    grid = []
    for g in (0.002, 0.003, 0.005, 0.0075, 0.010, 0.015, 0.020, 0.030):
        v0, v1, vs, frl = [], [], [], []
        for gp, i1, rb, _d in cache:
            sel = gp <= -g
            M = int(sel.sum())
            N = int(gp.size)
            frac = M / N
            po = float(i1[sel].sum()) / N if M else 0.0
            pb = float(rb.mean())
            if M:
                v0.append(float(i1[sel].mean()))
            v1.append((pb + po) / (1.0 + frac))
            vs.append(pb)
            frl.append(frac)
        a0, a1, as2 = arr(v0), arr(v1), arr(vs)
        st = stats(a1, "S1", "1+M/N")
        lev_g = SIGMA_TARGET / (st["sigma_nat_pct"] / 100.0)
        grid.append({
            "gap_threshold_pct": round(-g * 100, 2),
            "n_days": st["n_days"],
            "selected_frac": round(float(np.mean(frl)), 3),
            "i1_selected_bp": round(float(a0.mean()), 2),
            "base_bp": round(float(as2.mean()), 2),
            "s1_mean_gross_bp": st["mean_gross_bp"],
            "s1_ir_gross": st["ir_gross"],
            "s1_ir_net20": st["ir_net20"],
            "sigma_nat_pct": st["sigma_nat_pct"],
            "mu_at_sigma5_pct": round(st["ann_net_pct"] * lev_g, 2),
            "aum_cap_cny": round(A_STAR / lev_g, 0),
        })

    # sigma 分解：S1 的波动有多少来自底仓
    sd_b = float(A2.std(ddof=1)) / math.sqrt(1.0 + float(fr.mean()))
    sd_o = float(Aov.std(ddof=1))
    sd_t = float(A1.std(ddof=1))
    cov = (sd_t ** 2 - sd_b ** 2 - sd_o ** 2) / 2.0
    decomp = {
        "sd_total_bp": round(sd_t, 1),
        "sd_base_component_bp": round(sd_b, 1),
        "sd_overlay_component_bp": round(sd_o, 1),
        "cov_bp2": round(cov, 1),
        "corr": round(cov / (sd_b * sd_o), 3) if sd_b > 0 and sd_o > 0 else None,
        "share_variance_base": round((sd_b ** 2 + cov) / sd_t ** 2, 3),
        "why": ("底仓与 overlay 是同一批票的隔夜段与日内段，正相关 => overlay 不分散，只是加杠杆。"
                "S1 的 sigma 由底仓主导，而底仓的净收益只有 +7.8bp/日"),
    }

    # 近端子样本：与 enriched 面板起点对齐，看「最近一年半」是否更宽
    rmask = np.array([d >= "2025-06-06" for d in days])
    recent = {}
    if rmask.any():
        for tag, a in (("S1", A1[rmask]), ("S2", A2[rmask]), ("S0_on_AUM", Aov[rmask])):
            recent[tag] = stats(a, tag, "recent_2025-06-06+")

    # 成本 x sigma 敏感：mu@sigma5 = IR_net * 5% = (mean-cost)/sd * sqrt(P) * 5%
    X = math.sqrt(PERIODS) * SIGMA_TARGET
    cost_grid = []
    for c in (2.0, 5.0, 8.0, 12.0, 16.0, 20.0):
        ir_net = (s1s["mean_gross_bp"] - c) / s1s["sd_bp"] * math.sqrt(PERIODS)
        mu_c = ir_net * SIGMA_TARGET
        cost_grid.append({
            "cost_bp": c,
            "ir_net": round(ir_net, 2),
            "mu_at_sigma5_pct": round(mu_c * 100, 2),
            "ann_profit_cny": round(tri["aum_cap_cny"] * mu_c, 0),
            "meets_10pct": bool(mu_c >= MU_FLOOR),
        })
    # 反解：给定成本，要把 mu 顶到 10% 需要的 sigma_nat
    need = []
    for c in (5.0, 8.0, 12.0, 20.0):
        sd_need = (s1s["mean_gross_bp"] - c) * X / MU_FLOOR
        need.append({
            "cost_bp": c,
            "sd_needed_bp": round(sd_need, 1),
            "sd_now_bp": s1s["sd_bp"],
            "reduction_needed_pct": round((sd_need / s1s["sd_bp"] - 1.0) * 100, 1),
        })
    # 判据：这套 IR 下界>0 要多少天；>2.0 有没有解
    def days_for(ir: float, target: float = 0.0) -> float | None:
        if ir <= target:
            return None
        s = (ir - target) / 1.959964
        return PERIODS * (1.0 + ir * ir / (2.0 * PERIODS)) / (s * s)

    gate = {
        "ir_net": s1s["ir_net20"],
        "days_lower_gt0": round(days_for(s1s["ir_net20"], 0.0) or 0, 0),
        "days_lower_gt2": days_for(s1s["ir_net20"], 2.0),
        "n_days_now": s1s["n_days"],
        "verdict": ("IR_net 1.07 < 2.0 ⇒ 无论多少天，下界都不可能超过 2.0 ⇒ "
                    "「mu 下界 >= 10%」不是时间问题，是水平问题"),
    }

    # 逐年：判断「近端更好」是 alpha 抬升还是样本期效应
    import collections
    bidx = collections.defaultdict(list)
    for i, d in enumerate(days):
        bidx[d[:4]].append(i)
    by_year = []
    for y in sorted(bidx):
        idx = np.array(bidx[y], dtype=np.int64)
        if idx.size < 30:
            continue
        s1y, s2y = A1[idx], A2[idx]
        i1y = np.array([float(cache[i][1][cache[i][0] <= GAP_MAX].mean())
                        for i in idx if (cache[i][0] <= GAP_MAX).any()])
        by_year.append({
            "year": y,
            "n_days": int(idx.size),
            "s1_gross_bp": round(float(s1y.mean()), 2),
            "s1_ir_net20": round(float(s1y.mean() - COST_BP) / float(s1y.std(ddof=1))
                                 * math.sqrt(PERIODS), 2),
            "s2_gross_bp": round(float(s2y.mean()), 2),
            "s2_sd_bp": round(float(s2y.std(ddof=1)), 1),
            "i1_selected_bp": round(float(i1y.mean()) * 1e4, 2) if i1y.size else None,
        })
    quads = []
    k4 = A1.size // 4
    for q in range(4):
        sl = slice(q * k4, (q + 1) * k4) if q < 3 else slice(3 * k4, None)
        a, b = A1[sl], A2[sl]
        quads.append({
            "quarter": q + 1,
            "date_from": days[sl.start], "date_to": days[-1] if sl.stop is None else days[sl.stop - 1],
            "s1_gross_bp": round(float(a.mean()), 2),
            "s1_ir_net20": round(float(a.mean() - COST_BP) / float(a.std(ddof=1))
                                 * math.sqrt(PERIODS), 2),
            "s2_gross_bp": round(float(b.mean()), 2),
        })

    # 与 annualize 的 /2 口径对拍
    parity = {
        "annualize_total_equity_field_bp": 105.74 * 100.0 / PERIODS / 2.0,
        "S1_mean_gross_bp": s1s["mean_gross_bp"],
        "gap_bp": round(s1s["mean_gross_bp"] - 105.74 * 100.0 / PERIODS / 2.0, 2),
        "annualize_ir_implied": 2.15,
        "S1_ir_gross": s1s["ir_gross"],
        "note": ("annualize 的 /2 字段只把收益减半、波动未动，隐含「底仓零收益零波动」；"
                 "S1 是同一件事的显式算法：底仓收益进分子、隔夜波动进分母、占用进分母"),
    }

    res = {
        "purpose": "把 L1 从增量 alpha 还原成可执行的产品净值序列，钉死 sigma 的分母",
        "caliber": ("mom20 top20 等权；gap=open[d+1]/close[d]-1；i1=open->close；"
                    "成本 20bp/回合；年化分母 208.4 回合/年"),
        "shapes": rows,
        "sigma_triangle_S1": tri,
        "parity_vs_annualize_half": parity,
        "sigma_decomposition": decomp,
        "gap_grid": grid,
        "recent_subsample": recent,
        "cost_grid": cost_grid,
        "sigma_needed_for_mu10": need,
        "gate": gate,
        "by_year": by_year,
        "quarters": quads,
        "anatomy": {
            "n_days": len(days),
            "n_skipped_too_few_tradable": n_missing,
            "date_first": days[0] if days else None,
            "date_last": days[-1] if days else None,
            "gap_mean_all_top20_bp": round(float(arr(gap_all).mean()), 2),
            "gap_mean_selected_bp": round(float(arr(gap_sel).mean()), 2),
            "i1_mean_all_top20_bp": round(float(arr(i1_all).mean()), 2),
            "i1_mean_selected_bp": round(float(arr(i1_sel_mean).mean()), 2),
            "selected_frac_mean": round(float(np.mean(mfrac)), 4),
            "days_with_zero_selected": int(sum(1 for f in mfrac if f == 0.0)),
        },
        "elapsed_s": round(time.time() - t0, 1),
    }
    OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[written] {OUT} elapsed={res['elapsed_s']}s")


if __name__ == "__main__":
    main()
