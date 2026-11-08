"""同步回执：读侧的区间覆盖 + 写侧的一行事实。

回执是 append-only 的 JSONL，每行一次同步请求。它存在的唯一理由是
**区间比水位可靠**：单调水位在停机后恢复时会把中间的空洞一起宣告为已覆盖，
而区间只声明它真正请求过的那一段——空洞是「不在任何区间」的自然结果，
不需要额外的「不推进」分支，也不需要告警逻辑去猜。

覆盖在这里是**查询**，不是存储。append-only 的日志不能改写过行，所以不存在
「把相邻两段合并成一段」这个动作：`covered_through` 在查询时取包含该日的
区间上界，合并是查询的结果而不是它的前提。谁要是把合并结果写回日志，
就等于在日志里放一份和日志本身不一致的第二真相。

`requested_from` / `requested_to` 是**自然日**，不是交易日；覆盖判定必须落在
交易日上（调用方传入 sessions）。周五一段加周一一段，在自然日上是个两天的
洞，在交易日上无缝——把相邻判据写成「日期差一天」，每个周末都会造一个假空洞。

两侧各记各的，不互相指向：

| 谁 | 记什么 | 为什么不能反过来 |
|---|---|---|
| 回执 | `run_id`（自己是谁）+ `store_*`（离开时表是什么样） | 它是指针的**起点**，不指向别人 |
| 账本行 | `ex_table_*` / `window_ex_events`（读到什么） | 账本活得比回执久，指向回执就是指向会消失的东西 |

空洞结构（哪几段、每段多长）**不在回执里**：它需要日历，而回执写侧在同步层，
不该为了一个读数把日历依赖拖进每条同步链；它也能随时重算，冻进只增不改的文件
反而会被后来的读者当成当时的证据。见 `bars.ex_coverage`。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: 回执目录与事件表目录同级，都挂在 data/user_data/ 下：回执是运行事实，
#: 与账本同盘同生命周期，不进版本控制（`data/**` 已挡，见 .gitignore）。
RECEIPT_DIR = "user_data/sync_receipt"

#: 写入格式版本。读侧按字段读、不按版本分支：多一个字段不影响旧读者，
#: 少一个字段按「不覆盖」处理。版本号是给将来的迁移留的锚，不是开关。
SCHEMA_VERSION = 1

#: 只认这两种结局：「跳过」是请求没发生（无权限分支），「失败」是请求发生了
#: 但一条都没回来。两者都不能给任何标的提供覆盖。
COVERING_OUTCOMES = ("ok", "partial")

OUTCOME_OK = "ok"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
#: 请求成功了，但**逐标的完整性无法验证**——自定义源（扶摇等）把整份 dump 的
#: 失败吞成空返回，也用空返回表达「这批标的没有除权」，两者在返回值上同形。
#: 它不在 `COVERING_OUTCOMES` 里：读侧拿不到 failed_symbols 时，唯一诚实的答案是
#: 「我不知道」，而不是「全都没问题」。这条不是保守，是被 provider 的代码决定的：
#: `plugins/fuyao/provider.py:737-739` 捕获 `FuyaoError` 后 `return 空表`。
OUTCOME_UNVERIFIED = "unverified"
OUTCOMES = (OUTCOME_OK, OUTCOME_PARTIAL, OUTCOME_FAILED, OUTCOME_SKIPPED,
            OUTCOME_UNVERIFIED)

#: `scope` 是**调用方声明的意图**，不是从标的数反推的。`symbols` 表示这次只要了
#: 一部分标的，读侧因此不敢给任何一个标的记覆盖——它不知道哪些在清单里。
#: 默认取 `symbols`：声明不足时失守方向是「没有覆盖」，可以补；反过来则是
#: 凭空宣称已同步，补不回来。
SCOPE_UNIVERSE = "universe"
SCOPE_SYMBOLS = "symbols"
SCOPES = (SCOPE_UNIVERSE, SCOPE_SYMBOLS)


def new_run_id(now: datetime | None = None) -> str:
    """本次同步的身份：`20261108T040244Z-1a2b3c4d`。

    全串，不是前缀。回执是唯一能解析这串东西的地方，所以它必须自带时区与
    随机位：前缀形态（只留时间戳）在日志里能搜到、在回执里却拼不回去，
    等于写了一个查不到的指针。
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def build_receipt(*, asset_type: str, provider: str, scope: str,
                  requested: Sequence[str], requested_from: str, requested_to: str,
                  outcome: str, failed_symbols: Iterable[str] = (),
                  store: Mapping[str, Any] | None = None,
                  run_id: str | None = None,
                  recorded_at: str | None = None) -> dict[str, Any]:
    """组装一行回执。**区间是它的语义单位**，没有区间就没有回执。

    `store_*` 是这次同步结束时表的读数（`created/rows/first/last`）。它不进读侧
    的覆盖判据，只答「我离开时表是什么样」：覆盖由 `outcome + failed_symbols`
    决定，读数由 `store_*` 决定，两者不互相推导。
    """
    if scope not in SCOPES:
        raise ValueError(f"scope 必须是 {SCOPES} 之一，收到 {scope!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome 必须是 {OUTCOMES} 之一，收到 {outcome!r}")
    if str(requested_from) > str(requested_to):
        raise ValueError(f"requested_from {requested_from} 晚于 requested_to {requested_to}")
    st = store or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or new_run_id(),
        "recorded_at": recorded_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "provider": provider,
        "asset_type": asset_type,
        "scope": scope,
        "requested_n": len(requested),
        "requested_from": str(requested_from),
        "requested_to": str(requested_to),
        "outcome": outcome,
        "failed_symbols": sorted({str(s) for s in failed_symbols}),
        "store_created": bool(st.get("created")),
        "store_rows": st.get("rows"),
        "store_first": st.get("first"),
        "store_last": st.get("last"),
    }


def append_receipt(data_dir: Path, row: Mapping[str, Any]) -> Path:
    """追加一行。**文件由行自己的 `asset_type` 决定**，不由调用方再传一次参数：
    两个来源各自声明资产类型，迟早会有一处写错，而错的那次恰好是跨市场假覆盖。
    """
    path = receipt_path(Path(data_dir), str(row.get("asset_type") or "stock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(row), ensure_ascii=False, sort_keys=False)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    return path


def receipt_path(data_dir: Path, asset_type: str = "stock") -> Path:
    """回执文件路径。**按资产类型分文件**，不靠行内字段过滤。

    股票回执盖不住 ETF 的窗口，反之亦然。分文件让这条隔离由路径本身保证，
    不依赖读侧记得按 `asset_type` 过滤——漏了就变成跨市场假覆盖。
    """
    name = "adj_factor_etf.jsonl" if asset_type == "etf" else "adj_factor.jsonl"
    return Path(data_dir) / RECEIPT_DIR / name


@dataclass(frozen=True)
class Receipt:
    """一行的判定视图。字段缺失的一律降级成「不覆盖」，不猜。"""

    requested_from: str
    requested_to: str
    scope: str
    outcome: str
    failed_symbols: frozenset[str]

    def covers(self, symbol: str) -> bool:
        """这次请求能否为 `symbol` 在该区间上提供覆盖。"""
        if self.outcome not in COVERING_OUTCOMES:
            return False
        if self.requested_from > self.requested_to:
            return False        # 坏区间不给覆盖；勿让 from > to 变成「全盖」
        if self.scope != "universe":
            # 显式清单（scope=symbols）没有随行保存清单本身时判不了，
            # 按未覆盖处理：判不了的覆盖是 fail-open，而这里要 fail-closed。
            return False
        return symbol not in self.failed_symbols


def load_receipts(data_dir: Path,
                  asset_type: str = "stock") -> tuple[list[Receipt], list[str]]:
    """逐行读回执，返回 (可用记录, 问题清单)。**不抛异常**。

    问题清单带行号：只报「有几处坏」而不报「哪一行」，读者只能把整份文件
    重看一遍。坏行跳过而不是中断——一条坏行不该让整份历史失去可读性；
    但跳过必须留痕，静默跳过正是账本那边已经犯过一次的错。
    """
    path = receipt_path(data_dir, asset_type)
    if not path.is_file():
        return [], []
    rows: list[Receipt] = []
    problems: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append(f"{lineno}: 不是合法 JSON ({e.msg})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"{lineno}: 顶层不是对象")
            continue
        frm, to = obj.get("requested_from"), obj.get("requested_to")
        if not isinstance(frm, str) or not isinstance(to, str):
            problems.append(f"{lineno}: 缺 requested_from/requested_to")
            continue
        failed = obj.get("failed_symbols")
        if failed is None:
            failed = []
        if not isinstance(failed, list):
            problems.append(f"{lineno}: failed_symbols 不是数组")
            continue
        rows.append(Receipt(
            requested_from=frm,
            requested_to=to,
            scope=str(obj.get("scope") or ""),
            outcome=str(obj.get("outcome") or ""),
            failed_symbols=frozenset(str(s) for s in failed),
        ))
    return rows, problems


def coverage_intervals(data_dir: Path, symbol: str,
                       asset_type: str = "stock") -> list[tuple[str, str]]:
    """`symbol` 被请求过的区间，按起点升序。**不合并**——见模块 docstring。"""
    rows, _ = load_receipts(data_dir, asset_type)
    return sorted((r.requested_from, r.requested_to) for r in rows if r.covers(symbol))


def covered_through(data_dir: Path, symbol: str, as_of: str,
                    asset_type: str = "stock") -> str | None:
    """`as_of` 落在哪个请求区间里，就返回该区间的上界；不在任何区间返回 None。

    返回的上界**可以晚于 `as_of`**：一次请求盖到月底，问月中那天也应该答月底
    ——问的是「这天归哪次请求管」，不是「这天之后到哪算完」。

    上界是**请求边界**，不是数据边界：它证明「我问到了这里」，不证明
    「这里的公告都已发生」。所以它只能证明同步跑过，不能证明数据完整。
    """
    hits = [hi for lo, hi in coverage_intervals(data_dir, symbol, asset_type)
            if lo <= as_of <= hi]
    return max(hits) if hits else None


def window_covered(data_dir: Path, symbol: str, entry: str, exit_: str,
                   sessions: Iterable[str],
                   asset_type: str = "stock") -> bool:
    """`(entry, exit]` 内每个交易日都落在某个请求区间内。

    判据是「逐日落在某个区间」，不是「整段在一个区间里」：中间断开再补一次
    也能接上，只要没有交易日漏在外面。窗口内交易日为空时返回 False——
    空集真值是 fail-open 的标准样板，这里不给自己留那个口子。
    """
    iv = coverage_intervals(data_dir, symbol, asset_type)
    days = [d for d in sessions if entry < d <= exit_]
    if not days:
        return False
    return all(any(lo <= d <= hi for lo, hi in iv) for d in days)
