"""证据产物的自洽性：口径必须活在产物里，不能只活在上一轮的散文里。

三条守的是同一类事故——**数字对、标签错**，于是下游照标签读，读出一个不存在的量：
  * 池「均值 − 中位」这条恒等式旁边写着「日度中位的中位」。两个时间聚合一混，
    差 57bp，而恒等式本身在产物里是闭合的，所以没人发现。
  * `layer_w99` 被印成「双侧缩尾」，实现只 `clip(upper_bound=...)`——单边上尾。
  * `since` 子样本只存起点，于是「2025-01 起」被读成近两年，实际 1.5 年。

这些都不是估计量的问题，是**字段没把口径说出来**。所以测试也只钉口径：
恒等式必须闭合、两个统计量必须是两个字段、每个子样本必须自带终点与天数。

产物不在时跳过——新克隆没有 `data/.scratch/*`，那不是缺陷。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRATCH = REPO / "data" / ".scratch"


def _load(name: str) -> dict:
    p = SCRATCH / name
    if not p.exists():
        pytest.skip(f"{name} 不在（新克隆 / CI），本地重跑探针后才有")
    return json.loads(p.read_text(encoding="utf-8"))


def _blocks(d: dict):
    """取出所有带 `pool_mean_minus_median_bp` 的块（4 个口径 × 2 个 H）。"""
    for key, v in d.items():
        if isinstance(v, dict) and "pool_mean_minus_median_bp" in v:
            yield key, v


def test_pool_identity_closes_inside_the_artifact():
    """池均值 − 池中位 必须等于产物里那一个差，且「中位的中位」是另一个字段。

    这条是口径错的钉子：只要有人再把两个时间聚合写成一个数，恒等式就不闭合；
    而只要两个字段合一，第二条断言就红。
    """
    d = _load("width_frontier.json")
    n = 0
    for key, b in _blocks(d):
        lhs = b["pool_mu_bp"] - b["pool_med_bp"]
        assert abs(lhs - b["pool_mean_minus_median_bp"]) < 0.05, key
        assert abs(b["pool_med_of_daily_med_bp"] - b["pool_med_bp"]) > 1.0, key
        n += 1
    assert n == 4, f"应有 4 个口径块，实际 {n}"


def test_winsorize_labels_say_which_tail():
    """单边上尾与双侧缩尾必须是两个字段，且定义里写明是哪一个。

    它们数值接近（本例相差 0.3bp），正是这一点让标签错一直没被数值异常抓住——
    所以这里不比较大小，只比较**名字有没有把口径说出来**。
    """
    d = _load("width_frontier.json")
    defs = d["definitions"]
    assert "单边" in defs["layer_w99_bp"]
    assert "双侧" in defs["layer_w01w99_bp"]
    for key, b in _blocks(d):
        assert "layer_w99_bp" in b and "layer_w01w99_bp" in b, key


def test_both_win_rate_calibers_exist():
    """单次持有胜率只认 `stride_win`：每 h 日取一日，取到的 fwd_h 互不相交。

    逐日胜率（重叠）与分块均值胜率都会高估它——本例 72.9% / 79.5% / 67.5%，
    三个数都在产物里，谁也不能冒充谁。
    """
    d = _load("width_frontier.json")
    for key, b in _blocks(d):
        for f in ("win_daily", "block_win", "stride_win"):
            assert f in b, (key, f)
        assert 0.0 <= b["stride_win"]["win_rate"] <= 1.0, key
        # ceil(days/h) vs floor(days/h)：两个口径的样本数只差这一个。
        assert b["stride_win"]["n"] in (b["block_win"]["n_blocks"],
                                        b["block_win"]["n_blocks"] + 1), key


def test_since_blocks_carry_end_and_day_count():
    """子样本只存起点等于「不给分母」：383 天会被读成两年。"""
    d = _load("width_frontier.json")
    tags = [k for k in d if "_since_" in k]
    assert tags, "至少有一个子样本块"
    for t in tags:
        b = d[t]
        assert b["since"] and b["until"], t
        assert b["until"] > b["since"], t
        assert b["days"] > 0, t
    full = d["size_turnover_H60"]
    assert full["days"] > max(d[t]["days"] for t in tags), "全样本天数必须多于子样本"


def test_liquidity_identity_keeps_slopes_and_window():
    """每档斜率必须与网格同形落盘；没有斜率字段，「杀伤只在小盘端」就无法证伪。"""
    d = _load("liquidity_identity.json")
    for k in ("size_x_rate", "size_x_rate_since2024", "rate_x_amount", "price_x_amount"):
        b = d[k]
        assert len(b["slope_per_quintile_bp"]) == len(b["mat"]), k
        assert all(isinstance(v, float) for v in b["slope_per_quintile_bp"]), k
        assert b["until"] and b["days"] > 0, k


def test_survivorship_census_is_recorded():
    """生存偏差的**输入**必须写进产物：只报一个点估计、不记缺了多少只与占多大权重，
    那个数就既无法被复核，也无法被推翻。"""
    d = _load("survivorship_bound.json")
    c = d["census"]
    assert c["instruments"] > c["in_dump"] > 0
    assert c["missing"] == c["instruments"] - c["in_dump"]
    assert c["delisted"] > 0
    assert c["missing_weight"] == pytest.approx(c["missing"] / c["instruments"])
    assert d["baseline_bp"] > 0
    assert d["injection"], "注入结果不能是空的"
    assert d["mechanism_ladder"], "机制梯不能是空的"
