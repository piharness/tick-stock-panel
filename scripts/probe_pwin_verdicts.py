"""两个判定并排：`verdict`（相对 0.5）与 `verdict_incremental`（相对展开基线）。

## 这个脚本回答什么

`walk_forward` 现在同时给出两个结论，它们回答两个不同的问题：

    verdict              这个 p 比「无信息常数 0.5」好吗？
    verdict_incremental  这个 p 比「同样诚实的历史常数」好吗？

前者在有基础胜率优势时必然为正（`skill = 1 - 4p(1-p)` 的闭式解），
后者只在 p 真的时变时才有非零答案。并排看，才能分清「水平对」和「有区分度」。

## 为什么跑两个 window

`verdict_incremental` 对 window 敏感：window 越接近全历史，p 与展开基线越像，
增量越接近 0。默认 window=250 在 343 个决策日的账本上几乎等于全历史，
所以必须同时看一个短窗口，否则会把「窗口太宽」误读成「没有信息」。

## 口径

与 `probe_basis_axis.py` 一致：决策日取换手率前 10，次日收盘入场，持有 HORIZON 日。
`exit.date` 按日历精确给出，所以可见性走 `exit.date < today` 的精确分支，
不走「间隔 > horizon + 1」的保守回退。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "backend"))

from probe_basis_axis import HORIZON, build  # noqa: E402

from app.decision import bars, pwin  # noqa: E402

WINDOWS = (60, 250)


def _rows(df: pl.DataFrame, cal: list[str]) -> list[dict]:
    """把探针的 DataFrame 转成 `pwin` 认识的结算记录形状。"""
    pos = {d: i for i, d in enumerate(cal)}
    out = []
    for r in df.iter_rows(named=True):
        d = r["as_of"]
        i = pos[d] + 1 + HORIZON
        exit_date = cal[i] if i < len(cal) else None
        out.append({
            "as_of": d,
            "strategy_id": "probe_turnover_top10",
            "horizon_sessions": HORIZON,
            "p_win": 0.5,
            "outcome": int(r["outcome"]),
            "outcome_med": int(r["outcome_med"]),
            "exit": {"date": exit_date, "ret": float(r["ret"])},
        })
    return out


def _fmt(v, nd: int = 5) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:+.{nd}f}" if v != 0 else f"{0:.{nd}f}"
    return str(v)


def main() -> None:
    cal = bars.sessions(_HERE.parent / "data")
    picks = [(cal[i], cal[i + 1], cal[i + 1 + HORIZON])
             for i in range(1, len(cal) - HORIZON - 1)]
    df = build(picks)
    rows = _rows(df, cal)
    print(f"观测 {len(rows)}，决策日 {df['as_of'].n_unique()}，horizon={HORIZON}\n")

    for basis in ("median", "abs"):
        print(f"===== basis={basis} =====")
        for window in WINDOWS:
            wf = pwin.walk_forward(rows, basis=basis, window=window, sessions=cal)
            print(f"  window={window:<4}  days={wf['days']}  oos_days={wf['oos_days']}"
                  f"  n_eff={wf['n_eff']}  hac_lag={wf['hac_lag']}")
            print(f"    brier          {_fmt(wf['brier'], 4)}")
            print(f"    brier vs 0.5   {_fmt(wf['brier_const'], 4)}   "
                  f"brier vs clim  {_fmt(wf['brier_clim'], 4)}")
            print(f"    skill vs 0.5   {_fmt(wf['oos_skill'], 4)}   "
                  f"skill vs clim  {_fmt(wf['skill_vs_clim'], 4)}")
            print(f"    improvement    {_fmt(wf['improvement'])}   "
                  f"vs clim        {_fmt(wf['improvement_vs_clim'])}")
            print(f"    verdict        {wf['verdict']:<14} "
                  f"verdict_incremental  {wf['verdict_incremental']}")
            print(f"    → {wf['reason_incremental']}")
            print()

    # 两个判定是否真的会分叉？给出交叉表，避免只看一两个数字就下结论。
    print("===== 判定交叉表（median, 两个 window）=====")
    for window in WINDOWS:
        wf = pwin.walk_forward(rows, basis="median", window=window, sessions=cal)
        print(f"  window={window:<4} {wf['verdict']:<12} × {wf['verdict_incremental']}")


if __name__ == "__main__":
    main()
