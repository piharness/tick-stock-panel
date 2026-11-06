"""`verdict_incremental` 到底在测什么？—— 把「信息」和「估计量方差」分开。

## 问题

`walk_forward` 的第二套判定比较两个预测器：

    p  近端滚动胜率（trailing `window` 个决策日，收缩）
    c  展开全历史胜率（expanding，收缩）

实测两个窗口的改善都是负数或微正（−0.00237 / −0.00055 / −0.00548 / +0.00011）。
§9.10 把负号解释成「近端滚动这个时变方式不含信息」。这个解释**没有被证明**，
而且它和「p 与 c 都是同一个常数的带噪估计」这一更简单的假设不可区分。

## 这个脚本做的检验

对任意一天，令 `ȳ_i` 为当天命中率、`y` 为单个样本的 0/1 结果，则

    d_i = (c_i − y)² − (p_i − y)²  =  (c_i − p_i)(c_i + p_i − 2y)

对 y 取期望（E[y] = μ）：

    E[d_i] = (c_i − μ)² − (p_i − μ)²

对决策日取平均：

    E[d] = [Var(c) + (c̄ − μ)²] − [Var(p) + (p̄ − μ)²]

**也就是说：`verdict_incremental` 的点估计恒等于「两个估计量偏离真值的均方
误差之差」。** 若两者都无偏，它就等于 `Var(c) − Var(p)`——一个纯粹的
估计量方差比较，与「近端有没有额外信息」无关。

这给出一个可证伪的预测：实测改善应当逐位等于右边的分解，残差只是
「当天命中率 ȳ_i 与 μ 的抽样差」。

## 另外两件事

1. 逐日改善序列的自相关。20 日持有期让相邻决策日的收益重叠 19 天，
   `_mean_se_lcb` 把它们当独立样本，标准误低估多少？这里直接量出来。
2. iid 下界 vs Newey-West 下界，两个判定各自变成什么。

## 口径

与 `probe_pwin_verdicts.py` 完全一致（同一个 `build`、同一个可见性判定）。
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

from app.decision import bars, pwin  # noqa: E402

WINDOWS = (60, 250)
Z90 = 1.645


def _rows(df: pl.DataFrame, cal: list[str]) -> list[dict]:
    pos = {d: i for i, d in enumerate(cal)}
    out = []
    for r in df.iter_rows(named=True):
        d = r["as_of"]
        i = pos[d] + 1 + HORIZON
        out.append({
            "as_of": d,
            "strategy_id": "probe_turnover_top10",
            "horizon_sessions": HORIZON,
            "p_win": 0.5,
            "outcome": int(r["outcome"]),
            "outcome_med": int(r["outcome_med"]),
            "exit": {"date": cal[i] if i < len(cal) else None, "ret": float(r["ret"])},
        })
    return out


def _series(rows: list[dict], basis: str, window: int) -> dict:
    """复现 `walk_forward` 的循环，但把每天的 p / c / ȳ / d 都留下来。"""
    field = pwin._field(basis)
    prior = pwin._prior_of(basis)
    horizon = pwin.resolve_horizon(rows, basis=basis)
    days = pwin._by_day(pwin._select(rows, strategy_id=None, horizon=horizon, basis=basis),
                        field)
    usable = int(horizon or 0)
    out = {"p": [], "c": [], "ybar": [], "d": [], "mu": []}
    for i in range(pwin.MIN_DAYS, len(days)):
        today = days[i][0]
        visible, past_all = [], []
        for j in range(i):
            _, outs_j, exit_j = days[j]
            if not pwin._visible(exit_j, today, i - j, usable):
                continue
            visible.append(j)
            past_all.extend(outs_j)
        if not visible:
            continue
        kept = visible[-window:] if window > 0 else visible
        past = [y for j in kept for y in days[j][1]]
        if not past:
            continue
        p = pwin._shrink(sum(past), len(past), prior, pwin.DEFAULT_KAPPA)
        c = pwin._shrink(sum(past_all), len(past_all), prior, pwin.DEFAULT_KAPPA)
        outs = days[i][1]
        ybar = sum(outs) / len(outs)
        d = sum((c - y) ** 2 for y in outs) / len(outs) - \
            sum((p - y) ** 2 for y in outs) / len(outs)
        out["p"].append(p)
        out["c"].append(c)
        out["ybar"].append(ybar)
        out["d"].append(d)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _var(xs: list[float]) -> float:
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def _acf(xs: list[float], lag: int) -> float:
    m = _mean(xs)
    denom = sum((x - m) ** 2 for x in xs)
    num = sum((xs[i] - m) * (xs[i + lag] - m) for i in range(len(xs) - lag))
    return num / denom if denom > 0 else 0.0


def _hac_lcb(xs: list[float], lag: int) -> tuple[float, float]:
    """Newey-West（Bartlett 核）单侧下界，返回 (下界, 相对 iid 的方差膨胀倍数)。"""
    n = len(xs)
    m = _mean(xs)
    g0 = sum((x - m) ** 2 for x in xs) / n
    lr = g0
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        gk = sum((xs[i] - m) * (xs[i + k] - m) for i in range(n - k)) / n
        lr += 2.0 * w * gk
    se_iid = math.sqrt(g0 / n)
    se_hac = math.sqrt(max(lr, 1e-18) / n)
    return m - Z90 * se_hac, (se_hac / se_iid if se_iid > 0 else float("nan"))


def main() -> None:
    cal = bars.sessions(_HERE.parent / "data")
    picks = [(cal[i], cal[i + 1], cal[i + 1 + HORIZON])
             for i in range(1, len(cal) - HORIZON - 1)]
    df = build(picks)
    rows = _rows(df, cal)
    mu = _mean([float(r["outcome"]) for r in rows])
    mu_med = _mean([float(r["outcome_med"]) for r in rows])
    print(f"观测 {len(rows)}，决策日 {df['as_of'].n_unique()}，horizon={HORIZON}")
    print(f"全样本均值 μ：abs={mu:.6f}  median={mu_med:.6f}\n")

    for basis, truth in (("median", mu_med), ("abs", mu)):
        print(f"===== basis={basis} =====")
        for window in WINDOWS:
            s = _series(rows, basis, window)
            p, c, d = s["p"], s["c"], s["d"]
            var_p, var_c = _var(p), _var(c)
            bias_p = (_mean(p) - truth) ** 2
            bias_c = (_mean(c) - truth) ** 2
            predicted = (var_c + bias_c) - (var_p + bias_p)
            actual = _mean(d)
            iid_lcb = actual - Z90 * math.sqrt(_var(d) / len(d))
            hac_lcb, infl = _hac_lcb(d, HORIZON - 1)
            # 去掉机械偏置后的残差 = 唯一的信号通道：
            #   resid_i = d_i − [(c_i−μ)² − (p_i−μ)²]
            # 若近端窗口真的多含信息，它应当系统性为正。
            resid = [di - ((ci - truth) ** 2 - (pi - truth) ** 2)
                     for di, ci, pi in zip(d, c, p)]
            resid_lcb, resid_infl = _hac_lcb(resid, HORIZON - 1)

            print(f"  window={window:<4} 可评估日={len(d)}")
            print(f"    sd(p)={math.sqrt(var_p):.5f}   sd(c)={math.sqrt(var_c):.5f}"
                  f"   偏置²(p)={bias_p:.2e}  偏置²(c)={bias_c:.2e}")
            print(f"    分解预测改善 = {predicted:+.6f}")
            print(f"    实测平均改善 = {actual:+.6f}   残差 = {actual - predicted:+.2e}")
            print(f"    自相关 lag1={_acf(d, 1):+.3f} lag5={_acf(d, 5):+.3f} "
                  f"lag10={_acf(d, 10):+.3f} lag20={_acf(d, 20):+.3f}")
            resid_mean = _mean(resid)
            resid_se = (resid_mean - resid_lcb) / Z90
            t = resid_mean / resid_se if resid_se > 0 else float("nan")
            print(f"    iid 下界 {iid_lcb:+.6f}   HAC 下界 {hac_lcb:+.6f}"
                  f"   方差膨胀 ×{infl:.2f}")
            print(f"    残差(信号通道) {resid_mean:+.6f}  HAC 下界 {resid_lcb:+.6f}"
                  f"   t={t:.2f}")
            print()

    print("===== 与 walk_forward 的实测改善对照（应逐位一致）=====")
    for basis in ("median", "abs"):
        for window in WINDOWS:
            wf = pwin.walk_forward(rows, basis=basis, window=window)
            print(f"  {basis:<6} window={window:<4} "
                  f"improvement_vs_clim={wf['improvement_vs_clim']:+.6f}  "
                  f"{wf['verdict_incremental']}")


if __name__ == "__main__":
    main()
