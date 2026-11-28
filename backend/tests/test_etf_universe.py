"""ETF 行业 universe 的两条硬约束（2026-11-28）。

事故：`159980 有色ETF大成` 是**有色金属期货** ETF，名称里没有「商品/期货」字样，
按关键词剔除时漏网，被归进「有色」。2026-11-27 它凭 6.14 亿成交额压过
`512400 有色金属ETF南方`（5.53 亿）成为载体 —— 信号说「有色股票跌 41%」，
买到的是「有色期货跌 16%」，连位置闸都不过。

这两条测试锁住：名称判据单独不够用，按代码的判据必须在 classify 里。
"""
from __future__ import annotations

import sys
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import _diag_industry_etf_asym as core  # noqa: E402


def test_commodity_futures_etf_is_excluded_by_symbol():
    """三只商品期货 ETF 必须按代码剔；只给 name 时分不开（这是事故的成因）。"""
    for sym, name in [
        ("159980.SZ", "有色ETF大成"),
        ("159981.SZ", "能源化工ETF建信"),
        ("159985.SZ", "豆粕ETF华夏"),
    ]:
        assert core.classify(name, sym) is None, f"{sym} {name} 必须被剔除"
    # 名称判据对它无效：这正是必须加代码白名单的原因
    assert core.classify("有色ETF大成") == "有色"


def test_equity_etf_of_same_industry_survives():
    """剔除不能伤到同行业的股票 ETF —— 512400 仍是「有色」的唯一合法大载体。"""
    assert core.classify("有色金属ETF南方", "512400.SH") == "有色"
    assert core.classify("工业有色ETF广发", "512470.SH") == "有色"
    # 关键词剔除的其他类别不受影响
    assert core.classify("黄金ETF华安", "518880.SH") is None
    assert core.classify("豆粕ETF华夏", "159985.SZ") is None
    assert core.classify("沪深300ETF华泰柏瑞", "510300.SH") is None
