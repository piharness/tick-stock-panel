"""`verdict_incremental` 在**平稳**过程里能不能给出 PASS —— 这里给的是解析值而不是等。

## 为什么必须问这一句

§9.25 把「30 块」算清了：H=10/w=60 还差 18 个交易日。但那个计划默认
「攒够块数 ⇒ 有可能过门槛」。这一步没有验证过：`verdict_incremental` 的
被检量是

    d_i = (p_clim - y)^2 - (p - y)^2          （逐 pick，再按日平均）

把 `p = p* + e_w`（最近 `window` 个决策日）、`p_clim = p* + e_c`（全部可见）
代进去，逐 pick 取期望：

    E[d] = (e_c - e_w)(e_c + e_w) = σ_c² - σ_w²

**与真实胜率 p* 无关，只与两个估计量的方差有关。** 而窗口估计量永远比
全历史估计量噪（样本更少），所以 `σ_c² - σ_w² < 0` 恒成立；`T → ∞` 时
`σ_c² → 0` 而 `σ_w²` 停在 `n_w·p(1-p)/(n_w+kappa)²`，于是改善量**收敛到一个
负常数**，不是收敛到 0。

推论：平稳过程里 `P(PASS) → 0`，加数据只会把「UNKNOWN」换成「FAIL」。
H=5/w=60 那一组 58 块给出 FAIL，不是策略差，是这个代数式的必然结果。

## 这个脚本做什么

用**发布代码**（`pwin.walk_forward`）跑两个 DGP，不复制公式：

1. 平稳：真胜率恒为 0.5 ⇒ 量 `E[改善]`，与 `σ_c² - σ_w²` 对照；数 PASS 率。
2. 变点：最后一段真胜率抬到 `0.5 + a` ⇒ 这是唯一能让 `PASS` 出现的形状；
   给出「要多大的真实抬升才够」的功效曲线。

用法：

    uv run python scripts/probe_incremental_null.py
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "backend"))

from app.decision import pwin, settle  # noqa: E402

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 360
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 40
PICKS = 10
HORIZON = 10
WINDOW = 60
KAPPA = settle.DEFAULT_KAPPA
P0 = 0.5


def _calendar(n: int) -> list[str]:
    d0 = date(2024, 1, 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n + HORIZON + 4)]


def _ledger(cal: list[str], rates: list[float], seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for i in range(min(len(rates), len(cal) - HORIZON - 2)):
        p = rates[i]
        exit_ = cal[i + 1 + HORIZON]
        for _ in range(PICKS):
            y = 1 if rng.random() < p else 0
            rows.append({
                "as_of": cal[i],
                "strategy_id": "sim",
                "horizon_sessions": HORIZON,
                "p_win": P0,
                "outcome": y,
                "outcome_med": y,
                "exit": {"date": exit_, "ret": 0.0},
            })
    return rows


def _predicted_bias(n_window_picks: int, n_all_picks: int, p: float = P0) -> float:
    """σ_c² - σ_w²，收缩估计量的方差。"""
    vw = n_window_picks * p * (1 - p) / (n_window_picks + KAPPA) ** 2
    vc = n_all_picks * p * (1 - p) / (n_all_picks + KAPPA) ** 2
    return vc - vw


def _run(rates: list[float], cal: list[str], reps: int) -> dict:
    vals, passes, testable = [], 0, 0
    for r in range(reps):
        wf = pwin.walk_forward(_ledger(cal, rates, seed=r), basis="median",
                               horizon=HORIZON, window=WINDOW, kappa=KAPPA)
        m = wf["improvement_vs_clim"]
        if m is None:
            continue
        vals.append(m)
        testable += int(wf["significance_incremental"] != settle.SIGNIFICANCE_UNKNOWN)
        passes += int(wf["verdict_incremental"] == pwin.VERDICT_INCREMENTAL
                      and wf["significance_incremental"] == "PASS")
    vals.sort()
    return {
        "mean": sum(vals) / len(vals),
        "mid": vals[len(vals) // 2],
        "pos": sum(1 for v in vals if v > 0) / len(vals),
        "pass": passes / reps,
        "testable": testable / reps,
        "n": len(vals),
    }


def main() -> None:
    cal = _calendar(DAYS)
    n_all = DAYS * PICKS
    n_win = WINDOW * PICKS
    print(f"{DAYS} 决策日 × {PICKS} picks，H={HORIZON}，window={WINDOW}，"
          f"kappa={KAPPA:g}，basis=median\n")

    print("--- 1. 平稳过程（真胜率恒 0.5）：改善量的期望应该是 σ_c² - σ_w² < 0 ---")
    pred = _predicted_bias(n_win, n_all)
    print(f"    解析值 σ_c² - σ_w² = {pred:+.2e}"
          f"（σ_w={n_win * .25 / (n_win + KAPPA) ** 2:.2e}, "
          f"σ_c={n_all * .25 / (n_all + KAPPA) ** 2:.2e}）")
    stat = _run([P0] * DAYS, cal, reps=REPS)
    print(f"    实测均值 {stat['mean']:+.2e}，中位数 {stat['mid']:+.2e}，"
          f"正值占比 {stat['pos']:.0%}，可判定率 {stat['testable']:.0%}，"
          f"PASS 率 {stat['pass']:.0%}\n")

    print("--- 2. 变点：最后 120 天真实胜率抬到 0.5 + a ---")
    print(f"{'a':>6}{'实测改善':>13}{'解析 2aΔ-a²':>14}{'PASS 率':>9}{'可判定':>8}")
    for a in (0.02, 0.05, 0.10, 0.20, 0.30):
        rates = [P0] * (DAYS - 120) + [P0 + a] * 120
        st = _run(rates, cal, reps=REPS)
        # 窗口 60 天全在变点之后 ⇒ p ≈ p*+a；全历史 ≈ 混合 (1-1/3)·0.5 + ...
        share = 120 / DAYS
        p_clim = P0 + a * share
        approx = 2 * a * (P0 + a - p_clim) - a * a
        print(f"{a:>6.2f}{st['mean']:>+13.2e}{approx:>+14.2e}"
              f"{st['pass']:>9.0%}{st['testable']:>8.0%}")

    print("\n读法：平稳过程里改善量收敛到负常数，PASS 率 0；加交易日只会把")
    print("UNKNOWN 变成 FAIL。要出 PASS，必须存在**真实的**胜率抬升（第 2 段），")
    print("而且抬升幅度要大到 `2aΔ - a²` 能压过估计量方差之差。")


if __name__ == "__main__":
    main()
