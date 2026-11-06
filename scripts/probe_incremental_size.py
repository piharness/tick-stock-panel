"""`verdict_incremental` 的零效应覆盖率与目标效应功效——直接跑发布代码。

## 为什么还要再测一次

§9.26 已经用 `pwin.walk_forward` 跑过平稳与变点两个 DGP，结论是「平稳里恒 FAIL、
变点要 20–30pp 才过」。但那次有三个缺口，使它不能当验收读数：

1. **零效应只有 iid 一种**。真实账本的 `y` 有 19 天持有期重叠（MA(19) 的三角
   自相关）和日内 pick 同向，iid 零分布会把 `se` 低估 3–5 倍，于是 size 看起
   来很干净，其实只是没造出真实的自相关。
2. **没有把「目标效应」写成一个可核对的数**。§9.27 报 `mde_incremental`，
   §9.26 的注释又把它开方读成「3.2pp 的胜率抬升」；这两个读法从来没有在同一个
   实验里对上过。本脚本把 `sqrt(mde_incremental − 0.001)` 与**实测 80% 功率点**
   并排放，让刻度误差自己显形。
3. **PASS 率只在 2–3 个 a 上取过点**。如果检出力对 a 非单调（`d_i` 的方差随
   效应增长，而门槛也随 `se` 增长），单点读数会把「倒 U 的峰」误读成「单调」。

## 四个读数

    size        H0（无时变增量）下 significance_incremental == PASS 的比例
    power(a)    末段 120 天真实胜率抬到 p+a 时的 PASS 比例
    a80         power = 80% 的最小 a（线性插值）
    implied     sqrt(mde_incremental − MIN_INCREMENTAL_EFFECT)，即「把 MDE 当
                胜率单位」的读法。a80 / implied 就是这个读法的误差倍数。

三种零分布，各自回答一个不同的问题：

    iid     干净的 Bernoulli。给 size 的下界（没有自相关可利用）。
    ma20    共同因子是 20 日移动平均 ⇒ 自相关是三角的 (1−k/20)。这是持有期
            重叠的正确形状，给 size 的「结构性」读数。
    block   真实行情账本的 40 天循环块自助。保留真实的日间漂移与尾部。它**不是**
            size 的上界：漂移为负时 `lcb` 被推到门槛以下，size 反而被压低；漂移
            为正时才会抬高。所以它只回答「这份账本的结构下读数是多少」，方向由
            账本自己决定，不能当通用界。

⚠️ 边界：`block` 用的是 325 天探索样本（`data/.probe_per_day_exc.json`），
循环到 800 天后块会重复，长程结构是造出来的。它只用于「这份账本的结构下
size 会不会失控」，不能反过来证明真实账本有增量。

用法：
    python scripts/probe_incremental_size.py [--days=800] [--reps=200]
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.decision import pwin, settle  # noqa: E402

EXC = ROOT / "data" / ".probe_per_day_exc.json"

DAYS = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--days=")), 800)
REPS = next((int(a.split("=", 1)[1]) for a in sys.argv[1:]
             if a.startswith("--reps=")), 200)
PICKS, HORIZON, WINDOW, SEGMENT = 20, 20, 60, 120
KAPPA = settle.DEFAULT_KAPPA
P0 = 0.5
MIN_EFFECT = pwin.MIN_INCREMENTAL_EFFECT
MDE_Z = pwin._MDE_Z


def calendar(n: int) -> list[str]:
    d0 = date(2024, 1, 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n + HORIZON + 4)]


def _rows(days: list[list[int]], cal: list[str]) -> list[dict]:
    out = []
    for i, outs in enumerate(days):
        exit_ = cal[i + 1 + HORIZON]
        for y in outs:
            out.append({
                "as_of": cal[i], "strategy_id": "sim",
                "horizon_sessions": HORIZON, "p_win": P0,
                "outcome": int(y), "outcome_med": int(y),
                "exit": {"date": exit_, "ret": 0.0},
            })
    return out


def _draw(p: float, rng: random.Random) -> list[int]:
    return [1 if rng.random() < p else 0 for _ in range(PICKS)]


# ---------------------------------------------------------------------------
# 三种零分布
# ---------------------------------------------------------------------------


def ledger_iid(rates: list[float], seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    return [_draw(p, rng) for p in rates]


def ledger_ma20(rates: list[float], seed: int, rho_scale: float = 1.0
                ) -> list[list[int]]:
    """共同因子 = 20 日移动平均的白噪声 ⇒ acf(k) = (20−k)/20。

    `y_{i,t} = 1(u_{i,t} < Φ(f_t))`，`f_t` 的边际是 N(0, ρ²)，所以边际胜率
    仍是 0.5；`rho_scale` 控制「同一天 20 只有多同向」。真实账本里 20 只的
    日内 sd 远小于 √20 倍单只 sd（§9.29 第四节），所以这个参数不是装饰。
    """
    rng = random.Random(seed)
    n = len(rates)
    eps = [rng.gauss(0.0, 1.0) for _ in range(n + 20)]
    f: list[float] = []
    acc = sum(eps[:20])
    for i in range(n):
        f.append(acc / 20.0 * rho_scale)
        acc += eps[i + 20] - eps[i]
    norm = statistics.NormalDist()
    days: list[list[int]] = []
    for t, p in enumerate(rates):
        # 把边际精确校准到 p：`u − f ~ N(0, 1 + 1/20)`（f 是 20 日移动平均，
        # 方差 1/20），所以要 P(u < f + c) = p 需 c = √1.05·Φ⁻¹(p)。用
        # 2(p−0.5) 近似会在 p=0.6 时给出 0.58，把变点的 a 悄悄折掉 17%；
        # 用 √2 则会高估 25%，因为那是 Var(f)=1 的阈值。
        shift = math.sqrt(1.0 + rho_scale ** 2 / 20.0) * norm.inv_cdf(
            min(0.999, max(0.001, p)))
        days.append([1 if rng.gauss(0, 1) < f[t] + shift else 0
                     for _ in range(PICKS)])
    return days


def load_real(strategy: str) -> tuple[list[str], list[list[int]]]:
    raw = json.loads(EXC.read_text(encoding="utf-8"))[strategy]
    days = sorted(raw)
    return days, [[1 if v > 0 else 0 for v in raw[d]] for d in days]


def ledger_block(real: list[list[int]], n: int, seed: int,
                 bump: float = 0.0, tail: int = SEGMENT) -> list[list[int]]:
    """40 天循环块重抽；`bump` 只在末 `tail` 天把 0 翻成 1。

    翻转概率用 `bump / (1 − p_hat)`：`bump` 是**绝对**胜率抬升，不是翻转率。
    不做这个换算，`p_hat=0.38` 的策略会把 +10pp 的目标悄悄变成 +6.2pp。
    """
    rng = random.Random(seed)
    block = 40
    starts = [rng.randrange(len(real)) for _ in range(n // block + 2)]
    out: list[list[int]] = []
    for s in starts:
        for k in range(block):
            out.append(list(real[(s + k) % len(real)]))
            if len(out) >= n:
                break
        if len(out) >= n:
            break
    if bump > 0:
        p_hat = sum(sum(d) for d in real) / (len(real) * PICKS)
        q = min(1.0, bump / max(1e-9, 1.0 - p_hat))
        for i in range(n - tail, n):
            out[i] = [1 if (v == 0 and rng.random() < q) else v for v in out[i]]
    return out


def rates_const(n: int) -> list[float]:
    return [P0] * n


def rates_change(n: int, a: float, tail: int = SEGMENT) -> list[float]:
    return [P0] * (n - tail) + [P0 + a] * tail


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def evaluate(days: list[list[int]], cal: list[str]) -> dict:
    wf = pwin.walk_forward(_rows(days, cal), basis="median", horizon=HORIZON,
                           window=WINDOW, kappa=KAPPA, sessions=cal)
    return wf


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (mid - half, mid + half)


def cell(gen, reps: int, cal: list[str]) -> dict:
    """跑 `reps` 次，返回 PASS 率、可判定率、改善均值、mde 均值。"""
    passes = testable = 0
    imps, mdes, ses, neffs = [], [], [], []
    for r in range(reps):
        wf = evaluate(gen(r), cal)
        imps.append(wf["improvement_vs_clim"] or 0.0)
        mdes.append(wf["mde_incremental"] or 0.0)
        ses.append(wf["se_incremental"] or 0.0)
        neffs.append(wf["n_eff_incremental"] or 0)
        ok = wf["significance_incremental"] != settle.SIGNIFICANCE_UNKNOWN
        testable += int(ok)
        passes += int(wf["significance_incremental"] == settle.SIGNIFICANCE_PASS)
    lo, hi = wilson(passes, reps)
    return {
        "pass": passes / reps, "lo": lo, "hi": hi,
        "testable": testable / reps,
        "mean": sum(imps) / reps, "mde": sum(mdes) / reps,
        "se": sum(ses) / reps, "neff": sum(neffs) / reps,
        "k": passes, "n": reps,
    }


def main() -> None:
    cal = calendar(DAYS)
    real_days = load_real("momentum_20")[1] if EXC.exists() else None
    print(f"{DAYS} 决策日 × {PICKS} picks，H={HORIZON}，window={WINDOW}，"
          f"segment={SEGMENT}，kappa={KAPPA:g}，reps={REPS}")
    print(f"门槛 MIN_INCREMENTAL_EFFECT={MIN_EFFECT}，MDE_Z={MDE_Z:.3f}，"
          f"MIN_NEFF={settle.MIN_NEFF}\n")

    # ---- 一、零效应覆盖率 ----
    print("一、零效应覆盖率（H0：真实胜率恒定；PASS 即假阳性）")
    print(f"{'零分布':<10}{'PASS':>8}{'95%CI':>17}{'可判定':>8}{'改善':>12}"
          f"{'mde':>10}{'se':>10}{'n_eff':>7}")
    zeros: dict[str, dict] = {}
    gens = {
        "iid": lambda r: ledger_iid(rates_const(DAYS), 1000 + r),
        "ma20": lambda r: ledger_ma20(rates_const(DAYS), 2000 + r),
    }
    if real_days is not None:
        gens["block"] = lambda r: ledger_block(real_days, DAYS, 3000 + r)
    for label, gen in gens.items():
        st = cell(gen, REPS, cal)
        zeros[label] = st
        print(f"{label:<10}{st['pass']:>7.1%}{st['lo']:>7.1%}–{st['hi']:<8.1%}"
              f"{st['testable']:>8.0%}{st['mean']:>+12.2e}{st['mde']:>10.5f}"
              f"{st['se']:>10.2e}{st['neff']:>7.0f}")

    # ---- 二、变点功效 ----
    print("\n二、变点功效：末 120 天真实胜率 = 0.5 + a")
    grid = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
    print(f"{'a':>6}{'iid':>8}{'ma20':>8}{'block':>8}{'改善(iid)':>13}"
          f"{'mde(iid)':>10}{'se(iid)':>10}")
    power: dict[float, dict[str, float]] = {}
    for a in grid:
        row: dict[str, float] = {}
        iid_st: dict = {}
        for label in gens:
            if label == "iid":
                g = (lambda r, a=a: ledger_iid(rates_change(DAYS, a), 4000 + r))
            elif label == "ma20":
                g = (lambda r, a=a: ledger_ma20(rates_change(DAYS, a), 4500 + r))
            elif real_days is not None:
                g = (lambda r, a=a: ledger_block(real_days, DAYS, 5000 + r, bump=a))
            else:
                continue
            st = cell(g, max(40, REPS // 2), cal)
            row[label] = st["pass"]
            if label == "iid":
                iid_st = st
        power[a] = row
        print(f"{a:>6.2f}{row.get('iid', float('nan')):>8.0%}"
              f"{row.get('ma20', float('nan')):>8.0%}"
              f"{row.get('block', float('nan')):>8.0%}"
              f"{iid_st['mean']:>+13.2e}{iid_st['mde']:>10.5f}"
              f"{iid_st['se']:>10.1e}")

    # ---- 三、MDE 的可解释性 ----
    print("\n三、MDE 的可解释性：把 mde 开方当成「胜率抬升」会差多少")
    implied = math.sqrt(max(0.0, zeros["iid"]["mde"] - MIN_EFFECT))
    print(f"  零效应下 mde_incremental = {zeros['iid']['mde']:.5f}"
          f" ⇒ sqrt(mde − {MIN_EFFECT}) = {implied * 100:.2f}pp")
    for label in gens:
        curve = [(a, power[a][label]) for a in grid if label in power[a]]
        a80 = _interp80(curve)
        tail = (f"实测 a80 = {a80 * 100:.1f}pp；a80 / implied = {a80 / implied:.1f}×"
                if a80 is not None else "实测 a80 > 50pp（未达 80%）")
        print(f"  {label:<6} {tail}；曲线 {_shape(curve)}")

    # ---- 四、真实账本的结构 ----
    if real_days is not None:
        print("\n四、真实行情账本的结构（只读规模，不作结论）")
        for name in ("momentum_20", "reversal_20", "low_turnover", "high_turnover"):
            ds, dd = load_real(name)
            p_hat = sum(sum(d) for d in dd) / (len(dd) * PICKS)
            print(f"  {name:<14} 决策日 {len(ds)}，胜率 {p_hat:.1%}，"
                  f"可评估 {max(0, len(ds) - HORIZON - 1)} 天")
        print(f"  ⇒ H=20、window={WINDOW} 时 n_eff 上限 ≈ "
              f"{(DAYS - WINDOW - HORIZON) // HORIZON}；"
              f"325 天只有 {(325 - WINDOW - HORIZON) // HORIZON}，"
              f"低于 MIN_NEFF={settle.MIN_NEFF}。这就是为什么要循环到 {DAYS} 天："
              f"不循环，size 连测都测不了。")

    print("\n读法：size 是「零效应被报成 PASS」的比例，应 ≤5%；power 是「真实抬升")
    print("被报成 PASS」的比例，应 ≥80%。两者共用同一个门槛，所以只要 size 被门槛")
    print("压到 0，power 也一定低——这不是两个独立的问题，是同一个刻度问题。")


def _interp80(curve: list[tuple[float, float]]) -> float | None:
    """PASS 率首次 ≥80% 的 a，线性插值。"""
    prev = None
    for a, p in curve:
        if p >= 0.8:
            if prev is None:
                return a
            a0, p0 = prev
            return a0 + (0.8 - p0) * (a - a0) / (p - p0)
        prev = (a, p)
    return None


def _shape(curve: list[tuple[float, float]]) -> str:
    """单调升 / 倒 U / 单调降——只描述形状，不解释。"""
    ps = [p for _, p in curve]
    peak = ps.index(max(ps))
    if peak == 0:
        return "单调降"
    if peak == len(ps) - 1:
        return "单调升"
    return f"倒 U（峰在 a={curve[peak][0]:.2f}）"


if __name__ == "__main__":
    main()
