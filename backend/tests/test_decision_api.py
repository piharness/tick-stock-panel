"""决策账本 API 契约测试 — 成功 / 无数据 / 冲突 / 校验失败。

镜像 test_paper_api.py：只挂本路由的裸 FastAPI app，不经过主应用全局中间件。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.decision import router
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
BENCH = "000300.SH"
START = date(2026, 1, 5)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _seed(tmp_path: Path, n: int = 25) -> list[date]:
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


def _body(as_of: str, **kw) -> dict:
    return {
        "as_of": as_of,
        "picks": [{"symbol": SYM, "limit_price": 100.0, "p_win": 0.6}],
        "horizon_sessions": 5,
        **kw,
    }


def test_overview_without_data_is_unknown_not_pass(client: TestClient):
    r = client.get("/api/decision/overview")
    assert r.status_code == 200
    body = r.json()
    k4 = next(c for c in body["gates"]["checks"] if c["id"] == "K4_DATA_STALE")
    assert k4["status"] == "UNKNOWN"
    assert body["gates"]["decision"] == "GO_WITH_UNKNOWNS"


def test_create_card_then_idempotent(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path)
    r = client.post("/api/decision/cards", json=_body(days[0].isoformat()))
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    card_id = body["card"]["card_id"]

    again = client.post("/api/decision/cards", json=_body(days[0].isoformat()))
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["card"]["card_id"] == card_id

    got = client.get(f"/api/decision/cards/{card_id}")
    assert got.status_code == 200
    assert got.json()["verified"] is True


def test_create_card_blocks_on_stale_data(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path)
    stale = (days[-1] + timedelta(days=7)).isoformat()
    r = client.post("/api/decision/cards", json=_body(stale))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["gates"]["decision"] == "STOP_NEW"


def test_override_requires_reason_and_is_recorded(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path)
    stale = (days[-1] + timedelta(days=7)).isoformat()
    no_reason = client.post("/api/decision/cards", json=_body(stale, allow_stop=True))
    assert no_reason.status_code == 400

    ok = client.post("/api/decision/cards",
                     json=_body(stale, allow_stop=True,
                                override_reason="补登 2026-02 历史决策，用于校准回填"))
    assert ok.status_code == 200
    assert ok.json()["card"]["override"]["reason"].startswith("补登")


def test_invalid_pick_is_400(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path)
    r = client.post("/api/decision/cards", json={
        "as_of": days[0].isoformat(),
        "picks": [{"symbol": "600519", "limit_price": 100.0, "p_win": 0.6}],
    })
    assert r.status_code == 400
    assert "symbol" in r.json()["detail"]


def test_settle_then_score(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path)
    client.post("/api/decision/cards", json=_body(days[0].isoformat()))
    r = client.post("/api/decision/settle")
    assert r.status_code == 200
    assert r.json()["written"] == 1

    # 幂等：再跑一次不重复写
    assert client.post("/api/decision/settle").json()["written"] == 0

    score = client.get("/api/decision/score").json()["score"]
    assert score["n"] == 1
    assert score["verdict"] == "INSUFFICIENT"
    assert score["hit_rate"] == 1.0

    rows = client.get("/api/decision/settlements").json()
    assert rows["total"] == 1
    assert rows["settlements"][0]["exit"]["ret"] == pytest.approx(0.10, abs=1e-6)


def test_unknown_card_404(client: TestClient):
    assert client.get("/api/decision/cards/dc-2026-01-05-0000000000").status_code == 404
    assert client.get("/api/decision/cards/../../etc/passwd").status_code in (404, 405)


# ---------------------------------------------------------------------------
# auto_pwin：失败关闭与出处登记
# ---------------------------------------------------------------------------


def _seed_settlements(tmp_path: Path, *, days: int = 40, strategy: str = "mom",
                      wins: int | list[int] = 14, total: int = 20) -> None:
    """直接写结算流水。pwin 只读 rows，不要求行情在场。

    注意：行的出场日与决策日同天是测试捷径。**补登卡的 as_of 必须晚于最后
    一条结算的 as_of**——P1 修复后 suggest() 只看见出场日 < as_of 的结算
    （`api/decision.py` `_exit_visible`，回归测试见
    `test_auto_pwin_backfill_ignores_settlements_after_as_of`）。
    """
    from app.decision import ledger

    rows = []
    for i in range(days):
        d = (START + timedelta(days=i)).isoformat()
        w = wins[i] if isinstance(wins, list) else wins
        for k in range(total):
            y = 1 if k < w else 0
            rows.append({
                "kind": "TSP-DECISION-SETTLEMENT-v1",
                "key": f"dc-{d}-x:{k}", "card_id": f"dc-{d}-x", "as_of": d,
                "symbol": f"{k:06d}.SZ", "strategy_id": strategy,
                "account_id": "default", "horizon_sessions": 5, "p_win": 0.7,
                "limit_price": 100.0, "outcome": y, "outcome_med": y,
                "excess": 0.01, "excess_med": 0.01,
                "fill": {"status": "FILLED", "date": d},
                "exit": {"date": d, "ret": 0.01},
                "brier": 0.09, "logloss": 0.3,
            })
    ledger.append_settlements(tmp_path, rows)


def _body_no_pwin(as_of: str, **kw) -> dict:
    return {
        "as_of": as_of,
        "picks": [{"symbol": SYM, "limit_price": 100.0}],
        "horizon_sessions": 5,
        **kw,
    }


def _body_no_pwin_abs(as_of: str, **kw) -> dict:
    """abs 口径的 auto_pwin 请求体。

    这里必须用 abs：median 口径的 `outcome_med` 依赖全市场中位数
    （`settle._median_ret` 样本 <100 只即 None），单标的种子数据下
    `_select` 会把所有行丢掉，测试会因空样本 409 而**假通过**——
    可见性回归必须让样本真的进入 suggest() 才算数（与评审探针同口径）。
    """
    return _body_no_pwin(as_of, pwin_basis="abs", **kw)


def test_missing_pwin_without_auto_is_400(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path)
    r = client.post("/api/decision/cards", json=_body_no_pwin(days[0].isoformat()))
    assert r.status_code == 400
    assert SYM in r.json()["detail"]["message"]


def test_auto_pwin_fails_closed_without_history(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path)
    r = client.post("/api/decision/cards",
                    json=_body_no_pwin(days[0].isoformat(), auto_pwin=True))
    assert r.status_code == 409
    assert "没有可用的历史结算样本" in r.json()["detail"]["message"]


def test_auto_pwin_labels_unreliable_ledger_instead_of_refusing(client: TestClient,
                                                                tmp_path: Path):
    """前 20 天全胜、后 20 天全负 ⇒ 出卡，但卡上必须写清「证据不可靠」。

    旧行为是 409。那是永久停机：不出卡就没有新结算，没有新结算就永远
    unreliable。gate 的对象改成「有没有证据」之后，这一档降级为标签。
    """
    days = _seed(tmp_path, n=50)
    _seed_settlements(tmp_path, days=40, wins=[20] * 20 + [0] * 20)
    # 补登日晚于全部结算（最后一条 as_of=START+39）：可见性过滤下
    # suggest() 只看见出场日 < as_of 的结算，在 days[0] 补登会正确地 409。
    r = client.post("/api/decision/cards",
                    json=_body_no_pwin(days[46].isoformat(), auto_pwin=True,
                                       strategy_id="mom"))
    assert r.status_code == 200, r.text
    body = r.json()
    pwin = body["pwin"]
    assert pwin["reliable"] is False
    assert pwin["verdict"] == "UNRELIABLE"
    assert pwin["significance"] == "UNKNOWN"
    # 第二个检验同样要有可辨识性读数：40 天 / 20 日持有期 ⇒ n_eff=1 ⇒ UNKNOWN。
    # `verdict_incremental` 在这里是 NOT_TESTABLE（窗口盖住全部已可见日），
    # 两者都是「没测」，不是「没通过」。
    assert pwin["significance_incremental"] == "UNKNOWN"
    # 出处随卡封存：`source=ledger` 之外还要能读出「先验占多少」。
    sealed = body["card"]["forecast"]["pwin"]
    assert sealed["significance_incremental"] == pwin["significance_incremental"]
    assert sealed["prior_weight_effective"] == pytest.approx(
        sealed["kappa"] / (sealed["n_eff"] + sealed["kappa"]), abs=1e-6)
    assert sealed["prior_weight_effective"] > sealed["prior_weight"]


def test_legacy_require_reliable_false_maps_to_allow_prior(client: TestClient,
                                                           tmp_path: Path):
    """旧字段的唯一映射方向：`false` ⇒ `allow_prior=true`。

    这条不能删：客户端升级前只传旧字段，若映射反了就是静默反转行为。
    """
    days = _seed(tmp_path, n=50)
    _seed_settlements(tmp_path, days=40, wins=[20] * 20 + [0] * 20)
    # 同上：补登日晚于全部结算，800 条样本对补登卡全部可见。
    r = client.post("/api/decision/cards",
                    json=_body_no_pwin(days[46].isoformat(), auto_pwin=True,
                                       strategy_id="mom", require_reliable=False))
    assert r.status_code == 200, r.text
    assert r.json()["pwin"]["reliable"] is False
    assert r.json()["pwin"]["verdict"] == "UNRELIABLE"


def test_allow_prior_true_builds_card_on_cold_start(client: TestClient, tmp_path: Path):
    """没有证据时，唯一出路是**显式声明**用先验——声明一句就能满足，不是死锁。"""
    days = _seed(tmp_path)
    r = client.post("/api/decision/cards",
                    json=_body_no_pwin(days[0].isoformat(), auto_pwin=True,
                                       allow_prior=True))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pwin"]["source"] == "prior"
    assert body["pwin"]["prior_weight"] == 1.0
    assert body["pwin"]["prior_weight_effective"] == 1.0
    assert body["card"]["picks"][0]["p_win"] == 0.5


def test_allow_prior_false_conflicts_with_legacy_false(client: TestClient, tmp_path: Path):
    """两个开关互相矛盾时 400，不静默择一。"""
    days = _seed(tmp_path)
    r = client.post("/api/decision/cards",
                    json=_body_no_pwin(days[0].isoformat(), auto_pwin=True,
                                       allow_prior=False, require_reliable=False))
    assert r.status_code == 400
    assert "相互矛盾" in r.json()["detail"]["message"]


def test_auto_pwin_with_all_p_win_given_claims_no_ledger_provenance(
        client: TestClient, tmp_path: Path):
    """没有要补的 pick，就不该写一块「账本算的」出处——出处不能撒谎。"""
    days = _seed(tmp_path)
    r = client.post("/api/decision/cards",
                    json=_body(days[0].isoformat(), auto_pwin=True))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pwin"] is None
    assert "pwin" not in (body["card"]["forecast"] or {})


def test_auto_pwin_fills_shrunk_value_and_records_source(client: TestClient, tmp_path: Path):
    days = _seed(tmp_path, n=50)
    _seed_settlements(tmp_path, days=40, wins=14)      # 40 天 × 20 = 800，命中 560
    # 补登日晚于全部结算：可见性过滤不误伤可见样本（n 仍为 800）。
    r = client.post("/api/decision/cards",
                    json=_body_no_pwin(days[46].isoformat(), auto_pwin=True,
                                       strategy_id="mom"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pwin"]["source"] == "ledger"
    assert body["pwin"]["reliable"] is True
    assert body["pwin"]["n"] == 800
    # 收缩：(560 + 20*0.5) / (800 + 20)
    assert body["card"]["picks"][0]["p_win"] == pytest.approx(570 / 820, abs=1e-5)
    # 出处进 forecast ⇒ 进承诺哈希，换 p_win 就是另一张卡
    assert body["card"]["forecast"]["pwin"]["n"] == 800
    assert body["card"]["sealed"]["sha256"]


# ---------------------------------------------------------------------------
# auto_pwin：决策时点可见性（P1 look-ahead 回归，2026-11-06 独立评审）
# ---------------------------------------------------------------------------


def _seed_cards_and_settle(client: TestClient, days: list[date],
                           n: int = 8) -> None:
    """建 n 张显式 p_win 的卡并结算，制造"决策日之后才产生"的结算历史。

    出场日 = as_of + 1 + horizon（`docs/decision-ledger.md` §9.8）⇒ 第 i 张
    卡的出场日是 days[i + 6]。对 as_of=days[0] 的补登卡而言，这些结算全部
    是未来信息。
    """
    for i in range(n):
        r = client.post("/api/decision/cards", json=_body(days[i].isoformat()))
        assert r.status_code == 200, r.text
    s = client.post("/api/decision/settle")
    assert s.status_code == 200
    assert s.json()["written"] == n


def test_auto_pwin_backfill_ignores_settlements_after_as_of(client, tmp_path):
    """补登历史卡时，suggest() 只许看见出场日 < as_of 的结算。

    复现 P1（评审探针 `probe_pwin_backfill_leak.py` 的 pytest 化）：修复前
    全部 8 条结算（出场日 days[6..13]，均晚于 as_of=days[0]）都会进
    suggest()，请求 200 且 p_win 用未来样本算出。修复后必须 409：决策时点
    可见样本为 0，没有证据，fail-closed。
    """
    days = _seed(tmp_path)
    _seed_cards_and_settle(client, days)

    r = client.post("/api/decision/cards",
                    json=_body_no_pwin_abs(days[0].isoformat(), auto_pwin=True,
                                           allow_prior=False))
    assert r.status_code == 409, r.text
    assert "没有可用的历史结算样本" in r.json()["detail"]["message"]


def test_auto_pwin_visibility_boundary_is_strict(client, tmp_path):
    """边界：出场日 == as_of 的结算同样不可见（§9.8 是 exit.date < D，非 <=）。

    第 0 张卡的出场日正是 days[6]：补登 as_of=days[6] 时它"当天出场"，
    按口径仍不可见 ⇒ 可见样本 0 ⇒ 409。
    """
    days = _seed(tmp_path)
    _seed_cards_and_settle(client, days)

    r = client.post("/api/decision/cards",
                    json=_body_no_pwin_abs(days[6].isoformat(), auto_pwin=True,
                                           allow_prior=False))
    assert r.status_code == 409, r.text


def test_auto_pwin_uses_settlements_visible_at_as_of(client, tmp_path):
    """反向断言：可见样本必须仍进 suggest()——过滤不能把历史一并滤掉。

    days[14] 晚于全部出场日（days[6..13]）⇒ 8 条全部可见 ⇒ 正常出卡，
    `source=ledger`、n=8。这条防的是"一刀切不过滤"的反向错修。
    """
    days = _seed(tmp_path)
    _seed_cards_and_settle(client, days)

    r = client.post("/api/decision/cards",
                    json=_body_no_pwin_abs(days[14].isoformat(), auto_pwin=True,
                                           allow_prior=False))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pwin"]["source"] == "ledger"
    assert body["pwin"]["n"] == 8


def test_pwin_endpoint_empty_is_insufficient(client: TestClient):
    body = client.get("/api/decision/pwin?strategy_id=mom").json()
    assert body["p_win"] is None
    assert body["verdict"] == "INSUFFICIENT"


def test_score_basis_rejects_unknown(client: TestClient):
    assert client.get("/api/decision/score?basis=nope").status_code == 400
