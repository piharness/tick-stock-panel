"""LU 腿（涨停次日砸盘反转）的接线判据。

这里断言的六句话，每一句都对应用户会被误导的一种方式：

1. **`limit_price` 是「涨停价 − 0.01」，不是「愿意付的最高价」**：它把
   「开盘 ≤ 限价」这个撮合谓词恒等变换成「开盘没封涨停」。写错一分，一字板
   就会被算成成交——那是不可成交的样本进了收益。
2. **一字板不是信号**：开盘就在涨停价上的「换手板」买不到，把它算进候选
   等于凭空多出一批无法成交的标的。
3. **盘中快照不是日内跌幅**：13:40 的分区行数、字段、唯一性全部正常，
   只有 `quote_ts` 能分开。放行一次，账本里就会多出一条按半天行情算的
   「下跌 5%」。**否决必须落在 `candidates()` 里**，不能只在结算层挡——
   名单一旦落盘，人就会照着挂单。
4. **`horizon_sessions=0` 的出场日就是入场日**：多隔一夜是另一条腿
   （实测那一夜恒负）。
5. **卡的身份只看内容**：`entry_date` 从 None 变成日期、`warnings` 多一条
   「不是最新分区」，都不许把同一份清单变成第二张卡。
6. **同一天重跑是幂等的**：`produce_card` 第二次返回 `exists`，账本里仍是一张卡。

夹具的价格路径是刻意选成「前收盘恒 10.00」的：T 日涨停价因此恒为 11.00，
T+1 收盘 10.34 ⇒ T+2 涨停价 11.37、上限价 11.36。这两个数一旦被写成 12.09，
说明有人在用「T 日收盘 × 1.1」算 T+2 的涨停价——那是两天的基准价，不是一天。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.decision import router
from app.decision import ledger, lu_rebound, producer, settle
from app.decision.settle import _fill
from app.tickflow.repository import DataStore, KlineRepository

BENCH = "000300.SH"
A = "600001.SH"     # 换手板 + 次日可成交
B = "600002.SH"     # 换手板 + 次日一字涨停（买不到）
C = "600003.SH"     # 一字板（不是换手板）
D = "600004.SH"     # 换手板 + 只跌 3%（没到阈值）

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)   # T   = 涨停日
D3 = date(2026, 1, 8)   # T+1 = 被砸日 = 卡的 as_of
D4 = date(2026, 1, 9)   # T+2 = 入场日 = 出场日（horizon=0）

#: D4 的 (开盘, 收盘)。B 的开盘**恰好等于涨停价 11.37**——看得见但买不到。
T2_PATH: dict[str, tuple[float, float]] = {
    A: (10.34, 10.86),      # 平开后涨 5.03%：能成交，也是这条腿唯一赚钱的那种
    B: (11.37, 11.37),      # 一字封死：open == limit_up ⇒ 买不到
    C: (10.90, 11.00),      # 一字板次日炸板后仍走弱：不是信号，也不是跌幅不够
    D: (10.67, 10.80),
}

#: T+1 成交额（元）。**A 与 B 刻意不等**：截断规则是「成交额降序」，所以
#: 生产顺序是 [B, A]，而按代码升序是 [A, B]。两者相等时，端点里悄悄重排一次
#: 也测不出来——而顺序决定 `rank`，也就决定截断到 40 只时谁被留下。
AMOUNT: dict[str, float] = {A: 1.5e8, B: 3.0e8, C: 2.0e8, D: 2.0e8}


def _rows() -> list[dict]:
    """四个标的 × 五天的完整路径。前收盘恒定 10.00，T 日涨停价因此恒为 11.00。"""
    out: list[dict] = []

    def add(symbol: str, day: date, open_: float, close: float) -> None:
        out.append({"symbol": symbol, "date": day, "open": open_, "close": close,
                    "raw_close": close, "raw_low": min(open_, close),
                    # 成交额只在 T+1 有意义（截断按那一天排序），其余日子给同值。
                    "amount": AMOUNT[symbol] if day == D3 else 2e8})

    for symbol in (A, B, C, D):
        add(symbol, D0, 10.0, 10.0)
        add(symbol, D1, 10.0, 10.0)
        add(symbol, D2, 11.0 if symbol == C else 10.5, 11.0)   # T 日涨停
        # T+1：**四只全部回落**（连一字板也炸板），A/B/C 跌 6%+、D 只跌 3%。
        # C 必须也跌破阈值，否则它被剔除的原因就分不清是「一字板」还是
        # 「跌幅不够」——那条 `~yizi` 判据会变成一句永远为真的装饰。
        add(symbol, D3, 11.0, {A: 10.34, B: 10.34, C: 10.30, D: 10.67}[symbol])
        open_, close = T2_PATH[symbol]
        add(symbol, D4, open_, close)
    return out


def _write_market(
    tmp_path: Path,
    *,
    days: list[date] | None = None,
    intraday_on: date | None = None,
) -> None:
    """落一份四标的的市场。

    `days` 用来截断「次日分区还没落盘」的场景；`intraday_on` 给某一天盖上
    盘中 `quote_ts`（10:42:47），模拟实时路径写入的半日快照。
    """
    keep = set(days) if days else {D0, D1, D2, D3, D4}
    rows = [r for r in _rows() if r["date"] in keep]
    # `quote_ts` 是毫秒时间戳，`bars.day_last_quote_ts` 按本地时区还原成 HH:MM:SS。
    # 用同一个本地 `datetime` 反算，往返精确到秒，不受时区影响。
    stamp = None if intraday_on is None else int(
        datetime(intraday_on.year, intraday_on.month, intraday_on.day,
                 10, 42, 47).timestamp() * 1000)

    repo = KlineRepository(DataStore(tmp_path))
    frame = pl.DataFrame({
        "symbol": [r["symbol"] for r in rows],
        "date": [r["date"] for r in rows],
        "open": [r["open"] for r in rows],
        "high": [max(r["open"], r["close"]) for r in rows],
        "low": [r["raw_low"] for r in rows],
        "close": [r["close"] for r in rows],
        "volume": [1e6] * len(rows),
        "amount": [r["amount"] for r in rows],
        "raw_close": [r["raw_close"] for r in rows],
        "raw_high": [max(r["open"], r["close"]) for r in rows],
        "raw_low": [r["raw_low"] for r in rows],
    })
    if stamp is not None:
        # 只有指定的那一天带 quote_ts；其余分区整列缺失 ⇒ `entry_session_state`
        # 对「过去的交易日 + 缺列」判 closed（这是它文档里写明的分支）。
        frame = frame.with_columns(
            pl.when(pl.col("date") == intraday_on).then(pl.lit(stamp))
            .otherwise(None).cast(pl.Int64).alias("quote_ts")
        )
    repo.append_daily(frame.select(
        ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]))
    repo.append_enriched(frame)

    cal = sorted(keep)
    repo.append_index_enriched(pl.DataFrame({
        "symbol": [BENCH] * len(cal),
        "date": cal,
        "open": [4000.0] * len(cal),
        "high": [4000.0] * len(cal),
        "low": [4000.0] * len(cal),
        "close": [4000.0] * len(cal),
        "raw_close": [4000.0] * len(cal),
        "volume": [1e9] * len(cal),
        "amount": [1e12] * len(cal),
    }))

    path = tmp_path / "instruments" / "instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [A, B, C, D],
        "name": ["标的A", "标的B", "标的C", "标的D"],
        # 生产维表的 `listing_date` 是 **String**（实测 dtype，不是 date）：
        # `_derive` 对它做 `.str.to_date()`，写成 date 型会直接抛 SchemaError。
        "listing_date": ["2015-01-01"] * 4,
    }).write_parquet(path)


def _bar_at(raw_price: float) -> dict:
    """一个能还原出 `raw_open == raw_price` 的 bar。

    只喂 `{"raw_open": ...}` 是不行的：`settle._fill` 走的是 `bars.raw_open()`，
    它从 `open × raw_close / close` 派生，缺三列直接返回 None ⇒ 撮合判 INVALID。
    这里让复权因子恰为 1（close == raw_close），派生值就等于喂进去的价格。
    """
    return {"open": raw_price, "close": 1.0, "raw_close": 1.0, "raw_low": raw_price}


# ---------------------------------------------------------------------------
# 1. 上限价的恒等变换
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prev,pct", [(10.0, 0.10), (3.45, 0.10), (26.94, 0.20),
                                     (7.77, 0.30), (1.02, 0.10), (128.8, 0.10)])
def test_buy_cap_is_the_last_tick_below_limit_up(prev: float, pct: float) -> None:
    """`cap + 0.01` 必须恰好等于交易所涨停价，且撮合谓词与之等价。

    这条断言如果只是「cap ≈ limit_up − 0.01」，它抓不到舍入错位——
    固定差值在半分价上会差一分，而那正是「能不能买到」的边界。
    """
    cap = lu_rebound.buy_cap(prev, pct)
    lim = lu_rebound._limit_up_of(prev, pct)
    assert round(cap + 0.01, 2) == lim
    # 开盘 = cap ⇒ 成交（开盘没封死）
    assert _fill(_bar_at(cap), cap, mode="open_only")["status"] == "FILLED"
    # 开盘 = 涨停价 ⇒ 不成交（一字板买不到）
    assert _fill(_bar_at(lim), cap, mode="open_only")["status"] == "NOT_FILLED"


# ---------------------------------------------------------------------------
# 2. 候选口径
# ---------------------------------------------------------------------------


def test_candidates_keep_only_turnover_board_that_was_crashed(tmp_path: Path) -> None:
    _write_market(tmp_path)
    out = lu_rebound.candidates(tmp_path, D3.isoformat())
    assert out["status"] == "ready"
    assert out["entry_date"] == D4.isoformat()
    symbols = [r["symbol"] for r in out["rows"]]
    # C 是一字板（开盘即涨停）⇒ 买不到，不是信号
    # D 只跌了 3% ⇒ 没到阈值
    assert set(symbols) == {A, B}
    # 顺序 = 成交额降序（B 3.0 亿 > A 1.5 亿），不是代码序。
    # 截断到 40 只时先砍掉的是尾部，所以这个顺序就是「谁被留下」。
    assert symbols == [B, A]
    assert [r["rank"] for r in out["rows"]] == [1, 2]
    assert out["n_rows"] == 2 and out["truncated"] == 0

    cap = lu_rebound.buy_cap(10.34, 0.10)          # T+1 收盘才是 T+2 的基准价
    for row in out["rows"]:
        assert row["limit_price"] == cap == 11.36
        assert row["limit_up_price"] == 11.37
        assert row["prev_close_raw"] == 10.34
        assert row["drop_pct"] == pytest.approx(-6.0, abs=1e-3)
        assert row["lu_date"] == D2.isoformat()


def test_candidates_refuse_non_session(tmp_path: Path) -> None:
    """非交易日不是「0 只」，是「这一天不成立」。两者的后续动作相反。"""
    _write_market(tmp_path)
    out = lu_rebound.candidates(tmp_path, "2026-01-10")     # 周六，无分区
    assert out["status"] == "skipped_no_data"
    assert "不是已落盘的交易日" in out["reason"]


def test_candidates_reject_intraday_snapshot(tmp_path: Path) -> None:
    """盘中快照的行数、字段、唯一性全部正常——只有 `quote_ts` 能把它分开。

    放行一次，名单就会在 13:40 落盘，而「日内跌 5%」是按半天行情算的；
    人照着挂单，T+2 的开盘价买到的是一份从未成立过的信号。
    """
    _write_market(tmp_path, intraday_on=D3)
    out = lu_rebound.candidates(tmp_path, D3.isoformat())
    assert out["status"] == "skipped_not_closed"
    assert "盘中" in out["reason"] or "收盘" in out["reason"]
    assert out["n_rows"] == 0


def test_candidates_without_next_partition_still_list_but_has_no_entry(
    tmp_path: Path,
) -> None:
    """次日分区还没落盘：名单仍成立（不能因为「买不到」就否认信号），
    但 `entry_date` 必须为空并在 `warnings` 里说清楚。"""
    _write_market(tmp_path, days=[D0, D1, D2, D3])
    out = lu_rebound.candidates(tmp_path, D3.isoformat())
    assert out["status"] == "ready"
    assert out["entry_date"] is None
    assert any("次日分区尚未落盘" in w for w in out["warnings"])


# ---------------------------------------------------------------------------
# 3. 卡的形状 + 结算（入场日 = 出场日）
# ---------------------------------------------------------------------------


def _produce(tmp_path: Path, as_of: str = D3.isoformat()) -> dict:
    return producer._lu_card(tmp_path, as_of=as_of)


def test_card_carries_fixed_caliber_and_settles_same_day(tmp_path: Path) -> None:
    _write_market(tmp_path)
    out = _produce(tmp_path)
    assert out["status"] == "created", out
    card = ledger.get_card(tmp_path, out["card_id"])
    assert card is not None
    assert card["strategy_id"] == lu_rebound.STRATEGY_ID
    assert card["horizon_sessions"] == 0            # 入场日收盘出场
    assert card["fill_mode"] == "open_only"         # 只在集合竞价成交
    assert ledger.verify_card(card)[0] is True
    # 冷启动没有可见证据：p_win 只能来自先验，且出处必须注明
    assert (card["forecast"]["pwin"] or {}).get("source") == "prior"

    summary = settle.settle(tmp_path)
    assert summary["written"] == 2, summary
    rows = {r["symbol"]: r for r in ledger.load_settlements(tmp_path)}
    a, b = rows[A], rows[B]
    assert a["fill"]["status"] == "FILLED" and a["fill"]["basis"] == "OPEN"
    assert a["exit"]["date"] == a["fill"]["date"] == D4.isoformat()
    assert a["exit"]["ret"] == pytest.approx(10.86 / 10.34 - 1, abs=1e-6)
    # B 的次日开盘就在涨停价上：看得见，买不到。它不是「没发生」。
    assert b["fill"]["status"] == "NOT_FILLED"
    assert b["exit"] is None and b["outcome"] is None


def test_rerun_is_idempotent_and_identity_ignores_drift(tmp_path: Path) -> None:
    _write_market(tmp_path)
    first = _produce(tmp_path)
    second = _produce(tmp_path)
    assert second["status"] == "exists"
    assert second["card_id"] == first["card_id"]
    assert len(ledger.load_cards(tmp_path)) == 1

    # 同一份清单，换了 entry_date 与 warnings —— 仍是同一张卡
    cand = lu_rebound.candidates(tmp_path, D3.isoformat())
    drifted = dict(cand, entry_date=None, warnings=["as_of 不是最新分区"],
                   status="ready")
    assert lu_rebound.fingerprint(drifted) == lu_rebound.fingerprint(cand)
    # 限价差一分 —— 是另一份承诺
    changed = json.loads(json.dumps(cand))
    changed["rows"][0]["limit_price"] = round(changed["rows"][0]["limit_price"] - 0.01, 2)
    assert lu_rebound.fingerprint(changed) != lu_rebound.fingerprint(cand)


# ---------------------------------------------------------------------------
# 4. 接线：端点给的就是 `candidates()` 的那一份，不重算、不包装
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    return TestClient(app)


def test_endpoint_returns_the_same_list_as_candidates(tmp_path: Path) -> None:
    """端点必须是 `candidates()` 的**同一个答案**。

    这一条防的是「HTTP 层重算」：一旦端点在别处再算一份，页面上的限价与卡上的
    限价就有了第二个定义点，而没有任何东西会红——两份都自洽，只是不一样。
    """
    _write_market(tmp_path)
    resp = _client(tmp_path).get("/api/decision/lu-rebound/candidates",
                                params={"as_of": D3.isoformat()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    local = lu_rebound.candidates(tmp_path, D3.isoformat())
    assert body["status"] == local["status"] == "ready"
    assert body["rows"] == local["rows"]
    assert body["caliber"] == local["caliber"]


def test_endpoint_reports_terminal_states_instead_of_500(tmp_path: Path) -> None:
    """「这一天不成立」是一个正常答案，不是服务器错误。"""
    _write_market(tmp_path)
    client = _client(tmp_path)
    body = client.get("/api/decision/lu-rebound/candidates",
                      params={"as_of": "2026-01-10"}).json()
    assert body["status"] == "skipped_no_data"
    assert body["reason"]


def test_endpoint_is_409_when_the_universe_table_is_missing(tmp_path: Path) -> None:
    """缺维表 ⇒ 剔不了 ST 与次新 ⇒ fail-closed。

    不抛 400：请求没有写错，是数据不全。两者的处置相反——改请求 vs 补数据。
    """
    _write_market(tmp_path)
    (tmp_path / "instruments" / "instruments.parquet").unlink()
    resp = _client(tmp_path).get("/api/decision/lu-rebound/candidates",
                                 params={"as_of": D3.isoformat()})
    assert resp.status_code == 409, resp.text
    assert "维表" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 4. 接线：端点给的就是 `candidates()` 的那一份，不重算、不包装
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    return TestClient(app)


def test_endpoint_returns_the_same_list_as_candidates(tmp_path: Path) -> None:
    """端点必须是 `candidates()` 的**同一个答案**。

    这一条防的是「HTTP 层重算」：一旦端点在别处再算一份，页面上的限价与卡上的
    限价就有了第二个定义点，而没有任何东西会红——两份都自洽，只是不一样。
    """
    _write_market(tmp_path)
    resp = _client(tmp_path).get("/api/decision/lu-rebound/candidates",
                                params={"as_of": D3.isoformat()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    local = lu_rebound.candidates(tmp_path, D3.isoformat())
    assert body["status"] == local["status"] == "ready"
    assert body["rows"] == local["rows"]
    assert body["caliber"] == local["caliber"]


def test_endpoint_reports_terminal_states_instead_of_500(tmp_path: Path) -> None:
    """「这一天不成立」是一个正常答案，不是服务器错误。"""
    _write_market(tmp_path)
    client = _client(tmp_path)
    body = client.get("/api/decision/lu-rebound/candidates",
                      params={"as_of": "2026-01-10"}).json()
    assert body["status"] == "skipped_no_data"
    assert body["reason"]


def test_endpoint_is_409_when_the_universe_table_is_missing(tmp_path: Path) -> None:
    """缺维表 ⇒ 剔不了 ST 与次新 ⇒ fail-closed。

    不抛 400：请求没有写错，是数据不全。两者的处置相反——改请求 vs 补数据。
    """
    _write_market(tmp_path)
    (tmp_path / "instruments" / "instruments.parquet").unlink()
    resp = _client(tmp_path).get("/api/decision/lu-rebound/candidates",
                                 params={"as_of": D3.isoformat()})
    assert resp.status_code == 409, resp.text
    assert "维表" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. 管道接线：第二条腿只认自己的开关
# ---------------------------------------------------------------------------


def _auto_cfg(**kw) -> dict:
    base = dict(enabled=False, strategy_id="", top_n=20, horizon=20, basis="median",
                account_id="default", allow_prior=False, fill_mode="limit",
                execute=False)
    base.update(kw)
    return base


def test_stage_runs_lu_leg_when_primary_leg_is_off(tmp_path: Path, monkeypatch) -> None:
    """「开关打开但什么也没发生」是这条腿最可能的失败方式。

    LU 分支原先挂在主腿 `produce_card` 之后，而主腿有四个早退出口
    （未配策略 / 无引擎 / 扫描失败 / 名单没落盘）都会 `return`。主腿一关
    （`decision_auto_enabled=false` 就是默认值），第二腿的开关就成了装饰：
    界面上它是开的，账本里一张卡也没有，而且没有任何东西会红。

    这里同时钉住结算：只开 LU 时若沿用旧的 `disabled` 出口，连结算都不跑，
    卡发得出去、永远不结算——前向证据一条也攒不到。
    """
    from app.services import preferences

    _write_market(tmp_path)
    monkeypatch.setattr(preferences, "get_decision_auto_config", lambda: _auto_cfg())
    monkeypatch.setattr(preferences, "get_decision_lu_config", lambda: {"enabled": True})
    repo = KlineRepository(DataStore(tmp_path))

    out = producer.run_decision_stage(repo, engine=None, today=D3)
    assert out["status"] == "disabled"                    # 主腿确实关着
    assert out["lu_rebound"]["status"] == "created", out
    assert "settle" in out                                # 结算真的跑了
    assert len(ledger.load_cards(tmp_path)) == 1


def test_stage_carries_lu_result_through_primary_early_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """主腿空转（未配策略）时，第二腿的结论不许被那个 `return` 一起丢掉。"""
    from app.services import preferences

    _write_market(tmp_path)
    monkeypatch.setattr(preferences, "get_decision_auto_config",
                        lambda: _auto_cfg(enabled=True))
    monkeypatch.setattr(preferences, "get_decision_lu_config", lambda: {"enabled": True})
    repo = KlineRepository(DataStore(tmp_path))

    out = producer.run_decision_stage(repo, engine=None, today=D3)
    assert out["status"] == "no_strategy", out
    assert out["lu_rebound"]["status"] == "created", out
    assert "slate" in out and "settle" in out


# ---------------------------------------------------------------------------
# 5. 管道接线：第二条腿只认自己的开关
# ---------------------------------------------------------------------------


def _auto_cfg(**kw) -> dict:
    base = dict(enabled=False, strategy_id="", top_n=20, horizon=20, basis="median",
                account_id="default", allow_prior=False, fill_mode="limit",
                execute=False)
    base.update(kw)
    return base


def test_stage_runs_lu_leg_when_primary_leg_is_off(tmp_path: Path, monkeypatch) -> None:
    """「开关打开但什么也没发生」是这条腿最可能的失败方式。

    LU 分支原先挂在主腿 `produce_card` 之后，而主腿有四个早退出口
    （未配策略 / 无引擎 / 扫描失败 / 名单没落盘）都会 `return`。主腿一关
    （`decision_auto_enabled=false` 就是默认值），第二腿的开关就成了装饰：
    界面上它是开的，账本里一张卡也没有，而且没有任何东西会红。

    这里同时钉住结算：只开 LU 时若沿用旧的 `disabled` 出口，连结算都不跑，
    卡发得出去、永远不结算——前向证据一条也攒不到。
    """
    from app.services import preferences

    _write_market(tmp_path)
    monkeypatch.setattr(preferences, "get_decision_auto_config", lambda: _auto_cfg())
    monkeypatch.setattr(preferences, "get_decision_lu_config", lambda: {"enabled": True})
    repo = KlineRepository(DataStore(tmp_path))

    out = producer.run_decision_stage(repo, engine=None, today=D3)
    assert out["status"] == "disabled"                    # 主腿确实关着
    assert out["lu_rebound"]["status"] == "created", out
    assert "settle" in out                                # 结算真的跑了
    assert len(ledger.load_cards(tmp_path)) == 1


def test_stage_carries_lu_result_through_primary_early_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """主腿空转（未配策略）时，第二腿的结论不许被那个 `return` 一起丢掉。"""
    from app.services import preferences

    _write_market(tmp_path)
    monkeypatch.setattr(preferences, "get_decision_auto_config",
                        lambda: _auto_cfg(enabled=True))
    monkeypatch.setattr(preferences, "get_decision_lu_config", lambda: {"enabled": True})
    repo = KlineRepository(DataStore(tmp_path))

    out = producer.run_decision_stage(repo, engine=None, today=D3)
    assert out["status"] == "no_strategy", out
    assert out["lu_rebound"]["status"] == "created", out
    assert "slate" in out and "settle" in out
