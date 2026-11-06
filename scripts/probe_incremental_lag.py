"""探针：增量序列的滞后阶数由**持有期**决定，不由 `window` 决定。（§9.20）

§9.18 在这份账本上量出 `r1(d) = +0.98`，据此把 `lag` 从 `horizon-1` 改成
`max(horizon-1, window-1)`。那个读数不是零假设下的性质，原因有两层：

  1. **账本没有抽样噪声。** `_rows()` 每天恰好 18/20 或 2/20 命中。此时
     `d_i = δ_i·(p_clim+p_i-2y_i)` 里的 `y_i` 是常数，`d_i` 只剩 `δ_i` 的
     缓慢漂移 ⇒ r1 接近 1，与 `window` 无关。
  2. **账本有制度翻转。** 90% → 10% 让 `d_i` 在翻转后持续为正。那份持续性
     是**信号**，不是零假设下的依赖。HAC 要校正的是后者。

判据也不是 r1。HAC 校的是「均值的方差」，该看的是两件事：零假设下会不会
误报（尺寸）、备择下能不能报出来（功效）。本探针把两件事分开量：

  [A] 四种账本上的 r1：平稳/翻转 × 确定性/抽样。
  [B] 尺寸与功效：`lag=0` vs `lag=w-1`，n=57 与 n=200。
  [C] §9.18 的翻转表复现——它量的是功效消失，不是数据发现。
      这一节还断言本文件的结论折算与生产逐位一致：判据的单位是**绝对**
      Brier 下界，不是 `lcb / brier_clim`（§9.21 修）。
  [D] 结论。

用法：
    python scripts/probe_incremental_lag.py
"""
from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.decision import pwin  # noqa: E402
from app.decision.settle import hac_lcb  # noqa: E402
from probe_truncation_power import HORIZON, _daily_clim, _rows  # noqa: E402

KAPPA = 20.0
PRIOR = pwin.P_CONST_MEDIAN
PER_DAY = 20
REPS = 200


def acf(values: list[float], k: int) -> float:
    n = len(values)
    if n <= k:
        return 0.0
    mean = sum(values) / n
    dev = [v - mean for v in values]
    g0 = sum(d * d for d in dev) / n
    if g0 <= 0:
        return 0.0
    return sum(dev[i] * dev[i + k] for i in range(n - k)) / n / g0


def d_series(days: list[list[int]], window: int) -> list[float]:
    """逐日增量改善，O(1) 每天。

    `d_i = mean_j[(p_clim - y_j)² - (p - y_j)²]`，而 `y ∈ {0,1}` ⇒
    `Σ(p-y)² = p²·m - 2p·h + h`。用前缀和即可，不必展开 0/1 列表。
    """
    h = [sum(d) for d in days]
    m = [len(d) for d in days]
    pre_h, pre_m = [0], [0]
    for hi, mi in zip(h, m):
        pre_h.append(pre_h[-1] + hi)
        pre_m.append(pre_m[-1] + mi)

    out: list[float] = []
    for i in range(1, len(days)):
        lo = max(0, i - window) if window > 0 else 0
        kept_h, kept_m = pre_h[i] - pre_h[lo], pre_m[i] - pre_m[lo]
        if kept_m == 0:
            continue
        p = (kept_h + KAPPA * PRIOR) / (kept_m + KAPPA)
        pc = (pre_h[i] + KAPPA * PRIOR) / (pre_m[i] + KAPPA)
        b = (p * p * m[i] - 2 * p * h[i] + h[i]) / m[i]
        bc = (pc * pc * m[i] - 2 * pc * h[i] + h[i]) / m[i]
        out.append(bc - b)
    return out


def _iid(q: float, n: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    return [[1 if rng.random() < q else 0 for _ in range(PER_DAY)] for _ in range(n)]


def _det(q: float, n: int) -> list[list[int]]:
    wins = round(PER_DAY * q)
    return [[1] * wins + [0] * (PER_DAY - wins) for _ in range(n)]


def _verdict_of(lcb_abs: float) -> str:
    """生产判据的折算：**绝对** Brier 下界对 `MIN_INCREMENTAL_EFFECT`。

    门槛是绝对量（0.001 Brier）。拿 `lcb / brier_clim` 去比它，等于把
    0.4% 的相对改善当成 0.1% 的绝对改善——`window=51/52` 就是这么被这份
    探针报成 INCREMENTAL 的，而生产报 MARGINAL（§9.21）。
    """
    if lcb_abs <= 0:
        return pwin.VERDICT_NO_INCREMENT
    if lcb_abs < pwin.MIN_INCREMENTAL_EFFECT:
        return pwin.VERDICT_MARGINAL
    return pwin.VERDICT_INCREMENTAL


def _reject(days: list[list[int]], window: int) -> tuple[bool, bool]:
    s = d_series(days, window)
    e0 = hac_lcb(s, 0)
    ew = hac_lcb(s, window - 1)
    assert e0 is not None and ew is not None
    return e0[2] > 0, ew[2] > 0


def _rates(kind: str, n: int, window: int) -> tuple[float, float]:
    r0 = rw = 0
    for seed in range(REPS):
        if kind == "H0":
            days = _iid(0.3, n, 10_000 + seed)
        else:
            days = _iid(0.9, n // 2, 20_000 + seed) + _iid(0.1, n - n // 2, 30_000 + seed)
        a, b = _reject(days, window)
        r0 += a
        rw += b
    return r0 / REPS, rw / REPS


def main() -> None:
    print("[A] r1(d)：平稳/翻转 × 确定性/抽样（window=12，n≈59）")
    print("    账本                       r1      r2      说明")
    print("    " + "-" * 66)
    cases = [
        ("平稳 + 确定性", _det(0.3, 60), "y 是常数，只剩 δ 漂移"),
        ("平稳 + 二项抽样", _iid(0.3, 60, 7), "零假设的真实形态"),
        ("翻转 + 确定性", _det(0.9, 30) + _det(0.1, 30), "§9.18 用的就是这份"),
        ("翻转 + 二项抽样", _iid(0.9, 30, 701) + _iid(0.1, 30, 702), "有信号，也有噪声"),
    ]
    for name, days, note in cases:
        s = d_series(days, 12)
        print(f"    {name:<22}  {acf(s, 1):>+6.3f}  {acf(s, 2):>+6.3f}  {note}")
    print("    r1 跟的是「确定性」和「信号」，不是零假设下的依赖。")
    print("    零假设（平稳 + 抽样）那一行才是 HAC 要校正的对象：r1≈0。")

    print("\n[B] 尺寸与功效（w=40，各 200 次重复）")
    print("    场景              n     lag=0 拒绝率   lag=w-1 拒绝率")
    print("    " + "-" * 62)
    for kind, label in (("H0", "H0 平稳 0.3"), ("H1", "H1 翻转 0.9→0.1")):
        for n in (58, 200):
            a, b = _rates(kind, n, 40)
            print(f"    {label:<17} {n - 1:>3}    {a:>9.1%}      {b:>9.1%}")
    print("    H0 的拒绝率是误报；H1 的拒绝率是功效。")
    print("    n=57 时 lag=w-1 把功效从 100% 打到 0%，而尺寸并没有变好（本来就是 4.5%）。")

    print("\n[C] §9.18 的翻转表（确定性账本，可复现）")
    print("    window   lag=0 下界    lag=w-1 下界   lag=0 结论      lag=w-1 结论")
    print("    " + "-" * 72)
    rows = _rows()
    for window in (5, 12, 20, 28, 40, 51, 52, 53):
        s = _daily_clim(rows, window)
        wf = pwin.walk_forward(rows, horizon=HORIZON, window=window, kappa=KAPPA)
        e0 = hac_lcb(s, 0)
        ew = hac_lcb(s, window - 1)
        assert e0 is not None and ew is not None
        # lag=0 的结论直接读生产字段，并断言本文件的折算函数与它一致。
        # 探针自己复刻判据时，用的单位曾经和生产不同（相对 vs 绝对），
        # 于是 51/52 两个窗口在这里报 INCREMENTAL、生产报 MARGINAL——
        # 探针和被探的东西分叉，比没有探针更糟。
        v0 = _verdict_of(e0[2])
        assert v0 == wf["verdict_incremental"], (window, v0, wf["verdict_incremental"])
        vw = _verdict_of(ew[2])
        print(f"    {window:>6}  {e0[2]:>+12.6f}  {ew[2]:>+13.6f}  "
              f"{v0:<14} {vw}")

    flips = 0
    for window in range(1, 58):
        s = _daily_clim(rows, window)
        e0 = hac_lcb(s, 0)
        ew = hac_lcb(s, window - 1)
        assert e0 is not None and ew is not None
        if (_verdict_of(e0[2]) == pwin.VERDICT_INCREMENTAL
                and _verdict_of(ew[2]) != pwin.VERDICT_INCREMENTAL):
            flips += 1
    print(f"    同一份账本、同一批数据，只改滞后：{flips} 个窗口从 INCREMENTAL 掉出去。")
    print("    这不是「发现了假阳性区间」，是「把检验关掉了」——[B] 的 H1 那一行是同一件事。")

    print("\n[D] 结论")
    print("  · 零假设下 r1(d)≈0 ⇒ `lag=0`（horizon=1）是正确读数，不是「假定为零」；")
    print("    `n_eff=n` 也是正确值，不是把 n 抄了一遍。")
    print("  · `lag=w-1` 的唯一效果是在小样本上把功效打没：n=57 时 100% → 0%，")
    print("    而 H0 的误报率本来就是 4.5%，不需要它来修。")
    print("  · 增量序列与水平序列共用 `horizon-1`。§9.20 撤回 §9.18。")


if __name__ == "__main__":
    main()
