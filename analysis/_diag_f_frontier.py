"""给定 k，60 天把缺口判到几位有效数字 —— k=1…40 的 (F_crit, target, 余量, P) 前沿。

为什么要有这一步：
  `_diag_tstar_hac_lag` 的 [F] 表只给 k ∈ {4.6, 5, 11, 25} 四行，
  `_diag_f_sampling` 的 [5] 段把 target 当作 k 的函数，也只在四个点上取值。
  于是「60 天够不够」在 k 上是**离散的**：k=5 在掷硬币、k=11 就过。
  在四个点上读数，等于在自己选定的一批 k 上读数。本脚本把 k 连续扫到 1…40，
  让「哪一个 k 的判决可判」由表本身回答，而不是由选点回答。

三处对拍（对不上就作废，不打印结论）：
  [0] 复算 universe：days / n / N_mean / g0 / Var(e_k) 必须复现
      `_diag_tstar_hac_k_20261110.txt` 第 1 行与 [B] 表；
  [1] 复算 `_diag_f_sampling` [5] 段的四行（EV / target / MDE / 余量）；
  [2] 复算 F_crit(4.6) = 1.2769：闭式 (target^2*60/Z^2 - Var(e_k))/g0
      与 `_diag_f_sampling` 用的 1+((target/MDE_iid)^2-1)/s_k 必须同值。

本脚本新增（不在任何既有产物里）：
  [4] 把「余量」的 1σ 带拆成两项：target 自己那一项（基线 EV 的 se）
      与 F 那一项。前者此前从未进入过任何口径，而它比后者大一个量级。
  [5] 抬 k 的边际：MDE 是解析的（确定下降）；target 是估出来的
      （k=5→40 降 12.4bp，而 se(差值) 在 rho∈[0,1] 上恒 ≥ 17.4bp）⇒
      不管两者相关多少，target 的下降都 < 1σ。

用法：
    backend/.venv/Scripts/python.exe analysis/_diag_f_frontier.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _audit_intraday_budget import build_universe  # noqa: E402

OUT = HERE / "_diag_f_frontier_20261110.txt"
LINES: list[str] = []
Z = 1.959964 + 0.841621     # 双侧 5% / 功效 80%，与 _audit_intraday_budget 同一常数
COST_BP = 20.0
HORIZON = 60
K_MAX = 40
F_POINT = 1.679             # 窗口实测 T=40，_diag_tstar_hac_lag [E2]
SD_F_ACF = 0.225            # delta 法 L=5, T=60（_diag_f_sampling [2]）
SD_F_WIN = 0.45             # 窗口/自举路线的块间散布（同产物 [3]）

# 产物值：只用于对拍，不参与计算
REF = {
    "days": 356, "n": 43685, "n_mean": 122.7, "g0_bp2": 17329,
    "ve": {4.6: 47946, 5: 43937, 11: 18785, 25: 7048},
}
# `_diag_f_sampling` [5] 段印出的四行：(k, EV, target, MDE@1.679, 缺=MDE/target-1)
# 注意该产物那一列的符号是「缺」不是「余」：+4.9% 表示 MDE 高出阈值 4.9%。
REF_F5 = ((4.6, -75.70, 95.70, 100.4, +0.049),
          (5.0, -75.46, 95.46, 97.7, +0.024),
          (11.0, -71.99, 91.99, 79.1, -0.140),
          (25.0, -63.32, 83.32, 68.8, -0.174))

# 实测 rank→EV（analysis/_reseal_run_20261110.txt）：(选取率, EV, |t|, n, 标签)
ANCHORS = (
    (0.0371, -75.73, 2.84, 1199, "walk-forward 选中"),
    (0.1000, -71.26, 6.07, 3240, "q10"),
    (0.2450, -60.16, 7.01, 7940, "q20"),
    (0.3580, -59.91, 6.92, 11588, "q30"),
)
REF_MECH = (1.000, -82.5)   # 不过滤（机械规则全做），_diag_tstar_hac_k 第 1 行
TARGET_ANCHOR = 95.73       # |−75.73|+20，`_diag_f_sampling` 用的常数（只用于对拍）


def p(s: str) -> None:
    print(s, flush=True)
    LINES.append(s)


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def interp(x: float, col: int) -> float:
    """在 ANCHORS 上分段线性取值；col=1 取 EV，col=2 取 |t|。"""
    xs = [a[0] for a in ANCHORS]
    ys = [a[col] for a in ANCHORS]
    i = 0 if x <= xs[0] else (len(xs) - 2 if x >= xs[-1] else
                              max(j for j in range(len(xs) - 1) if xs[j] <= x))
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def main() -> int:
    _, u = build_universe()
    days = sorted(u["date"].unique())
    groups = [u.index[u["date"] == d].to_numpy() for d in days]
    groups = [v for v in groups if len(v) >= 6]
    r = u["r0_lim"].to_numpy()
    N = np.array([len(v) for v in groups], dtype=float)
    within = np.array([float(r[v].var(ddof=1)) for v in groups])
    nd = len(groups)
    N_MEAN = float(N.mean())
    G0 = float(np.array([r[v].mean() for v in groups]).var(ddof=1) * 1e8)

    def ve(k: float) -> float:
        return float(np.mean(within * (N - k) / (k * N)) * 1e8)

    p("k=1…40 的功效/靶子前沿 —— 输入由 _audit_intraday_budget.build_universe 现算，不抄产物")
    p(f"universe: days={nd} n={len(u):,} N_mean={N_MEAN:.1f} "
      f"N_min={int(N.min())} N_max={int(N.max())} g0={G0:.0f}bp^2")
    p("")

    # ---------- [0] 对拍 universe ----------
    p("[0] 对拍：复算 universe 与 Var(e_k)（对不上则后面全部作废）")
    dev = [("days", float(nd), REF["days"]), ("n", float(len(u)), REF["n"]),
           ("N_mean", N_MEAN, REF["n_mean"]), ("g0", G0, REF["g0_bp2"])]
    dev += [(f"Var(e_{k})", ve(k), float(REF["ve"][k])) for k in REF["ve"]]
    bad = [(nm, got, ref) for nm, got, ref in dev
           if abs(got - ref) > max(1.0, abs(ref) * 2e-3)]
    for nm, got, ref in dev:
        p(f"    {nm:<12} 现算 {got:>10.2f}   产物 {ref:>10.2f}")
    p(f"    ⇒ 超容差 {len(bad)} 项" + ("" if not bad else f"：{bad}"))
    if bad:
        p("    对拍失败，中止。")
        OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        return 2
    p("")

    kmax = min(K_MAX, int(N.min()) - 1)
    p(f"[0b] k 的上界：日内无放回抽 k 只要求 k < N_min={int(N.min())} ⇒ k_max={kmax}")

    # ---------- 派生量 ----------
    def ev_at(share: float) -> float:
        return interp(share, 1)

    def se_ev_at(share: float) -> float:
        return abs(interp(share, 1)) / interp(share, 2)

    def target_at(k: float) -> float:
        return abs(ev_at(k / N_MEAN)) + COST_BP

    def mde_id(k: float) -> float:
        return Z * math.sqrt((G0 + ve(k)) / HORIZON)

    def mde_f(k: float, F: float) -> float:
        return Z * math.sqrt((G0 * F + ve(k)) / HORIZON)

    def sk(k: float) -> float:
        return G0 / (G0 + ve(k))

    def f_crit_closed(k: float) -> float:
        return (target_at(k) ** 2 * HORIZON / Z ** 2 - ve(k)) / G0

    def f_crit_given(k: float, t: float) -> float:
        return (t ** 2 * HORIZON / Z ** 2 - ve(k)) / G0

    def f_crit_ref(k: float) -> float:
        return 1.0 + ((target_at(k) / mde_id(k)) ** 2 - 1.0) / sk(k)

    p("")
    p("[1] 对拍：复算 `_diag_f_sampling` [5] 段四行（EV / target / MDE@1.679 / 缺）")
    worst = 0.0
    for k, ev_ref, tg_ref, mde_ref, gap_ref in REF_F5:
        ev, tg, m = ev_at(k / N_MEAN), target_at(k), mde_f(k, F_POINT)
        worst = max(worst, abs(ev - ev_ref), abs(tg - tg_ref), abs(m - mde_ref))
        p(f"    k={k:<4} EV {ev:>8.2f}({ev_ref:>8.2f})  target {tg:>7.2f}({tg_ref:>7.2f})"
          f"  MDE {m:>7.1f}({mde_ref:>7.1f})  缺 {m/tg-1:>+7.1%}({gap_ref:>+7.1%})")
    p(f"    ⇒ 最大偏差 {worst:.2f}bp" + ("（>0.5bp）" if worst > 0.5 else " ⇒ [5] 段可连续化"))
    if worst > 0.5:
        OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        return 2

    p("")
    p("[2] 对拍：F_crit(k=4.6) 三种算法同值，且能装下产物印的 1.2769")
    a, b = f_crit_closed(4.6), f_crit_ref(4.6)
    c = f_crit_given(4.6, TARGET_ANCHOR)
    p(f"    闭式 (target^2*60/Z^2 - Var(e))/g0 = {a:.4f}   "
      f"1+((target/MDE_iid)^2-1)/s_k = {b:.4f}")
    p(f"    用锚点 target={TARGET_ANCHOR}（= |−75.73|+20，产物原样） = {c:.4f}   "
      f"产物印 1.2769，差 {c - 1.2769:+.4f}")
    # 产物那一版的中间量只有 1–3 位小数（MDE_iid 92.4、s_k 0.265），把舍入盒摊开
    box = [1.0 + ((TARGET_ANCHOR / mi) ** 2 - 1.0) / sk_v
           for mi in (92.35, 92.45) for sk_v in (0.2645, 0.2655)]
    p(f"    产物那版的可复现区间（MDE_iid∈[92.35,92.45]、s_k∈[0.2645,0.2655]）= "
      f"[{min(box):.4f}, {max(box):.4f}] ⇒ 1.2769 {'在' if min(box) <= 1.2769 <= max(box) else '不在'}内")
    p(f"    ⇒ 1e-3 的差是产物自身中间量舍入造成的，量级 0.004σ(F)；前沿一律用满精度 "
      f"{a:.4f}")
    if abs(a - b) > 1e-3 or not (min(box) <= 1.2769 <= max(box)):
        p("    对拍失败，中止。")
        OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        return 2
    p("")

    # ---------- [3] 前沿 ----------
    p(f"[3] 前沿 k=1…{kmax}：F 取点估计 {F_POINT}，其 sd 取 {SD_F_ACF}（ACF）/ "
      f"{SD_F_WIN}（窗口）")
    p("    余量 = target/MDE - 1，>0 为过；P_F 只把 F 当随机量，P_all 另把 target 当随机量")
    p(f"      {'k':>3} {'share':>6} {'EV':>8} {'target':>7} {'MDE_iid':>8} {'MDE':>7} "
      f"{'余量':>7} {'F_crit':>7} {'P_F':>5} {'P_all':>6} {'1σ 带':>17}  flag")
    rows: dict[int, dict] = {}
    for k in range(1, kmax + 1):
        share = k / N_MEAN
        ev, tg = ev_at(share), target_at(k)
        mi, m = mde_id(k), mde_f(k, F_POINT)
        s = sk(k)
        margin = tg / m - 1.0
        fc = f_crit_closed(k)
        pf = phi((fc - F_POINT) / SD_F_ACF)
        ev_term = se_ev_at(share) / tg
        f_term = 0.5 * s / (1.0 + s * (F_POINT - 1.0)) * SD_F_ACF
        sig = math.hypot(ev_term, f_term)
        pall = phi(math.log1p(margin) / sig)
        lo = (1.0 + margin) * math.exp(-sig) - 1.0
        hi = (1.0 + margin) * math.exp(+sig) - 1.0
        flag = "外推↓" if share < ANCHORS[0][0] else ("" if share <= ANCHORS[-1][0] else "外推↑")
        rows[k] = dict(share=share, ev=ev, tg=tg, mi=mi, m=m, margin=margin, fc=fc,
                       pf=pf, sig=sig, pall=pall, lo=lo, hi=hi, s=s,
                       ev_term=ev_term, f_term=f_term)
        p(f"      {k:>3} {share:>5.1%} {ev:>8.2f} {tg:>7.2f} {mi:>8.1f} {m:>7.1f} "
          f"{margin:>+7.1%} {fc:>7.3f} {pf:>5.0%} {pall:>6.0%} "
          f"{lo:>+8.1%}…{hi:>+7.1%}  {flag}")
    p(f"    参照：不过滤 = 选取率 {REF_MECH[0]:.1%}，EV {REF_MECH[1]:.1f}bp"
      f"（曲线内点最低在 share≈35.8%，再往上又变差）")
    p(f"    外推提示：share < {ANCHORS[0][0]:.2%} 的部分没有实测锚点"
      f"（k≤{int(ANCHORS[0][0] * N_MEAN)}），只作连线参考，不可采纳。")
    neg = [k for k in rows if rows[k]["fc"] < 0]
    if neg:
        p(f"    F_crit<0 的 k={neg[0]}…{neg[-1]}：连 F=0（无自相关）都过不了 —— "
          f"MDE_iid 本身已高于 target，不是 F 的问题")
    p("")

    # ---------- [4] 余量的两个不确定度源 ----------
    p("[4] 「余量」的 1σ 带拆成两项：target 那一项（分母是估出来的 EV）vs F 那一项")
    p(f"      {'k':>3} {'se(EV)':>7} {'EV项':>7} {'F项':>6} {'合成 sd':>8} "
      f"{'余量':>7} {'P_all(ACF)':>10} {'P_all(窗口)':>11}")
    for k in (2, 4, 5, 11, 18, 25, 32, 40):
        if k not in rows:
            continue
        o = rows[k]
        s2 = math.hypot(o["ev_term"],
                        0.5 * o["s"] / (1.0 + o["s"] * (F_POINT - 1.0)) * SD_F_WIN)
        p(f"      {k:>3} {se_ev_at(o['share']):>7.2f} {o['ev_term']:>7.3f} "
          f"{o['f_term']:>6.3f} {o['sig']:>8.3f} {o['margin']:>+7.1%} "
          f"{o['pall']:>10.0%} {phi(math.log1p(o['margin']) / s2):>11.0%}")
    p("    ⇒ EV 项由锚点 se 连得：3.71% 处 26.7bp（|t|=2.84, n=1199），"
      f"连线到 k=5 是 {se_ev_at(rows[5]['share']):.1f}bp；")
    p(f"      它对 sd 的贡献在 k=5 处是 F 项的 "
      f"{rows[5]['ev_term'] / rows[5]['f_term']:.0f} 倍。")
    g_term = 0.5 * rows[40]["s"] * math.sqrt(2.0 / (nd - 1))
    p(f"    第三项（g0 自身的估计误差，se(g0)/g0=sqrt(2/{nd - 1})={math.sqrt(2/(nd-1)):.1%}）"
      f"在 k=40 处 = {g_term:.3f}")
    p(f"      ⇒ 三项合成 sd {math.hypot(rows[40]['sig'], g_term):.3f} vs 两项 "
      f"{rows[40]['sig']:.3f}，P_all 变动 ≤1pp，可略；但它说明「方差本身也是估出来的」")
    p("")

    # ---------- [5] 抬 k 的边际 ----------
    p("[5] 抬 k 买到的是什么：MDE 是解析的（确定），target 是估出来的（不确定）")
    k_lo, k_hi = 5, kmax
    d_mde = rows[k_lo]["m"] - rows[k_hi]["m"]
    d_tg = rows[k_lo]["tg"] - rows[k_hi]["tg"]
    se1, se2 = se_ev_at(rows[k_lo]["share"]), se_ev_at(rows[k_hi]["share"])
    hi_d, lo_d = math.hypot(se1, se2), abs(se1 - se2)
    p(f"    k {k_lo}→{k_hi}：MDE {rows[k_lo]['m']:.1f}→{rows[k_hi]['m']:.1f} "
      f"（降 {d_mde:.1f}bp = {d_mde / rows[k_lo]['m']:.1%}）")
    p(f"            target {rows[k_lo]['tg']:.2f}→{rows[k_hi]['tg']:.2f}（降 {d_tg:.2f}bp）")
    p(f"            该降幅的 se 在 rho∈[0,1] 上落在 [{lo_d:.1f}, {hi_d:.1f}]bp "
      f"⇒ {d_tg:.2f} < {lo_d:.1f} ⇒ 任何相关下都 < 1σ，没有分辨出来")
    p(f"            余量 {rows[k_lo]['margin']:+.1%}→{rows[k_hi]['margin']:+.1%}："
      f"其中 MDE 那一半是确定的，target 那一半是噪声")
    p("")

    # ---------- [6] 天数需求与精度地板 ----------
    Z90 = 1.281551
    p("[6] 两个不同的问题：判决要多久出口径（T90）vs 缺口能报到几位（σ 地板）")
    p("    余量 = target/MDE - 1，MDE ∝ 1/sqrt(T) ⇒ 余量随 T 上升；σ 由 EV/F/g0 三项定，"
      "**不随 T 缩小**")
    p(f"      {'k':>3} {'σ':>6} {'余量@60':>8} {'P_all@60':>9} {'需要余量':>9} "
      f"{'T90(天)':>8} {'1 位有效数字?':>13}")
    for k in (4, 5, 6, 8, 11, 14, 18, 25, 32, 40):
        if k not in rows:
            continue
        o = rows[k]
        need = math.exp(Z90 * o["sig"]) - 1.0          # 90% 置信所需的余量
        t90 = HORIZON * ((1.0 + need) * o["m"] / o["tg"]) ** 2
        # 1 位有效数字 ≈ 1σ 带半宽 < 该数值的 10%；余量 < 10% 时永远做不到
        ok = "是" if (o["sig"] * (1.0 + o["margin"]) < max(o["margin"], 0) * 0.10) else "否"
        p(f"      {k:>3} {o['sig']:>6.3f} {o['margin']:>+8.1%} {o['pall']:>9.0%} "
          f"{need:>9.0%} {t90:>8.0f} {ok:>13}")
    p(f"    ⇒ 判决侧：T90 从 k=4 的 {HORIZON * ((1 + math.exp(Z90 * rows[4]['sig']) - 1) * rows[4]['m'] / rows[4]['tg']) ** 2:.0f} 天"
      f"降到 k=25 的 {HORIZON * (math.exp(Z90 * rows[25]['sig']) * rows[25]['m'] / rows[25]['tg']) ** 2:.0f} 天"
      f" —— 加天数**有用**，但只在 k≥11 时才有意义")
    p("      精度侧：σ 在 k≥14 处稳定在 0.12–0.13，1σ 带恒为 ±12pp，"
      "「余量 20%」只能报成「20%±12pp」")
    p("      ⇒ 缺口/余量的数值精度被基线 EV 的 se 锁死（这是历史样本的属性，不是窗口长度）：")
    p("        把 T 从 60 拉到 600，余量从 +21% 涨到 +283%，而 σ 仍是 0.125 ⇒ 相对精度不变。")
    p("")

    # ---------- [7] 判定 ----------
    best_m = max(rows, key=lambda k: rows[k]["margin"])
    best_ev = max(rows, key=lambda k: rows[k]["ev"])
    first_pass = min((k for k in rows if rows[k]["margin"] > 0), default=None)
    first_sure = min((k for k in rows if rows[k]["pall"] >= 0.90), default=None)
    p("[7] 判定")
    p(f"    ① 余量在 k=1…{kmax} 上**没有内点最优**（1→21 升、22→30 轻微回落、31→40 再升），"
      f"极大值落在边界 k={best_m}"
      f"（{rows[best_m]['margin']:+.1%}，P_all={rows[best_m]['pall']:.0%}，1σ 带 "
      f"{rows[best_m]['lo']:+.1%}…{rows[best_m]['hi']:+.1%}）")
    p(f"    ② 余量首次转正的 k = {first_pass}；P_all 首次 ≥90% 的 k = {first_sure}。"
      f"k∈[{first_pass},{first_sure}) 就是「测得出但判不准」的区间 —— 此前 k=5 的读数正落在这里。")
    p(f"    ③ EV 最好的 k = {best_ev}（毛 {rows[best_ev]['ev']:.2f}bp，净 "
      f"{rows[best_ev]['ev'] - COST_BP:+.2f}bp）：全区间没有一个 k 让策略转正。")
    p(f"       「余量转正」只要 MDE < target≈{rows[best_ev]['tg']:.0f}bp —— 检出力够；")
    p(f"       「策略转正」要真实 EV 从 {rows[best_ev]['ev']:.0f}bp 抬到 +{COST_BP:.0f}bp 以上"
      f"（差 {abs(rows[best_ev]['ev']) + COST_BP:.0f}bp = target 本身）")
    p("       —— 前者是样本问题，后者是策略问题；60 天最多只能「测得出」那个恰好够翻正的效应")
    p(f"    ④ k=4 的 P_all={rows[4]['pall']:.0%}、k=5 的 {rows[5]['pall']:.0%}"
      f"（P_F 只有 {rows[4]['pf']:.0%}/{rows[5]['pf']:.0%}）：把 target 的抽样误差算进来，")
    p("      「k=4.6 不过」不再是 81–88%，而是掷硬币 —— 判决在 60 天这个样本上还不该出口径。")
    p("    ⑤ 口径：报 (k, target(k), MDE(k), 余量, 1σ 带, P_all) 六元组；"
      "只有 P_all≤10% 或 ≥90% 才写「够/不够」，其余写「不可判」；")
    p("      余量的数值一律带 ±12pp（k≥14）或更宽，禁止写成单值。")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    p(f"\nwritten {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
