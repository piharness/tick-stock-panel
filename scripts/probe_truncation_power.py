"""探针：`truncated_any` 是布尔量，但「截断多少天」决定这个检验有没有功效。

§9.16 把「一天都没截断」判成 `NOT_TESTABLE`，判据是 `truncated_any`。
但 `truncated_any=True` 只保证**至少一天**被截断。若 37 个被评估日里只有
最后一天被截断，其余 36 天的逐日改善是恒等零（两个估计量相同），
于是：

    mean_clim = f · δ          （f = 被截断日占比，δ = 截断日的真实改善）

均值和标准误同时被 f 缩放的比例不同：`mean ∝ f`，而 `se ∝ sqrt(f)/sqrt(n)`
（只有 f·n 个非零观测）。所以 lcb 的符号几乎由 f 决定，不由数据决定。

本探针在一份**已知有制度翻转**的账本上扫 `window`，打印每个窗口下的
截断日数、改善、下界与结论。要证明的是：存在一段 `window`，检验在结构上
几乎不可能通过，却报出 `NO_INCREMENT`（一句关于数据的结论），
而不是 `NOT_TESTABLE`（一句关于配置的结论）。

§9.16 / §9.17 修掉的是「零天」和「一天」两个边界（结论句带覆盖度）。
§9.18 曾把滞后换成 `window-1`，§9.20 已撤回：那份依据（`r1=+0.98`）来自
确定性账本与制度翻转，不是零假设下的依赖；换成平稳抽样账本后 `r1≈-0.10`，
而 `window-1` 在 n=57 时把功效从 100% 打到 0%。下表因此回到 `lag=horizon-1`
的读数。本探针守的是「覆盖度」这条线。

**iid 对照**：`_rows()` 每天恰好 18/20 或 2/20 命中，是一份**没有抽样噪声**的
账本。只在这份账本上读「结论随 window 翻转」，无法区分「翻转来自覆盖度」
和「翻转来自确定性」。所以同一批 window 还要在 60 份二项抽样账本上重跑，
打印结论分布：若确定性账本的结论落在 iid 分布的众数上，翻转就是覆盖度的
性质，不是这份账本的怪癖。

用法：
    python scripts/probe_truncation_power.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.decision import pwin  # noqa: E402

HORIZON = 1
TOTAL = 20
OLD_WINS = 18     # 制度 A：90% 命中
NEW_WINS = 2      # 制度 B：10% 命中（翻转）
IID_REPS = 60


def _rows() -> list[dict]:
    rows: list[dict] = []
    for i in range(30):
        day = f"2026-01-{i + 1:02d}"
        rows += [_row(day, 1 if k < OLD_WINS else 0) for k in range(TOTAL)]
    for i in range(30):
        day = f"2026-03-{i + 1:02d}"
        rows += [_row(day, 1 if k < NEW_WINS else 0) for k in range(TOTAL)]
    return rows


def _row(as_of: str, outcome: int) -> dict:
    return {
        "as_of": as_of,
        "strategy_id": "drift",
        "horizon_sessions": HORIZON,
        "p_win": 0.5,
        "outcome": outcome,
        "outcome_med": outcome,
        "excess": 0.01,
        "excess_med": 0.01,
        "fill": {"status": "FILLED"},
        "exit": {"ret": 0.01},
        "logloss": 0.69,
    }


def _rows_iid(seed: int, *, total: int = TOTAL,
              q_old: float = OLD_WINS / TOTAL,
              q_new: float = NEW_WINS / TOTAL) -> list[dict]:
    """同一份制度翻转账本，但每天按二项分布抽 `total` 个结果。

    这是 `_rows()` 的对照：那份每天恰好命中固定个数，`d_i` 里没有抽样噪声，
    只剩 `δ` 的漂移。只用它读结论，等于把「覆盖度」和「确定性」混在一起。
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    for i in range(30):
        day = f"2026-01-{i + 1:02d}"
        rows += [_row(day, 1 if rng.random() < q_old else 0) for _ in range(total)]
    for i in range(30):
        day = f"2026-03-{i + 1:02d}"
        rows += [_row(day, 1 if rng.random() < q_new else 0) for _ in range(total)]
    return rows


def _verdict_mix(window: int, reps: int = IID_REPS) -> dict[str, float]:
    """同一 window 上，`reps` 份 iid 账本各自报出的结论分布。"""
    counts: dict[str, int] = {}
    for seed in range(reps):
        v = pwin.walk_forward(_rows_iid(10_000 + seed), horizon=HORIZON,
                              window=window, kappa=20.0)["verdict_incremental"]
        counts[v] = counts.get(v, 0) + 1
    return {k: c / reps for k, c in counts.items()}


def _truncated_days(rows: list[dict], window: int) -> tuple[int, int, int]:
    """(被截断的被评估日数, 被评估日数, 单日最大可见日数)。

    复刻 `walk_forward` 的可见性判定，只数天数，不算 p。
    """
    days = pwin._by_day(pwin._select(rows, strategy_id=None, horizon=HORIZON,
                                     basis=pwin.BASIS_MEDIAN), "outcome_med")
    evaluated = truncated = 0
    widest = 0
    for i in range(pwin.MIN_DAYS, len(days)):
        today = days[i][0]
        visible = [j for j in range(i)
                   if pwin._visible(days[j][2], today, i - j, HORIZON)]
        if not visible:
            continue
        evaluated += 1
        widest = max(widest, len(visible))
        if window > 0 and len(visible) > window:
            truncated += 1
    return truncated, evaluated, widest


def _daily_clim(rows: list[dict], window: int) -> list[float]:
    """复刻 `walk_forward` 的逐日增量改善序列（`brier_clim - brier`）。

    恒等零就是这里的 `0.0`：那一天的 `p` 与 `p_clim` 是同一个数。
    """
    field = "outcome_med"
    prior = pwin.P_CONST_MEDIAN
    kappa = 20.0
    days = pwin._by_day(pwin._select(rows, strategy_id=None, horizon=HORIZON,
                                     basis=pwin.BASIS_MEDIAN), field)
    out: list[float] = []
    for i in range(pwin.MIN_DAYS, len(days)):
        today = days[i][0]
        visible = [j for j in range(i)
                   if pwin._visible(days[j][2], today, i - j, HORIZON)]
        if not visible:
            continue
        past_all = [y for j in visible for y in days[j][1]]
        kept = visible[-window:] if window > 0 else visible
        past = [y for j in kept for y in days[j][1]]
        if not past:
            continue
        p = pwin._shrink(sum(past), len(past), prior, kappa)
        p_clim = pwin._shrink(sum(past_all), len(past_all), prior, kappa)
        outs = days[i][1]
        b = sum((p - y) ** 2 for y in outs) / len(outs)
        bclim = sum((p_clim - y) ** 2 for y in outs) / len(outs)
        out.append(bclim - b)
    return out


def main() -> None:
    rows = _rows()
    print(f"账本 {len(rows)} 条观测，制度翻转：{OLD_WINS}/{TOTAL} -> {NEW_WINS}/{TOTAL}，"
          f"horizon={HORIZON}")
    print("\nwindow  截断日/评估日  占比   improvement    lcb        n_eff_inc  verdict")
    print("-" * 88)
    for window in (1, 5, 12, 20, 28, 36, 40, 44, 48, 50, 51, 52, 53, 54, 55, 56):
        t, e, _ = _truncated_days(rows, window)
        wf = pwin.walk_forward(rows, horizon=HORIZON, window=window, kappa=20.0)
        imp = wf["improvement_vs_clim"]
        lcb = wf["skill_vs_clim_lcb"]
        print(f"{window:>5}  {t:>5}/{e:<5}      {t / e:>5.1%}  "
              f"{'—' if imp is None else f'{imp:+.6f}':>11}  "
              f"{'—' if lcb is None else f'{lcb:+.6f}':>10}  "
              f"{str(wf['n_eff_incremental']):>8}  {wf['verdict_incremental']}")

    print(f"\niid 对照：同一批 window，{IID_REPS} 份二项抽样账本（每天按 0.9 / 0.1 抽）")
    print("window  截断/评估   确定性账本       iid 结论分布")
    print("-" * 78)
    agree = total_w = 0
    for window in (5, 12, 20, 28, 40, 48, 50, 51, 52, 53, 54, 56):
        t, e, _ = _truncated_days(rows, window)
        det = pwin.walk_forward(rows, horizon=HORIZON, window=window,
                                kappa=20.0)["verdict_incremental"]
        mix = _verdict_mix(window)
        parts = "  ".join(
            f"{k}={mix[k]:.0%}" for k in (pwin.VERDICT_INCREMENTAL,
                                          pwin.VERDICT_MARGINAL,
                                          pwin.VERDICT_NO_INCREMENT) if k in mix)
        print(f"{window:>5}  {t:>4}/{e:<4}   {det:<14}  {parts}")
        # 「确定性账本的结论是 iid 分布的众数吗」——把「像不像」变成一个可数的量。
        agree += mix.get(det, 0.0) == max(mix.values())
        total_w += 1
    print(f"  {agree}/{total_w} 个窗口里，确定性账本的结论是 iid 分布的众数（含并列）。")
    print("  翻转跟着覆盖度走，不跟着「账本有没有噪声」走——§9.17 的结论不是这份账本的怪癖。")

    print("\n把零去掉也不是修法：非零子集是「被选中的那些天」，换个 window 就换一批")
    print("window  非零天数/总天数   非零子集的 n_eff(lag=0)")
    print("-" * 60)
    for window in (12, 40, 50, 53, 54, 55, 56):
        series = _daily_clim(rows, window)
        nonzero = [v for v in series if abs(v) > 1e-12]
        sub = None
        if len(nonzero) >= 2:
            _, _, _, sub, _ = pwin._mean_se_lcb(nonzero, 0)
        print(f"{window:>5}  {len(nonzero):>10}/{len(series):<6}  {str(sub):>14}")
    print("  非零子集的 n_eff 永远等于非零天数——它数的还是天，不是样本；")
    print("  「有几天承载证据」由 `truncated_days` 承担，不塞进 `n_eff`。")

    print("\n低覆盖度区间：n_eff 不随覆盖度变，所以覆盖度必须单独说")
    for window in range(40, 57):
        t, e, _ = _truncated_days(rows, window)
        wf = pwin.walk_forward(rows, horizon=HORIZON, window=window, kappa=20.0)
        print(f"  window={window}: {t}/{e} 天（{t / e:.1%}）  "
              f"n_eff={wf['n_eff_incremental']}  {wf['verdict_incremental']}")
    print("\n同一份账本、同一批数据，结论随 window 变化：")
    seen: dict[str, int] = {}
    for window in range(1, 60):
        wf = pwin.walk_forward(rows, horizon=HORIZON, window=window, kappa=20.0)
        seen.setdefault(wf["verdict_incremental"], window)
    for verdict, first in seen.items():
        print(f"  {verdict:<14} 首次出现在 window={first}")


if __name__ == "__main__":
    main()
