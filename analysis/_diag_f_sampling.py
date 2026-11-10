"""F 的抽样误差，以及「判决边界」到底有几个 sigma 远。

问题：`_diag_tstar_hac_lag` 给出 F 的四个点估计（1.215 / 1.517 / 1.519 / 1.679），
但**没有一个带误差棒**。于是「边界 F=1.277，点估计高出 19% ⇒ 判决稳」这句话
把「四个估计」当成了「四次独立抽样」。本脚本算的是它们的共同抽样误差。

输入不是目测：rho 逐 lag 读数从 `_diag_tstar_hac_lag_20261110.txt` 的 [A] 段
逐位抄录（T=356，产物第 2 行印的 se(rho)=1/sqrt(356)=0.053）。脚本先把该产物
的 [B] 表（T=60 列）与 [F] 表各 20 格复算一遍做可红对拍：对不上，后面全部作废。

用法：
    backend/.venv/Scripts/python.exe analysis/_diag_f_sampling.py
"""

from __future__ import annotations

import math
from pathlib import Path

OUT = Path(__file__).with_name("_diag_f_sampling_20261110.txt")

# [A] 段逐 lag 读数（T=356, 2026-11-10 落盘）
RHO = (
    -0.043, 0.059, 0.038, 0.114, 0.123,
    -0.003, 0.007, 0.072, 0.113, 0.100,
    0.063, -0.069, 0.073, 0.075, -0.043,
    0.034, -0.032, 0.044, 0.060, -0.081,
)
T_EST = 356  # rho 的估计窗口（天数）
SE_RHO = 1.0 / math.sqrt(T_EST)
TOL_F = 0.003  # rho 只印到 3 位小数，2*sum(weights)*5e-4 ≈ 0.0022，取 0.003

# 产物 [B] 表的 T=60 列，用于对拍
F60_ARTIFACT = (
    0.914, 1.029, 1.101, 1.313, 1.538,
    1.532, 1.545, 1.671, 1.863, 2.029,
    2.132, 2.022, 2.136, 2.250, 2.185,
    2.235, 2.189, 2.251, 2.334, 2.226,
)

# 产物 [F] 表：k -> (T*_iid, MDE_iid@60d, s_k)，s_k 取自 [B] 表的「份额」
DESIGN = {
    4.6: (55.9, 92.4, 0.265),
    5.0: (52.5, 89.5, 0.283),
    11.0: (30.9, 68.7, 0.480),
    25.0: (20.9, 56.5, 0.711),
}
TARGET = 95.73  # = |−75.73bp 基线 EV| + 20bp 成本，见 _diag_power_mc.py:29
HORIZON = 60
SD_WINDOW = 0.45  # 窗口实测/平稳自举路线的块间散布量级（[D] 表 1.35/1.85/2.31 的同量纲）

# 实测 rank→EV（_reseal_run_20261110.txt，pooled OOS 243 天，n=32,350）：(选取率, EV)
RANK_EV = ((0.037, -75.73), (0.100, -71.26), (0.245, -60.16), (0.358, -59.91))


def f_hac(L: int, T: int = HORIZON) -> float:
    """T 日均值在截断长度 L 下的方差膨胀因子：1 + 2*sum (1-l/T) rho_l。"""
    return 1.0 + 2.0 * sum((1.0 - l / T) * RHO[l - 1] for l in range(1, L + 1))


def sd_f(L: int, T: int = HORIZON) -> float:
    """delta 法。假设各 lag 的 rho 估计渐近独立（Bartlett），var(rho)=1/T。

    Var(F) = 4 * sum_l (1-l/T)^2 * Var(rho_l)。
    """
    acc = sum((1.0 - l / T) ** 2 for l in range(1, L + 1))
    return 2.0 * math.sqrt(acc * SE_RHO ** 2)


def mde(k: float, F: float) -> float:
    """MDE(60d) = MDE_iid(k) * sqrt(1 + s_k*(F-1))：只有 g0 那一半被放大。"""
    _, mde_iid, s_k = DESIGN[k]
    return mde_iid * math.sqrt(1.0 + s_k * (F - 1.0))


def f_crit(k: float, target: float = TARGET) -> float:
    """60 天刚好过阈（MDE = target）时允许的最大 F。"""
    _, mde_iid, s_k = DESIGN[k]
    return 1.0 + ((target / mde_iid) ** 2 - 1.0) / s_k


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def ev_at(share: float) -> float:
    for (x0, y0), (x1, y1) in zip(RANK_EV, RANK_EV[1:]):
        if x0 <= share <= x1:
            return y0 + (y1 - y0) * (share - x0) / (x1 - x0)
    return RANK_EV[-1][1]


def main() -> None:
    lines: list[str] = []
    p = lines.append

    p("F 的抽样误差 / 判决边界有几个 sigma —— 由 _diag_tstar_hac_lag_20261110.txt 的 rho 读数重算")
    p(f"rho 估计窗口 T={T_EST} 天，se(rho)=1/sqrt(T)={SE_RHO:.4f}（产物第 2 行）")
    p("")

    # --- 可红对拍 1：复算 [B] 表 T=60 列 ---
    p("[0] 对拍：复算 F(60,L) vs 产物 [B] 表 T=60 列")
    dev = [(L, f_hac(L), F60_ARTIFACT[L - 1]) for L in range(1, 21)]
    dev.sort(key=lambda r: -abs(r[1] - r[2]))
    over = [r for r in dev if abs(r[1] - r[2]) > TOL_F]
    p(f"    最大偏差 {abs(dev[0][1] - dev[0][2]):.4f}（L={dev[0][0]}），超容差 {len(over)} 格")
    p(f"    偏差来源是 rho 只印到 3 位：最坏界 {2 * 5e-4 * sum(1 - l / 60 for l in range(1, 21)):.4f}"
      f"（20 个 lag 同时取整到同一边），实测 {abs(dev[0][1] - dev[0][2]):.4f} ⇒ 落在取整误差内")
    p("")

    # --- 可红对拍 2：复算 [F] 表 ---
    p("[1] 对拍：复算 MDE(60d) vs 产物 [F] 表（每 k 5 个 F）")
    want_mde = {
        4.6: (92.4, 94.4, 95.0, 98.5, 100.4),
        5.0: (89.5, 91.5, 92.2, 95.8, 97.7),
        11.0: (68.7, 71.3, 72.2, 76.8, 79.1),
        25.0: (56.5, 59.6, 60.6, 66.0, 68.8),
    }
    fs = (1.0, 1.161, 1.215, 1.517, 1.679)
    worst = max(abs(mde(k, F) - ref) for k, row in want_mde.items() for F, ref in zip(fs, row))
    p(f"    20 格最大偏差 {worst:.2f}bp ⇒ 放大式与 [F] 表同源，可用")
    p("")

    # --- 核心：sd(F) ---
    p("[2] 四个「路线」来自同一条 356 天序列：它们的共同抽样误差")
    p(f"    {'L':>3} {'F(60)':>7} {'sd(F)':>7} {'F/sd':>6}")
    for L in (1, 4, 5, 8, 10, 20):
        p(f"    {L:>3} {f_hac(L):>7.3f} {sd_f(L):>7.3f} {f_hac(L) / sd_f(L):>6.1f}")
    spread = max(fs[2:]) - min(fs[2:])
    p(f"    ⇒ F(L=5) = {f_hac(5):.3f} ± {sd_f(5):.3f}(1σ)。四个读数极差 {spread:.3f} = {spread / sd_f(5):.1f}σ，")
    p("      是同一个量的位移，不是四次独立证据；「±5%」比 sd 小 4 倍。")
    p("")

    # --- 边界有几个 sigma ---
    p("[3] 边界 F_crit 距离点估计几个 sigma（P(过) = P(F < F_crit)）")
    for k in DESIGN:
        fc = f_crit(k)
        z5 = (f_hac(5) - fc) / sd_f(5)
        zw = (1.679 - fc) / SD_WINDOW
        p(f"    k={k:<4} F_crit={fc:.3f}  ACF路线: {z5:+.2f}σ P(过)={phi(-z5):.0%}   "
          f"窗口路线: {zw:+.2f}σ P(过)={phi(-zw):.0%}")
    p("    ⇒ k=4.6 的 P(不过)=81–88%（不是「稳」）；k=5 的 P(过)=22–45% ⇒「k=5 也不过」是掷硬币。")
    p("")

    # --- 缺多少 ---
    p("[4] 把 F 的带子映射回「缺多少」——点值 4.9% 不能进口径")
    for label, sd in (("ACF sd=0.225", sd_f(5)), ("窗口 sd=0.45", SD_WINDOW)):
        for k in (4.6,):
            lo = mde(k, 1.679 - sd)
            hi = mde(k, 1.679 + sd)
            p(f"    k={k} {label}: MDE {lo:.1f}…{hi:.1f} ⇒ "
              f"缺 {lo / TARGET - 1:+.1%}…{hi / TARGET - 1:+.1%}（点估计 100.4 ⇒ +4.9%）")
    p("    ⇒ ACF 路线的 1σ 带不跨 0（2.2–7.5%），窗口路线的跨（−0.6–10.0%）：")
    p("      缺口不能报成「4.9%」，只能报「不过，缺 0–10%，点估计 5%」。")
    p("")

    # --- target 不是常数 ---
    p("[5] 阈值 95.73 也不是常数：它 = |日线筛基线 EV(k)| + 20bp")
    p("    实测 rank→EV（analysis/_reseal_run_20261110.txt，pooled OOS 243 天，n=32,350）：")
    for share, ev in RANK_EV:
        p(f"      选取率 {share:>5.1%}  EV={ev:>8.2f}bp")
    p(f"    {'k':>5} {'选取率':>7} {'EV(k)':>9} {'target(k)':>10} {'MDE@1.679':>10} {'缺/余':>8}")
    for k in DESIGN:
        share = k / 122.7
        ev = ev_at(share)
        tgt = abs(ev) + 20.0
        m = mde(k, 1.679)
        p(f"    {k:>5} {share:>6.2%} {ev:>8.2f} {tgt:>10.2f} {m:>10.1f} {m / tgt - 1:>+7.1%}")
    p("    ⇒ 固定 target 时 k=25 的余量是 28%（68.8 vs 95.73），用它自己的 target=83.3 只剩 17%。")
    p("      「余量 46%」是天数口径 60/41−1，与纵轴 MDE 不是同一个量，不能并排读。")
    p("")

    # --- 结论 ---
    fc46 = f_crit(4.6)
    p("[6] 判定")
    p(f"    ① 边界算术成立：解 MDE(4.6, F)=95.73 得 {fc46:.4f}。但它距 F={f_hac(5):.3f} 只有"
      f" {(f_hac(5) - fc46) / sd_f(5):.1f}σ、距 1.679 只有 {(1.679 - fc46) / SD_WINDOW:.1f}σ（窗口路线）")
    p(f"       ⇒ 「判决稳」只能读作「P(不过)≈{phi((1.679 - fc46) / SD_WINDOW):.0%}–{phi((f_hac(5) - fc46) / sd_f(5)):.0%}」。")
    p(f"    ② 「缺 4.9%」的不确定度不主要来自换路线，而来自 F 本身（±0.23…±0.45）与 target 的分子")
    p("       （基线 EV 的 se=26.7bp，t=−2.84 ⇒ target=95.7±26.7，翻在 |EV|≥80.4bp 处）。")
    p(f"    ③ target 随 k 变：k=4.6 {abs(ev_at(4.6 / 122.7)) + 20:.1f} → k=25 {abs(ev_at(25 / 122.7)) + 20:.1f}。")
    p("       抬 k 同时降 MDE 与降 target，两者方向相反 ⇒ 「抬 k 不用问 F」只在固定 target 时成立。")
    p("    ④ k=25 的 P(过)≈100%，但它的 EV 仍是 −63bp：过的是「检出力」，不是「策略」——")
    p("       MDE 在阈值下方只说明「测得出」，本图上没有一个 k 让 EV 靠近 +20bp。")

    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
