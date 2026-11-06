"""`n_eff` 上限的单位：分子数决策日，分母数交易日（`docs/decision-ledger.md` §9.24）。

## 这个 bug 的形状

`_identifiable_sample` 的旧公式是 `truncated_days // (lag + 1)`。左边数的是
**决策日**（`walk_forward` 每评估一天加一），右边数的是**交易日**（`horizon`）。
两者只在「每个交易日都发卡」时相等——而那正好是门禁**不**想要的那种节奏。

每 3 个交易日发一张卡时，两张相邻卡的持有期仍然重叠 21 个交易日，只是这个
重叠摊在 3 倍的决策日上：`k` 个对照日跨 `3k` 个交易日，独立样本上限是
`3k/H`，代码给的是 `k/H`。低估 3 倍，而且**方向单调**：发卡越稀疏，错得越多。

## 为什么单靠这些数字不够

同一份账本上，`n_eff` 的可信度取决于 `se`（§9.23 换核解决）**和**分母的
单位（本节）。前者错了会让区间偏窄，后者错了会让分母偏小——两个都朝
「更容易报 FAIL」的方向偏，而 FAIL 也是一句结论。
"""
from __future__ import annotations

import datetime as dt
import random

from app.decision import pwin, settle

H = 21
LAG = H - 1
PICKS = 20


def _calendar(n: int, start: dt.date = dt.date(2024, 1, 2)) -> list[str]:
    """`n` 个交易日（跳过周末）。节假日不影响单位换算，只影响日期本身。"""
    out: list[str] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


#: 够 300 个决策日 × 每 5 个交易日一张卡。
CAL = _calendar(1600)


def _ledger(step: int, n_days: int = 300, *, seed: int = 7) -> list[dict]:
    """每 `step` 个交易日一张卡，命中数是 iid 抽的（不是常数）。

    常数命中数的账本逐日改善近乎确定性，`n_eff_nw` 会小到让上限根本不生效，
    这个 bug 就测不出来。`exit` 故意不带 `date`：可见性走 `gap_days > H+1`
    的保守回退，三种节奏因此有完全相同的对照日数——差别只剩单位。
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    for i in range(n_days):
        day = CAL[i * step]
        wins = rng.randint(8, 16)
        for j in range(PICKS):
            rows.append({
                "as_of": day,
                "strategy_id": "s",
                "horizon_sessions": H,
                "p_win": 0.5,
                "outcome": int(j < wins),
                "outcome_med": int(j < wins),
                "excess": 0.01,
                "excess_med": 0.01,
                "fill": {"status": "FILLED"},
                "exit": {"ret": 0.01},
                "logloss": 0.69,
            })
    return rows


def test_sparse_cadence_moves_the_cap_by_the_same_factor():
    """同一批对照日，发卡越稀疏，独立样本上限越大——按交易日折算。

    `k` 个对照日跨 `(k-1)·step + 1` 个交易日，上限是它除以 `H`。旧公式给的是
    `k // H`，与 `step` 无关：三种节奏全是 11。
    """
    caps = {}
    for step in (1, 3, 5):
        wf = pwin.walk_forward(_ledger(step), window=30, sessions=CAL)
        span = wf["truncated_trading_days"]
        k = wf["truncated_days"]
        assert wf["n_eff_cap_unit"] == "trading_days"
        # 跨度就是决策日在日历上的实际距离，不是「决策日数 × 某种假设」。
        assert span == (k - 1) * step + 1, (step, span, k)
        # 这份账本的 NW 原读数（277）远大于上限，所以上限就是返回值本身。
        assert wf["n_eff_incremental"] == span // (LAG + 1), (step, wf["n_eff_incremental"])
        caps[step] = wf["n_eff_incremental"]

    assert caps[1] == 11                      # 247 // 21，与旧公式一致
    assert caps[3] == 35                      # 739 // 21；旧公式仍给 11
    assert caps[5] == 58                      # 1231 // 21；旧公式仍给 11


def test_daily_cadence_is_identical_with_and_without_a_calendar():
    """每天发卡时两种口径必须逐位相同——这是「改单位不改读数」的保证。

    真实账本是逐日发的，所以这一条同时是回放的回归基线：如果换单位让任何
    一个数字动了，说明换算本身写错了，而不是发现了新东西。
    """
    rows = _ledger(1)
    old = pwin.walk_forward(rows, window=30)
    new = pwin.walk_forward(rows, window=30, sessions=CAL)
    for key in ("oos_days", "truncated_days", "n_eff",
                "n_eff_incremental", "hac_lag", "improvement", "improvement_vs_clim",
                "oos_lcb", "verdict", "verdict_incremental",
                "significance", "significance_incremental"):
        assert old[key] == new[key], key
    # 唯一该变的是「这个跨度是怎么来的」：逐日发卡时 247 个决策日 = 247 个交易日。
    assert old["truncated_trading_days"] is None
    assert new["truncated_trading_days"] == new["truncated_days"]
    # 唯一该变的是「这个上限是按什么单位算的」。
    assert old["n_eff_cap_unit"] == "decision_days"
    assert new["n_eff_cap_unit"] == "trading_days"


def test_missing_calendar_still_refuses_a_cap_below_one():
    """没有日历时退回旧口径，且**不假装**两者等价（`n_eff_cap_unit` 标注）。

    退回旧口径只会在稀疏账本上低估，不会高估：缺日历是「没算对」，不是
    「放宽门槛」。所以它不是 fail-open，不需要拦截任何请求。
    """
    wf = pwin.walk_forward(_ledger(3), window=30)
    assert wf["n_eff_cap_unit"] == "decision_days"
    assert wf["truncated_trading_days"] is None
    assert wf["n_eff_incremental"] == wf["truncated_days"] // (LAG + 1)


def test_remedy_counts_trading_days_and_converts_back_to_cards():
    """`NOT_TESTABLE` 的门槛是 630 个交易日，不是 630 张卡。

    30 个独立样本 × 21 个交易日 = 630 个交易日，这个数与发卡节奏无关；
    变的是它折成多少张卡。旧句子对 daily 和 sparse×2 印同一个「630 个可评估
    日」，把后者的等待时间多报了 2 倍——而门禁想要的方向正是稀疏发卡。
    """
    sparse = pwin.walk_forward(_ledger(2, 200), window=0, sessions=CAL)
    why = sparse["reason_incremental"]
    assert "630 个交易日" in why
    assert "每 2.0 个交易日一张卡" in why
    assert "约 316 个可评估日" in why
    assert "630 个可评估日" not in why

    daily = pwin.walk_forward(_ledger(1, 200), window=0, sessions=CAL)
    why_daily = daily["reason_incremental"]
    assert "630 个交易日" in why_daily
    # 每天发卡时两个单位重合，句子必须自己说出来，而不是让读者去猜。
    assert "也是 630 个可评估日" in why_daily


def test_identifiable_sample_uses_the_calendar_span_when_it_is_consistent():
    """`_identifiable_sample` 的边界：跨度 ≥ 决策日数才采信，否则退回旧口径。

    判定函数自己也会调它（`_incremental_verdict`），所以这里钉的是函数本身，
    不是 `walk_forward` 传参——只靠调用点传对参数，下个调用点就会绕过去。
    """
    # H=1 时上限就是跨度本身。
    assert pwin._identifiable_sample(999, 10, 0, trading_days=30) == 30
    assert pwin._identifiable_sample(999, 10, 0) == 10
    # 跨度小于决策日数 ⇒ 日历与账本对不上，退回决策日口径（不是取更小的那个）。
    assert pwin._identifiable_sample(999, 10, 0, trading_days=4) == 10
    # 仍然受 NW 原读数约束：上限不是「至少给这么多」。
    assert pwin._identifiable_sample(6, 10, 0, trading_days=30) == 6
    # 与 §9.22 的旧断言一致：1 天对照在 MA(19) 下最多 1 个独立样本。
    assert pwin._identifiable_sample(256, 1, 19) == 1


def test_trading_span_rejects_a_calendar_it_cannot_cover():
    """日历缺日、越界、为空时返回 `None`，由调用方标注单位，而不是猜一个数。"""
    assert pwin._trading_span(None, "2024-01-02", "2024-01-09", 1) is None
    assert pwin._trading_span([], "2024-01-02", "2024-01-09", 1) is None
    # 3 个决策日挤在同一个交易日上 ⇒ 跨度 1 < 3，日历解释不了这份账本。
    assert pwin._trading_span(["2024-01-02", "2024-01-03"],
                              "2024-01-02", "2024-01-03", 3) is None
    assert pwin._trading_span(["2024-01-02", "2024-01-03"],
                              "2024-01-02", "2024-01-03", 2) == 2


def test_estimate_exposes_the_cap_unit():
    """`estimate` 要透传同一个口径，否则 `/pwin` 与建卡会说两句话。"""
    rows = _ledger(3)
    est = pwin.estimate(rows, window=30, sessions=CAL)
    wf = pwin.walk_forward(rows, window=30, sessions=CAL)
    assert est["n_eff_incremental"] == wf["n_eff_incremental"]
    assert est["truncated_trading_days"] == wf["truncated_trading_days"]
    assert est["n_eff_cap_unit"] == "trading_days"
    assert est["significance_incremental"] == wf["significance_incremental"]
    # `MIN_NEFF` 仍是唯一门槛：35 ≥ 30，所以这里允许出现 FAIL/PASS，
    # 而不是停在 UNKNOWN——变的是分母的单位，不是门槛。
    assert est["n_eff_incremental"] >= settle.MIN_NEFF
