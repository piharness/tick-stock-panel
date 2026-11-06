"""`n_eff` 的截断上限：分子数的是决策日，分母数的是交易日。

## 问题

`_identifiable_sample` 里

    cap = truncated_days // (lag + 1)

`truncated_days` 是**决策日**计数（`walk_forward` 的 `for i in range(...)` 每
循环一次加一），`lag + 1` 是**交易日**（`horizon_sessions`）。两者只有在
「每个交易日都发卡」时才相等。门禁想要的恰是反过来的结果——发卡变稀疏。

## 这个脚本量什么

同一份账本三种发卡节奏（同一个交易日历）：

- `daily`：每个交易日一张卡（现状）；
- `sparse×3`：每 3 个交易日一张卡；
- `sparse×5`：每 5 个交易日一张卡。

`sparse` 下持有 21 个交易日的两张相邻卡只重叠 21 个**交易日**，对应 7 个
决策日 ⇒ 真上限是 `truncated_days / 7`，旧代码给的是 `truncated_days / 21`。

修法是给 `walk_forward` 一个交易日历（`sessions`），上限按真实跨度算；
第二个出口是 `NOT_TESTABLE` 的补救句——门槛的单位也是交易日，不是可评估日。

用法：

    uv run python scripts/probe_neff_units.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.decision import pwin  # noqa: E402

HORIZON = 21
TOTAL = 20


def _calendar(n: int, start: dt.date = dt.date(2024, 1, 2)) -> list[str]:
    """`n` 个交易日（跳过周末）。节假日不影响单位换算，只影响日期本身。"""
    out: list[str] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def _ledger(n_days: int, step: int, calendar: list[str]) -> list[dict]:
    """每 `step` 个交易日一张卡，每张 20 个 pick，命中数是 iid 抽的。

    用随机命中数而不是常数 18/20：常数账本的逐日改善近乎确定性，自相关极强，
    `n_eff_nw` 会小到让上限根本不生效，看不出这个 bug。
    """
    import random
    rng = random.Random(7)
    rows: list[dict] = []
    for i in range(n_days):
        as_of = calendar[i * step]
        wins = rng.randint(8, 16)
        for j in range(TOTAL):
            rows.append({
                "as_of": as_of,
                "strategy_id": "s",
                "horizon_sessions": HORIZON,
                "p_win": 0.5,
                "outcome": 1 if j < wins else 0,
                "outcome_med": 1 if j < wins else 0,
                "excess": 0.01,
                "excess_med": 0.01,
                "fill": {"status": "FILLED"},
                "exit": {"ret": 0.01},          # 无 exit.date ⇒ 走间隔回退
                "logloss": 0.69,
            })
    return rows


def run(step: int, window: int, calendar: list[str]) -> dict:
    rows = _ledger(420, step, calendar)
    old = pwin.walk_forward(rows, window=window)                  # 旧口径：无数日历
    new = pwin.walk_forward(rows, window=window, sessions=calendar)
    k = new["truncated_days"] or 0
    span = new["truncated_trading_days"] or 0
    return {
        "step": step,
        "oos": new["oos_days"],
        "truncated": k,
        "span": span,
        "old_cap": old["n_eff_incremental"],
        "new_cap": new["n_eff_incremental"],
        "true_cap": max(1, span // HORIZON) if span else 0,
        "unit": new["n_eff_cap_unit"],
        "verdict": new["verdict_incremental"],
        "sig": new["significance_incremental"],
        "reason": new["reason_incremental"],
    }


def main() -> None:
    calendar = _calendar(420 * 5 + HORIZON + 10)
    print(f"H={HORIZON}  lag={HORIZON - 1}  window=60  每张卡 20 个 pick（命中数 iid）\n")
    print(f"{'节奏':<10}{'oos':>6}{'对照日':>8}{'交易日跨度':>12}"
          f"{'旧上限':>8}{'新上限':>8}{'真上限':>8}  {'significance'}")
    for step, name in ((1, "daily"), (3, "sparse×3"), (5, "sparse×5")):
        r = run(step, 60, calendar)
        print(f"{name:<10}{r['oos']:>6}{r['truncated']:>8}{r['span']:>12}"
              f"{r['old_cap']:>8}{r['new_cap']:>8}{r['true_cap']:>8}  {r['sig']}"
              f"   ({r['unit']})")

    # 第二个出口：`NOT_TESTABLE` 的补救句按决策日算「要攒多少天」。
    print("\n--- window 覆盖全历史时（window=0）的补救句 ---")
    for step, name in ((1, "daily"), (3, "sparse×3")):
        r = run(step, 0, calendar)
        print(f"[{name}] {r['reason']}")


if __name__ == "__main__":
    main()
