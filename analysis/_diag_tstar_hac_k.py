"""把「se_blk/se_iid = 1.01」拆成「相关不存在」还是「相关被稀释」。

前一件产物 `_diag_tstar_hac.py` 在 k=5 上测得比值 1.00-1.01，据此写下
「日间独立假设在这个估计量上站得住」。本脚本检验该结论的一个替代解释：

    门禁估计量 m_d = mu_d + e_d
      mu_d = 当日全事件均值   —— 含日间序列相关，Bartlett(L=5) 膨胀 ~1.16
      e_d  = k 只子样的抽样误差 —— 跨日独立（每日重抽，不重叠）
    Var(m_d) = g0 + Var(e_k)
    Var(e_k) = E_d[ s_d^2 (N_d-k) / (k N_d) ]        (无放回子样精确方差)

所以日间相关**始终存在**，只是它在 Var(m) 里的份额
    s_k = g0 / (g0 + Var(e_k))
随 k 单调上升：k 越大、抽样噪声越小、共同因子的份额越大。
=> 预测：se 比值应从 k=5 的 ~1.02 升到 k=25 的 ~1.05，而不是恒为 1.00。

同时纠正一个跨对象并排：
  `_audit_intraday_budget.t_star(k, rho)` 的分母是 2(1-rho)*var_sel，
  即**双臂配对差**（日线筛选 vs 日线+分钟筛选）；`_diag_tstar_hac` 算的是
  **单臂水平**。rho=0 时两者差一个 2 倍量级的因子，不可同表并列。

可红测试：解析 Var(e_5) 必须复现 bootstrap 的 249.9bp/d -> 62450，否则作废。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _audit_intraday_budget import build_universe  # noqa: E402

OUT = HERE / "_diag_tstar_hac_k_20261110.txt"
LINES: list[str] = []
Z = 1.959964 + 0.841621
TARGET = 95.73
REPS = 6000
SEED = 20261110
KS = (4.6, 5, 11, 25)


def p(s: str) -> None:
    print(s, flush=True)
    LINES.append(s)


def main() -> int:
    _, u = build_universe()
    rng = np.random.default_rng(SEED)

    days = sorted(u["date"].unique())
    groups = [u.index[u["date"] == d].to_numpy() for d in days]
    groups = [v for v in groups if len(v) >= 6]
    n_day = len(groups)
    r = u["r0_lim"].to_numpy()
    N = np.array([len(v) for v in groups], dtype=float)

    dmean = np.array([r[v].mean() for v in groups])
    g0 = dmean.var(ddof=1)
    p(f"pool days={n_day} n={len(u):,} mean={r.mean()*1e4:.1f}bp "
      f"sd_day_mean={np.sqrt(g0)*1e4:.1f}bp N_mean={N.mean():.1f}")

    # 日内无放回方差（精确，含 N 个不同日容量）
    within = np.array([float(r[v].var(ddof=1)) for v in groups])

    def var_e(k: float) -> float:
        return float(np.mean(within * (N - k) / (k * N)))

    p("\n[A] 日度均值序列 mu_d 的序列相关（相关性所在处）")
    rho = {}
    for lag in range(1, 6):
        rho[lag] = float(np.corrcoef(dmean[:-lag], dmean[lag:])[0, 1])
        p(f"  lag={lag}  rho={rho[lag]:+.3f}")
    F_nw = 1.0 + 2.0 * sum((1 - l / 6) * rho[l] for l in range(1, 6))
    p(f"  Bartlett(L=5, NW 权重 1-l/6) F = {F_nw:.4f}  (se x {np.sqrt(F_nw):.4f})")
    for T in (40, 60):
        ft = 1.0 + 2.0 * sum((1 - l / T) * rho[l] for l in range(1, 6))
        p(f"  均值方差用权重 (1-l/{T})：F = {ft:.4f}  (se x {np.sqrt(ft):.4f})")

    p("\n[B] 精确分解 Var(m_d) = g0 + Var(e_k)   [bp^2]")
    p(f"  g0 = {g0*1e8:.0f}")
    p(f"  {'k':>5} {'Var(e_k)':>10} {'Var(m)':>10} {'sd(m)bp':>8} {'份额 s_k':>9} "
      f"{'预测F_eff':>10} {'预测比值':>9}")
    tab = {}
    for k in KS:
        ve = var_e(k)
        vm = g0 + ve
        sk = g0 / vm
        feff = 1.0 + sk * (F_nw - 1.0)
        tab[k] = dict(ve=ve, vm=vm, sk=sk, feff=feff)
        p(f"  {k:>5} {ve*1e8:>10.0f} {vm*1e8:>10.0f} {np.sqrt(vm)*1e4:>8.1f} "
          f"{sk:>9.3f} {feff:>10.4f} {np.sqrt(feff):>9.4f}")

    # --- 可红测试 ---
    p("\n[B2] 可红测试：解析式 vs 已落盘 bootstrap（k=5, T=40）")
    BP = 1e8  # raw -> bp^2
    p(f"  解析 sd(m_5) = {np.sqrt(tab[5]['vm'])*1e4:.1f}bp    "
      f"bootstrap se_iid*sqrt(40) = 39.51*sqrt(40) = {39.51*np.sqrt(40):.1f}bp")
    p(f"  解析 var_sel(5) = sd_pool^2*(1-5/N)/5 = "
      f"{(r.var(ddof=1)*(1-5/N.mean())/5)*BP:.0f}bp^2   "
      f"实测 Var(e_5) = {tab[5]['ve']*BP:.0f}bp^2")
    p(f"  解析双臂配对 T*(k=5,rho=0) = "
      f"{Z**2*2*tab[5]['ve']*BP/TARGET**2:.1f} 天   产物 t_star(k=5,rho=0) = 76.5 天")
    p(f"  解析单臂水平 T*(k=5)       = "
      f"{Z**2*tab[5]['vm']*BP/TARGET**2:.1f} 天   产物 T*_iid(40) = 53.5 天")

    p("\n[C] 门禁 T*：单臂水平 vs 双臂配对差（T* = Z^2 * Var_day_bp2 / target^2）")
    p("  注：同日配对差在同一天内相减 ⇒ 共同日因子 g0 整项抵消 ⇒ 配对口径**不含 g0、与 F 无关**，")
    p("      故 配对_HAC 恒等于 配对_iid。只有水平口径暴露在日间自相关下。")
    p(f"  {'k':>5} {'水平_iid':>9} {'水平_HAC':>9} {'配对_iid':>9} {'配对_HAC':>9}"
      f" {'T*放大':>8}")
    for k in KS:
        vm = tab[k]["vm"] * BP
        ve = tab[k]["ve"] * BP
        lv_i = Z ** 2 * vm / TARGET ** 2
        lv_h = Z ** 2 * (g0 * BP * F_nw + ve) / TARGET ** 2
        pr_i = Z ** 2 * 2 * ve / TARGET ** 2
        pr_h = pr_i  # 同日配对：g0 抵消
        p(f"  {k:>5} {lv_i:>9.1f} {lv_h:>9.1f} {pr_i:>9.1f} {pr_h:>9.1f}"
          f" {lv_h/lv_i:>8.3f}")

    p("\n[D] 实测：循环分块(5) vs 按日重抽，k 扫描（T=60, REPS=%d）" % REPS)
    p(f"  {'k':>5} {'se_iid':>8} {'se_blk':>8} {'比值':>6} {'预测比值':>9} {'预测-实测':>10}")

    def draw_iid(T: int, k: int) -> np.ndarray:
        di = rng.integers(0, n_day, size=T)
        s = np.empty((T, k), dtype=np.int64)
        for t, d in enumerate(di):
            s[t] = rng.choice(groups[d], size=k, replace=False)
        return s

    def draw_blk(T: int, k: int, blen: int) -> np.ndarray:
        nb = int(np.ceil(T / blen))
        s = np.empty((nb * blen, k), dtype=np.int64)
        for b in range(nb):
            s0 = int(rng.integers(0, n_day))
            for j in range(blen):
                s[b * blen + j] = rng.choice(groups[(s0 + j) % n_day], size=k, replace=False)
        return s[:T]

    T = 60
    for k in (5, 11, 25):
        ei = np.empty(REPS)
        eb = np.empty(REPS)
        for b in range(REPS):
            ei[b] = r[draw_iid(T, k)].mean()
            eb[b] = r[draw_blk(T, k, 5)].mean()
        si = float(ei.std(ddof=1) * 1e4)
        sb = float(eb.std(ddof=1) * 1e4)
        pred = float(np.sqrt(tab[k]["feff"]))
        # 比值的 MC 标准误：sd(se) / se = 1/sqrt(2(REPS-1)) per arm
        mc = 1.0 / np.sqrt(2 * (REPS - 1))
        p(f"  {k:>5} {si:>8.2f} {sb:>8.2f} {sb/si:>6.3f} {pred:>9.3f} "
          f"{sb/si-pred:>+10.3f}   (比值 MC se ~ {np.sqrt(2)*mc:.3f})")

    p("\n[E] 判定")
    p(f"  日间相关在门禁水平估计量里的份额：k=5 {tab[5]['sk']:.2f} -> "
      f"k=25 {tab[25]['sk']:.2f} -> 全样本 1.00")
    p(f"  故 se 比值不是常数 1.00，而是 k 的单调增函数；F_nw-1 = {F_nw-1:.3f} 是 k->inf 的极限")
    p(f"  T* 放大倍数：k=5 {tab[5]['feff']:.3f} / k=25 {tab[25]['feff']:.3f} / 全样本 {F_nw:.3f}")
    p(f"  60 天在 k=5 处的 MDE = {Z*np.sqrt(tab[5]['vm']/60)*1e4:.1f}bp "
      f"(阈值 {TARGET}) -> {'过' if Z*np.sqrt(tab[5]['vm']/60)*1e4 < TARGET else '不过'}")
    p(f"  60 天在 k=25 处的 MDE = {Z*np.sqrt(tab[25]['vm']/60)*1e4:.1f}bp "
      f"-> {'过' if Z*np.sqrt(tab[25]['vm']/60)*1e4 < TARGET else '不过'}")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    p(f"\nwritten {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
