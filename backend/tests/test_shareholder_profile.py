"""股东结构面板的三条硬约束。

每条都对应一个**实际踩过的坑**，不是假想的边界：

  1. 单位：东财 `FREE_HOLDNUM_RATIO` 是**百分数**（茅台集团 54.499 表示 54.5%）。
     当比例直接求和，机构占比会算出 6773%，而面板上只会显示一个荒谬的数字、
     不会报错 —— 这类错误比崩溃更难发现。
  2. `IS_HOLDORG` 是字符串 "1"/"0"。`bool("0")` 为 True，所以每一行都会被标成
     机构，"机构占比" 恒等于"前十合计"，看起来完全正常。
  3. `change` 字段混着三种东西：数字（增减股数）、"新进"、"不变"。
     直接 float() 会抛，直接显示会露出原始 JSON。

另外守一条跨模块不变量：**筹码分布图的 POC 必须等于关键价位里的 POC**。
两者共用 `_chip_weights`，如果哪天有人把其中一个改成"另算一遍"，这条会红。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.indicators.levels import compute_chip_distribution, compute_levels
from app.services import shareholder


# ---------- 1/2/3：十大股东的单位与类型 ----------

def _fake_top_float(tmp_path, is_org_value: str = "1") -> None:
    d = tmp_path / "shareholder" / "top_float"
    d.mkdir(parents=True)
    pl.DataFrame({
        "code": ["600519", "600519", "600519"],
        "period_end": ["2026-09-30"] * 3,
        "holder_name": ["集团", "个人甲", "个人乙"],
        "hold_num": [681282935, 1000, 2000],
        # 百分数口径：54.499 表示 54.5%
        "ratio": [54.499077, 0.5, 0.3],
        "rank": [1, 2, 3],
        "is_org": [is_org_value, "0", "0"],
        "holder_type": ["其它", "个人", "个人"],
        "HOLD_NUM_CHANGE": ["不变", "新进", "1552026"],
        "notice_date": ["2026-10-31"] * 3,
    }).write_parquet(d / "part.parquet")


def test_top_float_ratio_is_percent_not_fraction(tmp_path):
    _fake_top_float(tmp_path)
    shareholder._cache.clear()
    out = shareholder._top_float(tmp_path, "600519")
    assert out["rows"][0]["ratio"] == pytest.approx(0.5450, abs=1e-4), (
        "54.499 是百分数，除以 100 后应为 0.545；"
        "若得到 54.499 说明单位没换算，机构占比会溢出一百倍"
    )
    assert 0.0 <= out["org_ratio"] <= 1.0, "机构占比必须是 0-1，越界说明单位错了"


def test_is_org_string_zero_is_not_truthy(tmp_path):
    """`bool("0")` 为 True —— 这是本文件最想拦住的一行。"""
    _fake_top_float(tmp_path, is_org_value="0")
    shareholder._cache.clear()
    out = shareholder._top_float(tmp_path, "600519")
    assert all(r["is_org"] is False for r in out["rows"]), (
        "IS_HOLDORG='0' 必须解析成 False；用 bool() 会把所有持股人都标成机构"
    )
    assert out["org_ratio"] == pytest.approx(0.0, abs=1e-9)


def test_top_float_change_keeps_text_values(tmp_path):
    _fake_top_float(tmp_path)
    shareholder._cache.clear()
    out = shareholder._top_float(tmp_path, "600519")
    changes = [r["change"] for r in out["rows"]]
    assert changes[0] == "不变"
    assert changes[1] == "新进", "'新进'/'不变'必须原样透传，前端按文本分支渲染"
    assert changes[2] == "1552026"


# ---------- 4：缺失数据的表达 ----------

def test_missing_symbol_is_unavailable_not_error(tmp_path):
    _fake_top_float(tmp_path)
    shareholder._cache.clear()
    out = shareholder._top_float(tmp_path, "999999")
    assert out["period_end"] is None
    assert out["rows"] == []
    assert out["org_ratio"] is None, "缺失时给 None，不能给 0 —— 0 会被读成'完全没有机构'"


# ---------- 5：筹码分布的内部一致性 ----------

def _kline(n: int = 260, trend: float = 0.0015) -> pl.DataFrame:
    import datetime as dt

    rows = []
    px = 10.0
    d0 = dt.date(2025, 1, 1)
    for i in range(n):
        px *= 1 + trend
        rows.append({
            "date": d0 + dt.timedelta(days=i),
            "open": px * 0.995, "high": px * 1.02, "low": px * 0.98,
            "close": px, "volume": 1e6 + i * 1000,
            "amount": px * 1e6, "turnover_rate": 1.5,
        })
    return pl.DataFrame(rows)


def test_chip_ratios_sum_to_one():
    chip = compute_chip_distribution(_kline())
    assert chip is not None
    assert chip["profit_ratio"] + chip["trapped_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert sum(chip["weights"]) == pytest.approx(1.0, abs=1e-3)
    assert len(chip["prices"]) == len(chip["weights"]) == 40


def test_chip_poc_matches_levels_poc():
    """同一真相只能有一个定义点：分布图的最高柱 ＝ K 线上的「成交密集区(POC)」。"""
    df = _kline()
    chip = compute_chip_distribution(df)
    levels = compute_levels(df)
    sr_poc = [p for p in levels["sr"] if p["label"] == "成交密集区(POC)"]
    assert sr_poc, "关键价位里必须有 POC"
    assert sr_poc[0]["value"] == pytest.approx(chip["poc"], abs=0.011), (
        "同一份 _chip_weights 的两个消费者给出了不同的 POC —— "
        "分布图与 K 线会指向两个价位"
    )


def test_chip_handles_short_series():
    assert compute_chip_distribution(_kline(n=10)) is None, "不足 20 根应返回 None 而不是抛"
    assert compute_chip_distribution(pl.DataFrame()) is None


def test_chip_overhead_is_monotone():
    chip = compute_chip_distribution(_kline())
    o = chip["overhead"]
    assert o["p5"] <= o["p10"] <= o["p20"], "套牢盘占比随价格区间放宽必须单调不减"


# ---------- 6：代码空间隔离 ----------

def test_index_symbol_does_not_borrow_stock_holder_data(tmp_path):
    """`000001.SH`（上证指数）与 `000001.SZ`（平安银行）前缀相同。

    不按资产类型隔离时，查上证指数会拿到平安银行 406,492 户的完整股东数据：
    格式正确、数字齐全、接口 200，唯一的问题是这个数字属于另一只标的。
    """
    _fake_top_float(tmp_path)
    d = tmp_path / "shareholder" / "holder_num"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "code": ["000001"] * 5,
        "period_end": ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"],
        "HOLDER_NUM": [546413, 500000, 480000, 460000, 406492],
        "AVG_MARKET_CAP": [1e5] * 5,
        "notice_date": ["2025-04-20", "2025-08-16", "2025-10-24", "2026-03-20", "2026-04-24"],
    }).write_parquet(d / "part.parquet")
    shareholder._cache.clear()

    class _Repo:
        class store:
            data_dir = tmp_path

        def resolve_asset_type(self, symbol: str) -> str:
            return "index" if symbol.endswith(".SH") else "stock"

    idx = shareholder.build_profile(_Repo(), "000001.SH")
    assert idx["available"] is False, "指数不能有股东户数"
    assert idx["holder"]["latest"] is None
    assert idx["holder"]["trend"] == []

    stk = shareholder.build_profile(_Repo(), "000001.SZ")
    assert stk["available"] is True
    assert stk["holder"]["trend"], "个股应当拿到户数序列"
