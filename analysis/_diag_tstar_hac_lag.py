"""块长=5 正是相关所在的 lag：上一件的 1.01 是采样器自己抹掉的。

事实（同一 pool，356 天）：
  lag=1..3  rho = -0.043 / +0.059 / +0.038   （噪声量级）
  lag=4..5  rho = +0.114 / +0.123            （≈2.1σ / 2.3σ，相邻两 lag 同号）
=> 相关几乎全部落在 lag 4-5，即**周内周期**。

而 `_diag_tstar_hac.py` 的分块长度恰好取 5：
  循环分块把块边界两侧当成独立 => lag=b=5 的相关权重归零；
  块内 lag=l 的相关权重只有 (1-l/b)，l=4 时 0.2。
  => 该采样器对 lag=5 这个最大项**结构性失明**。实测比值 1.01 < 解析 1.02，且
     随 k 单调低于解析（-0.017/-0.024/-0.029，均同号）——方向正是「少算了」。

本脚本换窗口长度与重抽方式，给出 F 的区间，再看 60 天是否还站得住。

  F(L;T) = 1 + 2*sum_{l=1..L} (1-l/T) rho_l      （T 次均值的精确方差形态）
  se 比值(k) = sqrt(1 + s_k*(F-1))，s_k = g0/(g0+Var(e_k))
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _audit_intraday_budget import build_universe  # noqa: E402

OUT = HERE / "_diag_tstar_hac_lag_20261110.txt"
LINES: list[str] = []
Z = 1.959964 + 0.841621
TARGET = 95.73
REPS = 20000
SEED = 20261110
BP = 1e8


def p(s: str) -> None:
    print(s, flush=True)
    LINES.append(s)


def main() -> int:
    _, u = build_universe()
    rng = np.random.default_rng(SEED)

    days = sorted(u["date"].unique())
    groups = [u.index[u["date"] == d].to_numpy() for d in days]
    groups = [v for v in groups if len(v) >= 6]
    n = len(groups)
    r = u["r0_lim"].to_numpy()
    N = np.array([len(v) for v in groups], dtype=float)
    dmean = np.array([r[v].mean() for v in groups])
    g0 = dmean.var(ddof=1)
    within = np.array([float(r[v].var(ddof=1)) for v in groups])
    p(f"days={n} mean={dmean.mean()*1e4:.1f}bp sd_day={np.sqrt(g0)*1e4:.1f}bp")
    p(f"rho 的近似标准误 (iid) = 1/sqrt({n}) = {1/np.sqrt(n):.3f}")

    p("\n[A] 自相关谱：相关落在哪个 lag")
    rho = {}
    for lag in range(1, 21):
        rho[lag] = float(np.corrcoef(dmean[:-lag], dmean[lag:])[0, 1])
    for lag in range(1, 21):
        star = " *" if abs(rho[lag]) > 2 / np.sqrt(n) else "  "
        p(f"  lag={lag:>2}  rho={rho[lag]:+.3f}{star}")

    p("\n[B] F 随截断长度 L 的稳定性（T 次均值的精确形态，(1-l/T) 权重）")
    p(f"  {'L':>3} {'F(T=40)':>9} {'F(T=60)':>9} {'F(T=351)':>9}")
    for L in range(1, 21):
        vals = []
        for T in (40, 60, 351):
            Lc = min(L, max(T - 1, 1))
            vals.append(1.0 + 2.0 * sum((1 - l / T) * rho[l] for l in range(1, Lc + 1)))
        p(f"  {L:>3} {vals[0]:>9.3f} {vals[1]:>9.3f} {vals[2]:>9.3f}")

    p("\n[C] Newey-West 权重 (1-l/(L+1))：逐 lag 权重上限只到 L/(L+1)")
    for L in (2, 4, 5, 6, 8, 10, 12):
        f = 1.0 + 2.0 * sum((1 - l / (L + 1)) * rho[l] for l in range(1, L + 1))
        p(f"  L={L:>2}  F={f:.3f}  se x{np.sqrt(max(f,0)):.3f}  "
          f"(lag=L 权重 {1-L/(L+1):.2f})")

    p("\n[D] 直接对日度均值序列重抽（T=40），换块长/机制")
    p(f"  {'方案':<22} {'F':>7} {'se x':>7}")

    def f_boot(sampler, label: str) -> float:
        m = np.empty(REPS)
        for b in range(REPS):
            m[b] = sampler()
        f = float(m.var(ddof=1) * 40 / g0)
        p(f"  {label:<22} {f:>7.3f} {np.sqrt(max(f,0)):>7.3f}")
        return f

    def circ(b: int):
        nb = int(np.ceil(40 / b))
        s0 = rng.integers(0, n, size=nb)
        idx = np.concatenate([(int(x) + np.arange(b)) % n for x in s0])[:40]
        return dmean[idx].mean()

    for b in (5, 10, 20, 40):
        f_boot(lambda b=b: circ(b), f"循环分块 b={b}")

    def stat(q: float):
        out, cur = [], int(rng.integers(0, n))
        while len(out) < 40:
            out.append(cur)
            if rng.random() < 1.0 / q:
                cur = int(rng.integers(0, n))
            else:
                cur = (cur + 1) % n
        return dmean[np.array(out[:40])].mean()

    for q in (5.0, 10.0, 20.0):
        f_boot(lambda q=q: stat(q), f"平稳自举 均值块长 q={q:g}")

    p("\n[E] 分段稳定性：rho4/rho5 是不是一次事件簇造成的")
    half = n // 2
    for tag, seg in (("前半", dmean[:half]), ("后半", dmean[half:])):
        rr = [float(np.corrcoef(seg[:-l], seg[l:])[0, 1]) for l in (4, 5)]
        p(f"  {tag} n={len(seg)}  rho4={rr[0]:+.3f} rho5={rr[1]:+.3f}  "
          f"(se~{1/np.sqrt(len(seg)):.3f}）")

    p("\n[E2] 不算模型，直接量：不同长度窗口的均值有多散（重叠窗口，horizon 匹配）")
    p(f"  {'T':>4} {'n_win':>6} {'sd(win_mean)':>13} {'iid sd':>8} {'F':>6}")
    F_win = {}
    for T in (20, 40, 60, 120):
        roll = np.convolve(dmean, np.ones(T) / T, "valid")
        s_ov = float(roll.std(ddof=1))
        s_iid = float(np.sqrt(g0 / T))
        F_win[T] = (s_ov / s_iid) ** 2
        p(f"  {T:>4} {len(roll):>6} {s_ov*1e4:>13.1f} {s_iid*1e4:>8.1f} {F_win[T]:>6.2f}")
    p("  => T=20/40/60 稳定在 1.63-1.68（有平台），T=120 掉到 0.93（长窗都贴着全样本均值）")
    p("  => 以 F=1.68(T=40)/1.63(T=60) 为准；分块 b=40 的 2.31 是块数太少（nb=1）的噪声")

    p("\n[F] 换 F 之后 T* 与 60 天（k=4.6 是设计实际值：sel_rate 0.0371 x 122.7）")
    def var_e(k: float) -> float:
        return float(np.mean(within * (N - k) / (k * N)))

    F5 = 1.0 + 2.0 * sum((1 - l / 40) * rho[l] for l in range(1, 6))
    F5only = 1.0 + 2.0 * (1 - 5 / 40) * rho[5]
    p(f"  F(仅 lag5) = {F5only:.3f}   F(L=5) = {F5:.3f}   F(NW L=5) = 1.161   "
      f"F(窗口实测 T=40) = {F_win[40]:.3f}   阈值 = {TARGET}")
    p(f"  {'k':>5} {'F':>6} {'se60':>7} {'T*':>7} {'MDE60':>7} {'过阈值?':>8}")
    TT: dict[tuple[float, float], float] = {}
    for k in (4.6, 5, 11, 25):
        ve = var_e(k)
        for F in (1.0, 1.161, F5only, F5, F_win[40]):
            t = Z ** 2 * (g0 * BP * F + ve * BP) / TARGET ** 2
            se = np.sqrt((g0 * F + ve) / 60) * 1e4
            ok = "过" if Z * se < TARGET else "**不过**"
            TT[(k, F)] = t
            p(f"  {k:>5} {F:>6.3f} {se:>7.1f} {t:>7.1f} {Z*se:>7.1f} {ok:>8}")

    p("\n[G] 判定")
    p("  F 取 1.161(NW, lag5 权重 0) 时 k=4.6 的 MDE60 仍过阈值，但那是构造性下偏的 F。")
    p(f"  F 取 {F5only:.3f}（仅 lag5，分段稳定）时余量只剩 0.8%；F 取 {F5:.3f} 时不过，缺 2.9%。")
    p(f"  四条路线（仅lag5 / L=5 / 窗口实测 / 平稳自举q=10）落在 1.22-1.68，取 {F_win[40]:.3f} 为点估计")
    p(f"  => 结论：k=4.6 时 60 天不够（T*={TT[(4.6, F_win[40])]:.1f}），"
      f"k=11 时够，余量 {60/TT[(11, F_win[40])]-1:.0%}（T*={TT[(11, F_win[40])]:.1f}）。")
    p("  => 「60 天成立」这句话只在 F=1.161 那一支成立；那一支把 lag5 权重压到了 0。")
    p("  注：T* 对 F 不是乘法关系。T* = Z^2(g0*F + Var(e_k))/target^2，只有 g0 那一半被放大。")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    p(f"\nwritten {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
