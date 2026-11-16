"""13:00 错标的修法：**一份实现，两个 TDX 生产者**。

2026-11-13 复核的三条要求，每条都配了会红的输入：

1. `plugins/tdx._minute_rows` 必须绑到同一份修法上 —— 它是第二个 TDX 分钟生产者，
   原来没有修标、`amount` 也不走 ×1e4。tdx 接管分钟源之后 13:00 会以「每天隔离
   几行」的形式回来，而那时没人会记得「这条只在 tdxquant 活跃时成立」。
2. 「13:00 恒非法」不等于「它一定是 11:30」：窄窗口（只拿到 13:00 及其之后）
   看不出 11:30 是否在盘上，此时不动手，交给落盘路径（它看得见合并后的分区）。
3. 只在**能判**的时候动手：11:30 在且 OHLCV 不同 ⇒ 另一根 bar，不猜。
"""
from __future__ import annotations

from datetime import datetime

from app import minute_labels
from app.plugins.tdx import provider as tdx_provider
from app.plugins.tdxquant import provider as tdxquant_provider

DAY = "2026-11-12"


def _row(stamp: str, px: float = 10.0, vol: float = 1000.0) -> dict:
    return {
        "symbol": "600519.SH", "datetime": datetime.fromisoformat(f"{DAY} {stamp}"),
        "open": px, "high": px, "low": px, "close": px,
        "volume": vol, "amount": px * vol * 100,
    }


def _full_day(**kw) -> list[dict]:
    """09:31..11:30 + 13:01..15:00，合成 240 格。"""
    out = []
    for h, m in [(9, m) for m in range(31, 60)] + [(10, m) for m in range(60)] + [(11, m) for m in range(31)]:
        out.append(_row(f"{h:02d}:{m:02d}", **kw))
    for h, m in [(13, m) for m in range(1, 60)] + [(14, m) for m in range(60)] + [(15, 0)]:
        out.append(_row(f"{h:02d}:{m:02d}", **kw))
    return out


def _times(rows: list[dict]) -> list[str]:
    return [r["datetime"].strftime("%H:%M") for r in rows]


# ---------------------------------------------------------------- 一份实现


def test_both_tdx_providers_share_one_implementation():
    """两份实现会在下一次改判据时只改一份 —— 用 `is` 绑住，复制一份等值函数也红。"""
    assert tdxquant_provider._fix_morning_close_label is minute_labels.fix_morning_close_label
    assert tdx_provider.fix_morning_close_label is minute_labels.fix_morning_close_label
    assert tdxquant_provider.fix_morning_close_label is tdx_provider.fix_morning_close_label


# ---------------------------------------------------------------- 四支判据


def test_copy_of_1130_is_dropped():
    rows = _full_day()
    rows.append(_row("13:00"))  # 与 11:30 全等
    out = minute_labels.fix_morning_close_label("600519.SH", rows)
    assert len(out) == 240
    assert "13:00" not in _times(out)
    assert "11:30" in _times(out)


def test_1130_different_from_1300_is_left_alone():
    rows = [r for r in _full_day() if not r["datetime"].strftime("%H:%M") == "13:00"]
    rows.append(_row("13:00", px=10.5, vol=900.0))
    out = minute_labels.fix_morning_close_label("600519.SH", rows)
    assert len(out) == 241, "判不了就不动: 猜错就是删掉一根真 bar"
    assert "13:00" in _times(out)


def test_missing_1130_with_morning_seen_is_relabelled():
    """停牌合成格: 上午看见过、没有 11:30 ⇒ 13:00 顶替了它。"""
    rows = _full_day()
    rows = [r for r in rows if r["datetime"].strftime("%H:%M") != "11:30"]
    rows.append(_row("13:00", vol=0.0))
    out = minute_labels.fix_morning_close_label("600519.SH", rows)
    t = _times(out)
    assert "13:00" not in t
    assert t.count("11:30") == 1
    assert len(out) == 240


def test_missing_1130_without_morning_is_left_alone():
    """窄窗口: 只拿到 13:00 与 13:01。

    「这根不是 13:00」是这批数据能证明的；「它就是缺掉的 11:30」不是 ——
    11:30 可能在盘上（另一批写的），也可能这只票上午停牌。落盘路径看得见
    合并后的分区，那一侧才拍得了板。
    """
    rows = [_row("13:00", vol=800.0), _row("13:01", px=10.1, vol=700.0)]
    out = minute_labels.fix_morning_close_label("600519.SH", rows)
    assert _times(out) == ["13:00", "13:01"]


def test_single_narrow_bar_is_left_alone():
    out = minute_labels.fix_morning_close_label("600519.SH", [_row("13:00", vol=800.0)])
    assert _times(out) == ["13:00"]


def test_empty_and_no_1300_are_noops():
    assert minute_labels.fix_morning_close_label("600519.SH", []) == []
    rows = _full_day()
    assert _times(minute_labels.fix_morning_close_label("600519.SH", rows)) == _times(rows)


# ---------------------------------------------------------------- provider 接线


def _tdx_raw(stamp: str, px: float = 10.0, vol: float = 100_000.0,
             amount: float = 1_000_000.0) -> dict:
    return {"datetime": f"{DAY} {stamp}", "open": px, "high": px, "low": px,
            "close": px, "vol": vol, "amount": amount}


def test_tdx_minute_rows_drops_the_copy():
    """tdx 的 `amount` 是元（不走 ×1e4），副本的 amount 与正本只差取整 —— 一样要丢。"""
    out = tdx_provider._minute_rows(
        "600519.SH", [_tdx_raw("11:30"), _tdx_raw("13:00", amount=999_900.0)]
    )
    assert _times(out) == ["11:30"]


def test_tdx_minute_rows_relabels_when_1130_missing():
    rows = [_tdx_raw("09:31"), _tdx_raw("13:00", vol=0.0, amount=0.0)]
    out = tdx_provider._minute_rows("600519.SH", rows)
    assert _times(out) == ["09:31", "11:30"]