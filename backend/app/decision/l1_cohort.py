"""L1 缺口反转 — 每日候选生成器（生产侧唯一产地）。

## 它是什么，以及为什么不能只写「选 20 只」

    T 日收盘  : 全市场按 mom20 = close[T]/close[T-20] − 1 降序取前 20
    T+1 开盘  : 这 20 只里 open ≤ close[T] × (1 − 1%) 的才买
    T+1 收盘  : 卖出

cohort 不是「20 只」，是「20 只里低开的那些」——实测 8.71 只/日。把「低开才买」
当可选项，等于换了一个策略。所以本模块只输出**带限价的**清单，成交与否交给
`settle._fill`（`fill_mode="open_only"`），不在这里预判。

## 为什么必须照搬而不是重写

研究侧唯一产地是 `analysis/_diag_l1_vwap_curvature.py::build_cohort()`
（316 日 / 2,752 只-日）。本模块不 import 它（analysis 不是包，且生产要能独立
部署），而是照搬它的每一步，由 `backend/tests/test_l1_cohort_production.py`
的对拍测试证明两者产出同一批 cell。**对拍变红 = 口径分叉，改代码，不改期望值。**

## 两处前视，研究口径里也有，必须显式登记

1. `pick_daily` 要求 `fwd`（T+1 收盘）有限才参与排名 ⇒ T+1 停牌的票在研究里根本
   不进候选。
2. 池条件含 `finite(C2[i0])`（T+1 开盘与 T+2 收盘都有价）⇒ 同一类前视。

实盘两样都不知道。因此：
- `cohort_cells()` 用 `require_next=True`，与研究逐位一致（回测/对拍口径）；
- `candidates()` 用 `require_next=False`，只用到 `as_of` 为止的数据（实盘口径）。

两者差额由 `test_forward_look_sensitivity_is_registered` 钉住——**这个差额就是
研究结论对实盘的乐观量**，不能让它悄悄消失。

## 价格口径

`open` / `close` 是**复权**价，`raw_close` 是原始价。缺口判定在研究里用复权序列
（`open[d+1] / close[d]`），挂单价必须是原始价（结算撮合读 `raw_open`，§4）。
除权日两者分叉：此时 `raw_close × 0.99` 不等于复权阈值的折算式，本模块把这些
日期标进 `caliber["ex_rights_note"]`，不做静默修正——A 股除权日的「昨收」由行情源
给出除权参考价，实盘挂单应以行情源前收为准。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.decision import bars
from app.parquet import scan_enriched_parquet
from app.tickflow.partitions import iter_partition_dirs, parse_partition_range

#: 策略身份。账本按这个 id 分组 p_win，不同口径的胜率不可混算。
L1_STRATEGY_ID = "l1_gap_reversal"

#: 实盘候选的读取窗口（交易日）。`mom20` 只需要 21 行，取两倍余量吸收稀疏标的
#: 与停牌缺口；实测 21 就与全量名单逐位一致（见 `test_candidates_window_is_equivalent`）。
#:
#: 为什么要裁：全量 `_panel` 在 9p 上 `collect` 15,294,109 行要 29.2s，加上
#: `_to_mats` 8.7s 与 `_top_picks` 2.6s，冷启动 46s。而实盘只问「今天买哪几只」，
#: 不需要 2005 年以来的每一行。
_CANDIDATE_WINDOW = 45

#: 与 `analysis/asymmetry_probe.py::N_STOCK` / `_diag_l1_vwap_curvature.py` 同值。
L1_TOP_N = 20
#: gap ≤ −1%。符号约定：`gap_max` 是**上限**（负值），不是「最小跌幅」。
L1_GAP_MAX = -0.01
L1_POOL_MIN = 200
L1_MIN_NAMES = 3
L1_FILLED_POOL_MIN = 50

#: 排名回看行数（`mom20` 的上游窗口）。研究中写作 `HORIZON = 20`。
MOM_WINDOW = 20

_NEED_COLS = ("symbol", "date", "open", "close", "raw_close", "turnover_rate")


@dataclass(frozen=True)
class _Mats:
    """日期 × 标的的网格。缺失 = NaN（与研究的 pivot 语义一致）。"""

    dates: list[str]
    symbols: list[str]
    open: np.ndarray
    close: np.ndarray
    raw_close: np.ndarray
    turn: np.ndarray
    c2: np.ndarray

    @property
    def n_days(self) -> int:
        return len(self.dates)


# ---------------------------------------------------------------------------
# 读取 + 网格化
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, float], tuple[pl.DataFrame, _Mats]] = {}


def _window(data_dir: Path, as_of: str, days: int) -> tuple[str, list[Path]]:
    """`as_of` 及其之前 `days` 个交易日所对应的分区文件。

    返回 `(since, files)`：`since` 是窗口第一天的 ISO 日期，用于记录口径；
    `files` 直接交给 `_panel`，省掉一次全表枚举。

    只按分区目录名的区间上界筛选（enriched 是日分区，名即日期）。目录名排序
    与日期排序一致，所以取尾部即可，不需要把 5,309 个目录全解析一遍日期集合。
    """
    base = Path(data_dir) / bars.STOCK_ENRICHED
    parts: list[tuple[Any, Path]] = []
    for part in iter_partition_dirs(base):
        rng = parse_partition_range(part.name)
        if rng is None or rng[1].isoformat() > as_of:
            continue
        parts.append((rng[0], part))
    if not parts:
        return as_of, []
    tail = parts[-days:] if len(parts) > days else parts
    since = tail[0][0].isoformat()
    files = [f for _, p in sorted(tail, key=lambda x: x[1].name) for f in sorted(p.glob("*.parquet"))]
    return since, files


def _next_session(data_dir: Path, as_of: str) -> str | None:
    """`as_of` 之后最近的已落盘交易日；没有则 None。

    存在的理由是**窄窗不能替代日历**：裁剪后 `dates[-1]` 恒等于 `as_of`，
    用 panel 自己的日期列表判断「次日分区是否落盘」会永远说「还没落盘」，
    把一条真实告警变成常量。
    """
    base = Path(data_dir) / bars.STOCK_ENRICHED
    for part in iter_partition_dirs(base):
        rng = parse_partition_range(part.name)
        if rng is None:
            continue
        if rng[0].isoformat() > as_of:
            return rng[0].isoformat()
    return None


def _panel(data_dir: Path, *, files: list[Path] | None = None) -> tuple[pl.DataFrame, _Mats]:
    """读 enriched 日线并网格化。全量按 (目录, mtime) 缓存一次。

    `files` 非空时只读这些分区，**不进缓存**：它便宜（45 天约 1% 的行），
    而 `_CACHE.clear()` 只留一份 —— 与全量共用会互相踢掉，让「先看板再回测」
    变成两次 29s。

    缓存键用目录 mtime 而不是日期列表：新增一个 `date=` 子目录会更新父目录
    mtime，而列出 370 个子目录在 9p 挂载上要 stat 370 次（实测秒级）。
    """
    base = Path(data_dir) / bars.STOCK_ENRICHED
    if not base.is_dir():
        raise FileNotFoundError(f"缺少日线分区目录: {base}")
    resolved = str(base.resolve())
    try:
        stamp = base.stat().st_mtime
    except OSError:
        stamp = 0.0
    key = (resolved, stamp)
    if files is None:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None:
            return hit

    lf = scan_enriched_parquet(base if files is None else [str(f) for f in files])
    have = set(lf.collect_schema().names())
    missing = [c for c in _NEED_COLS if c not in have]
    if missing:
        raise ValueError(f"{bars.STOCK_ENRICHED} 缺列 {missing}，L1 无法计算")
    cols = list(_NEED_COLS) + (["amount"] if "amount" in have else [])
    df = lf.select(cols).collect()
    if df.is_empty():
        raise ValueError(f"{bars.STOCK_ENRICHED} 无数据")
    df = df.with_columns(pl.col("date").cast(pl.Utf8))
    # 矩阵法遇到重复 (date, symbol) 会静默覆盖一格，样本无声减少。
    if bool(df.select(pl.struct("date", "symbol").is_duplicated().any()).item()):
        raise ValueError("(date, symbol) 存在重复行——矩阵法会静默丢样本，拒绝继续")
    df = df.sort(["symbol", "date"])

    mats = _to_mats(df)
    if files is None:
        with _CACHE_LOCK:
            _CACHE.clear()  # 只留最新一份：日线每天变，旧网格没有复用价值
            _CACHE[key] = (df, mats)
    return df, mats


def _to_mats(df: pl.DataFrame) -> _Mats:
    dates = sorted(set(df["date"].to_list()))
    symbols = sorted(set(df["symbol"].to_list()))
    t_len, n_sym = len(dates), len(symbols)
    d_map = {d: i for i, d in enumerate(dates)}
    s_map = {s: i for i, s in enumerate(symbols)}
    di = np.fromiter((d_map[d] for d in df["date"].to_list()), np.int64, df.height)
    si = np.fromiter((s_map[s] for s in df["symbol"].to_list()), np.int64, df.height)

    def grid(col: str) -> np.ndarray:
        m = np.full((t_len, n_sym), np.nan)
        m[di, si] = _floats(df[col])
        return m

    o, c = grid("open"), grid("close")
    c2 = np.full((t_len, n_sym), np.nan)
    if t_len > 2:
        # c2[i] = open[i+1] → close[i+2]，与 `_diag_base_t0.leg(mats, "c2")` 同式。
        with np.errstate(divide="ignore", invalid="ignore"):
            c2[: t_len - 2] = c[2:] / o[1:-1] - 1.0
    return _Mats(dates, symbols, o, c, grid("raw_close"), grid("turnover_rate"), c2)


def _floats(series: pl.Series) -> np.ndarray:
    """Float64 视图，null → NaN（研究侧按 `np.isfinite` 过滤，NaN 是它的口径）。"""
    s = series.cast(pl.Float64)
    try:
        arr = s.to_numpy()
    except (TypeError, ValueError):
        arr = s.fill_null(float("nan")).to_numpy()
    out = np.asarray(arr, dtype=np.float64)
    out[~np.isfinite(out)] = np.nan
    return out


def _top_picks(
    df: pl.DataFrame, *, top_n: int, require_next: bool,
) -> dict[str, list[str]]:
    """每个决策日的 top_n 名单（`{date: [symbol, ...]}`）。

    `require_next=True` 复刻研究口径：`mom20` 与 `fwd` 都有限才参与排名。
    `require_next=False` 只要求 `mom20` 有限——**这是实盘唯一能给的**。

    `fwd` 的口径容易看错，别按名字猜：研究侧
    `analysis/asymmetry_probe.py::add_features` 里 `HORIZON = 20`，
    `fwd = close.shift(-20) / close - 1` 是**二十日后**的收益，不是次日。
    于是「`fwd` 有限」等于「该股二十个交易日后还有数据」——这是一条
    **二十天的生存过滤**：之后退市或长期停牌的票，在研究 cohort 里根本不出现。
    实盘没有这个信息，`require_next=False` 因此会多出这些票。差额由
    `test_forward_look_sensitivity_is_registered` 钉住。
    """
    d = df.with_columns(
        (pl.col("close") / pl.col("close").shift(MOM_WINDOW).over("symbol") - 1.0)
        .alias("mom20"),
        (pl.col("close").shift(-MOM_WINDOW).over("symbol") / pl.col("close") - 1.0)
        .alias("fwd"),
    )
    cond = pl.col("mom20").is_finite()
    if require_next:
        cond = cond & pl.col("fwd").is_finite()
    sub = (
        d.filter(cond)
        .with_columns(
            pl.col("mom20").rank("ordinal", descending=True).over("date").alias("rk")
        )
        .filter(pl.col("rk") <= top_n)
    )
    return {
        str(row["date"]): [str(s) for s in row["symbol"]]
        for row in sub.group_by("date").agg(pl.col("symbol").alias("symbol")).to_dicts()
    }


# ---------------------------------------------------------------------------
# 回测/对拍口径
# ---------------------------------------------------------------------------


def cohort_cells(
    data_dir: Path,
    *,
    top_n: int = L1_TOP_N,
    gap_max: float = L1_GAP_MAX,
    pool_min: int = L1_POOL_MIN,
    min_names: int = L1_MIN_NAMES,
    filled_pool_min: int = L1_FILLED_POOL_MIN,
    require_next: bool = True,
    start: str | None = None,
    end: str | None = None,
) -> pl.DataFrame:
    """全样本 cohort（`date` = 入场日，`symbol`）。与研究的 2,752 只-日逐位一致。

    这是**回测口径**：`require_next=True` 时含 `fwd` 与 `c2` 两处前视，只用于对拍
    与历史复核，不要拿它当实盘候选（用 `candidates()`）。`require_next=False`
    只去掉 `fwd` 那一处，用来量「研究结论对实盘的乐观量」——
    `c2` 的池条件留着，因为它同时是把 `i0+2` 越界定住的那道闸，去掉会改变
    池的边界语义而不只是放宽样本。

    `end` 截断数据再重算网格：**必须重新网格化**，否则尾部日期少两格会让
    `i0+2` 越界判断与当时不一致。它存在的唯一目的是复现「某个历史时点看到的世界」
    （研究产物里 316 日 / 2,752 只-日 / 121.5bp 就是 2026-11-05 那个时点）。

    `start` 与 `end` **故意不对称**：它只筛**信号日**，不截断数据。
    `mom20` 与 `fwd` 要回看/前看 20 个交易日，把它们一起裁掉会让区间头部
    凭空少掉 20 个信号日——那不是「同一策略的另一个窗口」，是另一个策略。
    两个参数的存在理由也不同：`end` 复现时点，`start` 让锚不随数据回填漂移
    （数据从 316 日扩到 4,592 日时，只限上界的锚会恒红，等于没有门）。
    """
    df, m = _panel(data_dir)
    if end is not None:
        df = df.filter(pl.col("date") <= end)
        m = _to_mats(df)
    picks = _top_picks(df, top_n=top_n, require_next=require_next)
    if start is not None:
        picks = {d: v for d, v in picks.items() if d >= start}
    d_map = {d: i for i, d in enumerate(m.dates)}
    s_map = {s: i for i, s in enumerate(m.symbols)}
    tradable = (
        np.isfinite(m.close) & (m.close > 0) & np.isfinite(m.open) & (m.open > 0)
    )

    rows: list[tuple[str, str]] = []
    for day in sorted(picks):
        i0 = d_map.get(day)
        if i0 is None or i0 + 2 >= m.n_days:
            continue
        c0 = m.close[i0]
        pool = tradable[i0] & np.isfinite(m.c2[i0]) & np.isfinite(m.turn[i0]) & (c0 > 0)
        if int(pool.sum()) < pool_min:
            continue
        lim = c0 * (1.0 - abs(gap_max))
        fill = pool & tradable[i0 + 1] & (m.open[i0 + 1] <= lim) & (lim > 0)
        if int((pool & fill).sum()) < filled_pool_min:
            continue
        sel = np.array(
            [s_map[s] for s in picks[day] if s in s_map], dtype=np.int64
        )
        if sel.size == 0:
            continue
        good = sel[fill[sel]]
        if good.size < min_names:
            continue
        entry = m.dates[i0 + 1]
        rows.extend((entry, m.symbols[j]) for j in good)

    return pl.DataFrame(
        {"date": [r[0] for r in rows], "symbol": [r[1] for r in rows]},
        schema={"date": pl.Utf8, "symbol": pl.Utf8},
    )


# ---------------------------------------------------------------------------
# 实盘口径
# ---------------------------------------------------------------------------


def candidates(
    data_dir: Path,
    as_of: str,
    *,
    top_n: int = L1_TOP_N,
    gap_max: float = L1_GAP_MAX,
    require_next: bool = False,
) -> dict[str, Any]:
    """`as_of` 收盘后，为下一交易日的集合竞价给出的挂单清单。

    **只用到 `as_of` 为止的数据**（`require_next=False`）。不预测会不会低开——
    那是 9:25 才发生的事，由 `settle._fill(open_only)` 判定。

    读取窗口是 `as_of` 前 `_CANDIDATE_WINDOW` 个交易日，不是全历史：`mom20`
    只要 21 行，而全量面板冷启动 46s。窗口边界由 `test_candidates_window_is_equivalent`
    与全量逐位对拍钉住——**改了窗口或改了 `shift` 的口径，那条测试会红**。
    """
    if require_next:
        raise ValueError("candidates() 是实盘口径，不接受 require_next=True")
    since, files = _window(data_dir, as_of, _CANDIDATE_WINDOW)
    df_all, m = _panel(data_dir, files=files)
    dates = m.dates
    if as_of not in set(dates):
        raise ValueError(f"{as_of} 不是已落盘的交易日（缺分区或非交易日）")
    latest = bars.latest_session(data_dir, bars.STOCK_ENRICHED)
    nxt = _next_session(data_dir, as_of)

    df = df_all.filter(pl.col("date") <= as_of)
    picks = _top_picks(df, top_n=top_n, require_next=require_next)
    chosen = picks.get(as_of, [])
    if not chosen:
        return _empty(as_of, nxt, top_n=top_n, gap_max=gap_max,
                      reason="no_ranking", panel_since=since)

    i0 = dates.index(as_of)
    s_map = {s: i for i, s in enumerate(m.symbols)}
    d_idx = df_all.filter(pl.col("date") == as_of).row(0, named=True)
    amt_by_symbol: dict[str, float | None] = {}
    if "amount" in df_all.columns:
        amt_by_symbol = {
            str(r["symbol"]): r["amount"]
            for r in df_all.filter(pl.col("date") == as_of)
            .select("symbol", "amount")
            .iter_rows(named=True)
        }

    out: list[dict[str, Any]] = []
    for rank, sym in enumerate(chosen, start=1):
        j = s_map.get(sym)
        if j is None:
            continue
        prev_raw = float(m.raw_close[i0, j])
        if not np.isfinite(prev_raw) or prev_raw <= 0:
            continue
        limit = _limit_price(prev_raw, gap_max)
        if limit <= 0:
            continue
        mom20 = _mom20_at(df, as_of, sym)
        amount = amt_by_symbol.get(sym)
        out.append(
            {
                "symbol": sym,
                "rank": rank,
                "mom20_pct": None if mom20 is None else round(mom20 * 100, 3),
                "prev_close_raw": round(prev_raw, 2),
                #: 集合竞价挂单价。开盘 ≤ 它才算 gap ≤ −1%，才该成交。
                "limit_price": limit,
                "gap_threshold_pct": round(gap_max * 100, 2),
                "day_amount_yi": None
                if amount is None or not np.isfinite(float(amount))
                else round(float(amount) / 1e8, 2),
                "tradable": bool(
                    np.isfinite(m.open[i0, j]) and m.open[i0, j] > 0
                ),
            }
        )

    return {
        "as_of": as_of,
        #: 下一交易日。`None` = 次日分区还没落盘（收盘后立刻调用时如此）。
        #: 来自 `_next_session()` 而不是 panel 的日期序列：裁剪后 `dates[-1]`
        #: 恒等于 `as_of`，用它判断会把这条永远写成 `None`。
        "entry_date": nxt,
        "strategy_id": L1_STRATEGY_ID,
        #: 本次读取的面板起点。写进响应是因为「窗口」也是口径的一部分：
        #: 同样的名单若由全历史算出，说明窗口不构成约束；若由 45 天算出，
        #: 读者需要知道它没见过更早的数据。
        "panel_since": since,
        "top_n": top_n,
        "gap_max": gap_max,
        "n_candidates": len(out),
        "candidates": out,
        "caliber": _caliber(gap_max),
        "warnings": _warnings(as_of, latest, nxt, out, d_idx),
    }


def _mom20_at(df: pl.DataFrame, day: str, symbol: str) -> float | None:
    """`as_of` 当日的 mom20（小数）。按 symbol 行序 shift，与研究一致。"""
    sub = (
        df.filter(pl.col("symbol") == symbol)
        .with_columns(
            (pl.col("close") / pl.col("close").shift(MOM_WINDOW) - 1.0).alias("mom20")
        )
        .filter(pl.col("date") == day)
    )
    if sub.is_empty():
        return None
    v = sub["mom20"][0]
    if v is None:
        return None
    f = float(v)
    return f if np.isfinite(f) else None


def _tick_floor(price, tick: float = 0.01):
    """把价格向下取整到 tick。**挂单价不能四舍五入。**

    标量与数组同式：护栏要在 200 万个格上验证，逐点 Python 循环会把测试拖成分钟级。
    返回 `np.float64`（它 `issubclass(float)`，JSON 序列化无碍）。

    开盘价本身是 tick 的整数倍（A 股 0.01，全样本 2,017,242 个 `raw_open` /
    `raw_close` 无一例外），所以对任意可成交的开盘价 p：

        p ≤ x  ⟺  p ≤ floor_tick(x)

    这是**恒等**，不是「近似」：右侧的 x 落在哪个 tick 之间，判定的结果都一样，
    取整只是把「可判定的数」换成「能挂出去的数」。而 `round(x, 2)` 在 x 的小数
    第 3 位 ≥5 时把限价抬高一分，于是把 gap 是 −0.997% 的票也买进来。

    实测（全样本 2,010,921 个可判定 cell，除权日另计）：`floor` 的谓词违反
    **0** 个；`round` 违反 **21,491** 个（占 1.07%）。落在 top20 名单上的那部分
    在 80 个交易日里多买了 11 个 cell，平均 −145bp，而同期 cohort +120bp。
    """
    return np.floor(price / tick + 1e-9) * tick


def _limit_price(prev_raw, gap_max: float = L1_GAP_MAX):
    """集合竞价挂单价 —— **唯一定义点**。

    `candidates()` 与所有护栏都必须走这个函数。2026-11-12 的教训：限价的定义
    曾在生产路径和回测路径各写一遍，只有一条被测试覆盖，一分钱的方向就是从
    两条实现的缝里漏的。把定义收成一个函数，护栏断言的就不再是「两份实现相等」
    （两边一起错也会绿），而是**谓词恒等**。
    """
    return _tick_floor(prev_raw * (1.0 + gap_max))



def _caliber(gap_max: float) -> dict[str, Any]:
    return {
        "cohort_definition": "top20 mom20 ∩ open[T+1]/close[T]-1 <= gap_max，T+1 收盘卖",
        "price_basis": "缺口判定用复权价（open/close），挂单价用原始价（raw_close）",
        "limit_tick": "挂单价按 0.01 向下取整；四舍五入会放宽一分、买进 gap>−1% 的票",
        "gap_max": gap_max,
        "mom_window_rows": MOM_WINDOW,
        "source": "app.decision.l1_cohort（唯一生产产地，对拍 analysis 316 日/2752 只-日）",
        "ex_rights_note": (
            "除权日 raw_close×0.99 与复权阈值不等价；实盘应以行情源『前收』"
            "（已是除权参考价）×0.99 为准"
        ),
        "forward_look_dropped": [
            "排名不再要求 T+1 收盘存在（研究口径要求 fwd 有限）",
            "池不再要求 T+1 开盘 / T+2 收盘存在（研究口径要求 C2 有限）",
        ],
    }


def _empty(as_of: str, entry_date: str | None, *, top_n: int, gap_max: float,
           reason: str, panel_since: str | None = None) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "entry_date": entry_date,
        "strategy_id": L1_STRATEGY_ID,
        "panel_since": panel_since,
        "top_n": top_n,
        "gap_max": gap_max,
        "n_candidates": 0,
        "candidates": [],
        "reason": reason,
        "caliber": _caliber(gap_max),
        "warnings": ["当日没有可排名的标的（mom20 全缺失）"],
    }


def _warnings(
    as_of: str, latest: str | None, nxt: str | None,
    out: list[dict[str, Any]], row: dict[str, Any],
) -> list[str]:
    """把「这份清单在什么情况下不该被相信」写在它旁边。

    两条时间判据都**不用 panel 自己的日期序列**：候选读取窗口裁到 `as_of`，
    窗口内 `dates[-1] == as_of`、没有次日格，用它们判断会把「as_of 不是最新」
    和「次日已落盘」两件事各写成一个常量。真实日历来自 `latest_session()` /
    `_next_session()`。
    """
    w: list[str] = []
    if latest is not None and as_of < latest:
        w.append(
            f"as_of={as_of} 不是最新分区（最新 {latest}）："
            "若用于次日实盘，请确认该日数据已收盘定版"
        )
    if nxt is None:
        w.append("次日分区尚未落盘：清算与撮合结果需等下一个交易日")
    qts = row.get("quote_ts")
    # enriched 的 quote_ts 记的是这一行的行情时间戳。收盘后仍停在竞价前快照的
    # 分区（`daily_pipeline` 记录的已知现象）会让 close 不是收盘价——此时限价算错。
    if qts is not None:
        try:
            stamp = int(qts)
        except (TypeError, ValueError):
            stamp = 0
        if stamp and stamp % 1000000 == 0:
            w.append(f"quote_ts={stamp} 形态可疑，请核对 close 是否为收盘定版价")
    if len(out) < L1_MIN_NAMES:
        w.append(
            f"候选只有 {len(out)} 只（研究口径要求 ≥{L1_MIN_NAMES} 只才成立）："
            "该日样本过少，不建议当作完整信号"
        )
    return w
