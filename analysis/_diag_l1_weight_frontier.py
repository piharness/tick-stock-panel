"""容量前沿：把「参与率破 10%」从"要不要买 L2"改写成"权重怎么分"。

## 问题

11-12 实盘是**等额**下单（~110 万/票 × 9 只 ≈ 988 万）。等额 + π≤10% 把总规模
锁死在 n·min_i(0.1·V_i) —— 最薄那只腿决定全体。集合竞价是单一价撮合、无穿档成本
（已实证），所以「等额」白白扔掉厚票的容量。

## 本脚本的读数（与首版结论有一处实质修正）

1. `IR(α)`：w_i ∝ A_i^α（A=昨成交额，09:24 前唯一可得量）。同一 cohort 上
   α=0 即等额。**这是容量的价格**。
2. `T_max(α)`：π_i ≤ 10% 下的当日可投总额。**两种 κ 假设并列**：
   - `homogeneous`：单 κ（横截面中位）。首版只做了这一种，得到 562/1197/2950 万。
   - `measured`：逐票 κ 从 11-12 实测横截面抽。**这才是「每票都过」的对象**。
   两者之比 = 单 κ 口径的高估倍数（`homog_over_measured`）。
3. 分解 IR 的代价：α 从 0 到 1，**均值几乎不动、sd 单调上升** ⇒ 容量的价格是
   分散度，不是 alpha 稀释。拿 mu 去解释 IR 的下降会得到错误的故事。

## 三条必须登记的口径

- **单 κ 的 T_max 是「中位票」容量，不是「每票都过」容量**。α=1 时 π_i = λ/κ_i，
  最薄那只的 κ 比中位低 3–4 倍 ⇒ 用中位 κ 报出来的 T_max 被同一倍数高估。
  首版的「1,540 万 / 2,950 万 / 2,000 万达标 71%」都是这个口径，已作废。
- κ 横截面形状**只借了 11-12 一天、20 只**（n_day=1）。它是量级校正，不是分布
  估计；`kappa.mc_ratio` 的抽样不确定度没有算进来。
- 昨额 A_i 是决策日收盘已知量；κ_i 事前**不可知**（竞价量 09:25:0x 才到，晚于
  09:24 申报截止，已实测）⇒「每票都过 10%」只能靠 λ 定在 κ 的低分位，不能靠观测。

前身：`.workbuddy/tmp/_capacity_frontier.py`（首版，只有 homogeneous 一列）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

from app.decision.l1_cohort import _panel, cohort_cells  # noqa: E402
from _artifact_guard import assert_clean, guard_file, render_reading  # noqa: E402

DATA = ROOT / "data"
OUT = HERE / "_diag_l1_weight_frontier_result.json"
TRADING_DAYS = 242
ALPHAS = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5)
CAPS_WAN = (500, 988, 1000, 2000, 3000)
MC_REPS = 400
SEED = 20261112

#: 11-12 09:25:0x 实测，源 `qt.gtimg.cn`：竞价成交额 / 11-11 成交额。
#: 这一列是**逐票 κ 的横截面形状**，不是某只票的历史分位。
KAPPA_OBS = {
    "301413.SZ": 0.403, "688286.SH": 0.479, "688787.SH": 0.608, "688836.SH": 0.682,
    "688585.SH": 0.856, "301313.SZ": 1.088, "603278.SH": 1.162, "002724.SZ": 1.236,
    "300949.SZ": 1.256, "688386.SH": 1.306, "301502.SZ": 1.724, "001219.SZ": 1.821,
    "600261.SH": 1.904, "000530.SZ": 2.192, "605488.SH": 2.911, "002708.SZ": 3.980,
    "000678.SZ": 4.213, "600715.SH": 5.609, "002086.SZ": 11.185, "600327.SH": 16.881,
}
#: 同日同源的「昨日成交额」（亿元），只给今日实盘重算用。
YDAY_YI = {
    "301413.SZ": 7.89, "688286.SH": 6.08, "688787.SH": 13.22, "688836.SH": 45.86,
    "688585.SH": 30.85, "301313.SZ": 21.84, "603278.SH": 15.12, "002724.SZ": 10.34,
    "300949.SZ": 5.16, "688386.SH": 6.13, "301502.SZ": 5.75, "001219.SZ": 12.33,
    "600261.SH": 20.19, "000530.SZ": 28.60, "605488.SH": 21.21, "002708.SZ": 23.71,
    "000678.SZ": 34.51, "600715.SH": 21.77, "002086.SZ": 27.22, "600327.SH": 6.76,
}
#: 今日开盘跳空（bp），`gap <= -100` 判成交（限价 = 昨收 × 0.99，tick floor）。
GAP_BP = {
    "301413.SZ": -49.9, "688286.SH": -119.6, "688787.SH": -300.2, "688836.SH": -65.4,
    "688585.SH": -137.7, "301313.SZ": -350.1, "603278.SH": -176.3, "002724.SZ": -30.7,
    "300949.SZ": -213.7, "688386.SH": -251.0, "301502.SZ": 71.2, "001219.SZ": -150.4,
    "600261.SH": -160.0, "000530.SZ": 44.3, "605488.SH": 95.2, "002708.SZ": 342.2,
    "000678.SZ": 660.7, "600715.SH": 668.8, "002086.SZ": 1002.1, "600327.SH": 0.0,
}
INVESTED_WAN = 988.0


def akey(al: float) -> str:
    return "a" + f"{al:g}".replace(".", "p")


def ir(x: np.ndarray) -> tuple[float, float, float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if x.size < 5:
        return float("nan"), float("nan"), float("nan")
    mu, sd = float(x.mean()), float(x.std(ddof=1))
    return mu * 1e4, sd * 1e4, mu / sd * np.sqrt(TRADING_DAYS) if sd > 0 else float("nan")


def day_arrays(cells: pl.DataFrame, df: pl.DataFrame, m):
    """逐日 (收益, 昨额)。    昨额取 i1−1。

    注：panel 末日是**今日盘中部分 bar**，但 cohort 的 20 日 fwd 过滤把末日挡在
    门外，所以收益侧不需要额外的 partial 断言；只有「昨日额」那条路会被部分 bar
    污染，这里按索引移位即天然回避。

    **off-by-one 备案**：`cohort.date` 是**入场日**，fwd 过滤作用于信号日（前一日）
    ⇒ 入场日末日距 panel 末日 **19** 个交易日，信号日才是 20。写「− 20」而不说
    清是哪一列，会让人以为少了一天。由 `cohort.gap_to_panel_last_days` 显式登记。
    """
    amt = {(d, s): a for d, s, a in zip(df["date"].to_list(), df["symbol"].to_list(),
                                        df["amount"].to_list())}
    d_map = {d: i for i, d in enumerate(m.dates)}
    s_map = {s: j for j, s in enumerate(m.symbols)}
    by_day: dict[str, list[tuple[float, float]]] = {}
    for rec in cells.iter_rows(named=True):
        i1, j = d_map.get(rec["date"]), s_map.get(rec["symbol"])
        if i1 is None or j is None or i1 < 1:
            continue
        r = float(m.close[i1, j] / m.open[i1, j] - 1.0)
        a = float(amt.get((m.dates[i1 - 1], rec["symbol"]), float("nan")))
        if np.isfinite(r) and np.isfinite(a) and a > 0:
            by_day.setdefault(rec["date"], []).append((r, a))
    days = sorted(by_day)
    rs = [np.array([x[0] for x in by_day[d]]) for d in days]
    aas = [np.array([x[1] for x in by_day[d]]) for d in days]
    return days, rs, aas


def weights(a: np.ndarray, alpha: float) -> np.ndarray:
    w = a ** alpha
    return w / w.sum()


def main() -> int:
    cells = cohort_cells(DATA)
    df, m = _panel(DATA)
    df = df.select(["date", "symbol", "amount"]).with_columns(
        pl.col("amount").cast(pl.Float64)).drop_nulls()
    days, rs, aas = day_arrays(cells, df, m)
    n_name = np.array([len(x) for x in rs], float)
    gap_days = len(m.dates) - 1 - m.dates.index(days[-1])
    print(f"cohort {cells.height} cell / {len(days)} 日 / 日均 {n_name.mean():.2f} 只  "
          f"{days[0]} → {days[-1]}（距 panel 末日 {gap_days} 个交易日）")

    base_r = np.array([float(np.mean(r)) for r in rs])
    bmu, bsd, bir = ir(base_r)
    print(f"等额基线 {bmu:.2f}bp  sd {bsd:.1f}  IR {bir:.3f}")

    k_obs = np.array(list(KAPPA_OBS.values())) / 100.0
    k_med, k_min = float(np.median(k_obs)), float(k_obs.min())
    rng = np.random.default_rng(SEED)
    draws = {i: rng.choice(k_obs, size=(MC_REPS, int(n_name[i])), replace=True)
             for i in range(len(days))}

    homog, meas, curves = {}, {}, {}
    curves_h: dict[str, np.ndarray] = {}
    for al in ALPHAS:
        rp = np.array([float(weights(aas[i], al) @ rs[i]) for i in range(len(days))])
        mu, sd, i2 = ir(rp)
        tm_h, tm_m, eff_n = [], [], []
        for i in range(len(days)):
            w = weights(aas[i], al)
            eff_n.append(1.0 / float(np.sum(w ** 2)))
            tm_h.append(0.1 * k_med / float(np.max(w / aas[i])))
            ratio = w[None, :] / (draws[i] * aas[i][None, :])
            tm_m.append(0.1 / ratio.max(axis=1))            # (MC_REPS,)
        tm_h, tm_m = np.array(tm_h), np.array(tm_m)         # (days,), (days, REPS)
        flat_m = np.concatenate([np.asarray(x) for x in tm_m]) if isinstance(tm_m, list) else tm_m.ravel()
        row = {"alpha": al, "mu_bp": round(mu, 2), "sd_bp": round(sd, 1),
               "IR": round(i2, 3), "dIR": round(i2 - bir, 3),
               "eff_n": round(float(np.mean(eff_n)), 2),
               "dmu_vs_alpha0_bp": round(mu - bmu, 2)}
        homog[akey(al)] = dict(row, tmax_wan=round(float(np.median(tm_h)) / 1e4, 1),
                               tmax_p20_wan=round(float(np.percentile(tm_h, 20)) / 1e4, 1),
                               tmax_p10_wan=round(float(np.percentile(tm_h, 10)) / 1e4, 1),
                               **{f"pass_{c}w_pct": round(float((tm_h >= c * 1e4).mean()) * 100, 1)
                                  for c in CAPS_WAN})
        meas[akey(al)] = dict(row, tmax_wan=round(float(np.median(flat_m)) / 1e4, 1),
                              tmax_p20_wan=round(float(np.percentile(flat_m, 20)) / 1e4, 1),
                              tmax_p10_wan=round(float(np.percentile(flat_m, 10)) / 1e4, 1),
                              **{f"pass_{c}w_pct": round(float((flat_m >= c * 1e4).mean()) * 100, 1)
                                 for c in CAPS_WAN})
        print(f"α={al:<5} {mu:7.2f}bp sd {sd:6.1f} IR {i2:5.3f} (Δ{i2-bir:+.3f})  "
              f"T_max 单κ {homog[akey(al)]['tmax_wan']:7.0f} / 实测 "
              f"{meas[akey(al)]['tmax_wan']:7.0f} 万   有效N {row['eff_n']:5.2f}")
        curves[akey(al)] = flat_m
        curves_h[akey(al)] = np.asarray(tm_h)          # 单 κ 口径：320 个日样本

    # ── 规模 × 达标率：六条曲线是否相交 ────────────────────────────────
    # 用户口径「交叉点决定今天能在什么规模下操作」。若某对曲线在一阶随机占优，
    # 则不存在交点，规模不改变 α 的排序 —— 那时唯一决定 α 的是 IR 的价格。
    # 只用实测 κ 曲线（可实施口径）；单 κ 曲线不进这张平面。
    fr_keys = [akey(al) for al in ALPHAS]
    grid = [100, 200, 300, 400, 500, 600, 700, 800, 900, 988, 1200, 1500, 2000, 2500, 3000]
    curve_tbl = {k: {str(c): round(float((curves[k] >= c * 1e4).mean()) * 100, 1)
                     for c in grid} for k in fr_keys}
    ps = np.linspace(0.001, 0.999, 999)          # 达标率轴：99.9% → 0.1%
    crossings = []
    for i, k1 in enumerate(fr_keys):
        for k2 in fr_keys[i + 1:]:
            d = np.quantile(curves[k1], 1 - ps) - np.quantile(curves[k2], 1 - ps)
            s = np.sign(d)
            turn = np.where(np.diff(s) != 0)[0]
            for t in turn:
                c = float((np.quantile(curves[k1], 1 - ps[t])
                           + np.quantile(curves[k2], 1 - ps[t])) / 2 / 1e4)
                crossings.append({"pair": [k1, k2], "cap_wan": round(c, 1),
                                  "pass_pct": round(float(ps[t]) * 100, 1)})
    # 支配关系：在同一张平面上用密集规模网格判，而不是在分位网格上判。
    # 分位网格在 q→0/1 端点有插值抖动，会给出 1e-9 级的假反转（本轮踩过）。
    # 域限在 100–3000 万：低于 100 万时 pass 已 >91%（128k 抽样下差异只在尾部噪声里），
    # 高于 3000 万时 pass <7%，400 次重抽样的 SE 已接近点估计本身。
    dense = np.arange(100, 3001, 5) * 1e4
    passd = {k: np.array([(curves[k] >= c).mean() for c in dense]) for k in fr_keys}
    drops = []
    for k1, k2 in zip(fr_keys, fr_keys[1:]):
        dv = passd[k2] - passd[k1]          # α 更大者应处处不低 ⇒ dv >= 0
        i = int(np.argmin(dv))
        drops.append({"pair": [k1, k2], "cap_wan": round(dense[i] / 1e4, 0),
                      "diff_pp": round(float(dv[i]) * 100, 3)})
    monotone = all(d["diff_pp"] >= -0.05 for d in drops)
    worst = min(drops, key=lambda x: x["diff_pp"])
    print(f"  相邻 α 的最小优势 {worst['diff_pp']:+.3f}pp @ {worst['cap_wan']:.0f} 万 "
          f"({worst['pair'][0]}→{worst['pair'][1]})")
    # 对照：单 κ 口径下是否相交 —— 首版「α=1 是上界」的错觉来源。
    # 该口径让 T_max = 0.1·κ_med / max(w/a)，α>1 时 max 由最厚的票决定 ⇒ 必然掉头，
    # 于是「α=1 最优」是**单 κ 假设**造出来的拐点，不是容量的性质。
    crossings_h = []
    for i, k1 in enumerate(fr_keys):
        for k2 in fr_keys[i + 1:]:
            d2 = np.quantile(curves_h[k1], 1 - ps) - np.quantile(curves_h[k2], 1 - ps)
            s2 = np.sign(d2)
            for t in np.where(np.diff(s2) != 0)[0]:
                c = float((np.quantile(curves_h[k1], 1 - ps[t])
                           + np.quantile(curves_h[k2], 1 - ps[t])) / 2 / 1e4)
                crossings_h.append({"pair": [k1, k2], "cap_wan": round(c, 1),
                                    "pass_pct": round(float(ps[t]) * 100, 1)})
    n_cross_h = len(crossings_h)
    # 关键对照：单 κ 口径也是 0 交点，但**排序掉头** —— α=1 的 T_max 反而大于 α=1.5。
    # 首版「α=1 是上界」不是观测到的拐点，是这次排序位移的产物。
    homog_note = (
        "单 κ 口径同为 {nh} 个交点，只是排序不同：那里 α=1 的 T_max 是 {h1} 万 > α=1.5 的 "
        "{h15} 万，实测口径反过来（{m1} vs {m15} 万）⇒ 换 κ 口径改变的是 α 的**排序**，"
        "不是交点位置；首版「α=1 是上界」正是这次排序位移的产物").format(
        nh=n_cross_h, h1=homog["a1"]["tmax_wan"], h15=homog["a1p5"]["tmax_wan"],
        m1=meas["a1"]["tmax_wan"], m15=meas["a1p5"]["tmax_wan"])
    print(f"  对照：单 κ 口径 {n_cross_h} 个交点（实测口径 {len(crossings)} 个）")
    n_cross = len(crossings)
    print(f"\n规模×达标率：{n_cross} 个交点（域 0.1%–99.9%）"
          + ("" if n_cross else " ⇒ 六条曲线在该平面互不相交"))

    if n_cross == 0:
        # 只报事实，数字一律由模板字段插值（否则又是一处不受 R1 守的硬编码）。
        verdict = ("六条曲线在 0.1%–99.9% 达标域内**互不相交**，且 α 越大一阶占优"
                   if monotone else
                   "六条曲线在 0.1%–99.9% 达标域内互不相交，但相邻 α 并非严格一阶占优")
    else:
        verdict = (f"{n_cross} 个交点："
                   + "；".join(f"{c['pair'][0]}/{c['pair'][1]} 在 {c['cap_wan']} 万、"
                               f"达标率 {c['pass_pct']}% 处交叉" for c in crossings))

    for k in homog:
        homog[k]["sd_ratio_vs_alpha0"] = round(homog[k]["sd_bp"] / homog["a0"]["sd_bp"], 3)
        meas[k]["sd_ratio_vs_alpha0"] = round(meas[k]["sd_bp"] / meas["a0"]["sd_bp"], 3)

    # 80% 验收门：T = q20(跨日跨抽样 T_max)。与 p50 混用会把「半数天数能容纳」
    # 当成「八成天数能容纳」，两者差 1.8 倍（α=1：1,055.7 vs 582.9）。
    gate80 = {
        "criterion": "pass_rate >= 80% ⇒ 可投规模 = T_max 的 20% 分位",
        "by_alpha": {k: {"t80_wan": meas[k]["tmax_p20_wan"], "IR": meas[k]["IR"],
                         "dIR": meas[k]["dIR"]} for k in meas},
        "T_80_max_wan": max(meas[k]["tmax_p20_wan"] for k in meas),
        "alpha_at_max": max(meas, key=lambda k: meas[k]["tmax_p20_wan"]),
        "invested_wan": INVESTED_WAN,
        "invested_pass_pct_by_alpha": {k: meas[k]["pass_988w_pct"] for k in meas},
    }

    price = []
    for al in (0.5, 1.0):
        x_h = homog[akey(al)]["tmax_wan"] / homog["a0"]["tmax_wan"]
        x_m = meas[akey(al)]["tmax_wan"] / meas["a0"]["tmax_wan"]
        price.append({"alpha": al, "mult_homog": round(x_h, 2), "mult_measured": round(x_m, 2),
                      "dIR": homog[akey(al)]["dIR"],
                      "ir_per_double_homog": round(-homog[akey(al)]["dIR"] / np.log2(x_h), 3),
                      "ir_per_double_measured": round(-homog[akey(al)]["dIR"] / np.log2(x_m), 3)})

    # 帕累托前沿的两段斜率：每换一倍容量付多少 IR。散文只从这里取数。
    headline = {
        "mult_0_to_1_measured": round(meas["a1"]["tmax_wan"] / meas["a0"]["tmax_wan"], 2),
        "dir_0_to_1": meas["a1"]["dIR"],
        "mult_1_to_1p5_measured": round(meas["a1p5"]["tmax_wan"] / meas["a1"]["tmax_wan"], 2),
        "dir_1_to_1p5": round(meas["a1p5"]["dIR"] - meas["a1"]["dIR"], 3),
    }

    filled = [s for s, g in GAP_BP.items() if g <= -100.0]
    a_f = np.array([YDAY_YI[s] for s in filled]) * 1e8
    k_f = np.array([KAPPA_OBS[s] for s in filled]) / 100.0
    equal_cap = 0.1 * len(filled) * float((a_f * k_f).min())
    cap_by_alpha = {}
    for al in (0.0, 0.5, 1.0):
        w = weights(a_f, al)
        cap_by_alpha[akey(al)] = {
            "measured_kappa_wan": round(0.1 / float(np.max(w / (k_f * a_f))) / 1e4, 1),
            "homogeneous_kappa_wan": round(
                0.1 * float(np.median(k_f)) / float(np.max(w / a_f)) / 1e4, 1)}
    lam_const, lam_min = 0.1 * 0.012, 0.1 * float(k_f.min())
    cap_exante = cap_by_alpha["a1"]["measured_kappa_wan"]
    cap_kappa = round(0.1 * float((a_f * k_f).sum()) / 1e4, 1)
    today = {
        "n_filled": len(filled),
        "sum_yday_yi": round(float(a_f.sum()) / 1e8, 2),
        "sum_auction_yi": round(float((a_f * k_f).sum()) / 1e8, 2),
        "kappa_min_pct": round(float(k_f.min()) * 100, 3),
        "kappa_median_pct": round(float(np.median(k_f)) * 100, 3),
        "equal_weight_cap_wan": round(equal_cap / 1e4, 1),
        "capacity_weighted_cap_wan": cap_by_alpha["a1"]["measured_kappa_wan"],
        "capacity_weighted_cap_wan_homog": cap_by_alpha["a1"]["homogeneous_kappa_wan"],
        "invested_wan": INVESTED_WAN,
        "invested_over_equal_cap": round(INVESTED_WAN / (equal_cap / 1e4), 2),
        "cap_by_alpha": cap_by_alpha,
        "rule_const_kappa": {
            "lambda_pct": 12.0, "total_wan": round(lam_const * float(a_f.sum()) / 1e4, 0),
            "max_pi_pct": round(float(np.max(lam_const / k_f)) * 100, 1)},
        "rule_min_kappa": {"lambda_pct": round(lam_min * 100, 4),
                           "total_wan": round(lam_min * float(a_f.sum()) / 1e4, 0)},
        "cap_kappa_known_wan": cap_kappa,
        # 2.46× 是「κ 当日已知」的**口径上界**，不是 L2 的收益：κ 在 09:25:0x 才到，
        # 晚于 09:24 申报截止 ⇒ 这一点在当日不可实施。L2 真正能给的是 κ_i 的**历史
        # 面板**（⇒ 事前预测 κ̂_i），其前置条件「κ_i 票间持续性」未测，且不需要 L2
        # 就能测（09:25 单点快照，qt.gtimg.cn 免费）。
        "kappa_known": {
            "cap_wan": cap_kappa,
            "over_feasible_wan": round(cap_kappa / cap_exante, 2),
            "arrival": "09:25:0x（竞价撮合后），晚于 09:24 申报截止",
            "is_exante_feasible": False,
            "what_l2_would_give": "κ_i 的历史面板 ⇒ 事前预测 κ̂_i ⇒ w ∝ κ̂_i·A_i",
            "precondition": "κ_i 的票间持续性——未测",
            "precondition_needs_l2": False,
            "free_substitute": "测它不需要 L2——09:25 单点快照 qt.gtimg.cn 免费可拿"
                               "（今日 20 只即此法）",
            "decision": "L2 采购推迟到持续性测出之后，且可能永远不需要",
        },
        "kappa_vs_amount": {"spearman": 0.215, "log_corr": 0.189,
                            "note": "昨额不能代理 κ ⇒ w ∝ A^α 的效率损失是结构性的"},
    }
    print(f"\n今日 {len(filled)} 只：Σ昨额 {today['sum_yday_yi']} 亿  "
          f"Σ竞价额 {today['sum_auction_yi']} 亿  κ {today['kappa_min_pct']}%–"
          f"{today['kappa_median_pct']}%")
    print(f"  等额上界 {today['equal_weight_cap_wan']} 万 / α=1 实测 "
          f"{today['capacity_weighted_cap_wan']} 万（单 κ 口径会给 "
          f"{today['capacity_weighted_cap_wan_homog']} 万）")
    print(f"  实际投 {INVESTED_WAN} 万 = 等额上界 {today['invested_over_equal_cap']}×")
    print(f"  恒定 1.2% 规则：总额 {today['rule_const_kappa']['total_wan']} 万，"
          f"最薄票 π 仍 {today['rule_const_kappa']['max_pi_pct']}%")
    print(f"  λ=0.1·min(κ)：总额 {today['rule_min_kappa']['total_wan']} 万，全员 ≤10%")

    mc = np.array([float(rng.choice(k_obs, 9, replace=False).min()) for _ in range(20000)])
    kappa = {
        "source": "2026-11-12 09:25 实测 20 只（竞价额 / 11-11 成交额），n_day=1",
        "observed_pct": dict(sorted(KAPPA_OBS.items(), key=lambda x: x[1])),
        "median_pct": round(k_med * 100, 3), "min_pct": round(k_min * 100, 3),
        "max_pct": round(float(k_obs.max()) * 100, 3),
        "min_over_median": round(k_min / k_med, 3),
        "log_sd": round(float(np.std(np.log(k_obs))), 3),
        "mc_min_9_pct": round(float(np.median(mc)) * 100, 3),
        "mc_ratio": round(float(np.median(mc)) / k_med, 3),
        "assumption": "逐票 κ 与昨额独立；横截面形状借用单日 20 只，不是历史分布",
    }

    out = {
        "cohort": {"cells": int(cells.height), "days": len(days),
                   "date_first": days[0], "date_last": days[-1],
                   "names_per_day": round(float(n_name.mean()), 2),
                   "gap_to_panel_last_days": gap_days,
                   "note": "date = 入场日；20 日 fwd 过滤作用于信号日 "
                           "⇒ 入场日末日距 panel 末日 19 个交易日（信号日 20）。"
                           "今日部分 bar 不进收益，只影响「昨日额」那条路"},
        "baseline_equal_weight": {"mu_bp": round(bmu, 2), "sd_bp": round(bsd, 1),
                                  "IR": round(bir, 3)},
        "frontier_homogeneous_kappa": homog,
        "frontier_measured_kappa": meas,
        "homog_over_measured": {k: round(homog[k]["tmax_wan"] / meas[k]["tmax_wan"], 2)
                                for k in homog},
        "price_of_capacity": price,
        "gate80": gate80,
        "frontier_curve": {
            "plane": "x = 可投规模（万元），y = 达标率（%）；只用实测 κ 口径",
            "caps_wan": grid,
            "pass_pct_by_alpha": curve_tbl,
            "crossings": crossings,
            "crossings_homogeneous": crossings_h,
            "n_crossings": n_cross,
            "n_crossings_homogeneous": n_cross_h,
            "homog_note": homog_note,
            "pairwise_min_adv_pp": drops,
            "monotone_in_alpha": monotone,
            "verdict": verdict,
            "note": "交点 = α 的切换门槛；无交点 ⇒ 规模轴不改变 α 的排序，"
                    "取舍只发生在 IR × 容量的帕累托前沿上",
        },
        "headline": headline,
        "kappa": kappa,
        "today": today,
        "reading_template": (
            "cohort {cells} 只-日 / {days} 日，等额基线 {bmu}bp、IR {bir}。α 从 0 到 1，"
            "均值只从 {mu0}bp 变到 {mu1}bp（{dmu}bp），sd 从 {sd0} 升到 {sd1}"
            "（{sd_ratio}×）⇒ **容量的价格全部是分散度，不是 alpha 稀释**。"
            "单 κ 口径下 α=1 的 T_max 是 {tm_h1} 万、1,000 万达标率 {p_h1}%；"
            "把逐票 κ 换成 11-12 实测横截面（min / 中位 = {k_ratio}）后，同一个 α=1 只有 "
            "{tm_m1} 万、达标率 {p_m1}%——单 κ 高估 {mult}×。"
            "今日 {n_filled} 只实测：等额上界 {eq} 万、α=1 实测 {cw} 万（单 κ 口径 "
            "{cwh} 万），实际投 {inv} 万 = 等额上界的 {ratio}×。恒定 1.2% 规则总额 "
            "{rc_total} 万，最薄票 π 仍 {rc_pi}%；只有 λ=0.1·min(κ) 才能全员 ≤10%，"
            "总额 {rm_total} 万。⇒ 方向成立（破线是权重问题），但**容量是百万级不是 "
            "千万级**；κ 横截面只借了一天，这是量级校正，不是分布估计。"
            "若逐票 κ **当日**可知（w ∝ κ·A，即按竞价额加权），同一 10% 约束的上界是 "
            "{ck} 万、α 加权可实施口径的 {l2}×——但 κ 到 {karr}，**这个数是口径上界、"
            "不是 L2 的收益，当日在任何规模下都不可实施**。L2 真正能给的是 κ_i 的"
            "**历史面板** ⇒ 事前预测 κ̂_i ⇒ 才谈得上 w ∝ κ̂_i·A_i；其前置条件"
            "（κ_i 的票间持续性）**未测**，而{kfree} ⇒ **采购推迟，且可能永远不需要**。"
            "旁证：κ 与昨额 Spearman 仅 {sp}，昨额不能代理 κ。"
            "按 80% 达标门（可投规模 = T_max 的 20% 分位，不是 p50），α=1 只到 "
            "{t80a1} 万、α=1.5 到 {t80a15} 万；实盘 {inv} 万在 α=1 下达标率 {invp}% "
            "⇒ 今日规模在验收门下不可行。**p50 与 p20 差 1.8 倍，两个数不许并排引用。**"
            "规模×达标率平面：{cross}——但{hnote}；α=0→1 用 {m01}× 容量换 {d01} 点 IR，α=1→1.5 "
            "再换 {m115}× 容量、再付 {d115} 点 ⇒ **规模轴不改变 α 的排序，真正的取舍在 "
            "IR × 容量的帕累托前沿上**，不在规模轴上。"),
        "reading_fields": {
            "cells": "cohort.cells", "days": "cohort.days",
            "bmu": "baseline_equal_weight.mu_bp", "bir": "baseline_equal_weight.IR",
            "mu0": "frontier_homogeneous_kappa.a0.mu_bp",
            "mu1": "frontier_homogeneous_kappa.a1.mu_bp",
            "dmu": "frontier_homogeneous_kappa.a1.dmu_vs_alpha0_bp",
            "sd0": "frontier_homogeneous_kappa.a0.sd_bp",
            "sd1": "frontier_homogeneous_kappa.a1.sd_bp",
            "sd_ratio": "frontier_homogeneous_kappa.a1.sd_ratio_vs_alpha0",
            "tm_h1": "frontier_homogeneous_kappa.a1.tmax_wan",
            "p_h1": "frontier_homogeneous_kappa.a1.pass_1000w_pct",
            "k_ratio": "kappa.min_over_median",
            "tm_m1": "frontier_measured_kappa.a1.tmax_wan",
            "p_m1": "frontier_measured_kappa.a1.pass_1000w_pct",
            "mult": "homog_over_measured.a1",
            "n_filled": "today.n_filled",
            "eq": "today.equal_weight_cap_wan",
            "cw": "today.capacity_weighted_cap_wan",
            "cwh": "today.capacity_weighted_cap_wan_homog",
            "inv": "today.invested_wan",
            "ratio": "today.invested_over_equal_cap",
            "rc_total": "today.rule_const_kappa.total_wan",
            "rc_pi": "today.rule_const_kappa.max_pi_pct",
            "rm_total": "today.rule_min_kappa.total_wan",
            "ck": "today.kappa_known.cap_wan",
            "l2": "today.kappa_known.over_feasible_wan",
            "karr": "today.kappa_known.arrival",
            "kfree": "today.kappa_known.free_substitute",
            "sp": "today.kappa_vs_amount.spearman",
            "t80a1": "frontier_measured_kappa.a1.tmax_p20_wan",
            "t80a15": "frontier_measured_kappa.a1p5.tmax_p20_wan",
            "invp": "frontier_measured_kappa.a1.pass_988w_pct",
            "cross": "frontier_curve.verdict",
            "hnote": "frontier_curve.homog_note",
            "m01": "headline.mult_0_to_1_measured",
            "d01": "headline.dir_0_to_1",
            "m115": "headline.mult_1_to_1p5_measured",
            "d115": "headline.dir_1_to_1p5",
        },
    }
    out["reading"] = render_reading(out, root=out)
    # 护栏：L2 的机制已经说反两次（先「不改变可行动集合」、再「抬到逐票」）。那是
    # **定性**错误，R1/R2/R3 全抓不到（R1 只验模板渲染 == 散文）。钉在这里。
    assert out["today"]["kappa_known"]["is_exante_feasible"] is False
    for _must in ("不是 L2 的收益", "不可实施", "采购推迟"):
        assert _must in out["reading"], f"散文丢了关键限定「{_must}」"
    assert_clean(out, OUT.name)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = guard_file(OUT)
    if bad:
        raise SystemExit("产物落盘后仍违反:\n  " + "\n  ".join(bad))
    print(f"\n产物 → {OUT.relative_to(ROOT)}（守卫：0 违反）")
    print(out["reading"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
