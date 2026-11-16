"""已知交易日集合的唯一取数点 = kline_daily 分区（日分区 + 月分区双布局）。

为什么单独成模块: dragon_tiger 与 auction_benchmark 曾各写一份
`^date=(\\d{4}-\\d{2}-\\d{2})$` 的目录名正则。日 K 迁到月分区
(`data/kline_daily/date=2026-11`) 后, 两份正则都只看得见迁移边界前的日分区,
于是「≤目标日的最近交易日」永远解析成 2026-10-30 —— 而这两个消费者都指望它
决定榜单日期。

回归症状 (2026-11-13 实测): `/api/market-recap/dragon-tiger?date=2026-11-13`
返回 10-30 的榜单且 state=ok; 上游当日 66 只其实就在一次请求之外。页面把 14 天
滞后解释成「龙虎榜 T 日盘后才公布, 是信息到达的硬约束」——把缺陷讲成了规律。

判据: 本函数对 2026-11 的月分区必须返回与 `kline_daily` 数据列一致的日期集合;
日分区仍走目录名 (零 parquet 扫描)。9p 挂载实测 17ms/次, 不需要缓存。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.tickflow.partitions import list_partition_dates


def kline_trading_days(data_dir: Path) -> list[date]:
    """本地日 K 覆盖的交易日 (升序)。扫描失败返回空表, 由调用方决定降级行为。"""
    return sorted(list_partition_dates(Path(data_dir) / "kline_daily"))
