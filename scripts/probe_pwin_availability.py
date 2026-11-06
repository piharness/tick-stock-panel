"""实证五：`walk_forward` 的样本外重放是否真的「只用决策日已知的信息」。

## 为什么需要这个探针

`pwin.py` 的模块注释声称「没有任何未来信息能进入 p_win」。但按**决策日索引**
回看 `days[i-window:i]`，而一张卡的结算日在 `as_of + horizon`：决策日 i 的 p
会用上决策日 i-1 的胜率，那个结果要等 horizon 个交易日之后才出来。在 i 时刻，
它根本不存在。

`scripts/probe_pwin_source.py` 的预登记口径是正确的（`cutoff = pos[t] - HORIZON`），
本探针把「旧口径」和「预登记口径」并排跑，让差异可见。

## 构造

40 天全负，之后 40 天全胜，horizon=20，window=250。

- 旧口径（按决策日回看）：第 40 天时"看到"了第 20–39 天的胜利，
  p 已经抬到 0.5 以上，当天的胜利不再算作意外 ⇒ 改善接近 0。
- 预登记口径：第 40 天只能看到第 0–18 天（全负）⇒ p≈0.2，
  当天在赢 ⇒ Brier 反而大幅变差 ⇒ 改善约 -0.31。

两者都可能判 UNRELIABLE，但**改善量差了一个数量级**。这不是学术洁癖：
判据是 `lcb > 0`，一个不可复现的改善量会在边界附近改变判定。
泄漏本身不是单向乐观——真实账本上它反而把改善压低了（见 docs 第 9 节）——
问题在于**这个数字无法用决策当时已知的信息复现**。

结论不是"策略变好了"，而是**一个 20 日持有期的策略，在其新制度尚未结算完之前，
无法被认证为可靠**。这是预登记的代价，也是它唯一的意义。

运行：`python scripts/probe_pwin_availability.py`
"""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.decision import pwin  # noqa: E402

HORIZON = 20
BAD_DAYS = 40
GOOD_DAYS = 40
PICKS = 20
KAPPA = 20.0
Z90 = 1.645


def series() -> list[dict]:
    base = date(2026, 1, 1)
    rows: list[dict] = []
    for i in range(BAD_DAYS + GOOD_DAYS):
        d = base + timedelta(days=i)
        win = i >= BAD_DAYS
        for _ in range(PICKS):
            rows.append({
                "as_of": d.isoformat(),
                "exit": {"date": (d + timedelta(days=HORIZON)).isoformat(), "ret": 0.01},
                "strategy_id": "regime_shift",
                "horizon_sessions": HORIZON,
                "p_win": 0.5,
                "outcome": 1 if win else 0,
                "outcome_med": 1 if win else 0,
            })
    return rows


def old_replay(rows: list[dict], *, window: int = 250, kappa: float = KAPPA) -> dict:
    """修复前的口径：只按决策日回看，不看结算延迟。用于对照，不是可用实现。"""
    by_day: dict[str, list[int]] = {}
    for r in rows:
        if r.get("outcome_med") is not None:
            by_day.setdefault(str(r["as_of"]), []).append(int(r["outcome_med"]))
    days = sorted(by_day)
    daily: list[float] = []
    for i in range(2, len(days)):
        past = [y for d in days[max(0, i - window):i] for y in by_day[d]]
        if not past:
            continue
        p = (sum(past) + kappa * 0.5) / (len(past) + kappa)
        outs = by_day[days[i]]
        b = sum((p - y) ** 2 for y in outs) / len(outs)
        bc = sum((0.5 - y) ** 2 for y in outs) / len(outs)
        daily.append(bc - b)
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)
    lcb = mean - Z90 * math.sqrt(var / len(daily))
    return {"days": len(daily), "improvement": mean, "lcb": lcb,
            "verdict": pwin.VERDICT_RELIABLE if lcb > 0 else pwin.VERDICT_UNRELIABLE}


def main() -> None:
    rows = series()
    old = old_replay(rows)
    new = pwin.walk_forward(rows, window=250, kappa=KAPPA)

    print("=" * 78)
    print(f"制度切换序列：{BAD_DAYS} 天全负 → {GOOD_DAYS} 天全胜 / horizon={HORIZON} / 每日 {PICKS} pick")
    print("=" * 78)
    print(f"{'':<24}{'评估日':>7}{'Brier改善':>13}{'单侧下界':>13}  判定")
    print(f"{'旧口径（按决策日回看）':<20}{old['days']:>7}{old['improvement']:>+13.5f}"
          f"{old['lcb']:>+13.5f}  {old['verdict']}")
    print(f"{'预登记口径（已结算）':<20}{new['days']:>7}{new['improvement']:>+13.5f}"
          f"{new['oos_lcb']:>+13.5f}  {new['verdict']}")
    print()
    if old["verdict"] == new["verdict"]:
        print(f"判定未翻转（两者都是 {old['verdict']}），但改善量相差 "
              f"{old['improvement'] - new['improvement']:+.5f}。")
        print("注意：泄漏**不是单向乐观**。真实账本上它反而把改善压低了"
              "（+0.086 → +0.123），")
        print("因为它把冷启动期 p≈0.5 的样本也算了进去。问题不在于偏大还是偏小，")
        print("而在于这个数字无法用决策当时已知的信息复现。")
    else:
        print(f"判定翻转：旧口径 {old['verdict']} → 预登记口径 {new['verdict']}。")
        print("旧口径把尚未结算的胜利当成已知信息，因此把一个刚翻正的策略判成了可靠。")


if __name__ == "__main__":
    main()
