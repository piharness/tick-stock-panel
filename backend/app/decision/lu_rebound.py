"""涨停次日砸盘反转（LU-3） — 决策账本里的第二条生产腿。

## 它赌的是什么

    T   日：换手板（收盘涨停，且开盘不在涨停价上）
    T+1 日：日内 open → close ≤ −5%（被砸）
    T+1 收盘后决策 → T+2 开盘买 → T+2 收盘卖（不留隔夜）

收益来自「砸盘的人必须在 T+1 卖完，而买盘第二天才回来」这一段时点错配。
它**不是**形态识别：形态（高开、封板强度、量比）在上一轮已经证伪（`docs/
early-entry-ignition.md`），这里赚的是执行约束的钱，不是预测的钱。

## 为什么它适合进账本（而不是继续留在 analysis/）

账本关心三件事：**能不能被证伪**、**能不能被结算**、**会不会自己变好**。
这条腿三件都满足，而且比 L1 更干净：

| 研究口径 | 账本落点 | 为什么是它 |
|---|---|---|
| `as_of` = T+1 收盘 | 卡的 `as_of` | 决策时点所有信息已定稿 |
| T+2 开盘买 | `fill_mode="open_only"` | 只在集合竞价成交，不许盘中补票 |
| T+2 收盘卖 | `horizon_sessions=0` | 入场日收盘出场，天然不留隔夜 |
| 上限价 = 涨停价下一分 | `limit_price` | 「除涨停开盘外一律成交」，把买不到的那批排除掉 |

`horizon=0` 还有一个账本层面的红利：相邻决策日的收益**不重叠**
（`_overlap_lag = max(horizon−1, 0) = 0`），所以 `n_eff` 约等于决策日数。
L1 用 20 日持有时要 400 个决策日才攒到 30 个等效样本，这条腿 30 天就到——
它是账本里第一条能在两个月内给出判决的策略。

## 口径（与研究侧的唯一定义点对齐）

研究侧唯一产地是 `docs/hotmoney-quant-game.md` 的第 3 节，实现见
`analysis/_diag_game_shift3.py`。本模块照搬它的每一步，不重新发明：

1. **剔除 ST**（按 `instruments.name` 当前名称）与**上市不足 90 个自然日**的次新；
2. 涨停 = `raw_close ≥ 交易所涨停价 − 0.005`；涨停价由 `app.price_limits`
   按板块/日期规则给出，不复权价计算（复权价会把涨跌停约束抹掉）；
3. **一字板排除**：`raw_open ≥ 涨停价 − 0.005` 的涨停不算「换手板」——
   开盘就封死的票买不到，把它算进信号等于把不可成交的样本记进收益；
4. **前收盘 = 窗口内该股最后一个交易日的 raw_close**：停牌跨日时这正是交易所
   用的涨跌幅基准价（`shift(1).over(symbol)` 在本模块的 21 日窗口内即此语义）；
5. 跌幅用 `close/open − 1`（复权，同一天内与 raw 比值恒等）。

实盘口径与研究口径**没有前视差**：`candidates()` 只用 `as_of` 及以前的数据，
入场价在 `as_of` 时点未知——它由结算用真实 bar 决定。这一点与 L1 不同
（L1 研究侧要求 `fwd` 有限，实盘拿不到），所以这里不需要登记「乐观量」。

## 三条不能省的判据

- **截断不是 alpha 决策**：实测 2018–2026 全部 14,990 个事件，按成交额或按跌幅
  截断到 20/40/60/100 只，日超额恒为 **+0.84%**（四位小数不变）。所以 `TOP_N`
  只是容量选择，`study()` 会把这条不变量算出来；改它不会改变结论。
- **`limit_price` 是「涨停价 − 0.01」，不是「收盘价 × 1.02」**：前者让成交判定
  恰好等于「开盘没封涨停」这个可成交条件；后者是一个未经检验的形态过滤。
- **报告里的数只能由 `study()` 产出**：任何写进文档的超额、t 值都要能一键重算
  （`python -m app.decision.lu_rebound --study`），否则它就是一段传说。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import polars as pl

from app.decision import bars
from app.price_limits import polars_limit_price, polars_price_limit_pct
from app.tickflow.partitions import iter_partition_dirs, locate_partition_dir, parse_partition_range

#: 策略身份。账本按它分组 p_win 与结算，不同口径的胜率不可混算。
STRATEGY_ID = "lu_t1_crash_rebound"

#: 入场日收盘出场。**不是 1**：1 表示多隔一夜，实测那一夜是负的
#: （`docs/hotmoney-quant-game.md`：「T+1 收盘买 → T+2 开盘卖」恒负）。
HORIZON_SESSIONS = 0

#: 只在集合竞价成交。盘中跌到限价再买进来的是另一批标的。
FILL_MODE = "open_only"

#: T+1 日内跌幅阈值。研究侧唯一取值，不对外暴露成参数（改它就是换策略）。
DROP_THRESHOLD = 0.05

#: 单日最多挂多少只。研究侧 p99 = 38、最大 128，取 40 覆盖 99% 的交易日；
#: 实测截断到 20/40/60/100 日超额不变，所以它是容量约束，不是信号参数。
TOP_N = 40

#: 次新剔除窗口（自然日）。研究侧同值：上市前 90 天内的涨停多是「无涨跌幅
#: 窗口」或连续一字，与策略假设无关。
MIN_LISTED_DAYS = 90

#: 生产窗口（交易日）。只需要 T+1 与 T 两行，多读 21 行是为了让停牌股的
#: 「前收盘」取到窗口内最后一个交易日——那是交易所的涨跌幅基准价。
_WINDOW = 21

#: 回放分块（交易日）。40 × 5,500 × 8 个矩阵约 14MB，避免整段历史一次性网格化。
_CHUNK = 40

_NEED_COLS = ("symbol", "date", "open", "close", "raw_close", "amount")


class LuReboundError(ValueError):
    """口径不成立（as_of 不是收盘数据 / 窗口不足）。API 层映射 400/409。"""


# ---------------------------------------------------------------------------
# 交易日与分区
# ---------------------------------------------------------------------------

_SESSION_LOCK = threading.Lock()
_SESSION_CACHE: dict[tuple[str, int], list[str]] = {}


def sessions(data_dir: Path) -> list[str]:
    """enriched 已落盘的交易日（ISO，升序）。缓存键 = (目录, 父目录 mtime)。

    与 `slate._session_dates` 同策略：新增一个 `date=` 子目录会更新父目录
    mtime，键变即失效，不靠手工 `clear()`——那种缓存迟早有一处忘了清，
    而这里的失效由文件系统保证。
    """
    base = Path(data_dir) / bars.STOCK_ENRICHED
    if not base.is_dir():
        return []
    try:
        stamp = base.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(base.resolve()), stamp)
    with _SESSION_LOCK:
        hit = _SESSION_CACHE.get(key)
    if hit is not None:
        return hit
    out: list[str] = []
    for part in iter_partition_dirs(base):
        rng = parse_partition_range(part.name)
        if rng is not None:
            out.append(rng[0].isoformat())
    out.sort()
    with _SESSION_LOCK:
        _SESSION_CACHE.clear()
        _SESSION_CACHE[key] = out
    return out


def _partition_files(data_dir: Path, days: Iterable[str]) -> list[Path]:
    base = Path(data_dir) / bars.STOCK_ENRICHED
    files: list[Path] = []
    for day in days:
        part = locate_partition_dir(base, bars.STOCK_ENRICHED, date.fromisoformat(day))
        if not part.is_dir():
            continue
        files.extend(sorted(part.glob("*.parquet")))
    return files


def _read(data_dir: Path, days: Sequence[str]) -> pl.DataFrame:
    files = _partition_files(data_dir, days)
    if not files:
        raise LuReboundError(f"{bars.STOCK_ENRICHED} 在 {days[0]}~{days[-1]} 没有分区")
    df = pl.read_parquet([str(p) for p in files], columns=list(_NEED_COLS))
    if df.is_empty():
        raise LuReboundError("日线为空")
    return df.with_columns(pl.col("date").cast(pl.Utf8))


def _instruments(data_dir: Path) -> pl.DataFrame:
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.is_file():
        raise LuReboundError(f"缺少维表 {path}：无法剔除 ST 与次新")
    cols = ["symbol", "name", "listing_date"]
    have = pl.read_parquet_schema(path)
    keep = [c for c in cols if c in have]
    return pl.read_parquet(path, columns=keep)


# ---------------------------------------------------------------------------
# 派生列：一个定义点，生产与回放共用
# ---------------------------------------------------------------------------


def _derive(df: pl.DataFrame, inst: pl.DataFrame) -> pl.DataFrame:
    """把日线变成「带涨停标记的壳」。生产（`candidates`）与回放（`study`）共用。

    `prev` 是**该股在本次读取窗口内的上一个交易日收盘**。窗口取得比需要的
    多（21 天）就是为了让停牌跨日的票取到停牌前那天——那是交易所算涨跌幅用的
    基准价，不是「前天」。
    """
    df = df.join(inst, on="symbol", how="left")
    df = df.filter(~pl.col("name").fill_null("").str.to_uppercase().str.contains("ST", literal=True))
    if "listing_date" in df.columns:
        df = df.filter(
            pl.col("listing_date").is_null()
            | ((pl.col("date").str.to_date() - pl.col("listing_date").str.to_date()).dt.total_days()
               >= MIN_LISTED_DAYS)
        )
    # 日线没有 raw_open 列：开盘价必须用当日复权比例还原。
    # 直接拿 `open` 当原始价会在除权日把「一字板」判成「换手板」（2010–2015
    # 复权错位时实测一字占比从 9~19% 掉到 0.3%）。
    df = df.with_columns((pl.col("open") * pl.col("raw_close") / pl.col("close")).alias("raw_open"))
    df = df.with_columns(pl.col("raw_close").shift(1).over("symbol").alias("prev_raw"))
    df = df.with_columns(
        # `date` 在 `_read()` 里统一成 Utf8（与日历的 ISO 串比较），而涨跌幅规则
        # 要按日期比较（创业板 2020-08-24 改革）——这里只把这一列转回日期型。
        polars_price_limit_pct(pl.col("symbol"), pl.col("date").str.to_date(), pl.lit(False))
        .alias("limit_pct")
    )
    df = df.with_columns(polars_limit_price(pl.col("prev_raw"), pl.col("limit_pct"), up=True).alias("limit_up"))
    df = df.filter(pl.col("prev_raw").is_not_null() & (pl.col("prev_raw") > 0))
    return df.with_columns(
        (pl.col("raw_close") >= pl.col("limit_up") - 0.005).alias("lu"),
        (pl.col("raw_open") >= pl.col("limit_up") - 0.005).alias("yizi"),
        (pl.col("close") / pl.col("open") - 1.0).alias("intraday"),
    )


def _limit_up_of(prev_raw: float, pct: float) -> float:
    """交易所涨停价（分位四舍五入）。**走 `price_limits` 的实现，不另写一遍公式。**

    另写一份的代价是可测的：`round(prev×(1+pct), 2)` 是银行家舍入 + 二进制误差，
    在 3.045 这类落在半分上的价格会与交易所差一分，而那 diff 恰好在「能不能
    买到」的边界上。
    """
    return float(pl.select(polars_limit_price(pl.lit(float(prev_raw)), pl.lit(float(pct)), up=True)).item())


def buy_cap(prev_raw: float, pct: float) -> float:
    """买入上限价 = **涨停价的下一分**。

    它不是「愿意付的最高价」这种主观参数，而是一个**恒等变换**：A 股报价都在
    0.01 的格点上，所以

        开盘价 ≤ 涨停价 − 0.01  ⟺  开盘价 < 涨停价  ⟺  开盘没封死（买得到）

    `settle._fill(mode="open_only")` 判的是「开盘 ≤ 限价」，两者合起来正好等于
    「除涨停开盘外一律按开盘价成交」。用涨停价本身做限价会把一字板算成成交
    （`open == limit` 时 `≤` 成立），那是不可成交的样本。
    """
    return round(_limit_up_of(prev_raw, pct) - 0.01, 2)


# ---------------------------------------------------------------------------
# 生产口径
# ---------------------------------------------------------------------------


def candidates(
    data_dir: Path,
    as_of: str,
    *,
    top_n: int = TOP_N,
    drop_threshold: float = DROP_THRESHOLD,
) -> dict[str, Any]:
    """`as_of`（= T+1）收盘后，为 T+2 集合竞价给出的挂单清单。

    **只用 `as_of` 及以前的数据**：T+2 的开盘价此刻未知，由 `settle._fill` 判定。

    返回结构对齐 `slate.build_slate`：`status` 回答「算出来了吗」，`rows` 是
    「照着挂的单」，`caliber` 是「这份清单的口径」，`warnings` 是「什么情况下
    不该信它」。刻意不复用 `slate` 模块本身——那份名单的 `top_n`、状态机与
    L1 的 45 日面板绑在一起，塞第二条腿会把两条口径焊死在一个文件里。
    """
    days = sessions(data_dir)
    if not days:
        return _terminal("skipped_no_data", as_of, reason="enriched 没有任何分区")
    if as_of not in set(days):
        return _terminal(
            "skipped_no_data", as_of,
            reason=f"{as_of} 不是已落盘的交易日（enriched 无该分区）：不按 0 只处理",
        )
    i = days.index(as_of)
    if i < 1:
        return _terminal("skipped_no_data", as_of, reason="窗口不足：as_of 之前没有交易日")

    # 收盘判据。盘中快照的价格结构完好（行数、字段、唯一性都正常），
    # 只有 quote_ts 能分开；用它计算「日内跌 5%」会把半天的行情当成一整天的跌幅。
    from app.decision import settle as settle_mod

    state, why = settle_mod.entry_session_state(Path(data_dir), as_of)
    if state != "closed":
        return _terminal(
            "skipped_not_closed", as_of,
            reason=f"as_of={as_of} 不是收盘数据（{why}）：盘中跌幅不是日内跌幅",
        )

    window = days[max(0, i - _WINDOW) : i + 1]
    entry = days[i + 1] if i + 1 < len(days) else None
    df = _derive(_read(data_dir, window), _instruments(data_dir))

    t_day = days[i - 1] if i >= 1 else None
    if t_day is None:
        return _terminal("skipped_no_data", as_of, reason="as_of 之前没有交易日（T 缺失）")

    t = df.filter(pl.col("date") == t_day).select(
        ["symbol", "lu", "yizi", "raw_close", "prev_raw", "limit_up", "limit_pct"]
    ).rename({"raw_close": "t_raw_close", "limit_up": "t_limit_up"})
    t1 = df.filter(pl.col("date") == as_of).select(
        ["symbol", "raw_close", "amount", "intraday"]
    ).rename({"raw_close": "t1_raw_close", "amount": "t1_amount", "intraday": "t1_intraday"})

    sig = (
        t.join(t1, on="symbol", how="inner")
        .filter(pl.col("lu") & ~pl.col("yizi"))
        .filter(pl.col("t1_intraday") <= -(drop_threshold) + 1e-12)
    )
    if sig.is_empty():
        return _terminal("empty", as_of, entry=entry, reason=f"{as_of} 没有被砸的换手板")

    names = _instruments(data_dir)
    if "name" in names.columns:
        sig = sig.join(names.select(["symbol", "name"]), on="symbol", how="left")

    rows: list[dict[str, Any]] = []
    for r in sig.iter_rows(named=True):
        pct = float(r.get("limit_pct") or 0.1)
        cap = buy_cap(float(r["t1_raw_close"]), pct)
        if cap <= 0:
            continue
        rows.append({
            "symbol": str(r["symbol"]),
            "name": r.get("name"),
            #: 集合竞价挂单价：开盘 ≤ 它才成交（= 开盘没封涨停）。
            "limit_price": cap,
            "limit_up_price": round(_limit_up_of(float(r["t1_raw_close"]), pct), 2),
            "prev_close_raw": round(float(r["t1_raw_close"]), 2),
            "drop_pct": round(float(r["t1_intraday"]) * 100, 3),
            "lu_date": t_day,
            "limit_pct": round(pct, 4),
            "day_amount_yi": None if r.get("t1_amount") is None
            else round(float(r["t1_amount"]) / 1e8, 3),
        })

    # 截断规则：成交额降序（容量优先），symbol 升序固定并列。
    # 实测截断对日超额无影响（见 `study()` 的 `truncation_invariance`），
    # 所以这里选的是「最容易成交的那批」，不是「最容易涨的那批」。
    rows.sort(key=lambda x: (-(x["day_amount_yi"] or 0.0), x["symbol"]))
    truncated = max(0, len(rows) - top_n)
    rows = rows[:top_n]
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    warnings = _warnings(days, as_of, entry, rows, truncated)
    return {
        "as_of": as_of,
        "entry_date": entry,
        "strategy_id": STRATEGY_ID,
        "status": "ready" if rows else "empty",
        "drop_threshold": drop_threshold,
        "top_n": top_n,
        "n_rows": len(rows),
        "truncated": truncated,
        "rows": rows,
        "caliber": caliber(),
        "warnings": warnings,
    }


def _warnings(
    days: Sequence[str], as_of: str, entry: str | None,
    rows: Sequence[dict[str, Any]], truncated: int,
) -> list[str]:
    w: list[str] = []
    latest = days[-1]
    if as_of < latest:
        w.append(
            f"as_of={as_of} 不是最新分区（最新 {latest}）：这是回看的名单，不是明天的挂单"
        )
    if entry is None:
        w.append("次日分区尚未落盘：T+2 入场只能等下一个交易日")
    if truncated:
        w.append(
            f"当日 {len(rows) + truncated} 只命中，按成交额截断到 {len(rows)} 只"
            "（截断对日超额无影响，见 study 的 truncation_invariance）"
        )
    thin = [r["symbol"] for r in rows if (r.get("day_amount_yi") or 0) < 0.5]
    if thin:
        w.append(f"{len(thin)} 只当日成交额不足 0.5 亿（{', '.join(thin[:5])}）：滑点会吃掉收益")
    return w


def fingerprint(cand: dict[str, Any]) -> str:
    """清单的**内容指纹**：只认会改变成交的东西（as_of + 标的序列 + 限价）。

    它进卡的 `forecast`，也就进承诺哈希。所以不能含 `entry_date` / `warnings` /
    `status`——这三样会随时间漂移（次日分区落盘、数据回填），而「重算一次」是
    本模块要支持的日常动作：周五盘后建的卡，周一重跑不该变成第二张卡。
    反过来，限价差一分或成员换一只就是另一份承诺，必须换 id。
    """
    payload = {
        "as_of": str(cand.get("as_of") or ""),
        "strategy_id": STRATEGY_ID,
        "rows": [
            [str(r.get("symbol")), f"{float(r.get('limit_price')):.2f}"]
            for r in (cand.get("rows") or [])
            if r.get("symbol") and r.get("limit_price") is not None
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def caliber() -> dict[str, Any]:
    """这份清单的口径。随卡封存（进内容哈希），事后不可改。"""
    return {
        "signal": "T 日换手板（收盘涨停且开盘不在涨停价）且 T+1 日内 open→close ≤ −5%",
        "as_of": "T+1 收盘后（决策时点所有信息已定稿，无前视）",
        "entry": "T+2 集合竞价，开盘 ≤ limit_price 才成交（open_only）",
        "exit": "T+2 收盘（horizon_sessions=0，不留隔夜）",
        "limit_price_rule": "涨停价 − 0.01：让成交判定恰好等于『开盘没封涨停』",
        "price_basis": "涨停/跌幅用不复权价与复权价各自的本义；挂单价用不复权价（settle 撮合口径）",
        "filters": [
            "剔除名称含 ST 的标的",
            f"剔除上市不足 {MIN_LISTED_DAYS} 个自然日的次新",
            "剔除一字板（开盘即封涨停）——买不到，不能算信号",
        ],
        "truncation": {"top_n": TOP_N, "rule": "成交额降序，symbol 升序固定并列",
                       "why": "实测截断到 20/40/60/100 日超额不变，故它是容量约束"},
        "source": "app.decision.lu_rebound（研究侧 docs/hotmoney-quant-game.md 第 3 节）",
    }


def _terminal(kind: str, as_of: str, *, reason: str, entry: str | None = None) -> dict[str, Any]:
    """没算出清单时的结构。**照样返回**——「今天为什么没有」必须留痕。"""
    return {
        "as_of": as_of,
        "entry_date": entry,
        "strategy_id": STRATEGY_ID,
        "status": kind,
        "drop_threshold": DROP_THRESHOLD,
        "top_n": TOP_N,
        "n_rows": 0,
        "truncated": 0,
        "rows": [],
        "caliber": caliber(),
        "warnings": [reason],
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 回放：同一条口径跑全历史，用的还是 `_derive`
# ---------------------------------------------------------------------------


def study(
    data_dir: Path,
    *,
    start: str = "2018-01-01",
    end: str | None = None,
    top_n: int = TOP_N,
) -> dict[str, Any]:
    """事件研究的唯一产地。`--study` 打印的就是它。

    与 `candidates()` 共用 `_derive()` 与 `buy_cap()`：口径只有一份实现，
    文档里的数必须能由这条路径重算出来（否则它就是传说）。

    回放按 40 个交易日分块网格化：`entry` 必须是**日历相邻**的下一交易日，
    而 `shift(-1).over(symbol)` 数行不数天——停牌股的下一个「行」可能是两周后，
    会把累计跌幅记成单日收益（本仓已栽过一次）。网格里 `i+1` 是日历上的下一日，
    停牌即 NaN，天然不成交。
    """
    days = sessions(data_dir)
    if not days:
        raise LuReboundError("enriched 没有任何分区")
    end = end or days[-1]
    want = [d for d in days if start <= d <= end]
    if not want:
        raise LuReboundError(f"{start}~{end} 没有交易日")
    inst = _instruments(data_dir)
    calendar = days

    records: list[dict[str, Any]] = []
    #: 入场日 → (全市场均值, 中位数)。**跨块累积**，所以在这里持有并在最后
    #: 一次性交给 `_summarize()`——不放到模块级，避免两次调用互相污染。
    market: dict[str, tuple[float, float]] = {}
    for k in range(0, len(want), _CHUNK):
        chunk = want[k : k + _CHUNK]
        chunk_set = set(chunk)
        lo = calendar.index(chunk[0])
        hi = min(len(calendar) - 1, calendar.index(chunk[-1]) + 1)
        window = calendar[max(0, lo - _WINDOW) : hi + 1]
        df = _derive(_read(data_dir, window), inst)

        dates = sorted(df["date"].unique().to_list())
        syms = sorted(df["symbol"].unique().to_list())
        n, m = len(dates), len(syms)
        if n < 3:
            continue
        d_ix = {d: i for i, d in enumerate(dates)}
        s_ix = {s: i for i, s in enumerate(syms)}
        di = np.fromiter((d_ix[d] for d in df["date"].to_list()), np.int64, df.height)
        si = np.fromiter((s_ix[s] for s in df["symbol"].to_list()), np.int64, df.height)

        def grid(col: str) -> np.ndarray:
            a = np.full((n, m), np.nan)
            a[di, si] = df[col].cast(pl.Float64).fill_null(float("nan")).to_numpy()
            return a

        intraday, raw_open, raw_close = grid("intraday"), grid("raw_open"), grid("raw_close")
        limit_up, amount = grid("limit_up"), grid("amount")
        lu, yizi = grid("lu") > 0.5, grid("yizi") > 0.5

        # 每日全市场基准：入场日开盘买、收盘卖的中位数与均值。
        # 与 `settle._median_ret(entry_price="open")` 同口径（同一天 open→close）。
        for i in range(n):
            row = intraday[i]
            ok = np.isfinite(row)
            if ok.sum() < 100:
                continue
            day = dates[i]
            market.setdefault(day, (float(np.nanmean(row[ok])), float(np.nanmedian(row[ok]))))

        for i in range(1, n - 1):
            day = dates[i]
            if day not in chunk_set:
                continue
            signal = (lu[i - 1] & ~yizi[i - 1]) & np.isfinite(intraday[i]) \
                & (intraday[i] <= -(DROP_THRESHOLD) + 1e-12)
            entry_day = dates[i + 1]
            if entry_day not in market:
                continue
            for j in np.where(signal)[0]:
                o, cap, c = raw_open[i + 1, j], limit_up[i + 1, j], raw_close[i + 1, j]
                if not (np.isfinite(o) and np.isfinite(cap) and np.isfinite(c)):
                    continue
                filled = float(o) <= float(cap) - 0.01 + 1e-9
                records.append({
                    "as_of": day,
                    "entry": entry_day,
                    "symbol": syms[j],
                    "drop": float(intraday[i, j]),
                    "filled": filled,
                    "ret": (float(c) / float(o) - 1.0) if filled else None,
                    "amount": float(amount[i, j]) if np.isfinite(amount[i, j]) else 0.0,
                })

    ev = pl.DataFrame(records) if records else pl.DataFrame(
        schema={"as_of": pl.Utf8, "entry": pl.Utf8, "symbol": pl.Utf8, "drop": pl.Float64,
                "filled": pl.Boolean, "ret": pl.Float64, "amount": pl.Float64}
    )
    return _summarize(ev, market=market, start=start, end=end, top_n=top_n)


def _summarize(ev: pl.DataFrame, *, market: dict[str, tuple[float, float]],
               start: str, end: str, top_n: int) -> dict[str, Any]:
    filled = ev.filter(pl.col("filled"))
    if filled.is_empty():
        return {"start": start, "end": end, "n_signal": ev.height, "n_filled": 0,
                "reason": "没有可成交的样本"}

    mean_map = {k: v[0] for k, v in market.items()}
    med_map = {k: v[1] for k, v in market.items()}

    def attach(frame: pl.DataFrame) -> pl.DataFrame:
        if frame.is_empty():
            return frame
        return frame.with_columns(
            pl.col("entry").replace_strict(mean_map, default=None).alias("mkt_mean"),
            pl.col("entry").replace_strict(med_map, default=None).alias("mkt_med"),
        ).with_columns(
            (pl.col("ret") - pl.col("mkt_mean")).alias("ex_mean"),
            (pl.col("ret") - pl.col("mkt_med")).alias("ex_med"),
        )

    def daily(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.group_by("entry").agg(
            pl.col("ex_mean").mean().alias("x"),
            pl.col("ex_med").mean().alias("xm"),
            pl.col("ret").mean().alias("r"),
            pl.len().alias("n"),
        ).sort("entry")

    def head(frame: pl.DataFrame) -> dict[str, Any]:
        g = daily(attach(frame))
        x = g["x"]
        return {
            "n": frame.height,
            "days": g.height,
            "ret_mean": round(float(frame["ret"].mean()), 6),
            "win_rate": round(float((frame["ret"] > 0).mean()), 4),
            "ex_mean_daily": round(float(x.mean()), 6),
            "ex_mean_t": round(float(x.mean() / (x.std() / np.sqrt(g.height))), 2),
            "ex_med_daily": round(float(g["xm"].mean()), 6),
        }

    base = head(filled)
    # 中位数口径的命中率 = 收益跑赢当日全市场中位数的占比（与 `settle` 的
    # `outcome_med` 同义）。它不是「涨没涨」，所以与 `win_rate` 是两个数：
    # 下跌市里前者会明显高于后者。
    with_mkt = attach(filled).filter(pl.col("mkt_med").is_not_null())
    base["hit_median"] = round(float((with_mkt["ret"] > with_mkt["mkt_med"]).mean()), 4)

    years: dict[str, Any] = {}
    yr = attach(filled).with_columns(pl.col("entry").str.slice(0, 4).alias("yr"))
    # `partition_by(as_dict=True)` 的键是元组（多列分组时才是多元素），
    # 不要用 `group_by()` 迭代——那个版本差异会让 `t[0]` 有时是标量、有时是 dict。
    for key, sub in sorted(yr.partition_by("yr", as_dict=True).items()):
        g = daily(sub)
        x = g["x"]
        years[str(key[0])] = {
            "n": sub.height,
            "days": g.height,
            "ex_mean_daily": round(float(x.mean()), 6),
            "t": round(float(x.mean() / (x.std() / np.sqrt(g.height))), 2) if g.height > 1 else None,
            "win_rate": round(float((sub["ret"] > 0).mean()), 4),
        }

    # 截断不变量：按成交额取当日前 k 只，日超额是否变化。
    inv: dict[str, Any] = {}
    for k in (20, 40, 60, 100):
        cut = filled.with_columns(
            pl.col("amount").rank("ordinal", descending=True).over("entry").alias("rk")
        ).filter(pl.col("rk") <= k)
        g = daily(attach(cut))
        inv[str(k)] = {"n": cut.height, "ex_mean_daily": round(float(g["x"].mean()), 6)}

    # 单日上限：极端日一天能有 128 个信号，TOP_N 截断是否改变结论。
    per_day = filled.group_by("entry").len()["len"]
    return {
        "start": start,
        "end": end,
        "n_signal": ev.height,
        "n_filled": filled.height,
        "fill_rate": round(filled.height / max(1, ev.height), 4),
        "top_n": top_n,
        "all": base,
        "years": years,
        "truncation_invariance": inv,
        "per_day": {
            "mean": round(float(per_day.mean()), 2),
            "p99": float(per_day.quantile(0.99)),
            "max": int(per_day.max()),
        },
    }


# ---------------------------------------------------------------------------
# 命令行：口径重算（只读）
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`python -m app.decision.lu_rebound --study|--candidates`

    `--study` 是**只读**的口径重算：文档里的每一个数都必须能由它重现。
    写账本不在这里——发卡走盘后管道或 `POST /api/decision/produce`，
    账本只接受「决策在结果之前被冻结」的记录，回放结果不冒充预登记。
    """
    ap = argparse.ArgumentParser(prog="python -m app.decision.lu_rebound",
                                 description="涨停次日砸盘反转：口径重算与次日清单")
    ap.add_argument("--data", default=None, help="数据目录（默认 $TSP_DATA_DIR 或 ./data）")
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--study", action="store_true", help="全历史事件研究（只读）")
    act.add_argument("--candidates", action="store_true", help="某日的次日挂单清单")
    ap.add_argument("--as-of", default=None, help="--candidates 的决策日（默认最新分区）")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args(argv)

    data_dir = Path(args.data or Path.cwd() / "data")
    if args.candidates:
        days = sessions(data_dir)
        if not days:
            print("没有 enriched 分区", file=sys.stderr)
            return 2
        out = candidates(data_dir, args.as_of or days[-1])
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if out["status"] in ("ready", "empty") else 3

    out = study(data_dir, start=args.start, end=args.end)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
