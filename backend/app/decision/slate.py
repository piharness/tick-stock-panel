"""每日名单（daily slate） — 「今天买哪几只」的唯一产地。

## 为什么需要它

在它之前，「给出股票名单」这件事在仓库里有 5 条互不知情的链路：
`Screener`（策略 score）、`Decision`（决策卡）、`L1 候选`（mom20）、
`Battle`（龙虎榜净买）、`Dashboard`（涨跌幅）。同一批标的在 5 处按 5 种方式
排序，截断常量有 7 套（引擎 100/50 → 卡 20 → L1 20 → 助手 30 → 龙虎榜 12 →
看板 8/5/5 → 前端再切一次）。于是「今天买什么」没有唯一答案，也没有任何
一处会因为「两份名单不相等」而变红。

本模块把「每日名单」收成一个**盘后一次成型的快照**：

    build_slate()  唯一产点：按策略口径算一次，截断只在这里发生
    write_slate()  唯一落盘：data/user_data/daily_slate/slate-<as_of>.json
    read_slate()   唯一消费：API 与前端只读磁盘，不再重算
    annotate_staleness()  新鲜度在**读取时刻**重算，不信文件里的旧结论

## 两条不能省的纪律

**一、status 与 stale 正交，不许合并。**
`status` 回答「名单算出来了吗」（ready / empty / skipped_not_closed / …），
`stale` 回答「它是不是最新的」。合并成一个字段，就会出现「名单有效但落后
三个交易日」被写成 `ready` 的场面——2026-11-15 实测：enriched 分区已到
11-13，最新决策卡 `as_of` 仍是 11-11，页面照常显示「已封存 10 张卡」，
没有任何一处报警。

**二、新鲜度必须在读取时刻算。**
文件里的 `stale_at_build` 只是「写它的时候旧不旧」。今天打开昨天的文件，
它一定旧了；把 `stale_at_build=False` 直接返回，等于让陈旧永远沉默。
`annotate_staleness()` 每次重新数 `as_of` 之后还落了几个交易日。

## 单份文件不是「每日名单」

「每日」是**序列**的属性，不是单份文件的属性。只写 `<as_of>.json` 的话，
每天都是「今天有没有文件」，而「上周三算过吗」没有任何地方能回答：
管道停一天就永久缺一天，页面仍显示最近一份名单，看起来一切正常。

所以本模块同时提供：

    series()    窗口内逐日点名（ready / empty / 终端 / 缺失），缺哪天一列可见
    backfill()  把窗口内缺失或未成立的日子补算一遍（幂等，已成立的不动）

`--coverage` 与 `--backfill` 是这两个函数的命令行入口，
见 `python -m app.decision.slate --help`。判据是「窗口内（除最新一日）
不许有洞」——最新一日可能正处在盘后数据未定稿的窗口里，那不是缺陷。

## 截断是策略的一部分，不是显示参数

L1 的 `top_n=20` 是研究口径（`analysis/_diag_l1_vwap_curvature.py` 同值），
把它做成可配置的「显示条数」等于换了一个策略。所以 `build_slate()` 对
L1 只接受 `top_n == L1_TOP_N`，其它值直接 `invalid_top_n`，不降级、不夹紧。
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.decision import bars, l1_cohort
from app.tickflow.partitions import iter_partition_dirs, parse_partition_range

logger = logging.getLogger(__name__)

#: 落盘结构的版本。任何改变行语义（排序键/截断/价格口径）的升级都必须递增，
#: 读取端遇到旧版本视为不可用——不能拿旧结构的 rows 当新口径的名单。
SLATE_SCHEMA_REVISION = 1

SLATE_DIRNAME = "daily_slate"

#: `status` 的取值。前两个是「名单出来了」，后四个是「为什么没出来」。
STATUS_READY = "ready"
STATUS_EMPTY = "empty"
STATUS_SKIPPED_NOT_CLOSED = "skipped_not_closed"
STATUS_SKIPPED_NO_DATA = "skipped_no_data"
STATUS_INVALID_TOP_N = "invalid_top_n"
STATUS_FAILED = "failed"

#: 「名单成立」的状态。`empty` 也算——它和「没算」必须分开，否则
#: 「今天全市场都不满足」会被读成「管道没跑」。
READY_STATES = (STATUS_READY, STATUS_EMPTY)

#: 谁写的这份名单。补算与管道产出的 `rows` 应当逐位相同（同一个 `as_of`、
#: 同一份收盘数据），区别只在 `built_at`。读的时候必须能分开：
#: 「盘后自动跑出来的」和「事后补的」是两种证据，混作一份就是把补救写成常态。
ORIGIN_PIPELINE = "pipeline"
ORIGIN_BACKFILL = "backfill"


class SlateError(ValueError):
    """名单自身非法（口径被改、结构版本不认）。调用方映射 400/409。"""


def slate_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / SLATE_DIRNAME


def slate_path(data_dir: Path, as_of: str) -> Path:
    return slate_dir(data_dir) / f"slate-{as_of}.json"


# ---------------------------------------------------------------------------
# 新鲜度
# ---------------------------------------------------------------------------


#: 分区日期列表的进程内缓存。键 = (目录, 父目录 mtime)。
#:
#: 为什么需要：`iter_partition_dirs()` 必须**列完** 600 个目录（scandir 本身
#: 在 D 盘实测 0.2s），不是「扫几个付几个」。而 `/api/decision/slate` 每次
#: 请求都要判新鲜度，0.2s 相当于把 overview 刚优化掉的 0.45s 又还回去一半。
#:
#: 为什么能自失效：新增一个 `date=` 子目录会更新父目录 mtime，键变 ⇒ 重列。
#: **不靠手工调用 clear()**——那种缓存迟早会有一处忘了清，而这里的失效
#: 由文件系统保证。只留最新一份，与 `l1_cohort._CACHE` 同策略。
_DIRS_LOCK = threading.Lock()
_DIRS_CACHE: dict[tuple[str, int], list[str]] = {}


def _session_dates(data_dir: Path) -> list[str]:
    """enriched 的全部交易日（升序）。缓存键带父目录 mtime。"""
    base = Path(data_dir) / bars.STOCK_ENRICHED
    if not base.is_dir():
        return []
    try:
        stamp = base.stat().st_mtime_ns
    except OSError:
        stamp = 0.0
    key = (str(base.resolve()), stamp)
    with _DIRS_LOCK:
        hit = _DIRS_CACHE.get(key)
    if hit is not None:
        return hit
    dates: list[str] = []
    for part in iter_partition_dirs(base):
        rng = parse_partition_range(part.name)
        if rng is not None:
            dates.append(rng[0].isoformat())
    dates.sort()
    with _DIRS_LOCK:
        _DIRS_CACHE.clear()
        _DIRS_CACHE[key] = dates
    return dates


def _freshness(data_dir: Path, as_of: str) -> tuple[str, int]:
    """返回 `(expected_as_of, lag_sessions)`。

    `lag_sessions` 是 `as_of` **之后**已落盘的交易日个数。用分区目录名算，
    不读数据列：`kline_daily_enriched` 是日分区，目录名即日期。

    这个「目录名即日期」的假设只对**日分区**成立。`kline_daily` 那种月分区
    （`date=2026-11`）在字符串序上排在 `date=2026-11-13` 之前，不能套用——
    所以本函数只服务 enriched，参数里也不接表名。

    不用 `latest_session()` 单独判「是否有更新」再另算天数：两个函数各扫一遍
    目录，且它们的口径迟早会分叉。这里一次扫描同时给出两个答案。
    """
    return _freshness_of(_session_dates(data_dir), as_of)


def _freshness_of(dates: list[str], as_of: str) -> tuple[str, int]:
    """`_freshness` 的纯函数内核。调用方已有日期列表时不必再列一次目录。

    `build_slate()` 需要同时回答「这个 as_of 是不是交易日」和「它还落后几天」，
    两问共用同一次列举——否则同一个窗口会在两处各解析一遍，而它们分叉时
    没有任何东西会红。
    """
    i = bisect.bisect_right(dates, as_of)
    later = dates[i:]
    if not later:
        return as_of, 0
    return later[-1], len(later)


def annotate_staleness(data_dir: Path, slate: dict[str, Any]) -> dict[str, Any]:
    """在**读取时刻**重算新鲜度，返回副本（不修改入参）。

    这是本模块唯一的「时间判据」，API 与测试都走它：文件里的
    `stale_at_build` 只记录写盘那一刻，不能当结论返回。
    """
    out = dict(slate)
    as_of = str(slate.get("as_of") or "")
    expected, lag = _freshness(Path(data_dir), as_of) if as_of else (as_of, 0)
    out["expected_as_of"] = expected
    out["lag_sessions"] = lag
    out["stale"] = lag > 0
    return out


# ---------------------------------------------------------------------------
# 生产
# ---------------------------------------------------------------------------


def build_slate(
    data_dir: Path,
    as_of: str | None = None,
    *,
    strategy_id: str = l1_cohort.L1_STRATEGY_ID,
    top_n: int = l1_cohort.L1_TOP_N,
    origin: str = ORIGIN_PIPELINE,
) -> dict[str, Any]:
    """算一份名单。**不写盘**——写盘是 `write_slate()` 的事。

    `as_of=None` 时取 enriched 的最新分区，而不是「今天」：管道在 15:35 跑、
    enriched 可能还停在昨天，取今天会得到 `skipped_no_data`；取最新分区则
    产出一份带 `stale=True` 的名单——「名单旧了」比「没有名单」更可诊断。
    """
    from app.decision import settle as settle_mod

    data_dir = Path(data_dir)
    dates = _session_dates(data_dir)
    if as_of is None:
        if not dates:
            return _terminal(
                STATUS_SKIPPED_NO_DATA, strategy_id=strategy_id, top_n=top_n,
                origin=origin, reason="enriched 没有任何分区：先跑盘后管道同步日K",
            )
        as_of = dates[-1]
    as_of = str(as_of)

    # `as_of` 必须是**已落盘的交易日**，不能只是「一个日期」。
    # 少了这一条，周六的名单会被算成 `empty`（候选为空）——而「当天没有交易」
    # 和「当天 0 只满足条件」是两件事。盘后管道的 `today` 在节假日正是这种日期，
    # 于是每天都会多出一份声称「今天 0 只」的名单，序列从此不可读。
    if as_of not in dates:
        return _terminal(
            STATUS_SKIPPED_NO_DATA, as_of=as_of, strategy_id=strategy_id,
            top_n=top_n, origin=origin,
            reason=f"{as_of} 不是已落盘的交易日（enriched 无该分区）：不按 0 只处理",
        )

    # 口径校验先于计算：参数错了不该白跑 45 天面板。
    if strategy_id == l1_cohort.L1_STRATEGY_ID and top_n != l1_cohort.L1_TOP_N:
        return _terminal(
            STATUS_INVALID_TOP_N, as_of=as_of, strategy_id=strategy_id, top_n=top_n,
            origin=origin,
            reason=(
                f"L1 的 top_n 是策略口径（研究侧同值 {l1_cohort.L1_TOP_N}），"
                f"不接受 {top_n}：改它等于换策略，不是改显示条数"
            ),
        )

    state, why = settle_mod.entry_session_state(data_dir, as_of)
    if state != "closed":
        return _terminal(
            STATUS_SKIPPED_NOT_CLOSED, as_of=as_of, strategy_id=strategy_id,
            top_n=top_n, origin=origin, reason=f"as_of={as_of} 不是收盘数据（{why}）",
        )

    try:
        cand = l1_cohort.candidates(data_dir, as_of, top_n=top_n)
    except (ValueError, FileNotFoundError) as exc:
        return _terminal(
            STATUS_FAILED, as_of=as_of, strategy_id=strategy_id, top_n=top_n,
            origin=origin, reason=f"{type(exc).__name__}: {exc}",
        )

    rows = list(cand.get("candidates") or [])
    warnings = list(cand.get("warnings") or [])
    if not rows and cand.get("reason"):
        warnings.append(f"候选为空：{cand['reason']}")
    if rows and len(rows) < top_n:
        warnings.append(
            f"排名取 {top_n} 只，实际可挂单 {len(rows)} 只"
            "（缺 raw_close 或限价 ≤ 0 的已在候选层丢弃）"
        )

    expected, lag = _freshness_of(dates, as_of)
    return {
        "schema_revision": SLATE_SCHEMA_REVISION,
        "as_of": as_of,
        #: 下一交易日（**已落盘**的那一个）；`None` = 次日分区还没落盘。
        #: 来自 l1_cohort 的日历判定，不是 panel 的日期序列（窄窗下 dates[-1]
        #: 恒等于 as_of）。周五/节前最后一天必然是 `None`——不是日历缺失，
        #: 本仓库没有任何数据源知道未来的交易日，页面文案不许写成「日历未落盘」。
        "entry_date": cand.get("entry_date"),
        "strategy_id": strategy_id,
        "status": STATUS_READY if rows else STATUS_EMPTY,
        "top_n": top_n,
        "n_rows": len(rows),
        #: 截断的唯一定义点。读者不需要知道引擎 limit、卡 top_n、助手 30 行
        #: 这些历史常量——名单只有这一个条数。
        "truncation": {
            "top_n": top_n,
            "rule": "mom20 = close[T]/close[T-20]-1，按日截面 rank(ordinal, desc) <= top_n",
            "source": "app.decision.l1_cohort.L1_TOP_N",
        },
        "rows": rows,
        "caliber": cand.get("caliber") or {},
        "panel_since": cand.get("panel_since"),
        "warnings": warnings,
        # 写盘时刻的事实；读取时由 `annotate_staleness()` 重新判定。
        "stale_at_build": lag > 0,
        "lag_sessions_at_build": lag,
        "expected_as_of_at_build": expected,
        "origin": origin,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _terminal(kind: str, *, reason: str, as_of: str | None = None,
              strategy_id: str = l1_cohort.L1_STRATEGY_ID,
              top_n: int = l1_cohort.L1_TOP_N,
              origin: str = ORIGIN_PIPELINE) -> dict[str, Any]:
    """没算出名单时的结构。**仍然写盘**——「今天为什么没有名单」必须留痕，
    否则第二天只看到一个空目录，分不清「没跑」和「跑了但数据没就绪」。"""
    return {
        "schema_revision": SLATE_SCHEMA_REVISION,
        "as_of": as_of,
        "entry_date": None,
        "strategy_id": strategy_id,
        "status": kind,
        "top_n": top_n,
        "n_rows": 0,
        "truncation": {"top_n": top_n, "rule": None, "source": None},
        "rows": [],
        "caliber": {},
        "panel_since": None,
        "warnings": [reason],
        "stale_at_build": None,
        "lag_sessions_at_build": None,
        "expected_as_of_at_build": None,
        "origin": origin,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": reason,
    }


def write_slate(data_dir: Path, slate: dict[str, Any]) -> Path | None:
    """原子落盘。返回路径；`as_of` 缺失（连日期都没有）时不写，返回 `None`。

    同 `as_of` 重复构建是幂等的：`built_at` 会更新，但 `first_built_at`
    保留第一次的时间——后者回答「这份名单最早什么时候就有了」，被覆盖掉
    等于把「管道停了三天」改写成「今天刚跑过」。
    """
    as_of = slate.get("as_of")
    if not as_of:
        logger.warning("slate: as_of 缺失，不落盘（status=%s）", slate.get("status"))
        return None
    path = slate_path(Path(data_dir), str(as_of))
    path.parent.mkdir(parents=True, exist_ok=True)

    previous = read_slate(Path(data_dir), str(as_of))
    first = (previous or {}).get("first_built_at") or slate.get("built_at")
    payload = dict(slate)
    payload["first_built_at"] = first

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def read_slate(data_dir: Path, as_of: str | None = None) -> dict[str, Any] | None:
    """读名单。`as_of=None` 取目录里日期最大的一份。

    文件名 `slate-YYYY-MM-DD.json` 的字典序即时间序，所以取 `sorted()[-1]`
    就够——不解析每份 JSON 的 `as_of` 再比较，那要把每一份都读进内存。
    """
    directory = slate_dir(Path(data_dir))
    if as_of is not None:
        path = directory / f"slate-{as_of}.json"
        if not path.is_file():
            return None
        return _load(path)
    if not directory.is_dir():
        return None
    files = sorted(p for p in directory.glob("slate-*.json") if p.is_file())
    if not files:
        return None
    return _load(files[-1])


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("slate: 读取失败 %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    rev = data.get("schema_revision")
    if rev != SLATE_SCHEMA_REVISION:
        # 旧结构不能当新口径用：`rows` 的字段集可能已经变了。
        logger.warning("slate: %s 的结构版本 %s != %s，视为不可用",
                       path, rev, SLATE_SCHEMA_REVISION)
        return None
    return data


def picks_from_slate(slate: dict[str, Any]) -> list[dict[str, Any]]:
    """名单 → 决策卡的 picks。限价就是名单里的 `limit_price`。

    这是「名单」与「承诺」之间唯一的桥：卡不再自己跑一次 `candidates()`，
    否则同一 `as_of` 会有两份名单，而两份相等的证明只能靠对拍测试——构造性
    相等优于对拍相等。
    """
    return [
        {"symbol": str(row["symbol"]), "limit_price": float(row["limit_price"])}
        for row in (slate.get("rows") or [])
        if row.get("symbol") and row.get("limit_price") is not None
    ]


def fingerprint(slate: dict[str, Any]) -> str:
    """名单的**内容指纹**：只认会改变成交的东西（symbol 序列 + 限价 + 口径）。

    它进卡的 `forecast`，也就进承诺哈希。所以不能用 `built_at`：
    同一批单重算一次会得到另一个 `built_at` ⇒ 另一个 `card_id` ⇒ 同一天两张卡，
    同一份证据被记两遍。而「重算」正是本模块要支持的日常动作（补跑、补算、
    名单写盘后重进卡），所以卡的身份必须只看内容。

    反过来，内容变了（限价差一分、成员换一只）就必须换 id——那是另一份承诺，
    不是同一份的修订版。
    """
    rows = [
        [str(row.get("symbol")), f"{float(row.get('limit_price')):.2f}"]
        for row in (slate.get("rows") or [])
        if row.get("symbol") and row.get("limit_price") is not None
    ]
    payload = {
        "as_of": str(slate.get("as_of") or ""),
        "strategy_id": slate.get("strategy_id"),
        "top_n": slate.get("top_n"),
        "rows": rows,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 序列：一份文件不是「每日名单」
# ---------------------------------------------------------------------------


def series(data_dir: Path, *, lookback: int = 20) -> dict[str, Any]:
    """窗口内逐日点名。回答「哪天有名单、哪天没有、哪天没成立」。

    **分母是 enriched 的交易日，不是日历日**：没有数据的日期在这里不存在，
    所以「缺一份名单」只可能是「那天开盘了但没算」，不可能是「那天休市」。
    这个分母与 `_freshness` 共用 `_session_dates()`，两处不会分叉。

    缺口的三种形态必须分开，它们下一步动作不同：

    - `missing`：**根本没有文件**。管道没跑，或跑了但写盘失败。
    - `unreadable`：文件在但读不出（结构版本不认/JSON 损坏）。旧口径的名单，
      不能当新口径用，也不能当「没算」——它需要重新生成。
    - `not_ready`：文件在、读得出，但 `status` 不是 ready/empty。
      原因在文件里（`reason`），是可诊断的。

    返回里带 `latest_ready`（窗口内最新一份**成立**名单的完整内容）：
    「今天该挂哪几张单」指的是它，不是目录里最新的那个文件。收在本函数里是
    为了避免二次遍历——调用方要同时知道序列和可执行的那一份。
    """
    data_dir = Path(data_dir)
    dates = _session_dates(data_dir)[-max(1, int(lookback)):]
    days: list[dict[str, Any]] = []
    latest_ready: dict[str, Any] | None = None
    for day in dates:
        path = slate_path(data_dir, day)
        present = path.is_file()
        payload = _load(path) if present else None
        status = None if payload is None else payload.get("status")
        days.append({
            "as_of": day,
            "present": present,
            "status": status,
            "n_rows": None if payload is None else payload.get("n_rows"),
            "built_at": None if payload is None else payload.get("built_at"),
            "origin": None if payload is None else payload.get("origin"),
            "reason": None if payload is None else payload.get("reason"),
        })
        if status in READY_STATES:
            latest_ready = payload

    missing = [d["as_of"] for d in days if not d["present"]]
    unreadable = [d["as_of"] for d in days if d["present"] and d["status"] is None]
    not_ready = [d["as_of"] for d in days
                 if d["present"] and d["status"] is not None
                 and d["status"] not in READY_STATES]
    return {
        "lookback": lookback,
        "sessions": len(days),
        "ready": sum(1 for d in days if d["status"] == STATUS_READY),
        "empty": sum(1 for d in days if d["status"] == STATUS_EMPTY),
        "missing": missing,
        "unreadable": unreadable,
        "not_ready": not_ready,
        #: 窗口内「没有可执行名单」的日期。顺序即时间序，页面直接照着标红。
        "holes": sorted(missing + unreadable + not_ready),
        "head": days[-1] if days else None,
        "days": days,
        "latest_ready": latest_ready,
    }


def backfill(
    data_dir: Path,
    *,
    lookback: int = 10,
    force: bool = False,
    origin: str = ORIGIN_BACKFILL,
) -> dict[str, Any]:
    """把窗口内**缺失或未成立**的日子补算一遍。

    幂等：`status ∈ READY_STATES` 且文件可读的一律跳过（`force=True` 才重算）。
    所以它可以每天多跑几次，也可以随时手工补——这正是「盘后自动生成」在
    管道漏跑、供应商迟交、容器重启之后仍然能收敛的方式。

    补算不改历史：`first_built_at` 由 `write_slate()` 从旧文件继承，
    `origin=backfill` 记下「这份是补的」。`rows` 由同一个 `as_of` 的收盘数据
    决定，因此与当天自动产出应当逐位相同——不相同就是数据被回填过，见
    `analysis/_verify_slate_vs_cards.py`。
    """
    data_dir = Path(data_dir)
    dates = _session_dates(data_dir)[-max(1, int(lookback)):]
    built: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed: list[dict[str, Any]] = []
    for day in dates:
        previous = read_slate(data_dir, day)
        if previous and previous.get("status") in READY_STATES and not force:
            skipped.append(day)
            continue
        payload = build_slate(data_dir, day, origin=origin)
        path = write_slate(data_dir, payload)
        if payload.get("status") in READY_STATES and path is not None:
            built.append({"as_of": day, "status": payload["status"],
                          "n_rows": payload.get("n_rows"),
                          "changed": (previous or {}).get("status") != payload["status"]})
        else:
            failed.append({"as_of": day, "status": payload.get("status"),
                           "reason": payload.get("reason")})
    return {
        "window": [dates[0], dates[-1]] if dates else [],
        "sessions": len(dates),
        "built": built,
        "skipped": skipped,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# 命令行：盘后核对与补算
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`python -m app.decision.slate --coverage|--backfill`

    退出码是判据，不是装饰：

    - 0：窗口内没有洞（`--backfill` 则：补算后没有失败项）
    - 2：没有任何分区可比对——判据没执行过，按失败处理
    - 3：有洞未补 / 补算后仍有洞

    **最新一日不计入洞。** 盘后管道 15:35 跑、数据可能 15:31 才定稿，
    这一天的名单本来就可能还没成立；把它算成洞会让这个判据在每个交易日
    下午恒红一次。最新一日的状态在输出里单独打印，由人判断。
    """
    ap = argparse.ArgumentParser(
        prog="python -m app.decision.slate",
        description="每日名单的覆盖核对与补算",
    )
    ap.add_argument("--data", default=None,
                    help="数据目录（默认 $TSP_DATA_DIR 或 ./data）")
    ap.add_argument("--lookback", type=int, default=10,
                    help="回看的交易日数（默认 10）")
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--coverage", action="store_true", help="逐日点名，有洞退出 3")
    act.add_argument("--backfill", action="store_true", help="补算缺失/未成立的日子")
    ap.add_argument("--force", action="store_true", help="连同已成立的一起重算")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    data_dir = Path(args.data or os.environ.get("TSP_DATA_DIR") or Path.cwd() / "data")
    if not (data_dir / bars.STOCK_ENRICHED).is_dir():
        print(f"没有 enriched 分区：{data_dir}", file=sys.stderr)
        return 2

    if args.backfill:
        out = backfill(data_dir, lookback=args.lookback, force=args.force)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for row in out["built"]:
                mark = "补" if row["changed"] else "写"
                print(f"{row['as_of']}  {mark}  {row['status']:<6} {row['n_rows']:>2} 只")
            for row in out["failed"]:
                print(f"{row['as_of']}  未成立  {row['status']}：{row['reason']}")
            print(f"\n窗口 {out['window']}：补 {len(out['built'])} / "
                  f"跳过 {len(out['skipped'])} / 失败 {len(out['failed'])}")
        if out["failed"]:
            return 3
        # 补完立刻复核：补算失败的形状与「本来就有洞」不同，退出码相同。
        view = series(data_dir, lookback=args.lookback)
        return 0 if not _holes_excluding_head(view) else 3

    view = series(data_dir, lookback=args.lookback)
    holes = _holes_excluding_head(view)
    if args.json:
        # `latest_ready` 是完整名单（含 20 行），与「逐日点名」不是一类东西：
        # JSON 输出只给序列，要看某一天的名单请用 `--data` 直接读文件。
        print(json.dumps({**view, "latest_ready": None, "holes_excluding_head": holes},
                         ensure_ascii=False, indent=2))
    else:
        for day in view["days"]:
            if not day["present"]:
                mark = "缺"
            elif day["status"] is None:
                mark = "坏"
            elif day["status"] in READY_STATES:
                mark = "·"
            else:
                mark = "!"
            print(f"{day['as_of']}  {mark}  {str(day['status'] or '—'):<18} "
                  f"{str(day['n_rows'] if day['n_rows'] is not None else '—'):>3}  "
                  f"{day['origin'] or ''}")
        print(f"\n{view['sessions']} 个交易日：就绪 {view['ready']} / 空 {view['empty']} / "
              f"未成立 {len(view['not_ready'])} / 缺失 {len(view['missing'])} / "
              f"不可读 {len(view['unreadable'])}")
        if view["holes"]:
            print(f"洞：{', '.join(view['holes'])}")
    return 3 if holes else 0


def _holes_excluding_head(view: dict[str, Any]) -> list[str]:
    """洞 = 没有可执行名单的日期，**去掉窗口最新一日**。

    最新一日的名单可能还没到点生成（见 `main()` 的说明）。去掉它之后，
    剩下任何一个洞都是「那天开盘了、但没有人算过」——没有正当理由。
    """
    head = (view.get("head") or {}).get("as_of")
    return [d for d in view.get("holes") or [] if d != head]


def summary(slate: dict[str, Any]) -> dict[str, Any]:
    """给管道/API 日志用的瘦身版（不含 rows）。"""
    return {
        "as_of": slate.get("as_of"),
        "entry_date": slate.get("entry_date"),
        "strategy_id": slate.get("strategy_id"),
        "status": slate.get("status"),
        "n_rows": slate.get("n_rows"),
        "stale_at_build": slate.get("stale_at_build"),
        "lag_sessions_at_build": slate.get("lag_sessions_at_build"),
        "origin": slate.get("origin"),
        "reason": slate.get("reason"),
    }


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
