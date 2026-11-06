"""§10 第 3 条欠着的那一步：用**不重叠 cohort** 校验块重抽的 se 是否被低估。

§6.2 与 §6.3 争的是「块长取 20 还是 40」。但块长只是逼近长程方差的一种手段，
同一件事还有一个**不需要块长**的量法：每 20 天取一个窗，窗与窗不重叠，于是
`k=n/20` 个观测近似独立，se 就是它们 sd/√k。它不逼近，它直接算。

这台设备（循环块重抽）的三个参数——B、reps、MIN_BLOCKS——在这里全部消失。
所以 cohort 可以当外部基准：块重抽的 se 若与它一致，块长就把记忆收全了；
若系统性地低于它，那台设备报的 MDE 与 p 都偏窄，且与选哪个 B 无关。

四张表：

  一、n=326：块重抽 se / cohort se。1.00 = 块长收全了记忆。
  二、n=250、8 个起点：确认段真正落到的长度上，这个比值是多少。§6.3 的
     「±20% 读数误差」到底该记在「窗太短」还是「B 选错」账上。
  三、同一个 250 天窗、不同 B 给出的**判定标签**：翻几次。§4 说「固定 250
     天、不看中期」，但 326 天里能切出 8 个 250 天窗——窗的选择本身没有
     登记，而它足以改变标签。
  四、cohort 口径下 250 天的 MDE：§4 的 FAIL 分支（MDE≤200）还够不够得着。

用法：
    python scripts/probe_cohort_check.py [--basis=mean|median|mcap] [--reps=400]
"""
from __future__ import annotations

import importlib.util
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRIDE = 20          # 持有期。cohort 的步长取它：窗与窗不重叠
WINDOW = 250         # 确认段（与 probe_net_diff.CONFIRM_N 同源）
STEP = 10            # 滚窗起点步长
MIN_OFFSET = 6       # 一个 offset 至少切出这么多窗，才让它的 se 进中位数
MDE_Z = 2.486        # 80% 功效、单侧 5%：与 probe_net_diff 同一定义


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pnd = _load("probe_net_diff", "scripts/probe_net_diff.py")
GATE = pnd.GATE


def series(basis: str | None = None) -> dict[str, list[float]]:
    """日序列（每笔 20 日超额，bp）。与 `probe_net_diff` 同源、同算法。"""
    b = basis or pnd.BASIS
    cache = ROOT / "data" / (".probe_per_day_exc.json" if b == "median"
                             else f".probe_per_day_exc_{b}.json")
    exc = json.loads(cache.read_text(encoding="utf-8"))
    days = sorted(set.intersection(*(set(v) for v in exc.values())))
    GATE.require_exploration(days, "probe_cohort_check.py", sys.argv[1:])
    return {k: [sum(v[d]) / len(v[d]) * 1e4 for d in days] for k, v in exc.items()}


def cohort_means(x: list[float], offset: int = 0, stride: int = STRIDE) -> list[float]:
    return [sum(x[i:i + stride]) / stride
            for i in range(offset, len(x) - stride + 1, stride)]


def cohort_se(x: list[float], stride: int = STRIDE) -> tuple[float, int]:
    """cohort se：所有对齐方式的中位数。返回 (se, 中位窗数)。

    **必须对 offset 取中位数**，否则「从第几天开始切」会变成一个没登记的
    自由度——它只影响边界窗，但 n=250 只有 12 个窗，一个边界窗就是 8%。
    """
    ses, ks = [], []
    for off in range(stride):
        ms = cohort_means(x, off, stride)
        if len(ms) >= MIN_OFFSET:
            ses.append(statistics.stdev(ms) / math.sqrt(len(ms)))
            ks.append(len(ms))
    return statistics.median(ses), int(statistics.median(ks))


def section_one(ser: dict[str, list[float]], reps: int) -> None:
    n = len(next(iter(ser.values())))
    print(f"\n一、n={n}：块重抽（B={pnd.BLOCK}）与 cohort 的 se（bp）")
    print(f"{'策略':<15}{'se_块':>9}{'se_cohort':>11}{'比值':>8}"
          f"{'MDE_块':>9}{'MDE_cohort':>12}{'k':>4}")
    for name, x in ser.items():
        r = pnd.evaluate_series(x, block=pnd.BLOCK, reps=reps, seed=0)
        se, k = cohort_se(x)
        print(f"{name:<15}{r['sd_b']:>9.1f}{se:>11.1f}"
              f"{se / r['sd_b']:>8.2f}{r['mde']:>9.1f}"
              f"{MDE_Z * se:>12.1f}{k:>4}")
    print("比值 1.00 = 块长把记忆收全了；<1 = 块重抽偏窄，它报的 MDE 与 p 都"
          "过于乐观。")


def window_table(ser: dict[str, list[float]], reps: int,
                 blocks=(20, 40, 60)) -> dict:
    """250 天窗上，各 B 的 se 与 cohort 的比。返回 {策略: {B: [比值…]}}。"""
    out: dict[str, dict[int, list[float]]] = {}
    starts = list(range(0, len(next(iter(ser.values()))) - WINDOW + 1, STEP))
    for name, x in ser.items():
        base = [cohort_se(x[s:s + WINDOW])[0] for s in starts]
        out[name] = {}
        for b in blocks:
            ratios = []
            for s, c in zip(starts, base):
                r = pnd.evaluate_series(x[s:s + WINDOW], block=b, reps=reps, seed=0)
                ratios.append(r["sd_b"] / c)
            out[name][b] = ratios
    return out


def section_two(rows: dict, starts: int) -> None:
    print(f"\n二、n={WINDOW}（{starts} 个起点）：块重抽 se / cohort se")
    print(f"{'策略':<15}{'B':>4}{'中位':>8}{'最差':>8}{'5% 分位':>9}")
    for name, per_b in rows.items():
        for b, rs in per_b.items():
            print(f"{name:<15}{b:>4}{statistics.median(rs):>8.2f}"
                  f"{max(abs(v - 1) for v in rs):>8.0%}"
                  f"{sorted(rs)[max(0, len(rs) // 20)]:>9.2f}")


def label_table(ser: dict[str, list[float]], reps: int,
                blocks=(20, 40)) -> dict[str, dict[int, list[str]]]:
    out = {}
    starts = list(range(0, len(next(iter(ser.values()))) - WINDOW + 1, STEP))
    for name, x in ser.items():
        out[name] = {}
        for b in blocks:
            labs = []
            for s in starts:
                r = pnd.evaluate_series(x[s:s + WINDOW], block=b, reps=reps, seed=0)
                labs.append(pnd.classify(r["net_lo"], r["net_hi"], r["mde"]))
            out[name][b] = labs
    return out


def section_three(rows: dict[str, dict[int, list[str]]]) -> None:
    print(f"\n三、同一个 n={WINDOW}、不同 B 的判定标签（每个起点一格）")
    print(f"{'策略':<15}{'B':>4}{'标签种类':>9}  明细")
    for name, per_b in rows.items():
        for b, labs in per_b.items():
            kinds = sorted(set(labs))
            print(f"{name:<15}{b:>4}{len(kinds):>9}  " + " ".join(
                f"{k}:{labs.count(k)}" for k in kinds))
    print("种类 >1 即：§4 的标签是「取哪一个 250 天窗」的函数，不是数据的函数。")


def section_four(ser: dict[str, list[float]]) -> None:
    """§4 的 FAIL 分支（MDE≤200）在 250 天上够不够得着。

    **不能用中位数回答。** 中位数说 reversal 是 224，看起来「门外」；但 8 个
    250 天窗里有 3 个 ≤200。用中位数下结论，等于把「窗的抽签」当成一个点。
    所以这里数窗：`≤200 窗数/8`。它同时回答两件事——这一档能不能被判 FAIL，
    以及这个「能」有多依赖抽到哪一段。
    """
    n = len(next(iter(ser.values())))
    starts = list(range(0, n - WINDOW + 1, STEP))
    print(f"\n四、{WINDOW} 天上 MDE≤{pnd.MDE_GATE:.0f}（§4 的 FAIL 分支）能不能到达")
    print(f"{'策略':<15}{'MDE@%d' % n:>9}{'@%d 中位' % WINDOW:>11}"
          f"{'≤200 窗数':>10}")
    for name, x in ser.items():
        se326, _ = cohort_se(x)
        ms = [MDE_Z * cohort_se(x[s:s + WINDOW])[0] for s in starts]
        hit = sum(1 for v in ms if v <= pnd.MDE_GATE)
        print(f"{name:<15}{MDE_Z * se326:>9.1f}{statistics.median(ms):>11.1f}"
              f"{f'{hit}/{len(ms)}':>10}")
    print("只有 reversal 在部分窗上够得着，且是 3/8——FAIL 不是「分辨率够了」，"
          "是抽到了窄的那一段。其余三档在任何窗上都到不了：那不是分辨率不足，"
          "是这一格在 250 天上为空。")


def main() -> None:
    reps = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
                 if a.startswith("--reps=")), pnd.REPS)
    ser = series()
    n = len(next(iter(ser.values())))
    print(f"样本 {n} 天；cohort 步长 {STRIDE}、窗长 {WINDOW}、起点步长 {STEP}；"
          f"重抽 {reps} 次")
    section_one(ser, reps)
    rows = window_table(ser, reps)
    per_b = next(iter(rows.values()))
    section_two(rows, len(per_b[next(iter(per_b))]))
    section_three(label_table(ser, reps))
    section_four(ser)


if __name__ == "__main__":
    main()
