"""交易日历（常量）—— 唯一能守住「两条线同时缺一天」的那条轴。

## 为什么需要一份常量日历

`auction_order.pool_frame_mismatch()` 是**相对**判据：帧 f[4] 的昨收 vs 名单里的
日线收盘。两边**同源落后一天**时差值为 0，它只会回答「是的，同一天」。这不是实现
缺陷，是相对判据的定义决定的 —— 再补一条相对比较也守不住同一格。

而那一格的后果不是「表难看」：`_prev_trade_day` 取「最后一格 < today」，前一交易日
整格缺失时安静退回再前一天；`_bar_is_final` 照样为真；名单声明 `entry_date=None`；
帧若同源滞后则逐票相等。于是某个早晨手里是两份前天的表，exit 0，或者更糟：
帧整体滞后 ⇒ 逐票 `stale_snapshot`(SKIP) ⇒ `degraded()` 打一张「每票 100 万」的
降级表 ⇒ **exit 0**。全 SKIP 不是拒绝出表，是照常下单。

## 为什么是常量，不是「再找一个数据源」

本仓里能看见的日期集合全是同一批 dump 的两个目录：`kline_daily` 与
`kline_daily_enriched` 实测 371=371 双向零差；`kline_index_daily` 249 天逐日相同、
mtime 只差 4 秒（同一次 pipeline 的相邻步骤）。它们能分「个股单侧失败」，分不了
「整天没跑」。fuyao 日历是唯一的外部权威，但它在 `data/data_sources/` 为空时返回
`None`，把「不知道」写成「没问题」—— 而这一格恰恰最不能容忍「不知道」。

## 常量怎么来的，以及它必须被证伪

区间 `2025-05-07 ~ 2026-12-31` 内的休市日 = 法定节假日落在工作日的那几天。
它不依赖任何一次运行，也不依赖面板 —— 所以它可以反过来审面板：

    backend/tests/test_auction_order_plan.py::test_calendar_constant_explains_every_panel_working_day

断言面板 371 个分区与常量**逐工作日相等**（0 个多余、0 个遗漏）。实测
2025-05-07~2026-11-12 共 397 个工作日：371 个有分区、26 个是节假日，无一例外。
这条等式是常量能被当成锚的前提；差一天就说明要么常量写错，要么那天真的缺。

## 覆盖范围是硬边界，不是注释

超出常量的日期返回 `None`（判不了），调用方必须**拒出表**、并在消息里说清
该补哪一段。留一条「未知就放行」的分支，等于把这一格重新变成静默 —— 那正是本文件
存在的理由。

## 第二条轴：实探日历（不是常量的替代品）

2026-11-12 实测 fuyao `/api/a-share/calendar/trading-days`：**无入参**、固定返回
`[当日-365 天, 当日]` 共 243 个交易日，八种参数写法全部被忽略。它**没有未来表**，
问「2027 年哪天开市」这条路是死的。但它也不需要未来表：本文件的两个判据只问过去，
滚动一年窗口每天都包含整个过去一年。

于是「2026-12-31 之后闹钟会瞎」这件事的真因不是「常量到期」，而是「常量是唯一定义点」。
把实探接进来之后，右端落在实探上，`prev_market_day` / `gap_days` 就能跨年作答，
`sessions_left` 只剩「常量还能顶多久」这一个含义。

**常量不退役，因为两者不是冗余关系。** 实探引入了一个常量没有的失败模式：上游
停更。表停在 3 天前时 `prev_market_day` 会安静返回 3 天前，`expect` 变松 ⇒ 面板
停了 3 天也判绿。所以：

  * 常量覆盖区间内**常量是权威**（它被面板 371 个分区逐日证过），实探只是审计；
  * 超出常量才用实探，且必须先过 `live_health` 体检（窗口 ≥350 天、右端不落后
    今天 >10 天），不合格就当「没有实探」；
  * 两者在重叠区**逐日对账**（`reconcile`），任一分歧 ⇒ 判不了。2026-11-12 实测
    重叠 243 天、双边零分歧，所以「一致」本身是比单边更强的证据。

## 「接得上」只有一个定义点

两个消费者都要问「这份实探能不能替常量说话」：`scripts/panel_stall_alarm.py`
（跨年续命）与 `analysis/auction_order.py`（09:20 下单闸门）。两边各写一遍
「体检通过 且 零分歧」的后果不是风格问题 —— 同一份实探会被一边判成「接得上」、
另一边判成「判不了」，而人能分辨这两行字的窗口只有一个 09:20。

所以这个合取写在 `usable()` 里，两边都调它。两个条件都不是「请求成功」：
体检通过才说明它明天还会前滚，零分歧才说明它今天说的是真的。
"""
from __future__ import annotations

from datetime import date, timedelta

#: 工作日休市日（A 股：周末恒不开市，即使国务院调休上班）。逐条 ISO，便于 grep 核对。
_HOLIDAY_ISO = (
    # 2025：面板区间从 05-07 起，之前的假期没有证据，不声明
    "2025-06-02",                                              # 端午
    "2025-10-01", "2025-10-02", "2025-10-03",                   # 国庆
    "2025-10-06", "2025-10-07", "2025-10-08",                   # 国庆 + 中秋
    # 2026
    "2026-01-01", "2026-01-02",                                 # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-23",                                 # 春节
    "2026-04-06",                                               # 清明
    "2026-05-01", "2026-05-04", "2026-05-05",                   # 劳动节
    "2026-06-19",                                               # 端午
    "2026-09-25",                                               # 中秋
    "2026-10-01", "2026-10-02", "2026-10-05",
    "2026-10-06", "2026-10-07",                                 # 国庆
)

HOLIDAYS = frozenset(date.fromisoformat(s) for s in _HOLIDAY_ISO)

#: 常量有证据的区间（左端 = 面板第一个分区；右端 = 2026 年末，跨年必须补表）。
COVERED = (date(2025, 5, 7), date(2026, 12, 31))

#: 距 `COVERED` 右端多少天开始告警。**常量，不是开关**——可配置的截止日
#: 会被一路推到失明当天，与 `panel_stall_alarm.MAX_TOLERABLE_LAG` 同一条道理。
#: 30 天的依据：国务院次年放假安排通常在上一年 11~12 月公布，"公布晚于预期"
#: 需要一个不至于只剩几天的缓冲。
EXPIRY_WARN_DAYS = 30

#: 实探日历（fuyao `/api/a-share/calendar/trading-days`）的健康判据。
#:
#: 2026-11-12 实测：该端点**无入参**，固定返回 `[当日-365 天, 当日]` 共 243 个
#: 交易日；八种参数写法（start/end、start_ms/end_ms、date、year、days、limit）
#: 全部被忽略，返回完全相同的 243 行。**所以它永远没有未来表**——问「2027 年
#: 哪天开市」这条路是死的，不必再试。
#:
#: 但这不重要，因为本模块的两个判据**只问过去**：`prev_market_day` 从 today-1
#: 往回走、`gap_days` 只问 `(prev, today)` 之间的格子。滚动一年窗口每天都包含
#: 整个过去一年，于是「右端」这个概念对判定并不存在，只对 `sessions_left`
#: 那个倒计时存在。真正会失明的只有常量，不是闹钟。
#:
#: 代价是引入了一个常量没有的失败模式：**上游自己停更**。表停在 3 天前时，
#: `prev_market_day` 会安静返回 3 天前的日子，`expect` 变松 ⇒ 面板停了 3 天
#: 也判绿。所以实探结果必须先过这两条体检，不合格就当作「没有实探」，
#: 不许用它顶替常量。
LIVE_MIN_SPAN_DAYS = 350      #: 窗口短于这个数 ⇒ 不是整年表，够不到远一点的过去
LIVE_MAX_RIGHT_GAP_DAYS = 10  #: 右端落后今天超过这个数 ⇒ 上游停更（春节最长假 8 天 + 周末）


def live_span(live) -> tuple[date, date] | None:
    """实探交易日集合的覆盖区间；空或未提供返回 `None`。"""
    if not live:
        return None
    return (min(live), max(live))


def live_health(live, today: date) -> dict:
    """实探日历是否是一份**仍在前滚的整年表**。

    返回 `{ok, reason, n, left, right, span_days, right_gap}`。`ok=False` 时调用方
    必须按「没有实探」处理，**不许**拿它去替代常量：一份停更的表比没有表更危险，
    因为它会安静地把 `expect` 往回挪，制造出「面板停了两天仍判绿」这种假绿
    ——正是本文件开篇那段要杀的东西。
    """
    s = live_span(live)
    if s is None:
        return {"ok": False, "reason": "没有实探日历（未配置/请求失败/未启用）",
                "n": 0, "left": None, "right": None,
                "span_days": None, "right_gap": None}
    left, right = s
    span_days = (right - left).days
    right_gap = (today - right).days
    if span_days < LIVE_MIN_SPAN_DAYS:
        reason = f"窗口只有 {span_days} 天（<{LIVE_MIN_SPAN_DAYS}）：不是整年表"
    elif right_gap > LIVE_MAX_RIGHT_GAP_DAYS:
        reason = f"右端 {right} 落后今天 {right_gap} 天（>{LIVE_MAX_RIGHT_GAP_DAYS}）：上游停更"
    else:
        reason = ""
    return {"ok": not reason, "reason": reason, "n": len(live),
            "left": left.isoformat(), "right": right.isoformat(),
            "span_days": span_days, "right_gap": right_gap}


def usable(live, today: date) -> tuple[set | None, dict]:
    """实探能否**接替**常量：体检通过 **且** 与常量在重叠区零分歧。

    返回 `(可接班的集合或 None, {health, reconcile})`。这是「什么叫接得上」的
    唯一定义点 —— `panel_stall_alarm._carries()` 与 `auction_order.resolve_pool()`
    都必须调它，不许各写一遍这个合取（理由见模块开篇「只有一个定义点」）。

    为什么不是「两个条件任一成立就够」：只体检通过 ⇒ 它可能与常量在重叠区打架
    （至少一边错）；只零分歧 ⇒ 它可能是一份停更的表（右端不动也照样与常量一致，
    因为重叠区全在过去）。两个都为真才同时排除了「错」与「旧」。

    空集合返回 `None`：`bool([])` 为假，`(None)` 与「没有实探」同形 —— 而
    「没有实探」正是调用方该走的降级分支。
    """
    h = live_health(live, today)
    rc = reconcile(live)
    ok = bool(live) and h["ok"] and not rc["disagree"]
    return (set(live) if ok else None), {"health": h, "reconcile": rc}


def is_market_day(d: date, live=None) -> bool:
    """`d` 是否 A 股交易日。

    优先级：**常量覆盖区间内一律用常量**（它被面板 371 个分区逐日证过，是离线权威）；
    超出常量才查实探日历；两者都不覆盖才退回「周末以外都是交易日」的近似
    ——调用方必须先用 `gap_days` 的边界检查挡住这一天，不许直接信这个兜底。
    """
    lo, hi = COVERED
    if lo <= d <= hi:
        return d.weekday() < 5 and d not in HOLIDAYS
    if live and d in live:
        return True
    s = live_span(live)
    if s and s[0] <= d <= s[1]:
        return False
    return d.weekday() < 5 and d not in HOLIDAYS


def prev_market_day(d: date, live=None) -> date:
    """严格早于 `d` 的最近交易日。只往回走 ⇒ 实探窗口的右端是不是今天都无所谓。"""
    x = d - timedelta(days=1)
    while not is_market_day(x, live):
        x -= timedelta(days=1)
    return x


def reconcile(live) -> dict:
    """常量与实探日历在**重叠区**逐日对账：`{overlap, disagree, n_overlap}`。

    两边都是「独立权威」：常量被面板分区证过，实探来自交易所日历接口。重叠区
    出现分歧说明**至少一边错了**，此时整条绝对锚不可信 —— 调用方必须判不了，
    而不是挑一个顺着自己结论的。

    这不是防御性编程：2026-11-12 实测重叠 243 天、双边零分歧，所以任何一天出现
    分歧都是新信息，值得停下人来。反过来说，正因为零分歧，「两边一致」才能被
    当成比单边更强的证据。
    """
    s = live_span(live)
    if s is None:
        return {"overlap": None, "disagree": [], "n_overlap": 0}
    a = max(COVERED[0], s[0])
    b = min(COVERED[1], s[1])
    if a > b:
        return {"overlap": None, "disagree": [], "n_overlap": 0}
    const, d = set(), a
    while d <= b:
        if d.weekday() < 5 and d not in HOLIDAYS:
            const.add(d)
        d += timedelta(days=1)
    liv = {d for d in live if a <= d <= b}
    dis = sorted(const ^ liv)
    return {"overlap": [a.isoformat(), b.isoformat()],
            "disagree": [x.isoformat() for x in dis],
            "n_overlap": len(const | liv)}


def gap_days(prev_iso: str | date, today: date, live=None) -> list[date] | None:
    """`prev` 之后、`today` 之前**本该有分区却没有**的交易日。

    - `[]`  ⇒ `prev` 就是 `today` 的前一格（相邻已由绝对锚证明）
    - 非空  ⇒ 面板缺了这些交易日 ⇒ 名单的排序日不是今天的前一格，调用方必须拒出表
    - `None`⇒ 超出可判区间，判不了 —— **不许当成 `[]`**

    `today` 自己是不是交易日不影响判定：休市日的前一格仍是上一交易日。

    `live` 传入**已过 `live_health` 体检**的实探集合时，可判区间的右端从常量右端
    延伸到实探右端 —— 这是 2026-12-31 之后唯一的活路。刻意不在这里复检健康状况：
    体检是调用方的决定，本函数只管「区间内逐日作答」，两件事混在一起会让
    「区间被放宽了」和「上游停更了」在返回值里同形。
    """
    prev = date.fromisoformat(prev_iso) if isinstance(prev_iso, str) else prev_iso
    lo, hi = COVERED
    s = live_span(live)
    if s is not None:
        lo, hi = min(lo, s[0]), max(hi, s[1])
    if today > hi or prev < lo:
        return None
    if prev >= today:
        return []
    out: list[date] = []
    d = prev + timedelta(days=1)
    while d < today:
        if is_market_day(d, live):
            out.append(d)
        d += timedelta(days=1)
    return out


def audit(partitions, lo: date | None = None, hi: date | None = None) -> dict:
    """把常量与一批分区日逐日对账：`{extra, missing}`，均为升序 `date`。

    * `extra`   —— 常量说休市，分区却存在；
    * `missing` —— 常量说交易日，却没有分区。

    两者非空即「常量与面板不一致」，**必须人看**：从这里分不清是常量写错日期，
    还是那一天真的缺。`lo/hi` 缺省取这批分区的 min/max —— 因此**右端缺格它看不
    见**（那归停摆闹钟），本函数只管 `[lo, hi]` 之内。

    为什么要有这个函数而不是在测试里再写一遍循环：`COVERED` 右端到期后要补
    次年的法定节假日，而**补进去的那一天当天没有任何分区可对照**（未来）。若审
    只写死在某个上界，补错一天可以一路绿到那天真的到来 —— 而那时错的是闹钟的
    「应有」，它会在一个本该休市的日子判「缺一格」。所以审计范围必须跟着面板
    增长，而不是跟着常量给的范围走。
    """
    have = sorted({date.fromisoformat(p) if isinstance(p, str) else p for p in partitions})
    if not have:
        return {"extra": [], "missing": []}
    lo = lo or have[0]
    hi = hi or have[-1]
    known = set(have)
    extra: list[date] = []
    missing: list[date] = []
    d = lo
    while d <= hi:
        if d.weekday() < 5:
            in_have = d in known
            if in_have != is_market_day(d):
                (extra if in_have else missing).append(d)
        d += timedelta(days=1)
    return {"extra": extra, "missing": missing}


def sessions_left(today: date) -> int:
    """`today` 之后、`COVERED` 右端（含）还有多少个**交易日**。越过右端为 0。

    与自然日分开报，因为这两数在这张表上不是一回事：2026-11-12 距右端
    **49 自然日**，其中只有 **35 个交易日**。闹钟的读数单位本来就是交易日
    （`lag` 也是），报自然日会把「还剩 5 周」说成「还剩 7 周」，而多出来的
    那两周它一格都判不了。
    """
    hi = COVERED[1]
    if today >= hi:
        return 0
    n, d = 0, today + timedelta(days=1)
    while d <= hi:
        if is_market_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def expiry(today: date) -> dict:
    """日历有效期读数 `{state, hi, days_left, sessions_left, warn_days}`。

    `state` 三态，**没有第四态**：

      * `covered` —— 右端在 30 天之外，此刻能判；
      * `warn`    —— 还剩 ≤`EXPIRY_WARN_DAYS` 天，调用方必须映射成
        **非零、但不是「停摆」** 的码（`panel_stall_alarm` 用 `5`）；
      * `blind`   —— 已越过右端，此后 `gap_days` 每天返回 `None`。

    为什么必须有 `warn`：`blind` 是**日程表上的悬崖**，不是随机故障。
    悬崖前不升级，悬崖后每天都是同一个「判不了」；永久噪音的结局是被关掉，
    于是闹钟恰好在它最该工作的那天停止工作。而只把天数印在输出里不够 ——
    绿的输出没人读，唯一到得了人的信道是退出码。
    """
    hi = COVERED[1]
    days_left = (hi - today).days
    if today > hi:
        state = "blind"
    elif days_left <= EXPIRY_WARN_DAYS:
        state = "warn"
    else:
        state = "covered"
    return {"state": state, "hi": hi.isoformat(), "days_left": days_left,
            "sessions_left": sessions_left(today), "warn_days": EXPIRY_WARN_DAYS}
