"""A股市场时间工具 — 固定北京时间 (UTC+8, 无夏令时)。

服务器/容器本地时区不可靠 (python:slim 镜像默认 UTC), 交易时段判断、
实时行情落盘日期等必须显式使用北京时间, 否则 Docker 部署时轮询窗口
与真实交易时段完全错开 (北京 9:15-15:05 = UTC 1:15-7:05)。
"""
from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

# A 股交易时段 (北京时间): 上午 9:30-11:30 (120 分钟) + 下午 13:00-15:00 (120 分钟) = 240 分钟
_TRADING_TOTAL_MINUTES = 240
_MORNING_START = dt_time(9, 30)
_MORNING_END = dt_time(11, 30)
_AFTERNOON_START = dt_time(13, 0)
_AFTERNOON_END = dt_time(15, 0)


def minute_grid() -> tuple[dt_time, ...]:
    """日内分钟 K 的规范时刻: 09:31..11:30 ∪ 13:01..15:00, 共 240 格。

    **按区间末日标注**, 与上面的会话边界 (`_MORNING_START=09:30`) 差的正是这 1 分钟:
    一根分钟 K 用「这一分钟的结束时刻」命名, 所以首格是 09:31 与 13:01。
    09:30 / 13:00 属于「区间首日标注」, 是另一套口径 —— 混进来会让同一天变成 241 根,
    且那 1 根通常是 11:30 的副本, 金额被双计 (2026-11-05 40 只 / 2026-11-12 32 只)。

    这是**全库落盘约定**, 不是本模块的选择: 2026-11-13 实测 6 个交易日 8,025,120 行
    越界 0 行 (抽样 2026-10-08 亦为 0), 5,570 只/日里 5,530+ 只都落在网格上。

    落盘闸门 `kline_sync._assert_minute_grid` 与审计脚本
    `analysis/_diag_minute_audit_1113.py` 都取这一份, 不许各自再写一遍。
    """
    out: list[dt_time] = []
    for start_min, n in ((9 * 60 + 31, 120), (13 * 60 + 1, 120)):
        out.extend(dt_time((start_min + i) // 60, (start_min + i) % 60) for i in range(n))
    return tuple(out)


def cn_now() -> datetime:
    """当前北京时间 (带时区)。"""
    return datetime.now(CN_TZ)


def cn_today() -> date:
    """当前北京日期。"""
    return datetime.now(CN_TZ).date()


def in_continuous_session(now: datetime | None = None) -> bool:
    """A股连续竞价时段 (北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。"""
    now = now or cn_now()
    return now.weekday() < 5 and (
        _MORNING_START <= now.time() <= _MORNING_END
        or _AFTERNOON_START <= now.time() <= _AFTERNOON_END
    )


def trading_minutes_elapsed_from_dt(dt: datetime) -> float:
    """根据北京时间 datetime 计算当日已交易分钟数。

    交易时段: 9:30-11:30 (0~120) + 13:00-15:00 (120~240)。
    - 开盘前 = 0; 午休(11:30-13:00) = 120(保持上午累计); 收盘后 = 240。
    - 非交易日(周末) = 240 (视作全天, 避免量比被折算成 0)。
    """
    if dt.weekday() >= 5:
        return float(_TRADING_TOTAL_MINUTES)
    t = dt.time()
    if t < _MORNING_START:
        return 0.0
    if t < _MORNING_END:
        return (dt.hour * 60 + dt.minute - 9 * 60 - 30) + dt.second / 60.0
    if t < _AFTERNOON_START:
        return 120.0  # 午休, 保持上午累计
    if t < _AFTERNOON_END:
        return 120.0 + (dt.hour * 60 + dt.minute - 13 * 60) + dt.second / 60.0
    return float(_TRADING_TOTAL_MINUTES)


def trading_minutes_elapsed() -> float:
    """当前已交易分钟数 (基于服务端北京时间)。

    量比折算的兜底: 当行情 timestamp 缺失时用服务端时间。
    优先使用 trading_minutes_elapsed_from_ts (行情真实时间, 更准)。
    """
    return trading_minutes_elapsed_from_dt(cn_now())


def trading_minutes_elapsed_from_ts(ts_ms: int | float | None) -> float:
    """从行情时间戳(毫秒)计算当日已交易分钟数。

    优先使用此函数: 行情 timestamp 是真实成交时间, 比服务端时间更准
    (服务端时间含网络/限流延迟)。

    Args:
        ts_ms: 毫秒级 Unix 时间戳 (TickFlow SDK quote.timestamp / kline.timestamp)

    Returns:
        已交易分钟数 (0~240)。timestamp 为 None/无效时返回 240 (视作全天,
        避免量比被折算成 0)。
    """
    if not ts_ms:
        return float(_TRADING_TOTAL_MINUTES)
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=CN_TZ)
    except (ValueError, TypeError, OSError):
        return float(_TRADING_TOTAL_MINUTES)
    return trading_minutes_elapsed_from_dt(dt)

