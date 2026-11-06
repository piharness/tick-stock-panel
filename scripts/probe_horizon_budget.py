"""给定日历预算，这个判定还能不能出结论 —— 把 H 当参数扫。

## 为什么不是「跑三个 `n_eff`」

§9.24 修的是单位：`n_eff = 交易日跨度 ÷ H`。修对之后能看出**发卡节奏**不改变
单位时间里的证据量。但那条推论只在「H 可以随便挑」时才是解药。H 不是功效
旋钮，它是**策略的持有期**：把 H 从 20 改成 5 不是把同一件事量得更准，是换
一个预测任务。所以下面每个 H 都用真实行情**重新结算一遍**，不是把同一份账本
除以不同的 H。

## 这个脚本量什么

1. **真实账本按 H 重结算后的读数**：verdict / significance / `n_eff` /
   改善量及其标准误。H 变了结果标签就变了（5 日与 20 日是不同的事件），
   胜率、自相关、改善量都会跟着变。
2. **日历**。`n_eff` 的门槛是 30 块，每块占 H 个交易日，而窗口预热期 `w` 内
   一天都不会被截断（两个估计量恒等 ⇒ 没有对照）。所以要 30 块，日历上至少
   需要 `30·H + w` 个交易日。这个式子直接判死一批配置：`30×20=600` 已经超过
   一年，**H=20 在任何窗口下都不可能一年内判定**，与发卡节奏无关。
   表里给的是「从当前账本还差多少天」，不是「从零要多久」——后者会把已经攒下的
   322 个可评估日算成零。
3. **稳健性**：同一批决策日换 `basis`（相对中位数 / 绝对涨跌）复核符号。

## 口径

决策日取换手率前 10，次日收盘入场，持有 `H` 个交易日。`p` 由 `walk_forward`
从历史胜率自己重估，不用卡片的 `p_win`——那份是常数 0.5，会让两个估计量恒等、
改善恒为 0。

用法：

    uv run python scripts/probe_horizon_budget.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "backend"))

from app.decision import bars, pwin, settle  # noqa: E402

DATA = _HERE.parent / "data"
TOP_N = 10
HORIZONS = (20, 10, 5)
WINDOWS = (60, 250)
#: 一年按 250 个交易日。日历预算只有这样才和账本同单位。
BUDGET = 250
#: 面板的判定门槛（`settle.MIN_NEFF`）。
TARGET = 30


def _day_frame(day: str) -> pl.DataFrame:
    part = DATA / bars.STOCK_ENRICHED / f"date={day}"
    files = sorted(part.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.scan_parquet([str(f) for f in files],
                           missing_columns="insert").select(
        ["symbol", "close", "turnover_rate"]).collect()


def _ledger(cal: list[str], frames: dict[str, pl.DataFrame],
            horizon: int) -> list[dict]:
    """把真实行情按 `horizon` 结算成 `pwin` 认识的账本。

    每个 `H` 都要重算：`exit` 落在不同的交易日，结果标签因此不同。复用同一份
    日帧，只换出场日——三个 `H` 面对的是同一批决策日、同一批标的。
    """
    rows: list[dict] = []
    for i in range(1, len(cal) - horizon - 1):
        as_of, entry, exit_ = cal[i], cal[i + 1], cal[i + 1 + horizon]
        if not all(d in frames for d in (as_of, entry, exit_)):
            continue
        base = (
            frames[as_of].select(["symbol", "turnover_rate"])
            .join(frames[entry].select(["symbol", pl.col("close").alias("ce")]),
                  on="symbol")
            .join(frames[exit_].select(["symbol", pl.col("close").alias("cx")]),
                  on="symbol")
            .filter((pl.col("ce") > 0) & (pl.col("cx") > 0))
        )
        if base.height < 200:
            continue
        base = base.with_columns(((pl.col("cx") / pl.col("ce")) - 1.0).alias("ret"))
        median = float(base["ret"].median())
        top = base.drop_nulls("turnover_rate").sort(
            "turnover_rate", descending=True).head(TOP_N)
        if top.height < TOP_N:
            continue
        for r in top.iter_rows(named=True):
            rows.append({
                "as_of": as_of,
                "strategy_id": "probe_horizon",
                "horizon_sessions": horizon,
                "p_win": 0.5,
                "outcome": int(r["ret"] > 0),
                "outcome_med": int(r["ret"] > median),
                "exit": {"date": exit_, "ret": float(r["ret"])},
            })
    return rows


def main() -> None:
    cal = bars.sessions(DATA)
    print(f"交易日 {len(cal)}（{cal[0]} .. {cal[-1]}），一年按 {BUDGET} 天，"
          f"门槛 {TARGET} 块\n")
    frames = {}
    for d in cal:
        f = _day_frame(d)
        if not f.is_empty():
            frames[d] = f

    print("--- 1. 真实账本按 H 重结算 ---")
    print(f"{'H':>3}{'W':>5}{'可评估':>7}{'对照日':>7}{'块数':>6}{'n_eff':>7}"
          f"{'改善':>12}{'se':>10}{'lcb':>11}  verdict×significance")
    readings = []
    for horizon in HORIZONS:
        ledger = _ledger(cal, frames, horizon)
        for window in WINDOWS:
            wf = pwin.walk_forward(ledger, basis="median", window=window,
                                   sessions=cal)
            n_eff = wf["n_eff_incremental"]
            mean, se = wf["improvement_vs_clim"], wf["se_incremental"]
            lcb = None if (mean is None or se is None) else mean - settle.Z90 * se
            readings.append((horizon, window, wf))
            print(f"{horizon:>3}{window:>5}{wf['oos_days']:>7}"
                  f"{wf['truncated_days']:>7}"
                  f"{(wf['truncated_days'] or 0) // horizon:>6}"
                  f"{n_eff if n_eff is not None else 0:>7}"
                  f"{mean:>+12.2e}"
                  f"{se if se is not None else float('nan'):>10.2e}"
                  f"{lcb if lcb is not None else float('nan'):>+11.2e}"
                  f"  {wf['verdict_incremental']}×{wf['significance_incremental']}")

    print("\n--- 2. 还差多少个交易日（从当前账本算，不是从零算）---")
    print(f"{'H':>3}{'W':>5}{'需要T':>7}{'已有':>6}{'还差':>6}   ≈")
    for horizon, window, wf in readings:
        # `30·H` 是被截断的交易日，`w` 是窗口预热：预热期内两个估计量恒等，
        # 一天对照都没有。`oos_days` 当「已经过的决策日」用（差 H+1 天，同一量级）。
        need = TARGET * horizon + window
        have = wf["oos_days"]
        gap = max(0, need - have)
        when = ("已越过" if gap == 0
                else f"{gap / BUDGET:.1f} 年" if gap >= BUDGET
                else f"{round(gap / (BUDGET / 12))} 个月")
        print(f"{horizon:>3}{window:>5}{need:>7}{have:>6}{gap:>6}   {when}")
    print(f"   一年（{BUDGET} 天）预算能容纳的最大 H = {BUDGET // TARGET}"
          f"（且 window=0）")

    print("\n--- 3. 稳健性：换 basis 后符号是否一致（window=60）---")
    print(f"{'H':>3}{'basis':>8}{'块数':>6}{'n_eff':>7}{'改善':>12}{'lcb':>11}"
          f"  verdict×significance")
    for horizon in HORIZONS:
        ledger = _ledger(cal, frames, horizon)
        for basis in ("median", "abs"):
            wf = pwin.walk_forward(ledger, basis=basis, window=60, sessions=cal)
            mean, se = wf["improvement_vs_clim"], wf["se_incremental"]
            lcb = None if (mean is None or se is None) else mean - settle.Z90 * se
            print(f"{horizon:>3}{basis:>8}"
                  f"{(wf['truncated_days'] or 0) // horizon:>6}"
                  f"{wf['n_eff_incremental'] or 0:>7}{mean:>+12.2e}"
                  f"{lcb if lcb is not None else float('nan'):>+11.2e}"
                  f"  {wf['verdict_incremental']}×{wf['significance_incremental']}")

    # 只有 H=5 真正越过了 30 块。它给出的结论就是「攒够了之后会看到什么」。
    for horizon, window, wf in readings:
        if (wf["n_eff_incremental"] or 0) >= TARGET:
            mean, se = wf["improvement_vs_clim"], wf["se_incremental"]
            hi = mean + settle.Z90 * se
            print(f"\n越过门槛的那一组：H={horizon} window={window}"
                  f"  n_eff={wf['n_eff_incremental']}"
                  f"  改善 {mean:+.2e}，单侧 95% 区间 [{mean - settle.Z90 * se:+.2e}, {hi:+.2e}]")
            print(f"  ⇒ {'认定的下限也 < 0' if hi < 0 else '区间跨 0：不是「证明更差」，是「认证不了增量」'}")


if __name__ == "__main__":
    main()
