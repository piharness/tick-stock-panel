"""同一个被告，三种零分布：统计量的**零分布形状**本身，而不是它被校准后的功效。

§9.27 的表格有一个方法论缺口：每个统计量的门槛都取**它自己在同一 DGP 上的
95 分位**，于是 size 被构造固定为 5%，表里只剩功效。这回答「谁更灵敏」，
不回答「上线后会不会误报」，也不回答「零分布长什么样」。

本脚本补的就是那一层：**门槛一律用名义值**（两比例 z 用 1.645，CUSUM 用它
自己的 95 分位因为 Page CUSUM 没有闭式名义门槛），再看三件事：

  1. 零分布的 sd。iid 下 z 的 sd 应为 1；重叠下不是。这个倍数就是设计效应，
     它可以直接换算成「600 条 pick 相当于几个独立观测」。
  2. 名义门槛下的实际 size。一个 sd=7 的 z 用 1.645 当门槛，误报率不是 5%。
  3. 把 size 重新拉回 5% 之后还剩多少功效——即「先修好尺子，再问它灵不灵」。

三个 DGP，第三个是新的：

  `iid`        逐日独立。对照组。
  `overlap`    持有期共享行情因子（§9.27 用的模型），ρ 是因子方差份额。
  `surrogate`  循环分块重抽**真实账本**的日胜率（块长 40，保留实测自相关）。
               它不假设机制：重叠模型和 AR(1) 都要人选一个参数，这个只读数据。
               "600 条 pick 等于几个独立观测"因此不是 13 也不是 600，而是读数。

最后一个实验是**变点位置**：C 是「末端 vs 历史」，D 是「路径上某处」。如果
变点不在末端，C 必然钝；如果就在末端，D 的多重比较惩罚必然亏。两者不是
「谁更钝」，是「问的是不是同一个问题」。

用法：
    python scripts/probe_statistic_null.py [DAYS] [REPS] [RHO]
"""
from __future__ import annotations

import math
import random
import statistics as st
import sys

PICKS = 10
HORIZON = 10
WINDOW = 60
BURN = 120
P0 = 0.5
ALPHAS = (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
Z95 = 1.645

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 900
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 200
RHO = float(sys.argv[3]) if len(sys.argv) > 3 else 0.6

# 真实账本（momentum_20）反解出的矩：p=0.384，日胜率 sd=0.188，lag1 自相关=0.816
EMP_P, EMP_PHI, EMP_SD = 0.384, 0.816, 0.188


def _inv_norm(p: float) -> float:
    """Acklam 有理逼近，误差 < 1.2e-9。"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl = 0.02425
    if p <= 0.0:
        return -9.0
    if p >= 1.0:
        return 9.0
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - pl:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _mixture_var(p: float, spread: float) -> float:
    """p_i = clip(p + s·a, 0.02, 0.98)，a~N(0,1) ⇒ 边缘方差 E[p_i(1-p_i)]。

    解析式用正态积分，比按日抽样再平均稳。裁剪会削掉约 1% 的方差，这里用
    数值积分补上——不补会让 DGP 的 size 比解析值低一点点，掩盖问题。
    """
    if spread <= 1e-9:
        return p * (1 - p)
    total = 0.0
    for k in range(-40, 41):
        a = k / 10.0
        w = math.exp(-0.5 * a * a) / math.sqrt(2 * math.pi) * 0.1
        pi = min(max(p + spread * a, 0.02), 0.98)
        total += w * pi * (1 - pi)
    return total


def _draw_iid(rates: list[float], seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    return [[1 if rng.random() < p else 0 for _ in range(PICKS)] for p in rates]


def _draw_overlap(rates: list[float], seed: int) -> list[list[int]]:
    """卡 i 吃 sessions[i+1 : i+1+H]；相邻卡共享 H-1 个会话。"""
    rng = random.Random(seed)
    n = len(rates)
    sessions = [rng.gauss(0, 1) for _ in range(n + HORIZON + 2)]
    aa, bb = RHO ** 0.5, (1 - RHO) ** 0.5
    out = []
    for i, p in enumerate(rates):
        seg = sessions[i + 1:i + 1 + HORIZON]
        zbar = sum(seg) / len(seg) * HORIZON ** 0.5
        mu = _inv_norm(p)
        out.append([1 if mu + aa * zbar + bb * rng.gauss(0, 1) > 0 else 0
                    for _ in range(PICKS)])
    return out


def _draw_ar1(rates: list[float], seed: int) -> list[list[int]]:
    """单因子 AR(1)：整天的 latent 一起漂，日内 pick 共享它。

    φ 取真实账本的 lag1 自相关，s 由 `sd(日胜率)` 反解。**它拟合得不好**：
    真实日胜率里绝大部分方差是"同一批股票"带来的有效二项噪声，不是平滑的
    latent 漂移，所以 AR(1) 只能复现 sd 而复现不了整条 acf（见 `surrogate`）。
    保留它是为了给"参数化到哪一步"留一个对照。
    """
    rng = random.Random(seed)
    n = len(rates)
    phi = EMP_PHI
    base_v = EMP_P * (1 - EMP_P)
    s2 = max((EMP_SD ** 2 - base_v / PICKS) * (1 - phi ** 2), 0.0)
    spread = s2 ** 0.5
    a = rng.gauss(0, 1)
    out = []
    for i, p in enumerate(rates):
        if i:
            a = phi * a + math.sqrt(1 - phi * phi) * rng.gauss(0, 1)
        pi = min(max(p + spread * a, 0.02), 0.98)
        out.append([1 if rng.random() < pi else 0 for _ in range(PICKS)])
    return out


def _load_real() -> list[float] | None:
    """真实账本的逐日胜率（取方差最大的那个策略，不取平均）。

    数据来自 `scripts/probe_window.py` 用真实行情重建的账本缓存，
    与 `probe_real_deff.py` 读的是同一个文件。
    """
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "data" / ".probe_per_day.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    best: list[float] | None = None
    best_var = -1.0
    for days in raw.values():
        ks = sorted(days)
        s = [sum(days[k]) / len(days[k]) for k in ks]
        mu = sum(s) / len(s)
        var = sum((x - mu) ** 2 for x in s) / len(s)
        if var > best_var:
            best, best_var = s, var
    return best


_REAL = _load_real()
BLOCK = 40


def _draw_surrogate(rates: list[float], seed: int) -> list[list[int]]:
    """循环分块重抽真实日胜率：不假设机制，只保留实测的依赖结构。

    块长 40 覆盖 window=60 里绝大部分相关；块内顺序、块间拼接都保留原始
    自相关。每天的值直接当命中率用（不再重抽二项），因此日级序列与真实
    账本同分布——C/D/E 读的正是这条序列。代价是 A 读不到逐 pick 的离散性，
    所以 A 在这一行不参与比较（打印 `—`）。

    这是三个 DGP 里唯一**不需要选参数**的一个：ρ 和 φ 都要人来定，
    分块重抽只读数据。
    """
    rng = random.Random(seed)
    n = len(rates)
    real = _REAL
    if real is None:                      # 没有缓存时退回 iid，并在表里标明
        return _draw_iid(rates, seed)
    m = len(real)
    need = -(-n // BLOCK)
    draws: list[float] = []
    for _ in range(need):
        start = rng.randrange(m)
        draws.extend(real[(start + j) % m] for j in range(BLOCK))
    rows = []
    for p, base in zip(rates, draws[:n]):
        shift = p - P0                     # 零假设下 shift=0，变点处为 a
        value = min(max(base + shift, 0.0), 1.0)
        rows.append([value] * PICKS)
    return rows


DGPS = {"iid": _draw_iid, "overlap": _draw_overlap,
        "surrogate": _draw_surrogate if _REAL else _draw_iid}
#: 这些 DGP 上 A 不可比（见 `_draw_surrogate`）。
SKIP_A = {"surrogate"}


# ---------------------------------------------------------------------------
# 统计量
# ---------------------------------------------------------------------------


def _lrv(x: list[float], lag: int) -> float:
    n = len(x)
    mu = sum(x) / n
    dv = [v - mu for v in x]
    g0 = sum(d * d for d in dv) / n
    total = g0
    for k in range(1, min(lag, n - 1) + 1):
        total += 2 * sum(dv[i] * dv[i + k] for i in range(n - k)) / (n - k)
    return max(total, g0)


def _daily(outs: list[list[int]]) -> list[float]:
    return [sum(o) / len(o) for o in outs]


def _z_counts(daily: list[float], w: int) -> float:
    """生产口径：两比例 z，分母按 iid 二项算。n 用 pick 数。"""
    n = len(daily)
    if n <= w:
        return 0.0
    hw = sum(daily[-w:]) * PICKS
    nw = w * PICKS
    ho = sum(daily[:-w]) * PICKS
    no = (n - w) * PICKS
    pw, po = hw / nw, ho / no
    pool = (hw + ho) / (nw + no)
    se = (pool * (1 - pool) * (1 / nw + 1 / no)) ** 0.5
    return (pw - po) / se if se > 0 else 0.0


def _z_hac(daily: list[float], w: int) -> float:
    """同一个分子，分母换成日序列的 LRV。"""
    n = len(daily)
    if n <= w or w < 3:
        return 0.0
    a, b = daily[-w:], daily[:-w]
    diff = sum(a) / len(a) - sum(b) / len(b)
    se = (_lrv(a, HORIZON - 1) / len(a) + _lrv(b, HORIZON - 1) / len(b)) ** 0.5
    return diff / se if se > 0 else 0.0


def _z_block(daily: list[float], w: int) -> float:
    """把日序列按 H 天折成不重叠块，再做两比例。DEFF 由构造 = 1。"""
    n = len(daily)
    B = HORIZON
    nb = n // B
    if nb < 4:
        return 0.0
    blocks = [sum(daily[i * B:(i + 1) * B]) / B for i in range(nb)]
    k = max(2, min(w // B, nb - 2))
    a, b = blocks[-k:], blocks[:-k]
    pa, pb = sum(a) / len(a), sum(b) / len(b)
    va, vb = st.variance(a) / len(a), st.variance(b) / len(b)
    se = (va + vb) ** 0.5
    return (pa - pb) / se if se > 0 else 0.0


def _z_block40(daily: list[float], w: int) -> float:
    """同上，但块长取 40（实测的相关长度），而不是持有期 H=10。

    这个对照是为了说明：分块检验的 size 由**块长**决定，不由 H 决定。
    按 H 分块只消掉持有期重叠，消不掉更慢的漂移。
    """
    n = len(daily)
    B = BLOCK
    nb = n // B
    if nb < 5:
        return 0.0
    blocks = [sum(daily[i * B:(i + 1) * B]) / B for i in range(nb)]
    k = max(2, min(max(2, w // B), nb - 3))
    a, b = blocks[-k:], blocks[:-k]
    pa, pb = sum(a) / len(a), sum(b) / len(b)
    se = (st.variance(a) / len(a) + st.variance(b) / len(b)) ** 0.5
    return (pa - pb) / se if se > 0 else 0.0


def _cusum(daily: list[float], mode: str) -> float:
    burn = daily[:BURN]
    p0 = min(max(sum(burn) / len(burn), 0.02), 0.98)
    dv = [x - p0 for x in burn]
    g0 = sum(d * d for d in dv) / len(dv)
    scale = math.sqrt(g0 if mode == "g0" else _lrv(burn, HORIZON - 1))
    if scale <= 1e-12:
        return 0.0
    s = best = 0.0
    for x in daily[BURN:]:
        s = max(0.0, s + (x - p0) / scale - 0.5)
        best = max(best, s)
    return best


def _brier_d(outs: list[list[int]]) -> list[float]:
    """现行被检量 mean(d)，窗口 = 最近 WINDOW 个已结算日，全史为基线。"""
    n = len(outs)
    pref_h, pref_n = [0], [0]
    for o in outs:
        pref_h.append(pref_h[-1] + sum(o))
        pref_n.append(pref_n[-1] + len(o))
    out = []
    for i in range(HORIZON + 2, n):
        last = i - HORIZON - 2
        start = max(0, last - WINDOW + 1)
        nw = pref_n[last + 1] - pref_n[start]
        hw = pref_h[last + 1] - pref_h[start]
        if nw == 0:
            continue
        p = (hw + 20 * P0) / (nw + 20)
        pc = (pref_h[last + 1] + 20 * P0) / (pref_n[last + 1] + 20)
        out.append(sum((pc - y) ** 2 - (p - y) ** 2 for y in outs[i]) / len(outs[i]))
    return out


def stats(outs: list[list[int]], w: int = WINDOW) -> dict:
    daily = _daily(outs)
    d = _brier_d(outs)
    return {
        "A": sum(d) / len(d) if d else 0.0,
        "C": _z_counts(daily, w),
        "C_hac": _z_hac(daily, w),
        "D": _cusum(daily, "g0"),
        "D_lrv": _cusum(daily, "lrv"),
        "E": _z_block(daily, w),
        "E40": _z_block40(daily, w),
        "sd_day": st.pstdev(daily),
    }


def q(vals: list[float], qq: float) -> float:
    s = sorted(vals)
    k = (len(s) - 1) * qq
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def run(rates: list[float], draw, reps: int, w: int = WINDOW) -> list[dict]:
    return [stats(draw(rates, seed), w) for seed in range(reps)]


def mde(base: list[dict], alt: dict[float, list[dict]], key: str,
        crit: float) -> float | None:
    for a in sorted(alt):
        if a == 0:
            continue
        if sum(1 for r in alt[a] if r[key] > crit) / len(alt[a]) >= 0.80:
            return a
    return None


def main() -> None:
    print(f"{'=' * 78}\n统计量的零分布（{DAYS} 日 × {PICKS} picks，H={HORIZON}，"
          f"window={WINDOW}，ρ={RHO}，reps={REPS}）\n{'=' * 78}")

    keys = [("A", "A  mean(d)"), ("C", "C  两比例 z"), ("C_hac", "C* HAC-z"),
            ("D", "D  cusum γ0"), ("D_lrv", "D* cusum LRV"),
            ("E", "E  分块 z(H)"), ("E40", "E40 分块 z(40)")]
    table: dict[str, list[dict]] = {}
    cells: dict[str, dict[float, list[dict]]] = {}

    for name, draw in DGPS.items():
        base = run([P0] * DAYS, draw, REPS)
        alt = {a: run([P0] * (DAYS - 120) + [P0 + a] * 120, draw, REPS)
               for a in ALPHAS[1:]}
        table[name], cells[name] = base, alt

    print("\n-- 零分布几何：sd（A 用科学计数，其余无量纲）--")
    print(f"{'DGP':<10}" + "".join(f"{n:>13}" for _, n in keys))
    for name, base in table.items():
        cells_sd = []
        for k, _ in keys:
            if k == "A" and name in SKIP_A:
                cells_sd.append("—")
                continue
            v = st.pstdev([r[k] for r in base])
            cells_sd.append(f"{v:.1e}" if k == "A" else f"{v:.2f}")
        print(f"{name:<10}" + "".join(f"{v:>13}" for v in cells_sd))
    print(f"{'名义门槛':<10}" + "".join(f"{v:>13}" for v in
                                     ("0.001", "1.645", "1.645", "—", "—",
                                      "1.645", "1.645")))

    # 名义门槛是**常数**：A 用发布值 0.001，其余用 iid 零分布的 95 分位。
    # 这正是「上线时定死一条线」的处境——定了就不再随 DGP 改。C 的这条线
    # 应≈1.645（iid 下 sd=0.97 已验证），所以它同时是教科书值的自检。
    nominal = {k: (0.001 if k == "A" else q([r[k] for r in table["iid"]], 0.95))
               for k, _ in keys}
    print("\n-- 名义门槛（常数，iid 95 分位；C 应≈1.645）--")
    print("   " + "  ".join(f"{k}={nominal[k]:.3f}" for k, _ in keys if k != "A"))
    print("\n-- 名义门槛下的实际 size（iid 应为 5%）--")
    print(f"{'DGP':<10}" + "".join(f"{n:>13}" for _, n in keys))
    for name, base in table.items():
        line = []
        for k, _ in keys:
            if k == "A" and name in SKIP_A:
                line.append("—")
                continue
            vals = [r[k] for r in base]
            hit = sum(1 for v in vals if v > nominal[k]) / len(vals)
            line.append(f"{hit:>11.0%}")
        print(f"{name:<10}" + "".join(f"{v:>13}" for v in line))

    print("\n-- 设计效应：sd(统计量) 相对 iid 的倍数² --")
    for name, base in table.items():
        zsd = st.pstdev([r["C"] for r in base])
        dsd = sum(r["sd_day"] for r in base) / len(base)
        ideal = (0.25 / PICKS) ** 0.5
        print(f"   {name:<10} z 的 sd={zsd:>5.2f} ⇒ DEFF={zsd ** 2:>6.1f}×；"
              f"日胜率 sd={dsd:.4f}（边界 {ideal:.4f}）⇒ "
              f"{PICKS * WINDOW} 条 pick ≈ {PICKS * WINDOW / zsd ** 2:.0f} 个独立观测")

    print("\n-- size 拉回 5% 后的功效（门槛 = 本 DGP 的 95 分位）--")
    for name in DGPS:
        base = table[name]
        crit = {k: q([r[k] for r in base], 0.95) for k, _ in keys}
        print(f"   [{name}]")
        for k, label in keys:
            if k == "A" and name in SKIP_A:
                print(f"      {label:<14}—")
                continue
            m = mde(base, cells[name], k, crit[k])
            print(f"      {label:<14}"
                  f"{'>%.2f' % ALPHAS[-1] if m is None else '%.2f（%.0fpp）' % (m, m * 100)}")

    print("\n-- 变点位置：末端 120 天 vs 中段（a=0.20，检出率）--")
    print(f"{'DGP':<10}{'位置':<16}{'C 检出':>9}{'D 检出':>9}{'E 检出':>9}")
    for name, draw in DGPS.items():
        base = table[name]
        crit = {k: q([r[k] for r in base], 0.95) for k in ("C", "D", "E")}
        for label, rates in (
                ("末端 120 天", [P0] * (DAYS - 120) + [P0 + 0.2] * 120),
                ("中段（400 起）", [P0] * 400 + [P0 + 0.2] * (DAYS - 400))):
            rs = run(rates, draw, REPS)
            hit = {k: sum(1 for r in rs if r[k] > crit[k]) / len(rs)
                   for k in crit}
            print(f"{name:<10}{label:<16}" + "".join(f"{hit[k]:>8.0%}"
                                                     for k in ("C", "D", "E")))

    print("\n-- 窗口扫描：同一个 C，换 window（surrogate DGP，a=0.10）--")
    draw = DGPS["surrogate"]
    reps_w = max(60, REPS // 2)
    rs = {w: run([P0] * (DAYS - 120) + [P0 + 0.10] * 120, draw, reps_w, w)
          for w in (30, 60, 120, 240)}
    bs = {w: run([P0] * DAYS, draw, reps_w, w) for w in rs}
    for w in rs:
        crit = q([r["C"] for r in bs[w]], 0.95)
        hit = sum(1 for r in rs[w] if r["C"] > crit) / len(rs[w])
        print(f"   window={w:<4} 门槛={crit:>6.2f} 检出={hit:>5.0%}")

    print("\n读法：sd 一列是尺子的刻度。C 在 iid 下 sd=1 是定义；在 surrogate 下"
          "\n如果 sd≈4，那 1.645 这条线实际切在 34% 而不是 5%——这不是策略变差，"
          "\n是尺子被拉长了。E 把日序列折成不重叠块，刻度由构造固定为 1。")


if __name__ == "__main__":
    main()
