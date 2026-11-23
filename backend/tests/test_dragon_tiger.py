"""龙虎榜服务测试 (不依赖真实网络)。

覆盖: 交易日回退 (非交易日/目标日解析)、历史日 JSON 缓存命中与落盘、
fuyao 未配置降级、当日未发布 fallback_prev、彻底失败 no_data、
AI 复盘摘要段构建。
日期用 2026-08-27/28 (写作时为过去交易日), 与仓库既有绝对日期测试风格一致。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.plugins.fuyao.client import FuyaoError
from app.services import dragon_tiger as dt


def _mk_days(data_dir: Path, *days: str) -> None:
    for d in days:
        (data_dir / "kline_daily" / f"date={d}").mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    _mk_days(tmp_path, "2026-08-26", "2026-08-27", "2026-08-28")
    return tmp_path


class _FakeProvider:
    """按 (board, date) 记录调用; fail_dates 中的日期抛 FuyaoError。"""

    def __init__(self, fail_dates: set[str] | None = None):
        self.calls: list[tuple] = []
        self.fail_dates = fail_dates or set()

    def dragon_tiger(self, board_type: str, date: str | None) -> dict:
        self.calls.append((board_type, date))
        if date in self.fail_dates:
            raise FuyaoError(f"code=3002: {date} 未就绪")
        iso = date or "2026-08-28"
        return {
            "trade_date": iso,
            "count": 2,
            "stock_count": 2,
            "stock_items": [
                {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
                 "change": 0.05, "net_value": 1.5e8, "net_rate": 0.01,
                 "buy_value": 2e8, "sell_value": 0.5e8, "hot_rank": 3, "range_days": 1,
                 "org_net_value": 0.8e8},
                {"thscode": "000858.SZ", "ticker": "000858", "name": "五粮液",
                 "change": -0.03, "net_value": -0.6e8, "net_rate": -0.004,
                 "buy_value": 0.4e8, "sell_value": 1e8, "range_days": 3},
            ],
            "hot_money_items": [
                {"name": "宁波桑田路", "buying": 1.9e8,
                 "rows": [{"thscode": "600519.SH", "name": "贵州茅台",
                           "hot_money_item_net_value": 1.2e8}]},
            ],
        }


def _use_provider(monkeypatch, provider) -> _FakeProvider:
    monkeypatch.setattr(dt, "_provider", lambda: provider)
    return provider


# ---- 交易日解析 ----

def test_resolve_rolls_back_non_trading_day(data_dir):
    # 周日 08-30 → 最近分区 08-28
    assert dt.resolve_trade_date(data_dir, date(2026, 8, 30)) == date(2026, 8, 28)
    # 08-27 是交易日 → 原样
    assert dt.resolve_trade_date(data_dir, date(2026, 8, 27)) == date(2026, 8, 27)


def test_resolve_older_than_all_partitions_returns_target(data_dir):
    # 早于全部本地分区 → 原样返回, 由调用方显式尝试 (失败如实 no_data)
    assert dt.resolve_trade_date(data_dir, date(2020, 1, 1)) == date(2020, 1, 1)


def test_resolve_sees_monthly_partition(tmp_path: Path):
    """月分区必须参与交易日解析 —— 回归 2026-11 的静默 14 天滞后。

    旧实现按 `^date=YYYY-MM-DD$` 扫目录名。日 K 迁到月分区 (date=2026-11) 后
    这道正则只看得见迁移边界前的日分区, 于是「≤目标日的最近交易日」永久返回
    2026-10-30: 接口 state=ok、榜单是两周前的, 而上游当天的榜单就在一次请求
    之外。判据用月分区真实 date 列, 不靠目录名声明。
    """
    import polars as pl

    (tmp_path / "kline_daily" / "date=2026-10-30").mkdir(parents=True)
    part = tmp_path / "kline_daily" / "date=2026-11"
    part.mkdir(parents=True)
    pl.DataFrame({
        "date": [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 11)],
        "symbol": ["600519.SH"] * 3,
    }).write_parquet(part / "part-0.parquet")

    assert dt.resolve_trade_date(tmp_path, date(2026, 11, 13)) == date(2026, 11, 11)
    # 月内命中同样要精确到日, 不能把整个月当成一个交易日
    assert dt.resolve_trade_date(tmp_path, date(2026, 11, 3)) == date(2026, 11, 3)


# ---- 状态与缓存 ----

def test_source_unavailable_without_fuyao(data_dir, monkeypatch):
    monkeypatch.setattr(dt, "_provider", lambda: None)
    out = dt.get_dragon_tiger(data_dir, None)
    assert out["state"] == "source_unavailable"


def test_fetch_stores_cache_then_hits_cache(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    out = dt.get_dragon_tiger(data_dir, None)  # 默认 → 最近分区 08-28
    assert out["state"] == "ok"
    assert out["trade_date"] == "2026-08-28"
    assert len(provider.calls) == 3  # 三榜一次取齐
    cache = data_dir / "dragon_tiger" / "date=2026-08-28.json"
    assert cache.exists()

    provider.calls.clear()
    out2 = dt.get_dragon_tiger(data_dir, date(2026, 8, 30))  # 周日 → 08-28 → 命中缓存
    assert out2["state"] == "ok" and out2["trade_date"] == "2026-08-28"
    assert provider.calls == []  # 纯本地, 不打网络


def test_explicit_history_date_uses_cache(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert provider.calls[0] == ("all", "2026-08-27")
    provider.calls.clear()
    dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert provider.calls == []


def test_unpublished_today_falls_back_to_prev(data_dir, monkeypatch):
    # 08-28 拉取失败 (未发布/未就绪) → 回退 08-27, state=fallback_prev
    provider = _use_provider(monkeypatch, _FakeProvider(fail_dates={"2026-08-28"}))
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "fallback_prev"
    assert out["trade_date"] == "2026-08-27"
    assert out["requested_date"] == "2026-08-28"
    # 回退日缓存以 ok 状态落盘 (不污染直查)
    cached = json.loads((data_dir / "dragon_tiger" / "date=2026-08-27.json").read_text(encoding="utf-8"))
    assert cached["state"] == "ok"


def test_total_failure_returns_no_data(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider(fail_dates={"2026-08-28", "2026-08-27"}))
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "no_data"
    assert "2026-08-28" in out.get("message", "")


def test_corrupt_cache_refetches(data_dir, monkeypatch):
    provider = _use_provider(monkeypatch, _FakeProvider())
    cache = data_dir / "dragon_tiger" / "date=2026-08-28.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{broken json", encoding="utf-8")
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "ok"
    assert provider.calls  # 缓存损坏 → 重新拉取


# ---- AI 复盘摘要 ----

def test_build_recap_context_contains_summary(data_dir, monkeypatch):
    _use_provider(monkeypatch, _FakeProvider())
    ctx = dt.build_recap_context(data_dir)
    assert "净买入居前" in ctx and "贵州茅台" in ctx
    assert "机构净买居前" in ctx
    assert "宁波桑田路" in ctx


def test_build_recap_context_empty_without_source(data_dir, monkeypatch):
    monkeypatch.setattr(dt, "_provider", lambda: None)
    assert dt.build_recap_context(data_dir) == ""


# ---- thscode 归一化 (2026-10-29 用户反馈: 退市整理期股/B股 thscode=null 进前端) ----

def test_derive_thscode_market_suffix():
    assert dt.derive_thscode("002731") == "002731.SZ"
    assert dt.derive_thscode("300750") == "300750.SZ"
    assert dt.derive_thscode("600519") == "600519.SH"
    assert dt.derive_thscode("688111") == "688111.SH"
    assert dt.derive_thscode("830799") == "830799.BJ"
    assert dt.derive_thscode("900920") == "900920.SH"   # 沪B
    assert dt.derive_thscode("200468") == "200468.SZ"   # 深B
    assert dt.derive_thscode("600519.SH") == "600519.SH"  # 已带后缀原样返回
    assert dt.derive_thscode(None) is None
    assert dt.derive_thscode("ABCDEF") is None


def test_normalize_stock_items_fills_null_thscode_from_ticker():
    items = [
        {"thscode": None, "ticker": "002731", "name": "萃华退", "net_value": -525558.3},
        {"thscode": None, "ticker": "200468", "name": "宁通信B"},
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
    ]
    out = dt.normalize_stock_items(items)
    assert [i["thscode"] for i in out] == ["002731.SZ", "200468.SZ", "600519.SH"]
    # 其它字段原样保留
    assert out[0]["name"] == "萃华退" and out[0]["net_value"] == -525558.3


def test_normalize_stock_items_drops_unidentifiable():
    out = dt.normalize_stock_items([
        {"thscode": None, "ticker": None, "name": "无代码"},
        {"thscode": "  ", "ticker": "nope", "name": "坏ticker"},
        "junk",
    ])
    assert out == []


def test_boards_of_normalizes_nested_seat_rows():
    class _NullProvider:
        def dragon_tiger(self, board_type, date):
            return {
                "trade_date": date,
                "stock_items": [{"thscode": None, "ticker": "900920", "name": "动力B股"}],
                "hot_money_items": [{"name": "T王", "buying": 1.0,
                                     "rows": [{"thscode": None, "ticker": "002731", "name": "萃华退"}]}],
            }

    got = dt._boards_of(_NullProvider(), "all", "2026-10-29")
    assert got["stock_items"][0]["thscode"] == "900920.SH"
    assert got["hot_money_items"][0]["rows"][0]["thscode"] == "002731.SZ"


def test_load_cache_normalizes_legacy_null_thscode(tmp_path):
    """旧版本写入的历史缓存含 null thscode → 读回即归一, 不把脏数据透给前端。"""
    path = tmp_path / "date=2026-10-29.json"
    path.write_text(json.dumps({
        "state": "ok",
        "trade_date": "2026-10-29",
        "all": {"stock_items": [{"thscode": None, "ticker": "002731", "name": "萃华退"}]},
        "org": {"stock_items": [{"thscode": "600519.SH"}]},
        "hot_money": {"hot_money_items": [
            {"name": "T王", "rows": [{"thscode": None, "ticker": "200468"}]},
        ]},
    }, ensure_ascii=False), encoding="utf-8")

    payload = dt._load_cache(path)
    assert payload["all"]["stock_items"][0]["thscode"] == "002731.SZ"
    assert payload["org"]["stock_items"][0]["thscode"] == "600519.SH"
    assert payload["hot_money"]["hot_money_items"][0]["rows"][0]["thscode"] == "200468.SZ"


def test_get_dragon_tiger_normalizes_provider_null_thscode(data_dir, monkeypatch):
    class _NullProvider:
        def dragon_tiger(self, board_type, date):
            return {
                "trade_date": "2026-08-28",
                "count": 1,
                "stock_count": 1,
                "stock_items": [{"thscode": None, "ticker": "002731", "name": "萃华退",
                                 "net_value": -525558.3}],
                "hot_money_items": [],
            }

    _use_provider(monkeypatch, _NullProvider())
    payload = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert payload["state"] == "ok"
    assert payload["all"]["stock_items"][0]["thscode"] == "002731.SZ"


# ---- 方向一致性统计 build_direction_stats ----

def _write_dt_cache(data_dir: Path, d: str, org_net: float, all_net: float, hm_net: float) -> None:
    """写一个龙虎榜缓存文件：org 榜一只票，带 org_net_value / net_value / hot_money_net_value。"""
    cache = data_dir / "dragon_tiger" / f"date={d}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "ok",
        "trade_date": d,
        "all": {"stock_items": [{"ticker": "600519", "name": "贵州茅台", "net_value": all_net}]},
        "org": {"stock_items": [{"ticker": "600519", "name": "贵州茅台",
                                  "org_net_value": org_net, "net_value": all_net,
                                  "hot_money_net_value": hm_net}]},
        "hot_money": {"hot_money_items": []},
    }
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_direction_stats_computes_same_direction_rate(tmp_path: Path):
    """3 天里 2 天机构与全榜同向 → rate ≈ 0.667。"""
    # 同向：org_net 与 all_net 同正/同负
    _write_dt_cache(tmp_path, "2026-08-26", org_net=1e8, all_net=1e8, hm_net=-1e8)
    _write_dt_cache(tmp_path, "2026-08-27", org_net=1e8, all_net=1e8, hm_net=-1e8)
    # 反向：org 净买，all 净卖
    _write_dt_cache(tmp_path, "2026-08-28", org_net=1e8, all_net=-1e8, hm_net=-1e8)

    out = dt.build_direction_stats(tmp_path, lookback_days=10)
    assert out["days"] == 3
    assert out["org_vs_all"]["same"] == 2
    assert out["org_vs_all"]["total"] == 3
    assert out["org_vs_all"]["rate"] == pytest.approx(2 / 3, abs=1e-3)
    # 机构 vs 游资：3 天全反向（org 正、hm 负）→ rate = 0
    assert out["org_vs_hotmoney"]["same"] == 0
    assert out["org_vs_hotmoney"]["rate"] == 0.0


def test_direction_stats_empty_when_no_cache(tmp_path: Path):
    out = dt.build_direction_stats(tmp_path)
    assert out["days"] == 0
    assert out["series"] == []
    assert any("无龙虎榜缓存" in c for c in out["caveats"])


# ---- 方向 → 前向超额收益判决 build_forward_verdict ----

class _FakeRepo:
    """只提供 build_forward_verdict 需要的 store.data_dir 与 enriched 日线目录。"""

    def __init__(self, data_dir: Path):
        self.store = type("S", (), {"data_dir": data_dir})()


def _write_enriched_daily(data_dir: Path, symbol: str, closes: list[float]) -> None:
    """按日分区写 enriched 日线，日期用 2026-08-01 起连续 N 天（含 raw_close）。"""
    import polars as pl
    from datetime import timedelta

    base = date(2026, 8, 1)
    root = data_dir / "kline_daily_enriched"
    for i, c in enumerate(closes):
        d = base + timedelta(days=i)
        part = root / f"date={d}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": [symbol], "date": [d],
            "open": [c], "high": [c], "low": [c], "close": [c],
            "raw_close": [c], "raw_high": [c], "raw_low": [c],
            "volume": [1.0], "amount": [1.0], "turnover_rate": [1.0],
            "consecutive_limit_ups": [0], "consecutive_limit_downs": [0],
            "quote_ts": [0],
        }).write_parquet(part)


def _write_verdict_cache(data_dir: Path, d: str, org_net: float, hm_net: float) -> None:
    """写一个龙虎榜缓存：all 榜带 thscode + hot_money_net_value，org 榜带 org_net_value。"""
    cache = data_dir / "dragon_tiger" / f"date={d}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "ok",
        "trade_date": d,
        "all": {"stock_items": [{"thscode": "600519.SH", "ticker": "600519",
                                  "name": "贵州茅台", "net_value": 1e8,
                                  "hot_money_net_value": hm_net}]},
        "org": {"stock_items": [{"thscode": "600519.SH", "ticker": "600519",
                                  "name": "贵州茅台", "org_net_value": org_net}]},
        "hot_money": {"hot_money_items": []},
    }
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_forward_verdict_empty_without_cache(tmp_path: Path):
    repo = _FakeRepo(tmp_path)
    out = dt.build_forward_verdict(repo)
    assert out["days"] == 0
    assert any("无龙虎榜缓存" in c for c in out["caveats"])


def test_forward_verdict_empty_without_daily(tmp_path: Path):
    _write_verdict_cache(tmp_path, "2026-08-01", org_net=1e8, hm_net=-1e8)
    repo = _FakeRepo(tmp_path)
    out = dt.build_forward_verdict(repo)
    # 有龙虎榜但无日线 → days=0（日线缺列或样本不足）
    assert out["days"] == 0


def test_forward_verdict_reports_t_value_and_baseline(tmp_path: Path):
    """构造可算前向 20 日收益的样本，验证判决字段齐全且方向与输入一致。"""
    # 日线：一只票 50 天，事件日放第 5 天（前向 20 日窗口完整覆盖）
    closes = [10.0 + i * 0.1 for i in range(50)]
    _write_enriched_daily(tmp_path, "600519.SH", closes)
    # 龙虎榜事件日 = 2026-08-05（日线第 5 天），前向 20 日覆盖到 08-25
    _write_verdict_cache(tmp_path, "2026-08-05", org_net=1e8, hm_net=-1e8)  # 机构买、游资卖

    repo = _FakeRepo(tmp_path)
    out = dt.build_forward_verdict(repo)
    assert out["days"] == 1
    v = out["verdict"]
    assert v is not None
    assert v["n"] >= 1
    # 判决字段必须存在且为数值
    for key in ("org_buy_ex20_bp", "org_sell_ex20_bp", "diff_bp", "t", "all_ex20_median_bp"):
        assert isinstance(v[key], (int, float))
    # 只有机构买样本（无机构卖），n_org_buy >= 1
    assert v["n_org_buy"] >= 1
