"""L1 筛选窗口与外层测试段的时间预算 (分离口径的钉子)。

锁四件事:
1. 折布局从**最老**数据起滚动, 所以"近一年筛选"在短历史上覆盖全部三折
   (3/3 = 100%), 不是只污染"最后一折"。
2. 保留最近 K 折后, `screen_plan` 把筛选窗切在该折 test_start 之前 2 根
   (1 根标签 horizon + 1 根), 保留折与筛选窗的重叠归零。
3. 陈旧度与样本外跨度共用同一段历史: K 变大时两者同时变大 —— 不是两个
   可以分别调的旋钮, 想两头都要只能把筛选搬进折内逐折重跑。
4. balanced 在 367 根上连折都建不出来 (需要 660 根), 因此该档的达标候选
   在当前数据下恒为 0, 与因子质量无关。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.backtest.mining import (
    generate_nested_folds,
    screen_plan,
    screen_validation_overlap,
    validation_config_for_profile,
)


def _business_labels(start: date, count: int) -> list[str]:
    labels: list[str] = []
    cursor = start
    while len(labels) < count:
        if cursor.weekday() < 5:
            labels.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return labels


@pytest.fixture(scope="module")
def labels_367() -> list[str]:
    """与 data/kline_daily_enriched 同长的交易日标签 (2025-05-07 起 367 根)。"""
    labels = _business_labels(date(2025, 5, 7), 367)
    assert labels[0] == "2025-05-07"
    return labels


@pytest.fixture(scope="module")
def labels_2428() -> list[str]:
    """回填原始 dump 后的长度 (2016-11-04 起 2428 个交易日)。"""
    return _business_labels(date(2016, 11, 4), 2428)


def test_recent_year_screen_window_covers_every_outer_test_bar(labels_367: list[str]) -> None:
    """现状: 近一年筛选窗口把三折测试段全包住 ⇒ 三折都不是样本外。"""
    config = validation_config_for_profile("exploratory")
    screen_end = date.fromisoformat(labels_367[-1])
    screen_start = screen_end - timedelta(days=365)

    report = screen_validation_overlap(
        labels_367, config, screen_start=screen_start, screen_end=screen_end
    )

    assert len(report["folds"]) == 3
    assert report["max_share"] == 1.0
    assert all(row["share"] == 1.0 for row in report["folds"])
    assert report["any_overlap"] is True


def test_screen_plan_ends_two_bars_before_first_kept_test(labels_2428: list[str]) -> None:
    """1 日前瞻 ⇒ 筛选窗最后一根的 IC 会吃到测试段第一根, 必须多退一根。"""
    config = validation_config_for_profile("balanced")
    folds = generate_nested_folds(labels_2428, config)
    plan = screen_plan(labels_2428, config, keep_last_folds=3)

    first_kept = folds[-3].outer.test_start
    assert plan["oos_first_test"] == first_kept
    assert plan["oos_last_test"] == folds[-1].outer.test_end
    assert plan["folds_available"] == len(folds) == 29
    assert plan["screen_end"] == labels_2428[labels_2428.index(first_kept) - 2]
    assert plan["label_horizon_bars"] == 1


def test_kept_folds_have_zero_overlap_with_the_planned_screen_window(
    labels_2428: list[str],
) -> None:
    config = validation_config_for_profile("balanced")
    plan = screen_plan(labels_2428, config, keep_last_folds=3)

    report = screen_validation_overlap(
        labels_2428,
        config,
        screen_start=plan["screen_start"],
        screen_end=plan["screen_end"],
    )
    kept = report["folds"][-3:]
    assert len(kept) == 3
    assert all(row["share"] == 0.0 for row in kept)
    # 被丢弃的折里确实有落在筛选窗内的 —— 说明剪切不是空转
    dropped = report["folds"][:-3]
    assert any(row["share"] > 0.0 for row in dropped)


def test_plan_trades_staleness_for_oos_span(labels_2428: list[str]) -> None:
    """K 变大: 样本外跨度变大, 筛选窗同时变旧。两者之和受同一段历史约束。"""
    config = validation_config_for_profile("balanced")
    plans = [screen_plan(labels_2428, config, keep_last_folds=k) for k in (1, 3, 5)]

    spans = [plan["oos_span_days"] for plan in plans]
    stale = [plan["staleness_days"] for plan in plans]
    assert spans == sorted(spans)
    assert stale == sorted(stale)
    assert spans[0] < spans[-1] and stale[0] < stale[-1]
    # 两者同增同减: 差值近似常数 (折步长), 不存在"又新又长"的档
    assert max(spans) - min(spans) > 300
    assert max(stale) - min(stale) > 300


def test_short_history_truncates_the_screen_window_out_loud(labels_367: list[str]) -> None:
    """367 根的 exploratory: 筛选窗被迫缩到 155 根, 必须显式报截断而不是静默。"""
    config = validation_config_for_profile("exploratory")
    plan = screen_plan(labels_367, config)

    assert plan["screen_start"] == labels_367[0]
    assert plan["screen_window_days_requested"] == 365
    assert plan["screen_window_days"] < 365
    assert plan["screen_truncated"] is True
    assert plan["screen_bars"] == labels_367.index(plan["screen_end"]) + 1
    # 即便被截断, 保留折与筛选窗仍然不相交
    report = screen_validation_overlap(
        labels_367, config,
        screen_start=plan["screen_start"], screen_end=plan["screen_end"],
    )
    assert all(row["share"] == 0.0 for row in report["folds"][-3:])


def test_full_history_keeps_the_full_screen_window(labels_2428: list[str]) -> None:
    config = validation_config_for_profile("balanced")
    plan = screen_plan(labels_2428, config, keep_last_folds=3)
    assert plan["screen_truncated"] is False
    # 起点吸附到窗口内第一个交易日, 因此实际跨度略短于 365 天
    assert 360 <= plan["screen_window_days"] <= 365


def test_balanced_cannot_build_folds_on_367_bars(labels_367: list[str]) -> None:
    config = validation_config_for_profile("balanced")
    with pytest.raises(ValueError, match="insufficient trading bars"):
        generate_nested_folds(labels_367, config)
    with pytest.raises(ValueError, match="insufficient trading bars"):
        screen_plan(labels_367, config)


def test_balanced_needs_660_bars_for_one_outer_fold() -> None:
    config = validation_config_for_profile("balanced")
    need = config.outer_train_bars + config.purge_bars + config.outer_test_bars
    assert need == 660

    too_short = _business_labels(date(2024, 1, 1), need - 1)
    with pytest.raises(ValueError, match="insufficient trading bars"):
        generate_nested_folds(too_short, config)

    just_enough = _business_labels(date(2024, 1, 1), need)
    folds = generate_nested_folds(just_enough, config)
    assert len(folds) == 1
    assert screen_plan(just_enough, config, keep_last_folds=1)["folds_available"] == 1


def test_plan_refuses_more_folds_than_history_has(labels_367: list[str]) -> None:
    config = validation_config_for_profile("exploratory")
    with pytest.raises(ValueError, match="outer folds available"):
        screen_plan(labels_367, config, keep_last_folds=4)
    with pytest.raises(ValueError, match="must be positive"):
        screen_plan(labels_367, config, keep_last_folds=0)
