"""仓位艺术 —— 规则可执行性 + 那条前视 bug 的回归测试。

重点不是「跑通」，是钉住三件事：
1. `state_age` 是段内序号（1,2,3...），不是整段总长 —— 后者等于提前知道这段会持续多久。
2. 滞回仓位在「满 N 日」之后才离场，且在状态翻出 down 的次日回场。
3. 停牌复牌的多日跌幅不能被算成单日收益（否则等权指数的回撤被放大）。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import position_art as api
from app.services import position_art as pa


def _days(n: int, start: date = date(2016, 1, 4)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _state_frame(seq: list[str]) -> pl.DataFrame:
    return pa.attach_runs(
        pl.DataFrame({"date": _days(len(seq)), "state": seq}).with_columns(
            pl.col("date").cast(pl.Date)
        )
    )


# ── 1. 段内序号 vs 整段总长（前视 bug 回归）──────────────────────────────────
def test_state_age_is_running_count_not_spell_length() -> None:
    seq = ["flat"] * 3 + ["down"] * 12 + ["flat"] * 3
    out = _state_frame(seq)
    ages = out["state_age"].to_list()
    assert ages[3:15] == list(range(1, 13)), "下行段内应是 1..12 的序号"
    assert ages[15] == 1, "状态切换后重新从 1 开始"
    # 前视版本会让整段都是 12 —— 那是这条测试唯一要防的事
    assert max(ages[3:15]) == 12 and ages[3] == 1


def test_len_prev_is_yesterday_age() -> None:
    out = _state_frame(["down"] * 3 + ["flat"])
    assert out["len_prev"].to_list() == [None, 1, 2, 3]
    assert out["state_prev"].to_list() == [None, "down", "down", "down"]


# ── 2. 滞回仓位 ───────────────────────────────────────────────────────────────
def test_weights_exit_after_confirm_days_and_reenter_next_day() -> None:
    # 3 天 flat（热身）+ 12 天下行 + 3 天 flat
    d = _state_frame(["flat"] * 3 + ["down"] * 12 + ["flat"] * 3)
    w = pa.add_weights(d, 10)["w"].to_list()
    # 段首日由「昨日 flat」决定 → 在场；第 1..10 天（索引 3..12）都在场
    assert w[3:13] == [1.0] * 10
    # 第 11 天起离场（此时昨日的 age 已到 10）
    assert w[13] == 0.0 and w[14] == 0.0
    # 翻出 down 的当天仍按昨日 down 处理 → 次日回场
    assert w[15] == 0.0
    assert w[16] == 1.0


def test_weights_confirm_zero_follows_state_daily() -> None:
    d = _state_frame(["flat", "down", "down", "flat"])
    w = pa.add_weights(d, 0)["w"].to_list()
    assert w == [1.0, 1.0, 0.0, 0.0]


def test_weights_reject_negative_confirm() -> None:
    with pytest.raises(ValueError):
        pa.add_weights(_state_frame(["flat", "down"]), -1)


# ── 3. 交易日历相邻 ───────────────────────────────────────────────────────────
def test_suspension_gap_is_not_a_one_day_return() -> None:
    """A 在 d2 缺一行：d1→d3 的 2 日跌幅不能被算成一日收益。"""
    dates = _days(4)
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "A", "B", "B", "B", "B"],
            "date": [dates[0], dates[1], dates[3]] + dates,
            "close": [10.0, 10.0, 8.0, 10.0, 10.0, 10.0, 10.0],
        }
    )
    daily = pa.compute_state_frame(panel)
    # d3 只有 B 的有效收益（0%），A 的 −20% 被丢弃
    d3 = daily.filter(pl.col("date") == dates[3])
    assert d3["n"].to_list() == [1]
    assert abs(float(d3["ew_ret"][0])) < 1e-9, "复牌跳变不得计入等权日收益"


def test_state_labels_on_monotone_decline() -> None:
    dates = _days(120)
    closes = [100.0 * (0.99**i) for i in range(120)]
    panel = pl.DataFrame({"symbol": ["A"] * 120, "date": dates, "close": closes})
    daily = pa.compute_state_frame(panel)
    assert daily["state"][-1] == "down"
    assert float(daily["ret60"][-1]) < pa.DOWN_CUT


def test_state_labels_on_monotone_rise() -> None:
    dates = _days(120)
    closes = [100.0 * (1.01**i) for i in range(120)]
    panel = pl.DataFrame({"symbol": ["A"] * 120, "date": dates, "close": closes})
    daily = pa.compute_state_frame(panel)
    assert daily["state"][-1] == "up"


# ── 4. 评价与安慰剂 ───────────────────────────────────────────────────────────
def test_evaluate_charges_switch_cost() -> None:
    m = pa.evaluate([0.01, 0.01], [1.0, 0.0], cost=0.002)
    # 进一次、出一次 = 2 次切换；净值 1.008 × 0.998
    assert m.switches == 2
    assert m.exposure_pct == 50.0
    assert m.final_eq == pytest.approx(1.005984, abs=5e-4)


def test_flat_blocks_groups_consecutive_zeros() -> None:
    assert pa.flat_blocks([1, 0, 0, 1, 0]) == [2, 1]


def test_placebo_is_deterministic_and_skips_all_in() -> None:
    rets = [0.001] * 400
    assert pa.placebo_p(rets, [1.0] * 400)["available"] is False
    w = [0.0] * 100 + [1.0] * 300
    a = pa.placebo_p(rets, w, iters=20, seed=3)
    b = pa.placebo_p(rets, w, iters=20, seed=3)
    assert a == b
    assert a["available"] is True


# ── 5. 装配 ───────────────────────────────────────────────────────────────────
def _synthetic_daily(n: int = 700) -> pl.DataFrame:
    """带三次下行的合成等权指数：够长、有 down/flat 两种状态。"""
    rets = [0.001] * n
    for frac, length, drop in ((0.28, 40, -0.012), (0.60, 50, -0.011), (0.85, 40, -0.010)):
        start = int(n * frac)
        for i in range(start, min(n, start + length)):
            rets[i] = drop
    return pa.add_state(
        pl.DataFrame({"date": _days(n), "ew_ret": rets})
        .with_columns(pl.col("date").cast(pl.Date))
        .with_columns((1.0 + pl.col("ew_ret")).cum_prod().alias("ew_idx"))
    )


def test_build_payload_reports_advice_from_yesterday_state() -> None:
    daily = _synthetic_daily()
    out = pa.build_payload(daily, confirm_days=10, stability={"available": True, "pass": False})
    assert out["available"] is True
    today = out["today"]
    assert today["advice"] in {"空仓", "在场"}
    last_state = str(daily["state"][-1])
    last_age = int(daily["state_age"][-1])
    expect_flat = last_state == "down" and last_age >= 10
    assert (today["advice"] == "空仓") is expect_flat
    # 净值序列首点为 1.0，末点等于等权指数末值（同一条收益序列累积）
    assert out["equity"][0]["base"] == pytest.approx(1.0)
    assert out["equity"][-1]["base"] == pytest.approx(float(daily["ew_idx"][-1]), rel=1e-3)


def test_build_payload_never_claims_win_when_rule_never_triggers() -> None:
    """平静序列里规则从不离场 → 净值与基准完全相同 → 不许判成「更好」。"""
    daily = pa.add_state(
        pl.DataFrame({"date": _days(700), "ew_ret": [0.0002] * 700})
        .with_columns(pl.col("date").cast(pl.Date))
        .with_columns((1.0 + pl.col("ew_ret")).cum_prod().alias("ew_idx"))
    )
    out = pa.build_payload(daily, confirm_days=10)
    by_id = {c["id"]: c for c in out["checks"]}
    assert by_id["AC3"]["pass"] is False, "持平不算胜"
    assert by_id["AC5"]["pass"] is False
    assert by_id["AC4"]["pass"] is False, "多重检验未做必须显式报红"


def test_build_payload_refuses_short_history() -> None:
    daily = _synthetic_daily(200)
    out = pa.build_payload(daily, confirm_days=10)
    assert out["available"] is False
    assert "不足" in out["reason"]


# ── 6. API ────────────────────────────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.state.data_dir = tmp_path
    pa.clear_cache()
    return TestClient(app)


def test_api_returns_degraded_payload_without_data(client: TestClient) -> None:
    r = client.get("/api/position-art")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "reason" in body


def test_api_rejects_unknown_confirm_days(client: TestClient) -> None:
    r = client.get("/api/position-art?confirm_days=7")
    assert r.status_code == 400
    assert "confirm_days" in r.json()["detail"]


def test_api_tail_degrades_without_data(client: TestClient) -> None:
    r = client.get("/api/position-art/tail")
    assert r.status_code == 200
    assert r.json()["available"] is False
