"""面板停摆闹钟：唯一能发现「整个 `kline_daily` 停止增长」的判据。

## 它回答什么，不回答什么

全项目的每个数都长在 `data/kline_daily` 上，但没有任何一条现有判据能看见
「这张表从某天起不再新增」。已有的检查都在回答别的问题：

  * `pool_frame_mismatch()` —— 帧前收 vs 名单日线收盘，**相对**判据，两边同源
    落后一天时差值为 0（见 `analysis/trading_calendar.py` 开篇）。
  * `symbols_lagging()` —— **个股**层面「这只票的最后一根 bar 太旧」，面板整体
    停了它会让每只票都 lagging，于是「全员异常」在告警里和「个别异常」同形。
  * `test_calendar_constant_explains_every_panel_working_day` —— 只证「常量与
    已有分区逐工作日相等」，是**内部一致性**，面板停在昨天它照样绿。

所以这里只做一件事：**右端的绝对位置**。

    max(have) 落后 prev_market_day(today) 超过 N 个交易日 ⇒ 红

## 为什么绑 `prev_market_day(today)`，不绑 `today`

绑 `today` 会让它每个交易日早晨必修红：11-12 的分区实测是 **13:34** 才建
（见 `analysis/_diag_pool_freshness.py` 的 `last_partition`），上午跑必红。
而「今天该有吗」这个问题在盘中根本没有唯一答案（收盘前分区本就不该存在）。
绑到前一交易日，答案就与时钟无关：**上一格在不在**。

## 为什么 N 默认 0，且必须打印出来

`N=0` 不是洁癖：容忍一个交易日，就等于让「整体停摆一天」静默通过，而一天的
滞后足以让 `_prev_trade_day` 安静退回再前一天（`trading_calendar.py` 开篇那段）。
但容忍度一旦可配置就会变成永久放宽，所以：

  * 默认 0，上限 `MAX_TOLERABLE_LAG`（5）—— 超过「一周不更新也算正常」的容忍
    度等于取消这个闹钟，直接拒绝而不是默默接受。
  * **绿也要打印 `lag / max_lag`**：`0/0` 与 `2/3` 都是绿，但前者是没落后，
    后者是落后了但被放过。只打印「✓」就把这两种情况合成了一种。

## 三个必须写明的边界

1. **只问「够不够新」，不问「有没有洞」**。中间某格缺失而末格在，这里绿。
   那是 `test_calendar_constant_explains_every_panel_working_day` 的职责
   （0 多余、0 遗漏），不是停摆——停摆的定义是右端不动。
2. **看不见「分区在但内容空」**。目录建了、parquet 零行或没有 `quote_ts`，
   这里绿。那是 `data_integrity` 与 `_bar_is_final` 的分工。一个脚本不能同时
   当两个判据用，否则任一方向报红都读不出病因。
3. **日历覆盖外不判断**（`exit 3`，非零）。`trading_calendar.COVERED` 止于
   2026-12-31；跨年必须补表，而不是让 `prev_market_day` 在表外用「周末以外
   都是交易日」自信地回答。`exit 3` 与 `auction_order` 的 `exit 4`（票池不可用）
   刻意分开：那个码已经被「休市日」和「前一格缺失」共用，不该再挂第三个含义。
4. **看不见「今天该有而没有」，粒度是一整个交易日**。11-12 的分区实测
   13:34 才建，所以任何「今天收盘后必须有今天」的判据都必须带一个时刻参数，
   而那个参数在 pipeline 晚跑时天天假红。因此判据只问「上一格在不在」：
   周五停摆 → 周一早晨红（`expect` 从 11-12 前进到 11-13 的那一刻）。
   代价是周五下午到周一早晨这 60 小时里它不响；要缩短就得引入时刻判据，
   那是**另一个**决定，不该顺手塞进这里。

## 分区名的解析是严格的

`date=2026-1-1` 这类非零填充名字不进 `have`，也不被静默跳过——它进 `dirty`，
主入口直接红。`data/` 是 9p 挂载，元数据按文件数线性收费，所以只 `iterdir()`
一层目录名，绝不递归、绝不 glob `*.parquet`。

## 通知：红了要送到人，否则等于没红

判据红而消息留在终端里，和没装这个闹钟是一回事。所以红了会推送到设置页里已配好的
Webhook（飞书 / 企业微信 / 自定义 JSON，见 `backend/app/services/webhook_adapter.py`）。
**不重写发送逻辑**：签名、按字节截断、HMAC 头部那里已经有一份定义和测试，抄第二份
必然只覆盖第一份的分支。

通知状态与判定状态**正交**，退出码因此有第五个：

  * `2` = 你还没配通道 ⇒ **配置问题**，一次性的，不是「面板停了」；
  * `4` = 配好了但**送不出去** ⇒ 通道故障，今天这条红没人知道。

把这两种塞进同一个 `1`，cron 只看退出码时就分不出「面板停了」和「闹钟哑了」。
`--no-notify` 关掉推送（CI / 纯判据），`--notify-test` 只测通道。

## 日历有效期：`5` 是「面板没停，但闹钟快瞎了」

判据依赖 `trading_calendar.COVERED`，右端 `2026-12-31`。越过之后常量对
`gap_days` 没有发言权，如果**没有第二条轴**，此后每天 `exit 3`。

2026-11-12 实测：fuyao 的 `/calendar/trading-days` 固定返回 `[当日-365 天, 当日]`
共 243 个交易日、无入参（八种参数写法全被忽略），**没有未来表**。但它也不需要：
本脚本的两个判据只问过去，滚动一年窗口每天都含整个过去一年。所以常量到期
不等于闹钟失明 —— 前提是这条实探轴接得上。默认开启（`--no-live` 关掉）。

`5` 因此收紧成「常量撑不住 **且** 实探接不了班」：实探不可用、停更（窗口短于
350 天或右端落后今天 >10 天）、或与常量在重叠区有分歧，三者任一即接不了班。
只报常量余额是**假警报**：那会让 `5` 从 2026-12-01 起每天响一次，而每一天
面板其实都照常可判。永久噪音的结局是被关掉，于是它恰好在最该工作的那天停止工作。

优先级仍是 **停摆 `1` > 日历 `5` > 正常 `0`**：若让 `5` 盖过 `1`，一次真实停摆
会被读成「只是日历快过期」。越过右端后，若实探能接替就仍是 `0`/`1`，接不了才 `3`。

阈值 `EXPIRY_WARN_DAYS` 刻意**不做命令行参数**，理由同 `--max-lag` 的上限。

## 实探轴为什么不能替代常量

实探引入了一个常量没有的失败模式：上游停更。表停在 3 天前时 `prev_market_day`
会安静返回 3 天前，`expect` 变松 ⇒ 面板停了 3 天也判绿。所以常量在它的覆盖
区间内仍是权威（被面板 371 个分区逐日证过），实探的作用是**审计**（`reconcile`：
重叠区逐日对账，2026-11-12 实测 243 天零分歧）与**续命**（超出常量才放宽右端）。
两者一致才算「接得上」—— 单边成立都不是证据。

用法：
    backend/.venv/Scripts/python.exe scripts/panel_stall_alarm.py
    backend/.venv/Scripts/python.exe scripts/panel_stall_alarm.py --today 2026-11-17
    backend/.venv/Scripts/python.exe scripts/panel_stall_alarm.py --json
    backend/.venv/Scripts/python.exe scripts/panel_stall_alarm.py --no-live
    backend/.venv/Scripts/python.exe scripts/panel_stall_alarm.py --notify-test
退出码：
    0 绿（未停摆，或落后未超限）
    1 红（停摆 / 分区名脏 / 一个分区都没有）—— 需要推送时已送达
    2 输入不可用（表目录缺失、`--max-lag` 越界、**要求推送但没配通道**）
    3 判不了（超出可判区间 / 常量已过期且实探接不了）
    4 判定为红，但**没有任何通道送达**（通道故障；与 1 必须分开）
    5 判据绿，但**常量在 30 天内到期且实探接不了班** —— 需人补表
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data"
TABLE = "kline_daily"

#: 容忍度上限。大于 5 个交易日等于「一周不更新也算正常」，那是取消闹钟而不是配置它。
MAX_TOLERABLE_LAG = 5

#: 严格零填充。`date.fromisoformat` 在 3.11 起接受 `20261112`，会让 `date=20261112`
#: 这种目录名通过解析，与 `date=2026-11-12` 落在同一个集合里而看不出区别。
_PART = re.compile(r"^date=(\d{4})-(\d{2})-(\d{2})$")

OK, STALLED, UNJUDGEABLE = "ok", "stalled", "unjudgeable"

#: 通知三态。与判定三态**正交**：判定问「面板停没停」，这里问「话说没说出去」。
SENT, UNSENT, NOCFG = "sent", "failed", "unconfigured"

#: 日历余额告警的退出码。**必须与 1 分开**：说「面板停了」而其实是「日历快过期」，
#: 处置完全不同（一个查管道，一个查常量表）。
EXPIRING = 5


_CAL_CACHE = None
_PROBE_CACHE = None


def _calendar():
    """按路径载入 `analysis/trading_calendar.py`（`analysis/` 不是包）。

    刻意不复写一份 `prev_market_day` / `gap_days`：日历只有一份定义点，
    第二份必然只覆盖第一份的分支（这条在 qq_symbol 的 `bj` 前缀上已经付过代价）。
    """
    global _CAL_CACHE
    if _CAL_CACHE is None:
        spec = importlib.util.spec_from_file_location(
            "trading_calendar", ROOT / "analysis" / "trading_calendar.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CAL_CACHE = mod
    return _CAL_CACHE


def sessions(data_dir: Path | None = None, table: str = TABLE
             ) -> tuple[list[date], list[str]]:
    """`(解析成功的分区日, 名字脏的分区名)`，升序。只列一层，不递归。

    表目录不存在时抛 `FileNotFoundError` —— 与「表存在但零分区」是两件事，
    前者是路径错（`exit 2`），后者是面板停摆（红）。
    """
    base = Path(data_dir or DEFAULT_DATA) / table
    if not base.is_dir():
        raise FileNotFoundError(f"{base} 不存在：`--data-dir` 指错了，或这张表还没建")
    ok: list[date] = []
    dirty: list[str] = []
    for c in base.iterdir():
        if not c.is_dir():
            continue
        m = _PART.match(c.name)
        if m is None:
            #: 不是 `date=` 前缀的目录（如 `.tmp`）直接不看；是前缀但格式坏的要报。
            if c.name.startswith("date="):
                dirty.append(c.name)
            continue
        y, mo, d = (int(x) for x in m.groups())
        try:
            ok.append(date(y, mo, d))
        except ValueError:
            dirty.append(c.name)
    ok.sort()
    dirty.sort()
    return ok, dirty


def _live_sessions() -> tuple[set | None, str]:
    """实探 fuyao 交易日历，返回 `(交易日集合, 说明)`；不可用时集合为 `None`。

    **只做转发**：取法住在 `analysis/calendar_probe.py`，因为
    `analysis/auction_order.py` 的 09:20 下单闸门也要它 —— 取法写两遍的代价见
    那个模块的开篇（`qq_symbol` 少 `bj` 前缀那次）。

    保留函数名而不是让 `main()` 直接调 `probe()`：测试需要**一个**切断点。
    判据测试不该联网，也不该被客户端超时把 0.9s 的套件拖成 21s。

    刻意**不抛**：常量覆盖区间内判据本来就能成立，一次网络抖动不该把可判的判定
    变成崩溃。失败降级为「没有实探」，由 `verdict()` 决定它对退出码的影响。
    """
    return _probe().probe()


def _probe():
    """按路径载入 `analysis/calendar_probe.py`（`analysis/` 不是包）。"""
    global _PROBE_CACHE
    if _PROBE_CACHE is None:
        spec = importlib.util.spec_from_file_location(
            "calendar_probe", ROOT / "analysis" / "calendar_probe.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PROBE_CACHE = mod
    return _PROBE_CACHE


def check(have, today: date, max_lag: int = 0, cal=None, live=None) -> dict:
    """纯函数，不读盘。`have` 是分区日序列，`today` 是判定基准日。

    返回的 `status` 三态：`ok` / `stalled` / `unjudgeable`。**没有第四态**，
    也没有「默认绿」的分支 —— 判不了必须是 `unjudgeable`，由调用方映射成非零。

    `live` 是**原始**实探集合（未体检）：体检与放行只在这里做一次，因为体检
    不合格的原因必须原样带到读数里 —— 若在调用方就降级成 `None`，「上游停更」
    和「压根没配」在输出上会长成同一行，处置却完全不同。
    """
    cal = cal or _calendar()
    lo, hi = cal.COVERED
    days = sorted(set(have))
    out = {
        "status": UNJUDGEABLE,
        "table": TABLE,
        "today": today.isoformat(),
        "max_lag": max_lag,
        "n_sessions": len(days),
        "max_have": days[-1].isoformat() if days else None,
        "first_have": days[0].isoformat() if days else None,
        "expect": None,
        "missing": [],
        "lag": None,
        "detail": "",
    }
    #: 日历余额与停摆判定**正交**，所以放在所有分支之前写：判不了的那一天
    #: 恰恰最需要知道「还剩几天」，把它放进「绿才写」的分支就等于只报好消息。
    out["calendar"] = cal.expiry(today)
    #: 实探轴同理：它决定的是「常量到期后还判不判得动」，与今天停没停无关。
    #: 「体检 + 对账 + 两者的与」只写在 `cal.usable()` 一处 —— `auction_order` 的
    #: 09:20 闸门调的是同一个函数，否则同一份实探会被一边判「接得上」、一边判
    #: 「判不了」，而人能分辨这两行字的窗口只有一个早晨。
    usable, judged = cal.usable(live, today)
    out["live"] = dict(judged["health"], usable=usable is not None)
    out["reconcile"] = judged["reconcile"]

    if not days:
        out.update(status=STALLED, lag=0,
                   detail="一个 date= 分区都没有：这不是「落后几天」，是这张表没有数据。"
                          "先确认 --data-dir 没指错，再查 pipeline 是否从未成功跑过。")
        return out

    #: 可判区间右端：常量右端，与实探右端取大。只有 `usable` 才参与放宽。
    span = cal.live_span(usable)
    right = max(hi, span[1]) if span else hi
    left = min(lo, span[0]) if span else lo

    #: 覆盖边界：两端都要查。左端缺失时 `gap_days` 会算出一个巨大的 lag，
    #: 那个数不是「停摆了多少天」，是「日历不知道这一年」。不许把它当读数报出去。
    if today > right or days[-1] < left:
        out["detail"] = (f"内置交易日历只覆盖 {lo}~{hi}"
                         f"{f'，实探覆盖 {span[0]}~{span[1]}' if span else ''}，"
                         f"today={today} / max(have)={days[-1]} 落在区间外 ⇒ "
                         f"「落后几个交易日」判不了。补 analysis/trading_calendar.py，"
                         f"不要在此处放行。")
        return out

    expect = cal.prev_market_day(today, usable)
    out["expect"] = expect.isoformat()

    #: `max_have >= expect` 时不必再问 `gap_days`：右端已到前一格或更新。
    #: 注意 `max_have > today`（回放 `--today` 时必然出现）**不是**错误：
    #: 上界「有没有多余分区」归 calendar 等式的职责，这里只管下界。
    if days[-1] >= expect:
        out.update(status=OK, lag=0, missing=[],
                   detail=f"右端 {days[-1]} ≥ 应有 {expect}：面板没有停。")
        return out

    missing = cal.gap_days(days[-1], today, usable)
    if missing is None:
        out["detail"] = (f"gap_days 拒绝了 {days[-1]}→{today}（超覆盖区间）⇒ 判不了。")
        return out
    miss = [d.isoformat() for d in missing]
    lag = len(miss)
    out.update(status=STALLED if lag > max_lag else OK, lag=lag, missing=miss)

    if lag > max_lag:
        out["detail"] = (f"右端停在 {days[-1]}，而 {today} 的前一交易日是 {expect}；"
                         f"{lag} 个交易日没有新分区：{', '.join(miss)}"
                         f"（上限 {max_lag}）。")
    else:
        #: 绿但落后 —— 这一支必须留下 `lag`，否则「放过」与「没落后」在输出上同形。
        out["detail"] = (f"右端停在 {days[-1]}，落后 {lag} 个交易日（{', '.join(miss)}），"
                         f"未超过上限 {max_lag}。")
    return out


def _carries(rep: dict) -> bool:
    """实探日历能否在常量到期后**接替判定**。

    读的是 `check()` 存下的 `live.usable`，不在这里重新算「体检通过 且 零分歧」：
    那个合取的定义点是 `trading_calendar.usable()`，而本函数与 `auction_order`
    的 09:20 闸门都是它的消费者。在这里再写一遍，就是给「接得上」造第二个定义 ——
    两边对同一份实探给出不同结论时，人只会相信手机上那条推送。
    """
    lv = rep.get("live") or {}
    return bool(lv.get("usable"))


def verdict(rep: dict, dirty=()) -> int:
    """把三态映射成退出码。`dirty` 非空一律红：面板里有解析不了的分区名。

    优先级刻意是 **停摆/判不了 先于 日历将失明**：`5` 只在判据本身为绿时出现，
    于是 `5` 的含义唯一 —— 「面板没停，该补的是日历」。反过来让 `5` 盖过 `1`，
    一次真实停摆会被读成「只是日历快过期」，而两者的处置完全不同。

    `5` 现在收紧成「常量撑不住 **且** 实探接不了班」。只报常量余额是**假警报**：
    2026-11-12 实测实探已能覆盖到常量右端之后，常量到期并不等于闹钟失明。
    放开这个条件，`5` 会从 2026-12-01 起每天响一次，而那一天面板其实照常可判 ——
    永久噪音的结局是被关掉，于是它恰好在最该工作的那天停止工作。
    """
    if dirty:
        return 1
    if rep["status"] == OK:
        if (rep.get("calendar") or {}).get("state") == "warn" and not _carries(rep):
            return EXPIRING
        return 0
    if rep["status"] == STALLED:
        return 1
    return 3


def _live_line(rep: dict) -> str:
    """实探轴自解释：读的人只看这一行就要知道「常量到期后还能不能判」。"""
    lv = rep.get("live")
    if lv is None:
        return "实探日历       —（判据未带回实探状态）"
    if not lv.get("n"):
        return f"实探日历       ✗ 不可用 —— {lv['reason']}（常量到期后无处接替）"
    rc = rep.get("reconcile") or {}
    dis = rc.get("disagree") or []
    head = (f"实探日历       {'✓' if lv['ok'] else '✗'} {lv['n']} 个交易日"
            f"（{lv['left']} … {lv['right']}）：{lv['reason'] or '整年窗口、右端贴合今天'}")
    if rc.get("overlap"):
        a, b = rc["overlap"]
        if dis:
            head += (f"\n               ✗ 与常量在 {a}…{b} 分歧 {len(dis)} 天："
                     f"{', '.join(dis[:5])}{' …' if len(dis) > 5 else ''}"
                     f" ⇒ 至少一边错，实探不能接替")
        else:
            head += f"\n               · 与常量在 {a}…{b} 逐日一致（{rc['n_overlap']} 天，0 分歧）"
    return head


def _calendar_line(rep: dict) -> str:
    """`warn`/`blind` 必须自解释：读的人只看这一行就要知道该改哪个文件。"""
    c = rep.get("calendar")
    if not c:
        return "日历有效期     —（判据未带回 expiry）"
    carries = _carries(rep)
    if c["state"] == "blind":
        if carries:
            return (f"日历有效期     ! 已越过 {c['hi']} {abs(c['days_left'])} 天："
                    f"常量已失效，判定改由实探承担 ⇒ 常量该补了，但今天的结论仍成立")
        return (f"日历有效期     ✗ 已越过 {c['hi']} {abs(c['days_left'])} 天："
                f"gap_days 已无法作答 ⇒ 补 analysis/trading_calendar.py 的 "
                f"_HOLIDAY_ISO 与 COVERED")
    tick = "!" if c["state"] == "warn" else "·"
    if c["state"] == "warn":
        tail = ("↓ 实探已接替：到期后判定不受影响，但常量该补了" if carries else
                "↓ 30 天内失明且实探不能接替，此后每天 exit 3 ⇒ 补 analysis/trading_calendar.py")
    else:
        tail = ""
    return (f"日历有效期     {tick} 右端 {c['hi']}：剩 {c['days_left']} 自然日 / "
            f"{c['sessions_left']} 个交易日 {tail}").rstrip()


def render(rep: dict, dirty=()) -> str:
    mark = {0: "✓", 1: "✗", 3: "?", EXPIRING: "!"}[verdict(rep, dirty)]
    n = rep["n_sessions"]
    span = (f"{rep['first_have']} … {rep['max_have']}" if n else "空")
    lag = "—" if rep["lag"] is None else f"{rep['lag']}"
    lines = [
        f"面板停摆读数   表={rep['table']}   判定基准日={rep['today']}",
        f"分区           {n} 个（{span}）",
        f"应有           {rep['expect'] or '—'}（prev_market_day({rep['today']})）",
        _calendar_line(rep),
        _live_line(rep),
        f"落后           {lag} 个交易日（上限 {rep['max_lag']}）",
        f"判定           {mark} {rep['status']} —— {rep['detail']}",
    ]
    if dirty:
        lines.append(f"分区名脏        {len(dirty)} 个：{', '.join(dirty[:5])}"
                     f"{' …' if len(dirty) > 5 else ''} ⇒ 红（名字坏了就不能当分区用）")
    if rep.get("notify"):
        n = rep["notify"]
        lines.append(f"通知           {n['status']} —— {n['detail']}")
    return "\n".join(lines)


def _backend_modules():
    """按路径载入后端的 `preferences` / `webhook_adapter`，失败返回 `None`。

    刻意不在这里重写发送：飞书签名、企微按字节截断、自定义通道的 HMAC 头部都已在
    `webhook_adapter` 里有一份定义和测试。抄第二份的话，改一边不改另一边 ——
    `qq_symbol` 少一个 `bj` 前缀那次已经付过代价。
    """
    root = str(ROOT / "backend")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from app.services import preferences, webhook_adapter  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — 缺依赖/无 app 上下文，都只是「发不出去」
        return None
    return preferences, webhook_adapter


def channels() -> list[tuple[str, str]]:
    """已配置的 `(通道名, url)`，按推送优先级。凭证与设置页同一份 `preferences.json`。

    空列表是**配置事实**，不是异常：调用方必须把它映射成 `2`（没配）而不是静默。
    """
    mods = _backend_modules()
    if mods is None:
        return []
    prefs, _ = mods
    out: list[tuple[str, str]] = []
    for name, getter in (("wecom", prefs.get_wecom_webhook_url),
                         ("feishu", prefs.get_feishu_webhook_url),
                         ("custom", prefs.get_custom_webhook_url)):
        try:
            url = (getter() or "").strip()
        except Exception:  # noqa: BLE001
            url = ""
        if url:
            out.append((name, url))
    return out


def _compose(rep: dict, test: bool = False) -> tuple[str, str]:
    """`(标题, 正文)`。收件人只看这一条就要知道哪张表、缺哪几天、怎么复核。"""
    if test:
        return ("[TickFlow] 停摆闹钟 · 通道测试",
                "这是一条**通道连通性测试**，不代表面板停摆。\n\n"
                "看到这条说明：闹钟红了的时候，这条消息会送到这里。")
    lag = rep["lag"]
    miss = rep["missing"]
    shown = ", ".join(miss[:10]) + (" …" if len(miss) > 10 else "")
    cal = rep.get("calendar") or {}
    cal_note = ([] if cal.get("state") != "warn" else
                [f"- 另：日历右端 `{cal['hi']}` 只剩 {cal['sessions_left']} 个交易日，"
                 f"过期后本闹钟每天都会答「判不了」"])
    body = "\n".join([
        f"**{rep['table']} 右端停了**",
        f"- 末格：`{rep['max_have']}`",
        f"- 应有：`{rep['expect']}`（{rep['today']} 的前一交易日）",
        f"- 落后：**{lag}** 个交易日（上限 {rep['max_lag']}）",
        f"- 缺：{shown}",
        f"- 分区：{rep['n_sessions']} 个（{rep['first_have']} … {rep['max_have']}）",
        *cal_note,
        "",
        "复核：`backend/.venv/Scripts/python.exe scripts/panel_stall_alarm.py`",
    ])
    return f"[TickFlow] 面板停摆：{rep['table']} 落后 {lag} 个交易日", body


def deliver(rep: dict, test: bool = False) -> dict:
    """把判定送到人。返回 `{status, channel, detail}`。

    `failed` 与 `unconfigured` 必须由调用方映射成**两个不同**的退出码：
    前者是通道坏了，后者是你还没配。合成一个数，cron 就分不出病因。
    """
    chans = channels()
    if not chans:
        return {"status": NOCFG, "channel": None,
                "detail": "没有任何通知通道已配置（设置页 → 飞书 / 企业微信 / 自定义 Webhook）"}
    mods = _backend_modules()
    if mods is None:
        return {"status": UNSENT, "channel": None, "detail": "backend 不可导入（httpx/依赖缺失）"}
    prefs, hook = mods
    title, body = _compose(rep, test)

    errors: list[str] = []
    for name, url in chans:
        try:
            if name == "wecom":
                ok = hook.send_wecom_markdown(url, title, body)
            elif name == "feishu":
                ok = hook.send_feishu(url, title, body, prefs.get_feishu_webhook_secret())
            else:
                ok = hook.send_custom(url, title, body, "panel_stall", {
                    "table": rep["table"], "max_have": rep["max_have"],
                    "expect": rep["expect"], "lag": rep["lag"], "missing": rep["missing"],
                })
        except Exception as e:  # noqa: BLE001 — 一个通道炸了不能挡住下一个
            errors.append(f"{name}: {e}")
            continue
        if ok:
            return {"status": SENT, "channel": name, "detail": f"已推送到 {name}"}
        errors.append(f"{name}: 适配器返回 False")
    return {"status": UNSENT, "channel": None, "detail": "；".join(errors)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="面板停摆闹钟：kline_daily 右端是否还在增长")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA))
    ap.add_argument("--today", default=None,
                    help="判定基准日（ISO）。默认本机今天；回放历史时显式传入")
    ap.add_argument("--max-lag", type=int, default=0,
                    help=f"容忍的滞后交易日数，默认 0，上限 {MAX_TOLERABLE_LAG}")
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--live", dest="live", action="store_true", default=True,
                    help="实探 fuyao 交易日历：用外部权威审计常量，并接替常量到期后的判定（默认开）")
    ap.add_argument("--no-live", dest="live", action="store_false",
                    help="不联网：只用内置常量，退出码 5 回到「常量余额」口径")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--notify", dest="notify", action="store_true", default=True,
                    help="红了就推送到已配置的 Webhook（默认开）")
    ap.add_argument("--no-notify", dest="notify", action="store_false",
                    help="只判不发：CI 或纯判据场景")
    ap.add_argument("--notify-test", action="store_true", help="只测通道：发一条测试消息，不跑判据")
    args = ap.parse_args(argv)

    if args.notify_test:
        got = deliver({}, test=True)
        print(f"通知通道 {got['status']} —— {got['detail']}")
        return {SENT: 0, NOCFG: 2, UNSENT: 4}[got["status"]]

    if not 0 <= args.max_lag <= MAX_TOLERABLE_LAG:
        print(f"--max-lag 必须在 0~{MAX_TOLERABLE_LAG}：超过一周的容忍度等于取消这个闹钟",
              file=sys.stderr)
        return 2

    today = date.fromisoformat(args.today) if args.today else date.today()
    try:
        have, dirty = sessions(Path(args.data_dir), args.table)
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    live = None
    if args.live:
        real_today = date.today()
        if today > real_today:
            #: 实探窗口的右端是**真实今天**。拿它去判一个未来日期，`right_gap` 会
            #: 是一年的量级，`live_health` 会答「上游停更」—— 而停更的是回放，
            #: 不是上游。错误病因比没有病因更贵，所以这里直接不查。
            print(f"回放未来日期 {today}（本机今天 {real_today}）：实探窗口以真实今天为锚，"
                  f"不与回放混用 ⇒ 本次仅用内置常量判定。", file=sys.stderr)
        else:
            live, note = _live_sessions()
            if live is None:
                #: 失败只在读数里出现，不改退出码 —— 常量覆盖内判据仍然成立，
                #: 而常量到期时 `verdict()` 自己会用 `_carries()` 把后果算进去。
                print(f"实探日历不可用（{note}）—— 本次仅用内置常量判定。", file=sys.stderr)

    rep = check(have, today, args.max_lag, live=live)
    rep["table"] = args.table
    rep["dirty"] = dirty
    code = verdict(rep, dirty)

    #: 只在**红**的时候推送：绿的闹钟不出声。红而没送到必须与「红且送到」分开，
    #: 否则 cron 只看退出码时，「面板停了」和「闹钟哑了」同形。
    if code == 1 and args.notify:
        rep["notify"] = deliver(rep)
        if rep["notify"]["status"] == NOCFG:
            code = 2
        elif rep["notify"]["status"] == UNSENT:
            code = 4

    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep, dirty))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
