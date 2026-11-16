"""分钟 bar 的**标签修正** —— 唯一实现，两个 TDX 源共用。

## 为什么单独一个模块

仓库里有两个 TDX 分钟生产者：`plugins/tdxquant`（DLL 批量，`Amount×1e4`）与
`plugins/tdx`（`get_minute` 流式，`amount` 直接是元）。它们产出的行形状相同，
「上午最后一根被标成 13:00」这件事也一样 —— 而 2026-11-13 之前只有 tdxquant
被修过，tdx 接管分钟源之后这个缺陷会以「每天隔离几行」的形式回来。

修法写两遍的代价不是多打几个字：两份实现会在下一次改判据时只改一份
（`market_time.minute_grid()` 已经踩过一次）。所以这里只留一份，
`tests/test_minute_labels.py` 用 `is` 把两个 provider 绑到它上面。

## 判据

13:00 在规范网格上**不存在**（下午第一根是 13:01），所以它一定是错标。但
「一定是错标」不等于「一定是 11:30」：

* 同日 **有** 11:30 且 OHLCV 全等 → 是它的副本，丢（40/40、32/32 实证）；
* 同日 **有** 11:30 但 OHLCV 不同 → 判不了，原样留着（交给审计的 L 轴）；
* 同日 **无** 11:30，且这一批里**看见过上午** → 是上午最后一根被顶替，改标
  （11-12 那 7 只停牌合成格）；
* 同日 **无** 11:30，但这批是从 13:00 才开始的**窄窗口** → 不动。

最后一条是 2026-11-13 复核加的。上一版在此处直接改标，理由是「13:00 恒非法」——
方向对，但结论多走了一步：窄窗口里只看得见 13:00 及其之后，看不见 11:30 是否
已经在盘上。「这根不是 13:00」是这批数据能证明的；「它就是缺掉的 11:30」不是。
落盘路径看得见合并后的分区，那一侧才是该拍板的地方（`kline_sync` 的逐行隔离）。

## 契约

纯函数：输入若干行（可能跨日），输出同样形状的行，只改 `datetime` 或删行。
不读盘、不写盘、不抛异常。
"""
from __future__ import annotations

import logging
from datetime import time

logger = logging.getLogger(__name__)

#: 上午最后、下午第一根。两个都是**终点标注**（见 `market_time.minute_grid`）。
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)

_OHLCV = ("open", "high", "low", "close", "volume")


def _hm(dt) -> tuple[int, int]:
    return (dt.hour, dt.minute)


def fix_morning_close_label(symbol: str, rows: list[dict]) -> list[dict]:
    """把错标成 13:00 的「上午最后一根」改回 11:30，或丢掉它的副本。

    只在**能判**的时候动手；判不了就原样返回（见模块 docstring 的四支）。
    """
    if not rows:
        return rows
    by_day: dict[object, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["datetime"].date(), []).append(r)
    out: list[dict] = []
    dropped = relabeled = 0
    for day in sorted(by_day, key=str):
        day_rows = by_day[day]
        mid = [r for r in day_rows if _hm(r["datetime"]) == _hm(AFTERNOON_OPEN)]
        if not mid:
            out.extend(day_rows)
            continue
        morning = [r for r in day_rows if _hm(r["datetime"]) == _hm(MORNING_CLOSE)]
        # 这批数据是否看见过上午：没有 11:30 时，只有看见过上午才敢说「它被顶替了」。
        saw_morning = any(_hm(r["datetime"]) <= _hm(MORNING_CLOSE) for r in day_rows)
        for r in day_rows:
            if _hm(r["datetime"]) != _hm(AFTERNOON_OPEN):
                out.append(r)
            elif morning and all(
                r.get(c) == morning[0].get(c) for c in _OHLCV
            ):
                dropped += 1
            elif not morning and saw_morning:
                out.append({**r, "datetime": r["datetime"].replace(hour=11, minute=30)})
                relabeled += 1
            else:
                # 有 11:30 但 OHLCV 不同 = 另一根 bar；没有 11:30 又没看见上午 =
                # 窄窗口无从判断。两支都原样留着。
                out.append(r)
    if dropped or relabeled:
        logger.info(
            "分钟K %s: 13:00 是上午最后一根的错标 — 丢副本 %d 根 / 改标 %d 根",
            symbol, dropped, relabeled,
        )
    return out
