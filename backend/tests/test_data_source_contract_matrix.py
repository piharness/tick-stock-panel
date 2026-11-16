"""数据源契约矩阵 —— 逐类注入「已知会发生的坏数据」，断言判据必须变红。

## 这份文件解决什么

`data_integrity` 现在有 20+ 条判据，但它们**是否覆盖了真实发生过的故障类型**
没有任何证据。历史上的每次事故（重复分区、指数混入个股表、批量超时静默丢弃、
复权系数跨时刻相除……）都是先发生、再补一条判据，补完之后没人验证过
「同类故障再来一次，这条判据会不会响」。

这里把故障类型写成可执行的注入：构造一份**干净**的分区作对照，再逐条注入
一个已知缺陷，调用生产判据 `inspect_daily_partition`，要求它必须返回 issue。

失败 = 该缺陷能静默穿过全部判据 → 就是下一个事故的入口。

## 约定

- 全部在 `tmp_path` 内构造，不读生产数据，不写 `data/`。
- 日期固定 2026-08-19/20（周四/周五），与真实 `cn_today()` 无关：
  「历史日」语义由参数传入，不由系统时钟决定。
- 判据只认磁盘事实，因此注入必须是**真实写到盘上的字节**，不能 monkeypatch
  判据内部函数 —— 否则测的是补丁，不是数据。
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.parquet import DAILY_STORAGE_SCHEMA
from app.services import data_integrity as di

DAY = date(2026, 8, 20)
PREV = date(2026, 8, 19)

SH_SYMS = [f"6000{i:02d}.SH" for i in range(20)]
BJ_SYMS = [f"9200{i:02d}.BJ" for i in range(5)]
ALL_SYMS = [*SH_SYMS, *BJ_SYMS]

INDEX_SYMBOL = "000001.SH"


def _ts(day: date, hh: int, mm: int) -> int:
    return int(datetime.combine(day, time(hh, mm), tzinfo=CN_TZ).timestamp() * 1000)


def _row(
    symbol: str,
    *,
    day: date = DAY,
    close: float = 10.0,
    volume: float = 1_000.0,
    amount: float | None = None,
    quote_ts: int | None = None,
) -> dict:
    amount = close * volume * 100 if amount is None else amount
    return {
        "symbol": symbol,
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "amount": amount,
        "quote_ts": quote_ts,
    }


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=DAILY_STORAGE_SCHEMA, strict=False)


def _write(root: Path, rows: list[dict], *, day: date = DAY, name: str = "part.parquet",
           table: str = "kline_daily") -> Path:
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    out = part / name
    _frame(rows).write_parquet(out)
    return out


def _write_instruments(root: Path, symbols: list[str], *, index: bool = False) -> None:
    table = "instruments_index" if index else "instruments"
    path = root / table / f"{table}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols, "name": symbols, "code": [s[:6] for s in symbols],
                  "exchange": [s[-2:] for s in symbols], "type": ["stock"] * len(symbols)}
                 ).write_parquet(path)


def _write_enriched(root: Path, rows: list[dict], *, day: date = DAY) -> None:
    """未复权价证人: VWAP 恒等式必须与 raw_low/raw_high 对账, 不能与复权价对账。"""
    part = root / "kline_daily_enriched" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r["symbol"] for r in rows],
        "date": [r["date"] for r in rows],
        "raw_high": [r["high"] for r in rows],
        "raw_low": [r["low"] for r in rows],
    }).write_parquet(part / "part.parquet")


def _verdict(root: Path, day: date = DAY) -> tuple[str, str] | None:
    """修补链判据: 只收「重拉能修好」的缺陷。"""
    issue = di.inspect_daily_partition(root, "kline_daily", day, deep=True)
    return None if issue is None else (issue.kind, issue.reason)


def _facts(root: Path, day: date = DAY) -> tuple[str, str] | None:
    """事实审计通道: 恒等式违约, 不驱动自动修补。"""
    issue = di.audit_fact_violations(root, "kline_daily", day)
    return None if issue is None else (issue.kind, issue.reason)


# --------------------------------------------------------------------------- 对照
def test_control_clean_authoritative_day_is_green(tmp_path: Path) -> None:
    """对照：权威历史（quote_ts 全 null、覆盖 20/20）必须判干净。

    这条不许失败 —— 它失败说明矩阵本身构造错了，后面所有红/绿都不可信。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    _write(tmp_path, [_row(s) for s in ALL_SYMS])
    assert _verdict(tmp_path) is None


def test_control_whole_day_midday_snapshot_is_red(tmp_path: Path) -> None:
    """对照：整天都是 11:00 快照 → 现有判据必须判 snapshot（已实现的能力）。"""
    _write_instruments(tmp_path, ALL_SYMS)
    stamp = _ts(DAY, 11, 0)
    _write(tmp_path, [_row(s, quote_ts=stamp) for s in ALL_SYMS])
    verdict = _verdict(tmp_path)
    assert verdict is not None and verdict[0] == "snapshot"


# ------------------------------------------------------------------- 已知静默洞
def test_single_symbol_stuck_at_midday_under_postclose_max_is_red(tmp_path: Path) -> None:
    """1 只股票停在 11:00，其余 24 只在 17:00 —— 必须判 snapshot 并点名该股票。

    `_partition_snapshot_defects` 的入口是 `_is_snapshot(day, max(quote_ts))`：
    只要分区里有**一行**盘后时间戳，symbol 级残留扫描就整体跳过。
    因此在真实数据里（11-05/06/11/12 全分区都是 17:47~21:10 的盘后快照），
    任何一只停在收盘前的股票都不会被发现。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    rows = [_row(s, quote_ts=_ts(DAY, 17, 0)) for s in ALL_SYMS]
    rows[0] = _row(SH_SYMS[0], quote_ts=_ts(DAY, 11, 0))
    _write(tmp_path, rows)
    verdict = _verdict(tmp_path)
    assert verdict is not None and verdict[0] == "snapshot", (
        f"停在 11:00 的 {SH_SYMS[0]} 未被告警: {verdict}"
    )


def test_duplicate_symbol_same_file_is_red(tmp_path: Path) -> None:
    """同一文件里同一个 (date,symbol) 出现两行 → 必须判坏。

    同键重复会让 `unique(subset=['date','symbol'])` 之外的每一次 join 都放大行数；
    面板类计算（收益、排名）会静默拿到两倍权重。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    rows = [_row(s) for s in ALL_SYMS]
    rows.append(_row(SH_SYMS[0], volume=2_000.0))
    _write(tmp_path, rows)
    assert _facts(tmp_path) is not None, "同键重复行未被告警"


def test_duplicate_partition_file_same_symbols_is_red(tmp_path: Path) -> None:
    """分区里多一个 `part-stream-0.parquet`，symbol 与 part.parquet 完全重叠 → 必须判坏。

    这是 2026-11-05 的真实事故形态（staging 残留）。当时靠下游 `(date,symbol)`
    唯一键判据发现；若下游没跑，盘上就是 2 倍行数。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    _write(tmp_path, [_row(s) for s in ALL_SYMS])
    _write(tmp_path, [_row(s, volume=2_000.0) for s in ALL_SYMS], name="part-stream-0.parquet")
    assert _facts(tmp_path) is not None, "两文件同 symbol 未被告警"


def test_index_symbol_inside_stock_table_is_red(tmp_path: Path) -> None:
    """指数行落进 `kline_daily` → 必须判坏。

    2026-11-05..11-12 实证：4 个分区里混进 `000002.SH`(上证A指)/`000688.SH`(科创50)。
    这些行的 close 是点位（3868.85），一旦进入个股排序就是「价格」，
    `mom20` 在 20 个交易日后会把它们排进 top20。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    _write_instruments(tmp_path, [INDEX_SYMBOL], index=True)
    _write(tmp_path, [_row(s) for s in ALL_SYMS] + [_row(INDEX_SYMBOL, close=3868.85)])
    assert _facts(tmp_path) is not None, "指数符号混入个股日线未被告警"


@pytest.mark.xfail(
    strict=True,
    reason="已由 minute_audit S 轴覆盖 (±1 手 / 2bp, 已进盘后管道); "
           "此处不重复实现同一判据",
)
def test_amount_inconsistent_with_volume_is_red(tmp_path: Path) -> None:
    """成交额与成交量×价格差 50% → 必须判坏。

    `amount` 不进 `DAILY_REQUIRED`，现有数值判据只查「非负/有限」。
    而均价类指标（VWAP、换手、容量）全部依赖 amount/volume 同口径。

    注: 这条**故意**不落在 `audit_fact_violations`。判据本体在
    `minute_audit` 的 S 轴 (日线自身均价), 同一恒等式写两遍、阈值还不同,
    就是下一次「两个数对不上」的来源。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    rows = [_row(s) for s in ALL_SYMS]
    _write_enriched(tmp_path, rows)
    rows[0] = _row(SH_SYMS[0], amount=5_000.0)  # 应为 1_000_000
    _write(tmp_path, rows)
    assert _facts(tmp_path) is not None, "amount 与 volume 不同口径未被告警"


@pytest.mark.xfail(
    strict=True,
    reason="缺「今日可交易池」证人: instruments 里有 320 只当日无行的退市/停牌标的, "
           "符号级缺口无法与它们区分",
)
def test_one_symbol_short_of_family_is_red(tmp_path: Path) -> None:
    """25 只里缺 1 只（96% 覆盖）→ 必须点名缺失 symbol。

    `MIN_COVERAGE_RATIO = 0.75` 是**文件级**下限：缺 1 只、缺 6 只都判干净。
    但「哪只股票没有今天」才是下游真正要知道的事（因子横截面少一只 = 排名整体偏移）。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    _write(tmp_path, [_row(s) for s in ALL_SYMS[1:]])
    assert _verdict(tmp_path) is not None, f"缺少 {ALL_SYMS[0]} 未被告警"


@pytest.mark.xfail(
    strict=True,
    reason="缺「源端停更」证人: 单日单表无法区分「今天本来就与昨天一样」与「源没翻页」",
)
def test_same_body_as_previous_day_is_red(tmp_path: Path) -> None:
    """今天的 OHLCV 与前一交易日逐行全等（源端停更但接口仍返回）→ 必须判坏。

    这是最典型的「静默停更」：行数、字段、数值都合法，只是不是今天的数据。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    body = [_row(s) for s in ALL_SYMS]
    _write(tmp_path, [dict(r, date=PREV) for r in body], day=PREV)
    _write(tmp_path, body)
    assert _verdict(tmp_path) is not None, "整日与前一交易日全等未被告警"


@pytest.mark.xfail(
    strict=True,
    reason="既有契约明确不判跨日时间戳 (test_issue_from_other_day_timestamp_not_flagged); "
           "改判需先拿到源端时间戳语义的证据",
)
def test_quote_ts_from_other_day_is_red(tmp_path: Path) -> None:
    """quote_ts 落在别的日期（源端时间戳没翻新）→ 必须判坏。

    `_is_snapshot` 要求 `ts.date() == day`，日期不符直接 False；
    随后 `_partition_snapshot_defects` 按当日窗口过滤也筛不到它 → 整体判干净。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    stale = _ts(PREV, 14, 0)
    _write(tmp_path, [_row(s, quote_ts=stale) for s in ALL_SYMS])
    assert _verdict(tmp_path) is not None, "quote_ts 停在上一交易日未被告警"


@pytest.mark.xfail(
    strict=True,
    reason="缺「半日量」证人: 量级只能与分钟线求和或自身历史分布对账, "
           "单日单表看不出 500 手是半日还是全天",
)
def test_half_day_volume_without_timestamp_is_red(tmp_path: Path) -> None:
    """半日成交量 + quote_ts 被清成 null（走 batch 路径重新编码）→ 必须判坏。

    这是 11-05/11-12 的「两条路径各转换一次」的镜像形态：一旦写入方把
    quote_ts 置 null，唯一能识别盘中快照的列就消失了，判据再也无法区分。
    """
    _write_instruments(tmp_path, ALL_SYMS)
    _write(tmp_path, [_row(s, volume=500.0) for s in ALL_SYMS])
    assert _verdict(tmp_path) is not None, "无时间戳的半日量未被告警"


# --------------------------------------------------------------- 覆盖率盲区
def test_instruments_missing_bj_still_green_but_join_loses_them(tmp_path: Path) -> None:
    """维表只有 20 只 SH、面板有 25 只（含 5 只 BJ）→ 必须报「维表不覆盖面板」。

    2026-11-13 的真实形态: `get_instruments("BJ")` 连续两天 09:10 返回 503,
    旧实现只记 WARNING 便用 SH+SZ 整表覆盖 ⇒ 355 只北交所从维表消失
    (占 11-12 成交额 0.72%), 而体检全绿。修好后 `panel - instruments` 从 0
    变 355 就必须当场红。
    """
    _write_instruments(tmp_path, SH_SYMS)
    _write(tmp_path, [_row(s) for s in ALL_SYMS])
    verdict = _facts(tmp_path)
    assert verdict is not None, "维表缺 5 只面板股票未被告警"
