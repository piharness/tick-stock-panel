"""实探 fuyao 交易日历 —— 本仓**唯一**一处向磁盘外问日期的地方。

## 为什么必须单独一个模块

有两个消费者：`scripts/panel_stall_alarm.py`（跨年续命）与
`analysis/auction_order.py`（09:20 的下单闸门）。取法写两遍就是 `qq_symbol`
少 `bj` 前缀那条老路 —— 两份实现只覆盖一份的分支，而分支差异只会在某一年
的某一天发作，那天恰好是最不能发作的一天。

本模块只做**取**。取回来的东西能不能用由 `trading_calendar.usable()` 判：
体检（窗口 ≥350 天、右端不落后今天 >10 天）与对账（重叠区逐日零分歧）都是
纯函数，不需要网络，也不该住在网络模块里。

## 刻意不做的事

* **不缓存**。滚动年窗的右端每天在动，而「右端 == 今天」本身就是这份表还活着的
  唯一证据。跨进程缓存等于把昨天的表当今天用 —— 那正好是本模块要供的那条判据
  最需要排除的失败模式（上游停更）。
* **不抛异常**。常量覆盖区间内判据本来就能成立，一次网络抖动不该把一个可判的
  判定变成崩溃。失败降级成 `(None, 原因)`，由调用方决定对退出码的影响。
* **原样带出原因**。「没配 key」「上游停更」「接口报错」三种处置完全不同，
  在这一层缩成同一个 `None` 之后，上层就再也分不出来了。
* **不自己解析 `date_ms`**。走 `provider.trading_days()`：上海零点毫秒 → 日期
  这个换算在本仓已有一份定义（`fuyao/provider.py::_date_of_ms`），抄第二份就
  只覆盖第一份的分支。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def probe() -> tuple[set | None, str]:
    """取近一年交易日集合，返回 `(交易日集合, 说明)`；不可用时集合为 `None`。

    第二项是给人读的病因，**不是**状态码：调用方必须把它原样放进读数或错误
    消息里，否则「缺凭证」会被读成「上游挂了」。
    """
    root = str(ROOT / "backend")
    if root not in sys.path:
        sys.path.insert(0, root)
    prov = None
    try:
        from app.plugins.fuyao import provider as fuyao_provider  # noqa: PLC0415

        ok, why = fuyao_provider.availability()
        if not ok:
            return None, why
        prov = fuyao_provider.FuyaoProvider()
        days = prov.trading_days()
        if not days:
            return None, "接口返回空列表"
        return set(days), f"{len(days)} 个交易日"
    except Exception as e:  # noqa: BLE001 — 网络/依赖/接口错误一律降级为「没有实探」
        return None, f"实探失败：{e}"
    finally:
        if prov is not None:
            prov.close()
