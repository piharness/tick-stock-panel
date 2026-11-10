"""门禁口径 T* 的第四个未检验假设：日间独立。

`_audit_intraday_budget.mde()` 与 `_diag_power_mc.level_se()` 都用
se(T) = sqrt(var_day / T)，即把 T 个交易日当 T 个独立观测（前者用解析方差，
后者用按日**有放回**重抽，两者都毁掉日间序列相关）。

本项目自己记过：日频自相关让朴素 t 高估约 26%（r0ex 40.5 -> HAC 32.3）。
若该系数在门禁估计量上同样成立，则 T* ∝ se^2 要乘 1.26^2 ≈ 1.59。
这不是小数：53 天 -> 84 天，落在 3 段之外。

做法：对同一宇宙、同一 k=5 抽样规则，比较两种重抽下的 T 日均值标准差
  iid 档：按日有放回抽 T 天（= 现有 MC / 解析式的假设）
  blk 档：循环分块抽（块长 5 天，保住日内->日间延续的共同因子）
两档用同一个 k、同一个 T，唯一差别是有没有保住日间相关。

输出：_diag_tstar_hac_20261110.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _audit_intraday_budget import build_universe  # noqa: E402

OUT = HERE / "_diag_tstar_hac_20261110.txt"
LINES: list[str] = []
Z = 1.959964 + 0.841621
TARGET = 95.73
REPS = 2000
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
    n_day = len(idx_by_day)
    r = u["r0_lim"].to_numpy()

    # 参考量：全事件日度均值序列的自相关（含共同因子，未做抽样）
    dmean = np.array([r[v].mean() for v in idx_by_day])
    p(f"pool days={n_day} n={len(u):,} mean={r.mean()*1e4:.1f}bp "
      f"sd_day_mean={dmean.std(ddof=1)*1e4:.1f}bp")

    p("\n[A] 日度均值序列自相关（全事件口径，非 k=5 抽样口径，只作背景）")
    for lag in range(1, 6):
        c = np.corrcoef(dmean[:-lag], dmean[lag:])[0, 1]
        p(f"  lag={lag}  rho={c:+.3f}")
    # Bartlett 带宽 5 的方差膨胀因子
    fac = 1.0 + 2.0 * sum((1 - l / 6) * np.corrcoef(dmean[:-l], dmean[l:])[0, 1]
                          for l in range(1, 6))
    p(f"  Bartlett(L=5) 方差膨胀因子 = {fac:.3f}  => se 乘 {np.sqrt(max(fac, 1e-9)):.3f}")

    p("\n[B] k=5 估计量：iid 按日重抽 vs 循环分块重抽（块长 5）")
    p(f"  {'T':>4} {'se_iid':>8} {'se_blk':>8} {'比值':>6} {'T*_iid':>8} {'T*_blk':>8}")

    def draw_iid(T: int, k: int) -> np.ndarray:
        di = rng.integers(0, n_day, size=T)
        sub = np.empty((T, k), dtype=np.int64)
        for t, d in enumerate(di):
            sub[t] = rng.choice(idx_by_day[d], size=k, replace=False)
        return sub

    starts = np.arange(n_day)

    def draw_blk(T: int, k: int, blen: int) -> np.ndarray:
        """循环分块：起点均匀、块内连续绕回，块数 ceil(T/blen)。"""
        nb = int(np.ceil(T / blen))
        sub = np.empty((nb * blen, k), dtype=np.int64)
        for b in range(nb):
            s0 = int(rng.integers(0, n_day))
            for j in range(blen):
                d = (s0 + j) % n_day
                sub[b * blen + j] = rng.choice(idx_by_day[d], size=k, replace=False)
        return sub[:T]

    out = {}
    for T in (40, 53, 60, 84, 120):
        ei = np.empty(REPS)
        eb = np.empty(REPS)
        for b in range(REPS):
            ei[b] = r[draw_iid(T, 5)].mean()
            eb[b] = r[draw_blk(T, 5, 5)].mean()
        si = float(ei.std(ddof=1) * 1e4)
        sb = float(eb.std(ddof=1) * 1e4)
        ti = T * (Z * si / TARGET) ** 2
        tb = T * (Z * sb / TARGET) ** 2
        out[T] = (si, sb, ti, tb)
        p(f"  {T:>4} {si:>8.2f} {sb:>8.2f} {sb/si:>6.2f} {ti:>8.1f} {tb:>8.1f}")

    p("\n[C] 判定")
    s40i, s40b, _, _ = out[40]
    ratio = s40b / s40i
    p(f"  40 天处 se 比值 = {ratio:.3f}  =>  T* 乘 {ratio**2:.2f}")
    p(f"  T*_iid(52.8) x {ratio**2:.2f} = {52.83*ratio**2:.1f} 天")
    p(f"  若比值 <= 1.02 ⇒ 日间相关对门禁估计量可忽略，60 天成立")
    p(f"  若比值 >= 1.15 ⇒ T* >= 70 天，60 天（3 段）不够，须 4-5 段")

    OUT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    p(f"\nwritten {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
