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

## 第三件事（13h）：这个脚本里的 HAC 曾经不是生产的 HAC

本脚本原先自带一份 `_hac_lcb`：Bartlett 权重、`γ_k` 分母取 `n`、floor 取
`max(lr, 1e-18)`。而生产 `settle.hac_lcb` 的默认核是**截断**（矩形）核、
分母取 `n-k`、floor 取 `max(lr, γ0)`。同一 `lag` 下三条差异同向叠加，本地
口径的 `se` 更小，于是「按 iid 算会窄多少倍」这个读数系统性偏小——而它被
`pwin.py` / `settle.py` 三处 docstring 引用成实测值。

现在 `_hac_stats` **直接调用 `settle.hac_lcb`**，本地不再有第二份估计量；
旧核只作为诊断列（`*_bartlett`）保留，用来复算改口径之前的读数。
读数落盘 `data/.scratch/pwin_variance.json`，docstring 里的倍数从那里抄。

## 口径

与 `probe_pwin_verdicts.py` 完全一致（同一个 `build`、同一个可见性判定）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import polars as pl

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "backend"))

from probe_basis_axis import HORIZON, build  # noqa: E402

from app.decision import bars, pwin, settle  # noqa: E402

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


def _hac_stats(xs: list[float], lag: int,
               kernel: str = settle.KERNEL_TRUNCATED) -> tuple[float, float]:
    """`(单侧下界, se/se_iid)` —— **委托生产 `settle.hac_lcb`**，不再本地实现。

    这里的第二份实现存在过一个版本：Bartlett 权重、`γ_k` 分母 `n`、
    floor `max(lr, 1e-18)`。它算出的 `se` 比生产小 20–25%，于是本脚本报的
    「iid 会窄多少倍」偏小，而那个数被五处 docstring 当实测值引用。
    `kernel` 只为把旧核当诊断列复算，默认走生产的截断核。
    """
    est = settle.hac_lcb(xs, lag, kernel=kernel)
    if est is None:
        return float("nan"), float("nan")
    _, se, lcb, se_iid = est
    return lcb, (se / se_iid if se_iid > 0 else float("nan"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    default=Path("data/.scratch/pwin_variance.json"),
                    help="读数落盘路径（data/ 被 .gitignore 覆盖）")
    args = ap.parse_args(argv)
    cal = bars.sessions(_HERE.parent / "data")
    picks = [(cal[i], cal[i + 1], cal[i + 1 + HORIZON])
             for i in range(1, len(cal) - HORIZON - 1)]
    df = build(picks)
    rows = _rows(df, cal)
    mu = _mean([float(r["outcome"]) for r in rows])
    mu_med = _mean([float(r["outcome_med"]) for r in rows])
    print(f"观测 {len(rows)}，决策日 {df['as_of'].n_unique()}，horizon={HORIZON}")
    print(f"全样本均值 μ：abs={mu:.6f}  median={mu_med:.6f}\n")

    dump: list[dict] = []
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
            d_lcb, infl = _hac_stats(d, HORIZON - 1)
            _, infl_bar = _hac_stats(d, HORIZON - 1, settle.KERNEL_BARTLETT)
            # 去掉机械偏置后的残差 = 唯一的信号通道：
            #   resid_i = d_i − [(c_i−μ)² − (p_i−μ)²]
            # 若近端窗口真的多含信息，它应当系统性为正。
            resid = [di - ((ci - truth) ** 2 - (pi - truth) ** 2)
                     for di, ci, pi in zip(d, c, p)]
            resid_lcb, resid_infl = _hac_stats(resid, HORIZON - 1)
            _, resid_infl_bar = _hac_stats(resid, HORIZON - 1, settle.KERNEL_BARTLETT)

            print(f"  window={window:<4} 可评估日={len(d)}")
            print(f"    sd(p)={math.sqrt(var_p):.5f}   sd(c)={math.sqrt(var_c):.5f}"
                  f"   偏置²(p)={bias_p:.2e}  偏置²(c)={bias_c:.2e}")
            print(f"    分解预测改善 = {predicted:+.6f}")
            print(f"    实测平均改善 = {actual:+.6f}   残差 = {actual - predicted:+.2e}")
            acf = {str(k): _acf(d, k) for k in (1, 5, 10, 20)}
            print("    自相关 " + " ".join(f"lag{k}={v:+.3f}" for k, v in acf.items()))
            resid_mean = _mean(resid)
            resid_se = (resid_mean - resid_lcb) / Z90
            t = resid_mean / resid_se if resid_se > 0 else float("nan")
            print(f"    iid 下界 {iid_lcb:+.6f}   HAC 下界 {d_lcb:+.6f}"
                  f"   se 膨胀 ×{infl:.2f}（旧 Bartlett ×{infl_bar:.2f}，诊断列）")
            print(f"    残差(信号通道) {resid_mean:+.6f}  HAC 下界 {resid_lcb:+.6f}"
                  f"   t={t:.2f}")
            print()
            dump.append({
                "basis": basis, "window": window, "days": len(d),
                "d_mean": actual, "d_sd": math.sqrt(_var(d)),
                "d_iid_lcb": iid_lcb,
                "d_hac_lcb": d_lcb,
                "d_se_infl": infl,
                "d_se_infl_bartlett": infl_bar,
                # infl² = (se/se_iid)² = LRV/γ0。它同时回答「floor 绑没绑」：
                # 截断核收的是 max(lr, γ0)，绑定当且仅当这个值 = 1。
                "d_lr_over_g0": infl ** 2,
                "d_neff": len(d) / infl ** 2 if infl > 0 else None,
                "acf": acf,
                "resid_mean": resid_mean, "resid_hac_lcb": resid_lcb,
                "resid_t": t,
                "resid_se_infl": resid_infl,
                "resid_se_infl_bartlett": resid_infl_bar,
            })

    print("===== 与 walk_forward 的实测改善对照（应逐位一致）=====")
    verdict_rows: list[dict] = []
    for basis in ("median", "abs"):
        for window in WINDOWS:
            wf = pwin.walk_forward(rows, basis=basis, window=window)
            # 这条 `n_eff` 属于 **verdict 序列**（逐日 Brier 改善，相对常数），
            # 与上面 `d`（增量改善）不是同一条序列——自协方差不同，不能互相代用。
            # `infl` 由 n/n_eff 反推：n_eff = round(n·(se_iid/se)²) ⇒ se/se_iid = √(n/n_eff)。
            neff = wf.get("n_eff")
            oos = wf.get("oos_days") or 0
            infl_v = math.sqrt(oos / neff) if neff else float("nan")
            verdict_rows.append({"basis": basis, "window": window,
                                 "oos_days": oos, "n_eff": neff,
                                 "n_eff_reliable": wf.get("n_eff_reliable"),
                                 "se_infl": infl_v})
            print(f"  {basis:<6} window={window:<4} "
                  f"improvement_vs_clim={wf['improvement_vs_clim']:+.6f}  "
                  f"{wf['verdict_incremental']}  n_eff={neff}  "
                  f"se 膨胀 ×{infl_v:.2f}")

    # docstring 里引用的「iid 会窄多少倍」只从这里抄，不再手写。
    infls = [r["d_se_infl"] for r in dump]
    neffs = [r["d_neff"] for r in dump if r["d_neff"] is not None]
    bars_ = [r["d_se_infl_bartlett"] for r in dump]
    summary = {
        "days": sorted({r["days"] for r in dump}),
        "se_infl_min": round(min(infls), 3),
        "se_infl_max": round(max(infls), 3),
        "neff_min": round(min(neffs), 1),
        "neff_max": round(max(neffs), 1),
        "se_infl_bartlett_min": round(min(bars_), 3),
        "se_infl_bartlett_max": round(max(bars_), 3),
        "verdict_se_infl_min": round(min(r["se_infl"] for r in verdict_rows), 3),
        "verdict_se_infl_max": round(max(r["se_infl"] for r in verdict_rows), 3),
        "verdict_neff_min": min(r["n_eff"] for r in verdict_rows),
        "verdict_neff_max": max(r["n_eff"] for r in verdict_rows),
    }
    print(f"\n口径：生产核 = {settle.KERNEL_TRUNCATED}")
    print(f"  增量序列 d：iid 收窄 ×{summary['se_infl_min']}–{summary['se_infl_max']}，"
          f"n_eff ≈ {summary['neff_min']:.0f}–{summary['neff_max']:.0f}"
          f"（旧 Bartlett ×{summary['se_infl_bartlett_min']}–"
          f"{summary['se_infl_bartlett_max']}）")
    print(f"  verdict 序列：iid 收窄 ×{summary['verdict_se_infl_min']}–"
          f"{summary['verdict_se_infl_max']}，"
          f"n_eff = {summary['verdict_neff_min']}–{summary['verdict_neff_max']}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(
        {"horizon": HORIZON, "z90": Z90, "windows": list(WINDOWS),
         "kernel": settle.KERNEL_TRUNCATED, "summary": summary,
         "rows": dump, "verdict_rows": verdict_rows},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"产物 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
