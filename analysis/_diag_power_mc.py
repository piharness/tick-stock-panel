"""用蒙特卡洛独立复核 _audit_intraday_budget 的功效表（不重算解析式，直接抽样）。

为什么要复核：
  预算判定的整个决策 = 「T*(rho) 是多少天」。T* 由解析式给出：
      se = sqrt(2(1-rho)·sd^2(1-k/n)/k / T)
  该式有三个未经检验的假设：日间独立、sd 用事件级 sd、配对差与门禁 C3 同估计量。
  这里用「按日重抽（含共同日因子）+ 日内无放回抽 k 只」的蒙特卡洛给独立答案：
    L 档：门禁 C3 真正要的估计量 —— 子集 EV 本身（不是与总体之差）
    D 档：解析式口径 —— 两套筛选的配对差
  两者若差一个量级，说明 rho 参数化的不是门禁所判的东西。

输出：_diag_power_mc_20261110.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _audit_intraday_budget import build_universe  # noqa: E402

OUT = HERE / "_diag_power_mc_20261110.txt"
LINES: list[str] = []
Z = 1.959964 + 0.841621
TARGET = 95.73          # -(-75.73) + 20bp
REPS = 4000
SEED = 20261110


def p(s: str) -> None:
    print(s, flush=True)
    LINES.append(s)


def main() -> int:
    _, u = build_universe()
    rng = np.random.default_rng(SEED)

    days = sorted(u["date"].unique())
    idx_by_day = [u.index[u["date"] == d].to_numpy() for d in days]
    idx_by_day = [v for v in idx_by_day if len(v) >= 6]
    r = u["r0_lim"].to_numpy()
    p(f"pool days={len(idx_by_day)} n={len(u):,} mean={r.mean()*1e4:.1f}bp")

    def draw_days(T: int, k: int):
        """按日重抽 T 天（含放回，捕获日间共同因子），每日无放回抽 k 只。"""
        di = rng.integers(0, len(idx_by_day), size=T)
        sub = np.empty((T, k), dtype=np.int64)
        for t, d in enumerate(di):
            sub[t] = rng.choice(idx_by_day[d], size=k, replace=False)
        return sub

    def level_se(T: int, k: int) -> tuple[float, float]:
        ev = np.empty(REPS)
        for b in range(REPS):
            s = draw_days(T, k)
            ev[b] = r[s].mean()
        return float(ev.std(ddof=1) * 1e4), float(ev.mean() * 1e4)

    def diff_se(T: int, k: int, rho: float) -> float:
        m = int(round(rho * k))
        out = np.empty(REPS)
        for b in range(REPS):
            di = rng.integers(0, len(idx_by_day), size=T)
            da, db = 0.0, 0.0
            for t, d in enumerate(di):
                pool = idx_by_day[d]
                a = rng.choice(pool, size=k, replace=False)
                if m >= k:
                    bb = a
                else:
                    common = a[:m]
                    rest_pool = np.setdiff1d(pool, common, assume_unique=False)
                    rest = rng.choice(rest_pool, size=k - m, replace=False)
                    bb = np.concatenate([common, rest])
                da += r[a].mean()
                db += r[bb].mean()
            out[b] = (da - db) / T
        return float(out.std(ddof=1) * 1e4)

    p("\n[L] 门禁 C3 的估计量：子集 EV 本身（随机筛选，无信息下的抽样 sd）")
    p(f"  {'T':>4} {'k':>3} {'se_bp':>8} {'MDE_bp':>8} {'T*(95.7bp)':>11}")
    tstar_level = {}
    for T in (40, 60, 80, 120):
        for k in (5,):
            se, mu = level_se(T, k)
            mde = Z * se
            ts = T * (mde / TARGET) ** 2
            tstar_level[T] = ts
            p(f"  {T:>4} {k:>3} {se:>8.2f} {mde:>8.1f} {ts:>11.1f}   (mu={mu:.1f}bp)")

    p("\n[D] 解析式口径：两套筛选的逐日配对差（含重叠率 rho）")
    p(f"  {'rho':>5} {'MC se':>8} {'MC MDE':>8} {'解析 MDE':>9} {'比值':>6} {'T*':>7}")
    analytic = {0.0: (47.24, 132.35, 76.5), 0.5: (33.41, 93.59, 38.2),
                0.8: (21.13, 59.19, 15.3), 0.95: (10.56, 29.60, 3.8)}
    for rho in (0.0, 0.5, 0.8, 0.95):
        se = diff_se(40, 5, rho)
        mde = Z * se
        a_se, a_mde, a_t = analytic[rho]
        p(f"  {rho:>5.2f} {se:>8.2f} {mde:>8.1f} {a_mde:>9.1f} {mde/a_mde:>6.2f} "
          f"{40*(mde/TARGET)**2:>7.1f}   (解析 se={a_se}, T*={a_t})")

    p("\n[e] 结论量：把 95.7bp 分辨到 0 所需交易日（门禁口径 vs 解析式口径）")
    se40, _ = level_se(40, 5)
    t_l = 40 * (Z * se40 / TARGET) ** 2
    p(f"  门禁口径 T* = {t_l:.1f} 天（k=5）")
    p(f"  解析式 T*(rho=0) = 76.5 天；T*(rho=0.5) = 38.2 天")
    p(f"  倍数 = {t_l/76.5:.2f}x (vs rho=0) / {t_l/38.2:.2f}x (vs rho=0.5)")
    p(f"  40 交易日单次请求上限 ⇒ 需 {int(np.ceil(t_l/40))} 段请求/标的，仍免费")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    p(f"\nwritten {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
