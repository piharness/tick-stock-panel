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

## 截断是策略的一部分，不是显示参数

L1 的 `top_n=20` 是研究口径（`analysis/_diag_l1_vwap_curvature.py` 同值），
把它做成可配置的「显示条数」等于换了一个策略。所以 `build_slate()` 对
L1 只接受 `top_n == L1_TOP_N`，其它值直接 `invalid_top_n`，不降级、不夹紧。
"""
from __future__ import annotations

import bisect
import json
import logging
import os
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
    dates = _session_dates(data_dir)
    if not dates:
        return as_of, 0
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
) -> dict[str, Any]:
    """算一份名单。**不写盘**——写盘是 `write_slate()` 的事。

    `as_of=None` 时取 enriched 的最新分区，而不是「今天」：管道在 15:35 跑、
    enriched 可能还停在昨天，取今天会得到 `skipped_no_data`；取最新分区则
    产出一份带 `stale=True` 的名单——「名单旧了」比「没有名单」更可诊断。
    """
    from app.decision import settle as settle_mod

    data_dir = Path(data_dir)
    if as_of is None:
        as_of = bars.latest_session(data_dir, bars.STOCK_ENRICHED)
        if as_of is None:
            return _terminal(
                STATUS_SKIPPED_NO_DATA, strategy_id=strategy_id, top_n=top_n,
                reason="enriched 没有任何分区：先跑盘后管道同步日K",
            )
    as_of = str(as_of)

    # 口径校验先于计算：参数错了不该白跑 45 天面板。
    if strategy_id == l1_cohort.L1_STRATEGY_ID and top_n != l1_cohort.L1_TOP_N:
        return _terminal(
            STATUS_INVALID_TOP_N, as_of=as_of, strategy_id=strategy_id, top_n=top_n,
            reason=(
                f"L1 的 top_n 是策略口径（研究侧同值 {l1_cohort.L1_TOP_N}），"
                f"不接受 {top_n}：改它等于换策略，不是改显示条数"
            ),
        )

    state, why = settle_mod.entry_session_state(data_dir, as_of)
    if state != "closed":
        return _terminal(
            STATUS_SKIPPED_NOT_CLOSED, as_of=as_of, strategy_id=strategy_id,
            top_n=top_n, reason=f"as_of={as_of} 不是收盘数据（{why}）",
        )

    try:
        cand = l1_cohort.candidates(data_dir, as_of, top_n=top_n)
    except (ValueError, FileNotFoundError) as exc:
        return _terminal(
            STATUS_FAILED, as_of=as_of, strategy_id=strategy_id, top_n=top_n,
            reason=f"{type(exc).__name__}: {exc}",
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

    expected, lag = _freshness(data_dir, as_of)
    return {
        "schema_revision": SLATE_SCHEMA_REVISION,
        "as_of": as_of,
        #: 下一交易日；`None` = 次日分区还没落盘。来自 l1_cohort 的日历判定，
        #: 不是 panel 的日期序列（窄窗下 dates[-1] 恒等于 as_of）。
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
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _terminal(kind: str, *, reason: str, as_of: str | None = None,
              strategy_id: str = l1_cohort.L1_STRATEGY_ID,
              top_n: int = l1_cohort.L1_TOP_N) -> dict[str, Any]:
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
        "reason": slate.get("reason"),
    }
