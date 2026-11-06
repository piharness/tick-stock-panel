"""策略两两配对的差分：可复现的 se 与「谁比谁好」的边界。

配对的价值不在排名，在**分母**：同一天的 20 条 pick 共享行情，两个策略的
日胜率差把共同成分消掉，差分的 se 比两个水平各自相加小得多。本文把这件
事钉成表，并顺手解决一个口径争议：同一份账本，名义显著的对照数会随 se
的估计方式在 5 与 3 之间跳。

两条 se：
  - HAC：截断核，lag=40（等于分块长度），`LRV` 下界取 γ0，与
    `settle.hac_lcb` 同一约定；
  - 块重抽：40 天循环块，固定种子，1000 次。

判定用双侧正态。多重比较按 6 次 Bonferroni（α=0.05/6≈0.0083）另算一行——
**名义 5/6、校正后 3/6** 是本账本的读数，"4/6" 在任何一种规则下都复现不出来。

⚠️ 跨策略的胜率差多半是选股池不同的同义反复：`low_turnover` 与
`high_turnover` 的 universe 几乎不重叠，18–23pp 的差是「换了股票」，不是
「这个方法更好」。这张表能支撑的是「配对把 se 压到 2–7pp」，不是排名。

用法：
    python scripts/probe_paired_diff.py
"""
from __future__ import annotations

import itertools
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / ".probe_per_day.json"
LAG, BLOCK, REPS, SEED = 40, 40, 1000, 0
NMAX = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--nmax=")), None)


def se_hac(x: list[float], lag: int = LAG) -> float:
    n = len(x)
    mu = sum(x) / n
    dev = [v - mu for v in x]
    g0 = sum(d * d for d in dev) / n
    lr = g0
    for k in range(1, min(lag, n - 1) + 1):
        lr += 2 * sum(dev[i] * dev[i + k] for i in range(n - k)) / (n - k)
    return math.sqrt(max(lr, g0) / n)


def se_block(x: list[float], block: int = BLOCK, reps: int = REPS,
             seed: int = SEED) -> float:
    n = len(x)
    rng = random.Random(seed)
    nb = math.ceil(n / block)
    vals = []
    for _ in range(reps):
        idx: list[int] = []
        for _ in range(nb):
            s = rng.randrange(n)
            idx.extend((s + k) % n for k in range(block))
        z = [x[i] for i in idx[:n]]
        vals.append(sum(z) / n)
    mu = sum(vals) / len(vals)
    return (sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def p_two_sided(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2))


def main() -> None:
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    days = sorted(set.intersection(*(set(v) for v in data.values())))
    if NMAX:
        days = days[:NMAX]
    ser = {k: [sum(data[k][d]) / len(data[k][d]) for d in days] for k in data}
    names = list(data)

    head = (f"{'对照':>27}{'Δ(pp)':>8}{'se_HAC':>8}{'se_块':>7}"
            f"{'t_HAC':>7}{'p_HAC':>8}{'t_块':>7}{'p_块':>8}{'非配对':>7}")
    print(f"{len(days)} 个共同决策日（{days[0]} … {days[-1]}），"
          f"{len(names)} 个策略，{len(names)*(len(names)-1)//2} 对")
    print(head)
    ps = []
    for a, b in itertools.combinations(names, 2):
        d = [x - y for x, y in zip(ser[a], ser[b])]
        m = sum(d) / len(d) * 100
        sh, sb = se_hac(d) * 100, se_block(d) * 100
        th, tb = m / sh, m / sb
        un = math.sqrt(se_hac(ser[a]) ** 2 + se_hac(ser[b]) ** 2) * 100
        ps.append((p_two_sided(th), p_two_sided(tb)))
        print(f"{a[:12] + '-' + b[:12]:>27}{m:>8.2f}{sh:>8.2f}{sb:>7.2f}"
              f"{th:>7.2f}{p_two_sided(th):>8.4f}{tb:>7.2f}{p_two_sided(tb):>8.4f}"
              f"{un:>7.2f}")

    print(f"\n名义 p<0.05：HAC {sum(p[0] < 0.05 for p in ps)}/6，"
          f"块重抽 {sum(p[1] < 0.05 for p in ps)}/6")
    print(f"Bonferroni p<0.0083：HAC {sum(p[0] < 0.05 / 6 for p in ps)}/6，"
          f"块重抽 {sum(p[1] < 0.05 / 6 for p in ps)}/6")
    print("\n⚠️ 排名不可外推：跨策略的差主要是 universe 不同，不是方法优劣；"
          "\n   这张表的用途是把差分的 se 钉在 2–7pp，供后续配对设计定样本量。")


if __name__ == "__main__":
    main()
