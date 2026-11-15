"""L1 生产侧 cohort 的口径锚。

为什么必须三个数同时命中
------------------------
研究侧唯一产地是 `analysis/_diag_l1_vwap_curvature.py::build_cohort()`，产物记着
316 日 / 2,752 只-日 / 121.5bp。分开看都不可靠：

- 只对 cell 数：成员可能整批换过，收益照旧；
- 只对收益：样本可能被悄悄筛过——删掉亏损的样本是最容易发生、最不报错的方向；
- 三个一起对上，才说明「同一批 cell」**并且**「同一条收益口径」。

第二条锚是与 research 原函数**逐位**比较。它只在 analysis 目录存在时跑（生产镜像
不带），所以锚点档永远跑、不给跳过留口子。

第三条是**故意要它不一致**：`require_next=False`（实盘口径）必须比研究口径多出
样本，差额就是「研究结论对实盘的乐观量」。若两条口径给出同一集合，说明实盘路径
偷偷用上了未来数据——这是本模块最危险、且完全不报错的失败方式。
"""
from __future__ import annotations

import sys
from datetime import date
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "analysis"))

DATA = ROOT / "data"

#: 研究产物的历史时点锚（`_diag_l1_vwap_entry_result.json` / `_annualize_result.json`）。
#:
#: **两端都写死。** 只限上界的锚在数据回填后会恒红：2026-11-14 把 `kline_daily`
#: 补到 2005 年后，同一个 `end="2026-11-06"` 的 cohort 从 2,752 只-日变成 39,980，
#: 门就一直红着——恒红的门等于没有门（这条教训写在 MEMORY §15b）。
#: `start` 是**信号日**（不是入场日）：入场日 2025-06-06 对应信号日 2025-06-05。
ANCHOR_START = "2025-06-05"
ANCHOR_END = "2026-11-06"
ANCHOR_CELLS = 2752
ANCHOR_DAYS = 316
ANCHOR_DAY_BP = 121.49

#: 全样本锚（数据覆盖到 2026-11-13 时的读数）。
#:
#: 与上面的研究锚分工不同：研究锚回答「口径有没有分叉」，全样本锚回答
#: 「这批数据上策略还成不成立」。它是**快照**——每次回填历史都要重算并更新，
#: 更新时必须同时解释「为什么变」，不许只改数字。
FULL_DAYS = 4592
FULL_CELLS = 39980
FULL_DAY_BP = 96.02


@pytest.fixture(scope="module")
def l1():
    from app.decision import l1_cohort

    if not (DATA / "kline_daily_enriched").is_dir():
        pytest.skip("缺本地日线分区，无法校验口径锚")
    return l1_cohort


def _day_equal_bp(l1, cells: pl.DataFrame, *, end: str | None = None) -> float:
    """`open→close` 的**日等权**均值（bp）。研究报的 121.5 就是这个口径。

    不是 cell 等权：一天 8 只和一天 3 只不该给它 8 : 3 的票数权重。
    """
    df, m = l1._panel(DATA)
    if end is not None:
        df = df.filter(pl.col("date") <= end)
        m = l1._to_mats(df)
    d_map = {d: i for i, d in enumerate(m.dates)}
    s_map = {s: i for i, s in enumerate(m.symbols)}
    d = np.array([d_map[x] for x in cells["date"].to_list()])
    s = np.array([s_map[x] for x in cells["symbol"].to_list()])
    r = m.close[d, s] / m.open[d, s] - 1.0
    agg: dict[str, list[float]] = defaultdict(list)
    for v, day in zip(r, cells["date"].to_list()):
        agg[day].append(float(v))
    return float(np.mean([np.mean(v) for v in agg.values()]) * 1e4)


def test_cohort_reproduces_published_anchor(l1) -> None:
    """三个数必须同时命中，缺一都说明口径动了。

    天数**严格**相等（窗口两端都钉住，多一天少一天都是分组变了）。
    只-日与 bp 各有容差，原因写在断言里：研究产物是 2026-11-05 那个时点算的，
    现在 enriched 多了 11-07…11-13 五个交易日，池条件（`filled_pool_min`）
    在边界上会翻转 4 个 cell —— 差的是数据快照，不是实现。
    容差取 0.5% / 1.5bp，是这 4 个 cell 的实测幅度，不是为了让测试变绿。
    """
    cells = l1.cohort_cells(DATA, start=ANCHOR_START, end=ANCHOR_END)
    assert cells["date"].n_unique() == ANCHOR_DAYS, (
        f"入场日 {cells['date'].n_unique()} ≠ {ANCHOR_DAYS}：窗口或分组变了")
    assert abs(cells.height - ANCHOR_CELLS) <= ANCHOR_CELLS * 0.005, (
        f"cohort 只-日 {cells.height} 偏离研究口径 {ANCHOR_CELLS} 超过 0.5%：成员集合已分叉")
    assert _day_equal_bp(l1, cells, end=ANCHOR_END) == pytest.approx(
        ANCHOR_DAY_BP, abs=1.5
    ), "样本数对但收益对不上 ⇒ 收益口径（复权/入场价/出场价）变了，不是成员变了"


def test_cohort_full_history_anchor(l1) -> None:
    """全样本锚：数据扩到 20 年后，策略还成不成立。

    这是回填历史上限之后**唯一**还看得见的判据。研究锚只管 316 日，316 日
    好不代表 22 年好；而全样本锚一旦被悄悄改小（或 `start` 被误加），
    22 年逐年为正这个结论就没有任何东西守着它了。
    """
    cells = l1.cohort_cells(DATA, end=ANCHOR_END)
    assert cells["date"].n_unique() == FULL_DAYS, (
        f"全样本入场日 {cells['date'].n_unique()} ≠ {FULL_DAYS}：数据覆盖变了"
        "（回填/删除分区）。变了就更新锚，并在提交里说明是哪一侧动的")
    assert cells.height == FULL_CELLS, (
        f"全样本只-日 {cells.height} ≠ {FULL_CELLS}：成员集合变了")
    bp = _day_equal_bp(l1, cells, end=ANCHOR_END)
    assert bp == pytest.approx(FULL_DAY_BP, abs=1.0), (
        f"全样本日等权 {bp:.2f}bp ≠ {FULL_DAY_BP}bp：收益口径变了")
    avg = cells.height / cells["date"].n_unique()
    assert abs(avg - 8.71) <= 0.05, f"日均只数 {avg:.2f} 偏离研究口径 8.71"


def test_cohort_identical_to_research_function(l1, monkeypatch) -> None:
    """与 research 原函数逐位相同。analysis 不在时跳过——但锚点档不会跳过。

    research 侧把 `EXPECT_DAYS/EXPECT_CELLS` 写死在 2026-11-05 的 316 日窗口上，
    数据一扩它自己就 `SystemExit`。这里把它们改写成**全样本锚**再对拍：
    期望值来自上面那条 `test_cohort_full_history_anchor`（同一份登记），
    不是从 `mine` 反推——否则实现错了两边一起错，对拍变成循环论证。
    """
    C = pytest.importorskip("_diag_l1_vwap_curvature")
    # analysis 的 `DATA = Path("data")` 是相对 cwd 的；pytest 在 backend/ 下跑，
    # 不切目录它会 SystemExit("no enriched partitions found")。
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(C, "EXPECT_DAYS", FULL_DAYS)
    monkeypatch.setattr(C, "EXPECT_CELLS", FULL_CELLS)
    research = C.build_cohort()
    mine = l1.cohort_cells(DATA)
    rs = set(zip(research["date"].to_list(), research["symbol"].to_list()))
    ms = set(zip(mine["date"].to_list(), mine["symbol"].to_list()))
    assert rs == ms, (
        f"只 research 有 {len(rs - ms)} 条、只生产有 {len(ms - rs)} 条："
        "两处 cohort 定义已分叉，修生产侧，不要调期望值")


def test_forward_look_sensitivity_is_registered(l1) -> None:
    """实盘口径必须比研究口径**多**样本；一样多就是未来数据漏进来了。"""
    strict = l1.cohort_cells(DATA, require_next=True)
    live = l1.cohort_cells(DATA, require_next=False)
    assert live.height > strict.height, (
        "实盘口径没有多出任何样本：研究里那条「二十日后仍有数据」的生存过滤"
        "被带到了实盘路径上，等于用未来信息筛掉了当时还在的票")
    # 多出的是尾部二十个交易日 + 之后退市的票，量级有上限；超了说明别的口径松了。
    assert live.height < strict.height * 1.35, (
        f"实盘口径多出 {live.height - strict.height} 条（{live.height / strict.height:.2f}×），"
        "超出「去掉二十日生存过滤」能解释的量级")


def test_candidates_do_not_read_future_rows(l1, monkeypatch) -> None:
    """把数据库截到 `as_of` 当天，candidates 的输出必须一字不变。

    这是本模块唯一真正的「未来函数」检查：比较的是**同一份代码在两个数据库快照上
    的结果**，而不是代码长什么样。
    """
    as_of = "2026-11-10"
    full = l1.candidates(DATA, as_of)
    df, _ = l1._panel(DATA)
    trimmed = df.filter(pl.col("date") <= as_of)
    monkeypatch.setattr(l1, "_panel", lambda _d: (trimmed, l1._to_mats(trimmed)))
    cut = l1.candidates(DATA, as_of)

    assert [c["symbol"] for c in cut["candidates"]] == [
        c["symbol"] for c in full["candidates"]
    ], "截断未来数据后榜单变了 ⇒ candidates 读到了 as_of 之后的行"
    assert [c["limit_price"] for c in cut["candidates"]] == [
        c["limit_price"] for c in full["candidates"]
    ]


def test_candidates_limit_price_is_raw_close_times_threshold(l1) -> None:
    """挂单价必须是**原始**收盘价 ×0.99 —— 结算撮合读 `raw_open`，§4。

    用复权价挂单不会报错，只会在除权前后买在错误的价位上。
    """
    out = l1.candidates(DATA, "2026-11-10")
    assert out["n_candidates"] > 0, "该日应有候选（研究口径 8.7 只/日）"
    for c in out["candidates"]:
        assert c["limit_price"] == pytest.approx(c["prev_close_raw"] * 0.99, abs=0.01)
        assert c["gap_threshold_pct"] == -1.0
        assert c["rank"] >= 1


def test_candidates_reject_non_session(l1) -> None:
    with pytest.raises(ValueError, match="不是已落盘的交易日"):
        l1.candidates(DATA, "2026-11-08")  # 周日


def test_limit_price_predicate_is_identical_on_every_clean_cell(l1) -> None:
    """**谓词恒等**，不是「两份实现相等」。全样本、逐格，不需要回测腿。

    恒等式：`(raw_open ≤ limit_price) ⟺ (缺口 ≤ −1%)`。

    为什么换掉原来的「生产 vs 回测逐日比集合」：那条护栏只看末 20 个交易日，
    而 cohort 层面的取整分叉是 ~12.8%/日 的事件 ⇒ 窗口里一个分叉日都没有的概率
    约 6%，窗口一滚就绿，bug 还在。更根本的是它断言「两个实现相等」——两边错得
    一样就同绿。这里的判定不依赖任何一天落在窗口里。

    除权日是**登记的例外**，不是漏网：策略缺口用复权价，挂单价用原始价，
    `raw_close × 0.99` 与复权阈值在除权日本来就不等价（模块 docstring §价格口径）。
    所以断言拆成两条：干净格 **0 违反**；违反**全部**落在除权格上。
    """
    df, m = l1._panel(DATA)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_open = m.open * m.raw_close / m.close
        gap_adj = m.open[1:] / m.close[:-1] - 1.0    # 策略真正判定的缺口（复权）
    prev = m.raw_close[:-1]
    nxt = raw_open[1:]
    valid = np.isfinite(prev) & np.isfinite(nxt) & (prev > 0) & (nxt > 0)

    limit = l1._limit_price(prev)                   # 数组同式，与生产同一函数
    truth = gap_adj <= l1.L1_GAP_MAX + 1e-12
    admitted = nxt <= limit + 1e-9
    viol = valid & (admitted != truth)

    # 除权日：adj_factor 在 i0 → i0+1 变了
    ratio = m.raw_close / m.close
    with np.errstate(invalid="ignore", divide="ignore"):
        ex_rights = np.abs(ratio[1:] / ratio[:-1] - 1.0) > 1e-9

    assert int(valid.sum()) >= 1_900_000, "可判定 cell 少于 190 万：数据分区被截断了"
    clean_viol = int((viol & ~ex_rights).sum())
    assert clean_viol == 0, (
        f"限价谓词在 {clean_viol} 个非除权格上不成立 ⇒ 挂单价放行了 gap > −1% 的票。"
        "先确认 `_limit_price` 是不是被换成了 round")
    # 除权格上的违反必须「有」，否则说明除权掩码失效、这条断言变成空射程
    assert int(ex_rights.sum()) >= 8_000
    assert int((viol & ex_rights).sum()) >= 3_000
    assert not bool((viol & ~valid).any())

    # 敏感性自证：同样 200 万个格，round 会违反多少次（≥1 万才算抓住了东西）
    round_limit = np.round(prev * (1.0 + l1.L1_GAP_MAX), 2)
    round_viol = valid & ((nxt <= round_limit + 1e-9) != truth)
    assert int((round_viol & ~ex_rights).sum()) >= 10_000, (
        "换回 round 之后反例不足 1 万 —— 断言已经不再对取整方向敏感，是空射程")


def test_round_limit_admits_a_gap_above_threshold_on_pinned_prices() -> None:
    """11 个实盘样本（末 81 个决策日的 top20 名单，逐位取自原始分区）。

    这一条不读 parquet：把价格钉死，`_limit_price` 若被换成 round，
    这 11 行全部翻向。它测的是**取整方向**，与数据是否更新无关。
    """
    from app.decision import l1_cohort

    fixtures = [
        ("301234.SZ", 69.20, 68.51), ("002726.SZ", 2.21, 2.19),
        ("000802.SZ", 6.26, 6.20), ("600371.SH", 14.15, 14.01),
        ("001216.SZ", 29.09, 28.80), ("002815.SZ", 24.44, 24.20),
        ("688026.SH", 29.25, 28.96), ("301513.SZ", 67.32, 66.65),
        ("001216.SZ", 24.43, 24.19), ("301313.SZ", 51.20, 50.69),
        ("300296.SZ", 7.32, 7.25),
    ]
    for sym, prev, open_px in fixtures:
        gap = open_px / prev - 1.0
        assert gap > -0.01, f"{sym} 缺口 {gap:.4%} 不在 (−1%,0]，样本写错了"
        assert open_px > l1_cohort._limit_price(prev) + 1e-9, (
            f"{sym} 前收 {prev} 开盘 {open_px}：floor 限价 {l1_cohort._limit_price(prev)} "
            "仍然放行了，取整没有向下")
        assert open_px <= round(prev * 0.99, 2) + 1e-9, (
            f"{sym} round 限价 {round(prev * 0.99, 2)} 没有把 {open_px} 放进来，样本失效")


def test_fill_set_census_has_no_unregistered_divergence(l1) -> None:
    """生产腿与回测腿的全部差异必须落进**四个登记类**，一类一格。

    冻结在 `ANCHOR_START → ANCHOR_END` 上数，所以计数是稳定值，不是「今天恰好这样」。
    任何第五类出现（或已有类计数漂移）都会红——这比「逐日相等」诚实：
    两条腿本来就不可能相等（回测的池条件含 `c2`，那是 T+2 的信息，实盘没有）。

    `df` 不按 `start` 截断（`mom20` 要回看 20 日），只有**信号日**进统计窗口。
    """
    end = ANCHOR_END
    df_all, _ = l1._panel(DATA)
    df = df_all.filter(pl.col("date") <= end)
    m = l1._to_mats(df)
    dates, syms = m.dates, m.symbols
    d_map = {d: i for i, d in enumerate(dates)}
    s_map = {s: i for i, s in enumerate(syms)}
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_open = m.open * m.raw_close / m.close

    picks = l1._top_picks(df, top_n=l1.L1_TOP_N, require_next=False)
    cells = l1.cohort_cells(DATA, require_next=False, start=ANCHOR_START, end=end)
    backtest: dict[str, set[str]] = defaultdict(set)
    for d, s in zip(cells["date"].to_list(), cells["symbol"].to_list()):
        backtest[d].add(s)

    tradable = np.isfinite(m.close) & (m.close > 0) & np.isfinite(m.open) & (m.open > 0)
    census: dict[str, int] = defaultdict(int)
    for k in range(len(dates) - 1):
        if dates[k] < ANCHOR_START:
            continue
        entry = dates[k + 1]
        prod = set()
        for s in picks.get(dates[k], []):
            j = s_map[s]
            pr, ro = m.raw_close[k, j], raw_open[k + 1, j]
            if not (np.isfinite(pr) and pr > 0 and np.isfinite(ro) and ro > 0):
                continue
            if ro <= l1._limit_price(float(pr)) + 1e-9:
                prod.add(s)
        expect = backtest.get(entry, set())
        if prod == expect:
            census["same"] += 1
            continue
        if k + 2 >= len(dates):
            census["i0+2_oob"] += 1
        elif len(prod) < l1.L1_MIN_NAMES and not expect:
            census["min_names"] += 1
        else:
            c0 = m.close[k]
            pool = tradable[k] & np.isfinite(m.c2[k]) & np.isfinite(m.turn[k]) & (c0 > 0)
            lim = c0 * 0.99
            filled = pool & tradable[k + 1] & (m.open[k + 1] <= lim) & (lim > 0)
            if int(filled.sum()) < l1.L1_FILLED_POOL_MIN:
                census["filled_pool_min"] += 1
            elif int(pool.sum()) < l1.L1_POOL_MIN:
                census["pool_min"] += 1
            elif any(not pool[s_map[s]] for s in (prod - expect)):
                census["pool_membership"] += 1
            else:
                census["adj_vs_raw"] += 1

    #: 冻结窗口（信号日 2025-06-05 → 2026-11-06，346 个决策日）的逐类计数。
    #:
    #: 窗口加 `start` 前这里是 366 天、`same=347`（全历史口径）。改窗口后总数少
    #: 20 天，各类按比例缩放：`same` 94.809% → **94.798%**，只差 0.011pp；
    #: `adj_vs_raw` 4 → 3 是除权日样本少 1 例（1.09% → 0.87%）。
    #: 所以这是**窗口变了**，不是两条腿分开了 —— 下面的比例断言就是这个区分的判据：
    #: 绝对计数会随窗口动，`same` 占比掉了才是真的分叉。
    EXPECT = {
        "same": 328,
        #: 回测按「当日成交 <3 只」丢整天，生产照挂 —— 登记过的样本数闸
        "min_names": 11,
        #: 池条件要求 `c2`（T+2 收盘）有限 ⇒ 回测丢掉、生产买进。前视量，不是 bug
        "pool_membership": 3,
        #: 生产用原始价、回测用复权价定缺口 ⇒ 除权日分叉（3 例）+ 1 例恰好 −100bp
        #: 的浮点边界（`open == close*0.99` 在 float 下差 1e-16）
        "adj_vs_raw": 3,
        "i0+2_oob": 1,
        "pool_min": 0,
        "filled_pool_min": 0,
    }
    full = {k: census.get(k, 0) for k in EXPECT}
    assert full == EXPECT, (
        f"分叉普查变了：{full} ≠ {EXPECT}。新增的类别/计数要先登记再改期望值；"
        "`same` 掉下来 = 生产腿又和回测腿分开了")
    total = sum(full.values())
    assert total == 346, f"窗口内决策日 {total} ≠ 346：窗口被改了，先确认 start/end"
    assert full["same"] / total >= 0.945, (
        f"`same` 占比 {full['same'] / total:.3%} 跌破 94.5%：分叉率在上升，"
        "不是窗口变小")



# ---------------------------------------------------------------------------
# 端到端：L1 卡 → 次日结算
# ---------------------------------------------------------------------------

#: L1 的两个口径决定都落在结算里，所以必须用真 bar 走一遍 `settle`：
#: `horizon_sessions=0`（入场日收盘出场）与 `fill_mode="open_only"`（不许盘中补票）。
AS_OF = "2026-01-02"
ENTRY = "2026-01-05"
SYM_OPEN = "600000.SH"      # 低开：开盘 9.90 = 限价，按开盘成交
SYM_INTRADAY = "600001.SH"  # 高开 10.50、盘中最低 9.80：只有 limit 模式会买到
PREV_CLOSE = 10.00
LIMIT = PREV_CLOSE * 0.99   # 9.90


def _write_part(root: Path, iso: str, rows: list[tuple[str, float, float, float, float]]) -> None:
    day = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [date.fromisoformat(iso)] * len(rows),
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [1e4] * len(rows),
            "amount": [1e6] * len(rows),
        }
    )
    enriched = day.with_columns(
        pl.col("close").alias("raw_close"),
        pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
    )
    for table, frame in (("kline_daily", day), ("kline_daily_enriched", enriched)):
        path = root / table / f"date={iso}"
        path.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(path / "part.parquet")


def _seed_two_days(root: Path) -> None:
    """前一日收盘 10.00；次日的两只票分别演示「低开」与「高开回落」。"""
    _write_part(
        root, AS_OF,
        [(SYM_OPEN, PREV_CLOSE, PREV_CLOSE, PREV_CLOSE, PREV_CLOSE),
         (SYM_INTRADAY, PREV_CLOSE, PREV_CLOSE, PREV_CLOSE, PREV_CLOSE)],
    )
    # 复权价 = 原始价（无除权），raw_open 因此直接等于 open。
    _write_part(
        root, ENTRY,
        [(SYM_OPEN, 9.90, 10.40, 9.85, 10.30),
         (SYM_INTRADAY, 10.50, 10.60, 9.80, 10.20)],
    )


def _settle_l1(root: Path, *, fill_mode: str, horizon: int = 0) -> dict[str, dict]:
    from app.decision import ledger, settle

    card = ledger.build_card(
        as_of=AS_OF,
        picks=[
            {"symbol": SYM_OPEN, "limit_price": LIMIT, "p_win": 0.6},
            {"symbol": SYM_INTRADAY, "limit_price": LIMIT, "p_win": 0.6},
        ],
        horizon_sessions=horizon,
        strategy_id="l1_gap_reversal",
        fill_mode=fill_mode,
    )
    ledger.save_card(root, card)
    settle.settle(root)
    return {r["symbol"]: r for r in ledger.load_settlements(root)}


def test_l1_open_only_rejects_intraday_fill(tmp_path: Path) -> None:
    """同样两条 bar：`open_only` 只买低开那只，`limit` 两只都买。

    这是 L1 落地的全部技术风险所在——若允许盘中补票，「高开走弱」的样本会混进
    统计，而那正是研究中亏钱的那一边。
    """
    _seed_two_days(tmp_path)
    open_only = _settle_l1(tmp_path, fill_mode="open_only")

    assert open_only[SYM_OPEN]["fill"]["status"] == "FILLED"
    assert open_only[SYM_OPEN]["fill"]["basis"] == "OPEN"
    assert open_only[SYM_OPEN]["fill"]["price"] == pytest.approx(9.90)
    assert open_only[SYM_INTRADAY]["fill"]["status"] == "NOT_FILLED"
    assert open_only[SYM_INTRADAY]["fill"]["basis"] == "OPEN_ONLY"
    assert open_only[SYM_INTRADAY]["exit"] is None


def test_l1_horizon_zero_exits_same_session(tmp_path: Path) -> None:
    """`horizon_sessions=0` ⇒ 入场日收盘出场，收益 = close[T+1]/open[T+1] − 1。

    这不是「持有 1 天」：那样出场在下下个交易日收盘，多隔一夜，收益口径完全不同。
    """
    _seed_two_days(tmp_path)
    rows = _settle_l1(tmp_path, fill_mode="open_only")
    row = rows[SYM_OPEN]
    assert row["fill"]["date"] == ENTRY
    assert row["exit"]["date"] == ENTRY, "horizon=0 必须当日出场"
    assert row["exit"]["ret"] == pytest.approx(10.30 / 9.90 - 1.0, abs=1e-6)


def test_fill_mode_is_part_of_the_commitment(tmp_path: Path) -> None:
    """两种撮合口径必须是两张卡：否则同一卡号下能换成交判定，预登记就失效了。"""
    from app.decision import ledger

    common = dict(
        as_of=AS_OF,
        picks=[{"symbol": SYM_OPEN, "limit_price": LIMIT, "p_win": 0.6}],
        horizon_sessions=0,
        strategy_id="l1_gap_reversal",
    )
    limit_card = ledger.build_card(**common, fill_mode="limit")
    open_card = ledger.build_card(**common, fill_mode="open_only")
    assert limit_card["card_id"] != open_card["card_id"]
    # 旧卡没有 fill_mode 字段，哈希口径不能因此改变（账本只追加）。
    legacy = ledger.build_card(**common)
    assert legacy["card_id"] == limit_card["card_id"]
    assert ledger.verify_card(legacy)[0]


def test_api_candidates_endpoint_round_trip() -> None:
    """路由必须真的接到域函数：域名对、限价为正、非交易日 400。

    HTTP 层不做业务判断，所以这里只验「接上了」——口径的正确性由上面那些
    域级测试负责。两者分开，才不会让一个 200 掩盖口径分叉。
    """
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.decision import router

    if not (DATA / "kline_daily_enriched").is_dir():
        pytest.skip("缺本地日线分区")
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=DATA))
    client = TestClient(app)

    ok = client.get("/api/decision/l1/candidates", params={"as_of": "2026-11-10"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["strategy_id"] == "l1_gap_reversal"
    assert body["n_candidates"] > 0
    assert all(p["limit_price"] > 0 for p in body["candidates"])

    bad = client.get("/api/decision/l1/candidates", params={"as_of": "2026-11-08"})
    assert bad.status_code == 400, "非交易日必须 400，不能 500 也不能返回空清单"
