"""κ 的单位是「独立样本」，`n` 的单位是「观测」——差多少？

## 问题

`_shrink(hits, n, prior, kappa)` 把 `kappa=20` 个虚拟样本混进 `n` 条观测里。
但 386 条观测只来自 39 个决策日、20 日持有期 ⇒ 独立窗口约 2 个。所以：

    p_win = (77 + 20*0.5) / (386 + 20) = 0.214286      # 现行
    p_win = (0.2*1 + 20*0.5) / (1 + 20) = 0.485690     # 换成 n_eff=1

位移 0.271404。§9.15 把它记为「单位错误」，并规定改之前必须先用探针拿外部证据。
这个脚本就是那个探针。它要回答的不是「哪个数字好看」，而是：

**这批数据到底能把命中率估到多准？换分母之后，p_win 还落在区间里吗？**

## 三个口径

1. **非重叠窗口**：把决策日按 `horizon` 切成互不重叠的块，每块一个命中率。
   这是「你到底有几个独立观测」最不带假设的读法。
2. **设计效应（解析）**：日胜率序列 `ȳ_i` 的 `n_eff = D·(se_iid/se_hac)²`，
   HAC 滞后取 `horizon-1`。与 `settle.effective_sample` 同一套公式，只是作用在
   **胜率**序列上，而不是 Brier 差分序列上。
3. **对 p_win 的后果**：把各口径的 `n_eff` 代进收缩公式，看 p_win 与 skill。

## 为什么不用块自助

`block=horizon` 在 `D=2·horizon` 时只剩两个块，移动块重抽的期望不等于样本均值
（中间的日子被重复计入更多次），实测可以把均值 0.17 的区间抬到 [0.17, 0.27]。
在 n≈2H 这个区间里它不是可信工具，所以换成非重叠窗口 + 解析区间。

## 附带检验：增量判定的退化

`walk_forward(window=250)` 在 39 个决策日上运行时，`visible[-250:]` 永远等于
`visible` ⇒ `p ≡ p_clim` ⇒ `improvement_vs_clim` 恒为 0。旧实现据此输出
`NO_INCREMENT`（「近端不比全历史多含信息」）——那不是「没有增量」，是
「两个估计量恒等、检验无功效」。§9.16 之后这里返回 `NOT_TESTABLE` 且不报数字，
本脚本的 [D] 段用来复核这个状态（`truncated_any=False` ⇒ `NOT_TESTABLE`）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import polars as pl

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "backend"))

from probe_basis_axis import HORIZON, build  # noqa: E402

from app.decision import bars, pwin, settle  # noqa: E402

SRC = _HERE.parent / "data"
KAPPA = pwin.DEFAULT_KAPPA
PRIOR = 0.5
Z90 = 1.645


def daily(df: pl.DataFrame, field: str) -> tuple[list[str], list[float], list[int]]:
    g = (df.group_by("as_of")
           .agg(pl.col(field).mean().alias("h"), pl.len().alias("n"))
           .sort("as_of"))
    return g["as_of"].to_list(), g["h"].to_list(), g["n"].to_list()


def _se_iid(xs: list[float]) -> float:
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var / n)


def non_overlapping(hs: list[float], ns: list[int], block: int) -> list[float]:
    """按 horizon 切互不重叠的块，返回每块的合并命中率。"""
    out = []
    for s in range(0, len(hs), block):
        h, n = hs[s:s + block], ns[s:s + block]
        if sum(n) > 0:
            out.append(sum(x * y for x, y in zip(h, n)) / sum(n))
    return out


def shrink(mean: float, n_eff: float) -> float:
    return (mean * n_eff + KAPPA * PRIOR) / (n_eff + KAPPA)


def brier_skill(p: float, mean: float) -> float:
    brier = mean * (1 - p) ** 2 + (1 - mean) * p ** 2
    return 1 - brier / (PRIOR * (1 - PRIOR))


def main() -> None:
    # `--days N` 只取最近 N 个决策日，复现 `verify_pwin_e2e.py` 的 39 日账本；
    # 不带参数则是全历史（321 个可评估日那套）。
    last = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 0
    cal = bars.sessions(SRC)
    idx = list(range(1, len(cal) - HORIZON - 1))
    if last:
        idx = idx[-last:]
    picks = [(cal[i], cal[i + 1], cal[i + 1 + HORIZON]) for i in idx]
    df = build(picks)
    print(f"交易日 {len(cal)}，决策日 {df['as_of'].n_unique()}，观测 {df.height}"
          f"{f'（最近 {last} 个决策日）' if last else '（全历史）'}")
    print(f"持有期 {HORIZON}，kappa {KAPPA}，prior {PRIOR}\n")

    for field, label in (("outcome_med", "median（跑赢同日中位数）"),
                         ("outcome", "abs（ret > 0）")):
        days, hs, ns = daily(df, field)
        D, N = len(days), sum(ns)
        mean = sum(h * n for h, n in zip(hs, ns)) / N

        print(f"===== {label} =====")
        print(f"观测 n={N}  决策日 D={D}  命中 {round(mean * N)}  合并命中率 {mean:.6f}")

        blocks = non_overlapping(hs, ns, HORIZON)
        print(f"\n[A] 非重叠 {HORIZON} 日窗口：{len(blocks)} 个")
        print("    " + "  ".join(f"{b:.3f}" for b in blocks))
        if len(blocks) >= 2:
            spread = max(blocks) - min(blocks)
            print(f"    块间极差 {spread:.3f}——这就是「2 个独立观测」长什么样")

        se_iid = _se_iid(hs)
        est = settle.hac_lcb(hs, HORIZON - 1)
        se_hac = est[1]
        n_eff, reliable = settle.effective_sample(D, se_iid, se_hac, HORIZON - 1)
        lo, hi = mean - Z90 * se_hac, mean + Z90 * se_hac
        print(f"\n[B] 日胜率序列   se_iid={se_iid:.4f}  se_hac={se_hac:.4f}  "
              f"膨胀 ×{se_hac / se_iid if se_iid else float('nan'):.2f}")
        print(f"    n_eff={n_eff}  reliable={reliable}  "
              f"（构造值 D//H = {max(1, D // HORIZON)}）")
        print(f"    命中率 90% 区间 [{lo:.4f}, {hi:.4f}]"
              f"{'   ← NW 不可用，区间本身只是示意' if not reliable else ''}")

        print("\n[C] 分母 → p_win → skill")
        for name, ne in (("n（现行）", N),
                         ("n_eff（Brier 序列代理）", 1),
                         ("n_eff（胜率序列）", n_eff)):
            p = shrink(mean, ne)
            inside = lo <= p <= hi
            print(f"    {name:<24} n_eff={ne:<5} p_win={p:.6f}  "
                  f"skill_vs_0.5={brier_skill(p, mean):+.4f}  "
                  f"{'在区间内' if inside else '在区间外'}")

        wf = pwin.walk_forward(
            [{"as_of": r["as_of"], "outcome_med": r["outcome_med"],
              "outcome": r["outcome"], "horizon_sessions": HORIZON,
              "strategy_id": "s",
              "exit": {"date": cal[min(len(cal) - 1,
                                       cal.index(r["as_of"]) + 1 + HORIZON)]}}
             for r in df.iter_rows(named=True)],
            strategy_id="s", horizon=HORIZON, window=pwin.DEFAULT_WINDOW)
        # `improvement_vs_clim` 在 `NOT_TESTABLE` 时是 None（§9.16），不能直接格式化。
        imp = ("—" if wf["improvement_vs_clim"] is None
               else f"{wf['improvement_vs_clim']:+.6f}")
        print(f"\n[D] window={pwin.DEFAULT_WINDOW} 对 {wf['days']} 个决策日"
              f"（可评估 {wf['oos_days']}）：truncated_any={wf['truncated_any']}  "
              f"improvement_vs_clim={imp}  "
              f"lcb={wf['skill_vs_clim_lcb']}  "
              f"verdict={wf['verdict_incremental']}")
        if not wf["truncated_any"]:
            print("    没有一天被截断 ⇒ 两个估计量恒等，检验没有功效：不报数字。")
        print()


if __name__ == "__main__":
    main()
