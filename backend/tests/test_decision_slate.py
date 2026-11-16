"""每日名单（daily slate）的判据。

这一组只回答三个问题，每个都对应一条真实发生过的失败：

1. **「今天买哪几只」是不是只有一个答案？**
   `test_producer_card_picks_equal_slate_rows` 断言卡的 picks 与名单逐位相等，
   且 `test_rejects_non_l1_top_n` 断言条数不能被配置改成别的策略。
   在收敛之前，策略页显示 100 只、卡片 20 只、助手 30 只、L1 20 只，
   没有任何一处会因为「两份名单不相等」而变红。

2. **「旧了」会不会被写成「没问题」？**
   `test_staleness_is_recomputed_at_read_time` 拿一份 `stale_at_build=false`
   的旧文件，断言读取时仍然判 stale。2026-11-15 的现场：enriched 已到 11-13，
   最新卡停在 11-11，页面照常显示「已封存 10 张卡」。

3. **API 会不会偷偷重算？**
   `test_api_reads_disk_and_does_not_recompute` 把 `l1_cohort.candidates`
   换成抛异常，断言端点仍返回 200。HTTP 层重算 = 第二产地。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.decision import router
from app.decision import bars, l1_cohort, ledger, producer, slate
from app.tickflow.repository import DataStore, KlineRepository

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

SYM = "600519.SH"
BENCH = "000300.SH"
START = date(2026, 1, 5)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path, n: int = 40) -> list[date]:
    days = [START + timedelta(days=i) for i in range(n)]
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame({
        "symbol": [SYM] * n, "date": days,
        "open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n,
        "close": [100.0] * n, "volume": [1e4] * n, "amount": [1e6] * n,
    }))
    repo.append_enriched(pl.DataFrame({
        "symbol": [SYM] * n, "date": days,
        "open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n,
        "close": [110.0 if i > 1 else 100.0 for i in range(n)],
        "volume": [1e4] * n, "amount": [1e6] * n,
        "raw_close": [110.0 if i > 1 else 100.0 for i in range(n)],
        "raw_high": [110.0] * n, "raw_low": [100.0] * n,
    }))
    repo.append_index_enriched(pl.DataFrame({
        "symbol": [BENCH] * n, "date": days,
        "open": [4000.0] * n, "high": [4000.0] * n, "low": [4000.0] * n,
        "close": [4000.0] * n, "raw_close": [4000.0] * n,
        "volume": [1e9] * n, "amount": [1e12] * n,
    }))
    return days


def _prefs(**kw):
    base = dict(enabled=False, strategy_id="", top_n=20, horizon=0, basis="abs",
                account_id="default", allow_prior=False, fill_mode="limit",
                execute=False)
    base.update(kw)
    return base


def _write_slate(tmp_path: Path, **kw) -> Path:
    payload = {
        "schema_revision": slate.SLATE_SCHEMA_REVISION,
        "as_of": "2026-11-13",
        "entry_date": None,
        "strategy_id": l1_cohort.L1_STRATEGY_ID,
        "status": slate.STATUS_READY,
        "top_n": 20,
        "n_rows": 1,
        "truncation": {"top_n": 20, "rule": "test", "source": "test"},
        "rows": [{
            "symbol": SYM, "rank": 1, "mom20_pct": 3.0, "prev_close_raw": 100.0,
            "limit_price": 99.0, "gap_threshold_pct": -1.0, "day_amount_yi": 1.0,
            "tradable": True,
        }],
        "caliber": {},
        "panel_since": None,
        "warnings": [],
        "stale_at_build": False,
        "lag_sessions_at_build": 0,
        "expected_as_of_at_build": "2026-11-13",
        "built_at": "2026-11-13T16:00:00+08:00",
        "first_built_at": "2026-11-13T16:00:00+08:00",
    }
    payload.update(kw)
    path = slate.slate_path(tmp_path, str(payload["as_of"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. 截断：唯一且不可配
# ---------------------------------------------------------------------------


def test_rejects_non_l1_top_n():
    """L1 的 20 只是研究口径，不是显示条数。

    改它等于换策略（`_diag_l1_vwap_curvature.py` 同值 20）。所以直接拒绝，
    而不是夹紧到 20 或按 10 只算——后者会让「策略没变、只是少看几只」
    这种说法成立，而实际上名单已经换了一批。
    """
    out = slate.build_slate(DATA, "2026-11-13", top_n=10)
    assert out["status"] == slate.STATUS_INVALID_TOP_N
    assert out["rows"] == []
    assert "换策略" in str(out.get("reason"))


def test_truncation_block_matches_rows():
    """落盘的 `truncation.top_n` 必须与实际行数口径一致（行数可少，不可多）。"""
    out = slate.build_slate(DATA, "2026-11-13")
    assert out["status"] in slate.READY_STATES
    assert out["truncation"]["top_n"] == out["top_n"] == l1_cohort.L1_TOP_N
    assert len(out["rows"]) <= out["top_n"]
    assert out["n_rows"] == len(out["rows"])


# ---------------------------------------------------------------------------
# 2. 新鲜度：读取时刻重算，不信文件
# ---------------------------------------------------------------------------


def _mk_partitions(tmp_path: Path, dates: list[str]) -> None:
    base = tmp_path / bars.STOCK_ENRICHED
    for d in dates:
        (base / f"date={d}").mkdir(parents=True, exist_ok=True)


def test_staleness_is_recomputed_at_read_time(tmp_path: Path):
    """文件里写 `stale_at_build=false` 不算数。

    构造：名单 as_of=11-12，磁盘上已有 11-13。读取时 lag 必须是 1、
    stale 必须是 True，且入参不被修改（纯函数）。
    """
    _mk_partitions(tmp_path, ["2026-11-12", "2026-11-13"])
    old = {"as_of": "2026-11-12", "stale_at_build": False,
           "lag_sessions_at_build": 0}
    out = slate.annotate_staleness(tmp_path, old)
    assert out["stale"] is True
    assert out["lag_sessions"] == 1
    assert out["expected_as_of"] == "2026-11-13"
    assert "stale" not in old  # 不原地改写


def test_status_and_stale_are_orthogonal(tmp_path: Path):
    """`ready` + `stale` 必须能同时成立。

    合并成一个字段就会出现「名单有效」被读成「名单最新」——
    11-15 现场正是如此：卡是有效的，但它落后两个交易日。
    """
    _mk_partitions(tmp_path, ["2026-11-13"])
    out = slate.annotate_staleness(tmp_path, {"as_of": "2026-11-12",
                                              "status": slate.STATUS_READY})
    assert out["status"] == slate.STATUS_READY
    assert out["stale"] is True


def test_both_axes_take_all_four_combinations(tmp_path: Path):
    """四格都要能出现，且彼此不带动。

    只钉「ready + stale」一格，等于只证明了一种巧合。真正要证明的是
    `annotate_staleness()` **只碰新鲜度三个字段**：`status` 由文件决定，
    读的时候不许因为「旧了」被改写，也不许因为「新」就把一个未成立的名单
    说成成立。四格逐一列出，任何一格缺失都说明两根轴粘上了。
    """
    _mk_partitions(tmp_path, ["2026-11-12", "2026-11-13"])   # 11-13 已落盘
    cases = [
        ("2026-11-13", slate.STATUS_READY, False),    # 最新且成立
        ("2026-11-12", slate.STATUS_READY, True),     # 成立但落后一天
        ("2026-11-13", slate.STATUS_FAILED, False),   # 未成立，但确实是最新那次
        ("2026-11-12", slate.STATUS_FAILED, True),    # 未成立且落后
    ]
    for as_of, status, want_stale in cases:
        out = slate.annotate_staleness(tmp_path, {"as_of": as_of, "status": status})
        assert out["status"] == status, "新鲜度改写了状态"
        assert out["stale"] is want_stale, (as_of, status, out)


def test_old_schema_is_treated_as_unreadable(tmp_path: Path):
    """结构版本不认 ⇒ 视为没有名单，不能拿旧 rows 当新口径。"""
    path = slate.slate_path(tmp_path, "2026-11-13")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_revision": 0, "as_of": "2026-11-13",
                                "rows": [{"symbol": "X"}]}), encoding="utf-8")
    assert slate.read_slate(tmp_path) is None


# ---------------------------------------------------------------------------
# 3. 名单 → picks：唯一的桥
# ---------------------------------------------------------------------------


def test_picks_from_slate_keeps_limit_price_and_drops_unusable():
    sl = {"rows": [
        {"symbol": "A", "limit_price": 9.99},
        {"symbol": "B", "limit_price": None},   # 没有限价 = 无法撮合
        {"symbol": None, "limit_price": 1.0},
    ]}
    assert slate.picks_from_slate(sl) == [{"symbol": "A", "limit_price": 9.99}]


def test_producer_card_picks_equal_slate_rows(tmp_path: Path, monkeypatch):
    """卡与名单逐位相等——这是**构造性**的，不是对拍出来的。

    `producer._l1_picks` 读刚落盘的名单，不再自己跑 `candidates()`。
    所以这条测试同时钉住两件事：卡没有第二份名单，且卡记录的是名单的 as_of
    （不是调用方传的 today）。
    """
    from app.services import preferences

    days = _seed(tmp_path, n=40)
    monkeypatch.setattr(
        preferences, "get_decision_auto_config",
        lambda: _prefs(enabled=True, strategy_id=l1_cohort.L1_STRATEGY_ID,
                       horizon=0, allow_prior=True))
    repo = KlineRepository(DataStore(tmp_path))
    out = producer.run_decision_stage(repo, engine=None, today=days[30])
    assert out["status"] == "created", out

    written = slate.read_slate(tmp_path)
    assert written is not None
    card = ledger.load_cards(tmp_path)[0]

    assert [p["symbol"] for p in card["picks"]] == \
        [r["symbol"] for r in written["rows"]]
    assert [p["limit_price"] for p in card["picks"]] == \
        [r["limit_price"] for r in written["rows"]]
    assert str(card["as_of"]) == written["as_of"]
    assert (card.get("forecast") or {}).get("slate", {}).get("status") == "ready"


# ---------------------------------------------------------------------------
# 4. API：只读磁盘
# ---------------------------------------------------------------------------


def test_api_reads_disk_and_does_not_recompute(client: TestClient, tmp_path: Path,
                                               monkeypatch):
    _write_slate(tmp_path)
    monkeypatch.setattr(
        l1_cohort, "candidates",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("API 不该重算名单")),
    )
    r = client.get("/api/decision/slate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slate"]["as_of"] == "2026-11-13"
    assert body["slate"]["rows"][0]["symbol"] == SYM
    assert body["card"] is None


def test_api_404_instead_of_falling_back(client: TestClient, tmp_path: Path):
    """指定日期没有名单就 404，不降级到最新——降级会把「那天没算」
    静默改写成「那天算出来的是这一份」。"""
    _write_slate(tmp_path)
    r = client.get("/api/decision/slate?as_of=2026-11-12")
    assert r.status_code == 404
    assert "名单不存在" in r.json()["detail"]


def test_api_reports_stale_without_changing_status(client: TestClient,
                                                   tmp_path: Path):
    """名单是 11-12 的，磁盘已有 11-13：status 仍是 ready，stale=true。"""
    _write_slate(tmp_path, as_of="2026-11-12")
    base = tmp_path / bars.STOCK_ENRICHED
    for d in ("2026-11-12", "2026-11-13"):
        (base / f"date={d}").mkdir(parents=True, exist_ok=True)
    r = client.get("/api/decision/slate")
    assert r.status_code == 200, r.text
    sl = r.json()["slate"]
    assert sl["status"] == "ready"
    assert sl["stale"] is True
    assert sl["lag_sessions"] == 1
    assert sl["expected_as_of"] == "2026-11-13"


def test_api_serves_the_latest_ready_list_when_the_head_failed(client: TestClient,
                                                              tmp_path: Path):
    """最近一次运行没成立 ⇒ 主表仍是最近一份**成立**的名单，head 单独说原因。

    这是「可执行」与「最新」两根轴在接口层的分工：
    - `slate` 回答「明早照哪份挂单」，今天没算就用昨天那份（它仍是有效的）；
    - `head` 回答「最近一次运行怎么样」，成不成立都要给。
    把两者合并，要么藏掉唯一可执行的名单，要么把昨天的名单说成今天的。
    """
    base = tmp_path / bars.STOCK_ENRICHED
    for d in ("2026-11-12", "2026-11-13"):
        (base / f"date={d}").mkdir(parents=True, exist_ok=True)
    _write_slate(tmp_path, as_of="2026-11-12")
    _write_slate(tmp_path, as_of="2026-11-13", status=slate.STATUS_SKIPPED_NOT_CLOSED,
                 rows=[], n_rows=0, reason="quote_ts=11:20:00 早于 15:00:00，是盘中快照")

    body = client.get("/api/decision/slate").json()
    assert body["slate"]["as_of"] == "2026-11-12"
    assert body["slate"]["status"] == "ready"
    assert body["slate"]["rows"][0]["symbol"] == SYM
    assert body["head"]["as_of"] == "2026-11-13"
    assert body["head"]["status"] == "skipped_not_closed"
    assert body["series"]["sessions"] == 2
    assert body["series"]["holes"] == ["2026-11-13"]

    # 指定日期时不许被「最新成立」覆盖：读者点的是那一天。
    exact = client.get("/api/decision/slate?as_of=2026-11-13").json()
    assert exact["slate"]["status"] == "skipped_not_closed"
    assert exact["slate"]["rows"] == []


# ---------------------------------------------------------------------------
# 5. 真实数据锚
# ---------------------------------------------------------------------------


def test_limit_price_is_tick_aligned_to_the_cent():
    """挂单价必须落在 0.01 的格子上，**表示也要干净**。

    这是与「历史卡逐位相同」分开的一条判据。`_verify_slate_vs_cards.py`
    用 1e-6 容差比较口径（0.01 的整数倍之间，真分叉至少差一分钱），
    所以它放得过 `13.040000000000001`——而那个值会进 JSON、进 API、进前端，
    让「同一份名单」与历史卡永远差 1e-15。两条判据各管一件事：

    - 这条：生产值本身是 tick 的整数倍（严格相等）；
    - 那条：跨版本的名单同序同价（容差）。
    """
    out = slate.build_slate(DATA)
    assert out["rows"], "没有名单可比对——判据未执行"
    for row in out["rows"]:
        v = row["limit_price"]
        assert v == round(v, 2), f"{row['symbol']} 限价 {v!r} 不是 0.01 的整数倍"
        assert abs(v * 100 - round(v * 100)) < 1e-6, row


def test_real_slate_tracks_latest_complete_session():
    """真数据上 `as_of` 必须等于 enriched 的最新完整交易日。

    这条会在「管道没跑」时变红——而它正是 11-12/11-13 两天没人发现的缺口。
    允许滞后 0：名单要么是最新，要么 stale 字段会说出来；不允许的是
    「以为是最新」。
    """
    out = slate.build_slate(DATA)
    assert out["status"] in slate.READY_STATES, out
    latest = bars.latest_session(DATA, bars.STOCK_ENRICHED)
    assert out["as_of"] == latest
    assert out["n_rows"] == len(out["rows"]) <= l1_cohort.L1_TOP_N
    # 名单里的每一行都必须能变成 pick。这一段钉住「下游偷偷再截一刀」——
    # 引擎 limit / 助手 30 行 / 卡 top_n 的历史分叉都长在这个缝里。
    assert len(slate.picks_from_slate(out)) == out["n_rows"]
    for row in out["rows"]:
        # 限价必须是 0.01 向下取整，不是四舍五入（会多买 gap > -1% 的票）。
        expected = int(row["prev_close_raw"] * 99 + 1e-9) / 100
        assert abs(row["limit_price"] - expected) < 1e-9, row


def test_freshness_cache_invalidates_when_partition_appears(tmp_path: Path):
    """新增分区后必须重新列举——缓存失效靠父目录 mtime，不靠手工 clear。

    这条防的是「read-your-write 不成立」：管道刚落完当天分区，页面却还按
    上一次列举的结果说「已是最新」。MEMORY 里那类「34 处手工调用 clear()」
    的缓存，缺的就是这条证明。

    注意本函数此前在文件里**定义了两遍**（同内容）。Python 静默让后者覆盖前者，
    pytest 只收一条——改前一份等于没改，而没有任何东西会红。合并成一条时
    顺手把它钉在文件末尾：重复定义在这里不是风格问题，是判据失效。
    """
    _mk_partitions(tmp_path, ["2026-11-12"])
    first = slate.annotate_staleness(tmp_path, {"as_of": "2026-11-12"})
    assert first["lag_sessions"] == 0

    _mk_partitions(tmp_path, ["2026-11-13"])
    second = slate.annotate_staleness(tmp_path, {"as_of": "2026-11-12"})
    assert second["lag_sessions"] == 1, "新增分区后缓存没有失效"
    assert second["expected_as_of"] == "2026-11-13"


# ---------------------------------------------------------------------------
# 6. 序列：一份文件不是「每日名单」
# ---------------------------------------------------------------------------


def test_series_separates_missing_from_not_ready(tmp_path: Path):
    """缺口的三种形态必须分开，它们的下一步动作不同。

    构造：三个交易日，只有第一天成立。
    - 第二天：文件在、status=failed ⇒ `not_ready`（原因在文件里）
    - 第三天：没有文件 ⇒ `missing`（管道没跑）
    把两者压成「缺失」，就分不清「要不要重跑」和「要不要查为什么失败」。
    """
    days = ["2026-11-11", "2026-11-12", "2026-11-13"]
    _mk_partitions(tmp_path, days)
    _write_slate(tmp_path, as_of=days[0])
    _write_slate(tmp_path, as_of=days[1], status=slate.STATUS_FAILED,
                 rows=[], n_rows=0, reason="boom")

    view = slate.series(tmp_path, lookback=10)
    assert view["sessions"] == 3
    assert view["ready"] == 1
    assert view["missing"] == [days[2]]
    assert view["not_ready"] == [days[1]]
    assert view["holes"] == [days[1], days[2]]
    assert view["head"]["as_of"] == days[2]
    assert view["latest_ready"]["as_of"] == days[0]


def test_series_treats_old_schema_as_unreadable_not_missing(tmp_path: Path):
    """文件在但结构版本不认 ⇒ `unreadable`，不是 `missing`。

    「没有文件」和「有文件但读不懂」都要重算，但前者要问管道为什么没跑，
    后者是旧口径的遗留——把它报成缺失，等于让一次结构升级看起来像管道停摆。
    """
    _mk_partitions(tmp_path, ["2026-11-13"])
    path = slate.slate_path(tmp_path, "2026-11-13")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_revision": 0, "as_of": "2026-11-13"}),
                    encoding="utf-8")
    view = slate.series(tmp_path, lookback=5)
    assert view["missing"] == []
    assert view["unreadable"] == ["2026-11-13"]
    assert view["holes"] == ["2026-11-13"]


def test_build_refuses_a_non_session_date(tmp_path: Path):
    """`as_of` 不是已落盘的交易日 ⇒ 不按「0 只」处理。

    盘后管道的 `today` 在周末/节假日正是一个非交易日：少了这条，
    每天都会多出一份声称「今天 0 只满足条件」的名单，而那天根本没开盘。
    `empty` 是策略结论，`skipped_no_data` 是数据事实——两者不能互换。
    """
    _mk_partitions(tmp_path, ["2026-11-12", "2026-11-13"])
    out = slate.build_slate(tmp_path, "2026-11-14")   # 周六
    assert out["status"] == slate.STATUS_SKIPPED_NO_DATA
    assert out["rows"] == []
    assert "不是已落盘的交易日" in out["reason"]


def test_backfill_fills_holes_and_leaves_ready_alone(tmp_path: Path):
    """补算只补洞：已成立的那一天不重写（`built_at` 不变即证明）。

    幂等是它敢每天多跑几次、也敢随时手工补的前提。若每次都重写，
    「这份名单什么时候就有了」会被当天的时间戳反复覆盖。
    """
    days = _seed(tmp_path, n=40)
    day = days[39].isoformat()
    first = slate.build_slate(tmp_path, day)
    assert first["status"] in slate.READY_STATES
    path = slate.write_slate(tmp_path, first)
    before = json.loads(path.read_text(encoding="utf-8"))["built_at"]

    out = slate.backfill(tmp_path, lookback=2)
    assert out["failed"] == []
    assert day in out["skipped"], out
    after = json.loads(path.read_text(encoding="utf-8"))["built_at"]
    assert after == before, "已成立的日子被重写了"

    # 窗口内其余交易日应被补齐，且标成 backfill。
    view = slate.series(tmp_path, lookback=2)
    assert view["holes"] == [], view
    for entry in view["days"]:
        assert entry["present"]
        assert entry["status"] in slate.READY_STATES
    assert slate.read_slate(tmp_path, days[38].isoformat())["origin"] == slate.ORIGIN_BACKFILL


def test_producer_never_falls_back_to_an_older_slate(tmp_path: Path, monkeypatch):
    """今天的名单没算出来 ⇒ 今天不发卡，**不许**拿昨天的名单顶上。

    回退分支看起来是兜底，实际是把「今天没算」改写成「今天用昨天的名单」：
    卡上写昨天的日期与昨天的限价，而调用方以为它是今天的。
    而且这个兜底永远生效——目录里最新那份名单总是存在。
    """
    from app.services import preferences

    days = _seed(tmp_path, n=40)
    older = slate.build_slate(tmp_path, days[29].isoformat())
    assert older["status"] in slate.READY_STATES
    slate.write_slate(tmp_path, older)

    monkeypatch.setattr(
        preferences, "get_decision_auto_config",
        lambda: _prefs(enabled=True, strategy_id=l1_cohort.L1_STRATEGY_ID,
                       horizon=0, allow_prior=True))
    monkeypatch.setattr(
        slate, "build_slate",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("构建失败")))

    repo = KlineRepository(DataStore(tmp_path))
    out = producer.run_decision_stage(repo, engine=None, today=days[30])
    assert out["status"] == slate.STATUS_FAILED, out
    assert ledger.load_cards(tmp_path) == [], "回退到旧名单发了一张日期不对的卡"


def test_slate_identity_in_the_card_is_content_not_build_time(tmp_path: Path):
    """同一批单重算一次名单，必须落回同一个 `card_id`。

    名单的身份用**内容指纹**，不用 `built_at`。否则补跑（16:40 的二次成型、
    手工 --backfill、写盘失败后重试）每多跑一次就多一张卡：同一份证据在
    Brier 里被算两遍，而两张卡的 picks 一模一样，看起来完全正常。

    **只改 `built_at`** 是关键：两次 `run_decision_stage` 若在同一秒内跑完，
    `built_at` 相同，用 `built_at` 当身份也能「通过」——那种测试什么都没证明。
    这里显式把构建时刻推到一小时后，再比身份。

    变异检验：把 `fingerprint(slate)` 换回 `slate["built_at"]`，本测试变红。
    """
    days = _seed(tmp_path, n=40)
    day = days[30].isoformat()
    slate.write_slate(tmp_path, slate.build_slate(tmp_path, day))

    picks_a, meta_a, err = producer._l1_picks(tmp_path, day)
    assert err is None and picks_a

    payload = slate.read_slate(tmp_path, day)
    payload["built_at"] = "2026-01-01T16:40:00+08:00"   # 模拟「一小时后重算」
    slate.write_slate(tmp_path, payload)

    picks_b, meta_b, err = producer._l1_picks(tmp_path, day)
    assert err is None
    assert picks_a == picks_b
    assert meta_a["slate"] == meta_b["slate"], "构建时刻进了名单身份"

    common = dict(as_of=day, picks=picks_a, horizon_sessions=0,
                  strategy_id=l1_cohort.L1_STRATEGY_ID,
                  fill_mode=ledger.FILL_MODE_OPEN_ONLY)
    # `_l1_picks` 给的是名单原样的 picks；`p_win` 由 `produce_card` 之后补上。
    # 这里直接建卡，所以要自己补——用常数即可：本测试问的是卡的身份，
    # 不是 p_win 的出处。
    card_a = ledger.build_card(
        forecast=dict(meta_a), **{**common, "picks": [dict(p, p_win=0.5) for p in picks_a]})
    card_b = ledger.build_card(
        forecast=dict(meta_b), **{**common, "picks": [dict(p, p_win=0.5) for p in picks_b]})
    assert card_a["card_id"] == card_b["card_id"]

    # 反向：内容真的变了（限价差一分）必须换 id——那是另一份承诺。
    moved = [dict(p, p_win=0.5) for p in picks_a]
    moved[0]["limit_price"] = float(moved[0]["limit_price"]) - 0.01
    card_c = ledger.build_card(forecast=dict(meta_a), **{**common, "picks": moved})
    assert card_c["card_id"] != card_a["card_id"]


def test_second_run_does_not_create_a_second_card(tmp_path: Path, monkeypatch):
    """盘后二次成型（16:40/18:10）跑第二遍不许多出一张卡。

    `run_decision_stage` 是幂等阶段：名单重写、结算跳过、发卡落回同一 id。
    这条与上面那条分工不同——上面钉「身份由什么决定」，这条钉「阶段本身可重入」。
    """
    from app.services import preferences

    days = _seed(tmp_path, n=40)
    monkeypatch.setattr(
        preferences, "get_decision_auto_config",
        lambda: _prefs(enabled=True, strategy_id=l1_cohort.L1_STRATEGY_ID,
                       horizon=0, allow_prior=True))
    repo = KlineRepository(DataStore(tmp_path))

    first = producer.run_decision_stage(repo, engine=None, today=days[30])
    assert first["status"] == "created", first
    second = producer.run_decision_stage(repo, engine=None, today=days[30])
    assert second["status"] == "exists", second
    cards = ledger.load_cards(tmp_path)
    assert len(cards) == 1, [c["card_id"] for c in cards]
    assert cards[0]["card_id"] == first["card_id"]


def test_real_data_has_no_hole_before_the_newest_session():
    """真数据上，最近 10 个交易日里（除最新一日）不许有洞。

    这是「盘后自动生成」唯一可执行的判据：`build_slate()` 能算出来不等于
    管道写过——本判据读磁盘，不重算。

    去掉最新一日是有意的：盘后管道 15:35 跑、数据可能 15:31 才定稿，
    这一天的名单本来就可能还没成立；把它算成洞会让本测试在每个交易日
    下午恒红一次，恒红等于没有门。更久以前的洞由页面（20 天覆盖条）与
    `python -m app.decision.slate --coverage` 负责。
    """
    view = slate.series(DATA, lookback=10)
    assert view["sessions"] >= 2, "窗口里没有足够交易日，判据未执行"
    head = (view["head"] or {}).get("as_of")
    holes = [d for d in view["holes"] if d != head]
    assert not holes, (
        f"这些交易日没有可执行名单：{holes}。"
        "补算：python -m app.decision.slate --backfill --lookback 20"
    )
