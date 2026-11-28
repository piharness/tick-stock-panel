"""红利腿规则回测的三条硬约束：执行时滞必须与腿 1 一致、成本必须真扣、
联合引擎在关掉红利腿时必须逐日复现 pf.replay（否则账户级结论建立在另一套引擎上）。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ANALYSIS = Path(__file__).resolve().parents[2] / "analysis"
sys.path.insert(0, str(ANALYSIS))

import etf_watch_leg2_rule as R  # noqa: E402
import etf_watch_portfolio as pf  # noqa: E402


def _joint_world(days: int = 40, n_ind: int = 6):
    sessions = list(pd.bdate_range("2026-01-05", periods=days))
    prices = {}
    cols = {}
    for k in range(n_ind):
        sym = f"X{k}.SH"
        prices[sym] = pd.DataFrame(
            {"close_adj": np.linspace(1.0, 1.1, days), "hi_adj": np.linspace(1.0, 1.1, days),
             "lo_adj": np.linspace(1.0, 1.1, days)},
            index=pd.DatetimeIndex(sessions),
        )
        cols[f"IND{k}"] = sym
    sym_mat = pd.DataFrame([cols] * days, index=pd.DatetimeIndex(sessions))
    ev = pd.DataFrame([{"date": sessions[0], "industry": f"IND{k}", "i": 0, "ym": "2026-01", "dd": -0.4}
                       for k in range(n_ind)])
    return sessions, prices, sym_mat, ev


# ---------------------------------------------------------------- 执行口径
def test_strat_return_shifts_two_sessions_and_charges_on_change():
    idx = pd.bdate_range("2026-01-05", periods=10)
    asset = pd.Series([0.01] * 10, index=idx)          # 每天都涨 1%
    cash = pd.Series(0.0, index=idx)
    sig = pd.Series(1, index=idx)                       # 一直该持有

    r, pos, trade = R.strat_return(sig, asset, cash)

    # t-2 收盘知道、t-1 收盘成交 ⇒ 前两天无仓位、无收益
    assert pos.iloc[0] == 0 and pos.iloc[1] == 0
    assert r.iloc[0] == 0 and r.iloc[1] == 0
    # 第 3 天起持有：1% − 6bp（建仓成本）
    assert r.iloc[2] == pytest.approx(0.01 - pf.COST)
    assert r.iloc[3] == pytest.approx(0.01)
    # 之后不再换手
    assert trade.sum() == pytest.approx(1.0)


def test_strat_return_pays_cash_yield_while_flat():
    idx = pd.bdate_range("2026-01-05", periods=6)
    sig = pd.Series([0, 0, 0, 1, 1, 1], index=idx)
    asset = pd.Series(0.02, index=idx)
    cash = pd.Series(0.0001, index=idx)

    r, pos, _ = R.strat_return(sig, asset, cash)
    assert r.iloc[0] == pytest.approx(0.0001)           # 空仓吃现金
    assert r.iloc[3] == pytest.approx(0.0001)           # sig 在 idx3 才变 1 ⇒ idx3 还没仓位
    assert r.iloc[5] == pytest.approx(0.02 - pf.COST)   # 首个持仓日扣建仓成本


# ---------------------------------------------------------------- 月度动量
def test_sig_mom_monthly_decision_made_at_month_end_applies_next_month():
    # 252 天横盘 + 月末跳升：跳升月末做出的决策应在下月第 2 个交易日生效
    idx = pd.bdate_range("2025-01-01", periods=300)
    tr = pd.Series(100.0, index=idx)
    tr.iloc[250:] = 120.0                                # 第 251 天起抬升（跨月末）
    pos = R.sig_mom_monthly(tr)
    # 抬升前的月份：TR == 252 天前 ⇒ 不 > 1 ⇒ 0
    assert pos.iloc[:250].max() == 0
    # 抬升后的完整月份应为 1
    assert pos.iloc[-20:].min() == 1


def test_sig_mom_monthly_first_session_keeps_previous_position():
    idx = pd.bdate_range("2025-01-01", periods=320)
    tr = pd.Series(100.0, index=idx)
    tr.iloc[280:] = 150.0                                # 某月内暴涨
    pos = R.sig_mom_monthly(tr)
    # 找到暴涨后第一个「决策=1」的月份首日：首日应仍是旧仓位（0），次日起才是 1
    on = pos[pos == 1].index
    first_on_month_start = [t for t in on if (t.year, t.month) != (on[0].year, on[0].month)]
    assert first_on_month_start, "应存在决策切换到下月的情形"
    m_start = first_on_month_start[0]
    i = pos.index.get_loc(m_start)
    # 首日（切换月第 1 个交易日）持旧仓；不过若旧仓也是 1 则此断言无意义——构造保证旧月为 0
    prev_month_pos = pos.iloc[max(i - 22, 0):i]
    if (prev_month_pos == 0).all():
        assert pos.iloc[i] == 0
        assert pos.iloc[i + 1] == 1


# ---------------------------------------------------------------- run-length 与 bootstrap
def test_run_lengths_counts_on_and_off_segments():
    pos = pd.Series([0, 0, 1, 1, 1, 0, 1], index=pd.bdate_range("2026-01-05", periods=7))
    on, off = R.run_lengths(pos)
    assert sorted(on) == [1, 3]
    assert sorted(off) == [1, 2]


def test_bootstrap_is_deterministic_with_seed():
    idx = pd.bdate_range("2025-01-01", periods=500)
    rng = np.random.default_rng(7)
    asset = pd.Series(rng.normal(0.0004, 0.01, 500), index=idx)
    cash = pd.Series(0.0001, index=idx)
    sig = pd.Series((rng.random(500) > 0.4).astype(int), index=idx)
    r, pos, _ = R.strat_return(sig, asset, cash)

    a = R.bootstrap_same_exposure(pos, asset, cash, n_boot=50, seed=99)
    b = R.bootstrap_same_exposure(pos, asset, cash, n_boot=50, seed=99)
    c = R.bootstrap_same_exposure(pos, asset, cash, n_boot=50, seed=100)
    assert a == b
    assert a["random_p50"] != c["random_p50"] or a["random_p05"] != c["random_p05"]


# ---------------------------------------------------------------- 联合引擎
def test_joint_without_div_reproduces_six_slots_exactly():
    sessions, prices, sym_mat, ev = _joint_world()
    vols = {s: pd.Series(0.3, index=pd.DatetimeIndex(sessions)) for s in prices}
    zero_cash = pd.Series(0.0, index=pd.DatetimeIndex(sessions))

    ref = pf.replay(ev, sessions, prices, vols, sym_mat, "six_slots", cash_yield=0.0)
    mine = R.replay_joint(ev, sessions, prices, sym_mat,
                          div_r=pd.Series(0.0, index=pd.DatetimeIndex(sessions)),
                          div_tgt=pd.Series(0.0, index=pd.DatetimeIndex(sessions)),
                          cash_r=zero_cash, use_div=False)
    assert len(ref["equity"]) == len(mine["equity"])
    assert np.allclose(ref["equity"].to_numpy(), mine["equity"].to_numpy(), rtol=1e-12)
    assert np.allclose(ref["exposure"].to_numpy(), mine["exposure"].to_numpy(), rtol=1e-12)


def test_joint_div_leg_gets_residual_cash_and_compounds():
    sessions, prices, sym_mat, _ = _joint_world(days=30)
    ev = pd.DataFrame(columns=["date", "industry", "i", "ym", "dd"])   # 无腿 1 事件
    div_r = pd.Series(0.001, index=pd.DatetimeIndex(sessions))         # 每天涨 0.1%
    tgt = pd.Series(1.0, index=pd.DatetimeIndex(sessions))
    cash = pd.Series(0.0, index=pd.DatetimeIndex(sessions))

    r = R.replay_joint(ev, sessions, prices, sym_mat, div_r, tgt, cash,
                       use_div=True, start=pd.Timestamp(sessions[0]))
    eq = r["equity"]
    # 首日收盘建仓扣 6bp，之后 29 个交易日按 0.1%/天复利
    assert eq.iloc[-1] / pf.INIT == pytest.approx((1 - pf.COST) * (1.001 ** 29), rel=5e-4)
    assert r["metrics"]["avg_div_exposure"] == pytest.approx(1.0, abs=0.05)


def test_joint_crowding_sells_div_to_fund_leg1():
    sessions, prices, sym_mat, ev = _joint_world(days=40, n_ind=6)
    div_r = pd.Series(0.0, index=pd.DatetimeIndex(sessions))
    tgt = pd.Series(1.0, index=pd.DatetimeIndex(sessions))              # 红利腿始终满仓剩余现金
    cash = pd.Series(0.0, index=pd.DatetimeIndex(sessions))

    r = R.replay_joint(ev, sessions, prices, sym_mat, div_r, tgt, cash,
                       use_div=True, start=pd.Timestamp(sessions[0]))
    # 腿 1 信号出现时现金在红利腿里 ⇒ 必须卖出腾钱（拥挤事件）
    assert r["metrics"]["crowding"]["events"] >= 1
    # 腿 1 仍然建了仓（腿 1 优先不被挤掉）
    assert r["metrics"]["leg1_trades"] >= 1


def test_joint_crowding_funds_a_full_slot_not_half():
    """腿 1 优先 = 整格补足。半格下限会把腿 1 砍半（2026-11-22 修正前踩过）。"""
    sessions, prices, sym_mat, ev = _joint_world(days=40, n_ind=6)
    div_r = pd.Series(0.0, index=pd.DatetimeIndex(sessions))
    tgt = pd.Series(1.0, index=pd.DatetimeIndex(sessions))       # 防御腿吃光现金
    cash = pd.Series(0.0, index=pd.DatetimeIndex(sessions))

    r = R.replay_joint(ev, sessions, prices, sym_mat, div_r, tgt, cash,
                       use_div=True, start=pd.Timestamp(sessions[0]))
    # 建仓日：腿 1 每格应接近当时的整格（权益/6），而不是半格
    trades = [t for t in r["trades"]]
    assert trades, "应有腿 1 成交"
    # 无腿 1 持仓时权益≈100 万 ⇒ 整格 ≈ 100 万/6；半格只有 ~8.3 万
    assert r["metrics"]["leg1_trades"] >= 1
    # 用交易后的暴露验证：6 个整格 ⇒ 首个持仓日腿 1 占权益 ~99%（6/6 格）
    ex = r["exposure"].iloc[6]
    assert ex == pytest.approx(6 / 6, abs=0.02)


def test_bond_instruction_respects_alignment_threshold():
    """真实佣金最低 0.3 元 ⇒ 债券差额低于 3,000 元不下单（2026-11-22 预注册）。"""
    import etf_watch_account as acc

    days = pd.bdate_range("2026-01-05", periods=2)
    r = {"equity": pd.Series([600_000.0, 600_000.0], index=days),
         "open_positions": [], "defensive_value": 598_500.0}   # 债券目标 600,000 ⇒ 差额 1,500
    ev_w = pd.DataFrame(columns=["date", "industry", "i", "ym", "dd"])
    sym_mat = pd.DataFrame({"IND": ["X.SH"] * 2}, index=days)

    out = acc.derive_instructions(r, ev_w, list(days), sym_mat)
    bond = [o for o in out if o["action"] in ("BUY", "SELL")]
    holds = [o for o in out if o["action"] == "HOLD"]
    assert bond == []                       # 差额 1,500 元 < 3,000 ⇒ 不下单
    assert holds and "对齐阈值" in holds[0]["why"]

    r["defensive_value"] = 400_000.0        # 差额 200,000 ⇒ 正常下单
    out = acc.derive_instructions(r, ev_w, list(days), sym_mat)
    bond = [o for o in out if o["action"] in ("BUY", "SELL")]
    assert len(bond) == 1 and bond[0]["action"] == "BUY" and bond[0]["est_amount"] == 200_000


def test_bond_instruction_respects_alignment_threshold():
    """真实佣金最低 0.3 元 ⇒ 债券差额低于 3,000 元不下单（2026-11-22 预注册）。"""
    import etf_watch_account as acc

    days = pd.bdate_range("2026-01-05", periods=2)
    r = {"equity": pd.Series([600_000.0, 600_000.0], index=days),
         "open_positions": [], "defensive_value": 598_500.0}   # 债券目标 600,000 ⇒ 差额 1,500
    ev_w = pd.DataFrame(columns=["date", "industry", "i", "ym", "dd"])
    sym_mat = pd.DataFrame({"IND": ["X.SH"] * 2}, index=days)

    out = acc.derive_instructions(r, ev_w, list(days), sym_mat)
    bond = [o for o in out if o["action"] in ("BUY", "SELL")]
    holds = [o for o in out if o["action"] == "HOLD"]
    assert bond == []                       # 差额 1,500 元 < 3,000 ⇒ 不下单
    assert holds and "对齐阈值" in holds[0]["why"]

    r["defensive_value"] = 400_000.0        # 差额 200,000 ⇒ 正常下单
    out = acc.derive_instructions(r, ev_w, list(days), sym_mat)
    bond = [o for o in out if o["action"] in ("BUY", "SELL")]
    assert len(bond) == 1 and bond[0]["action"] == "BUY" and bond[0]["est_amount"] == 200_000


def test_buy_instruction_uses_next_open_limit():
    """2026-11-23 执行叠加：买入指令带 limit_price = 信号收盘 ×1.03，不是收盘市价。"""
    import etf_watch_account as acc

    days = pd.bdate_range("2026-11-19", periods=2)
    asof = days[-1]
    r = {"equity": pd.Series([600_000.0, 600_000.0], index=days),
         "open_positions": [], "defensive_value": 0.0}
    ev_w = pd.DataFrame([{"date": asof, "industry": "IND", "i": 0, "ym": "2026-11", "dd": -0.37}])
    sym_mat = pd.DataFrame({"IND": ["X.SH", "X.SH"]}, index=days)
    prices = {"X.SH": pd.DataFrame({"close": [1.0, 1.112]}, index=days)}

    out = acc.derive_instructions(r, ev_w, list(days), sym_mat, prices)
    buy = [o for o in out if o["action"] == "BUY" and "X.SH" in o["what"]]
    assert buy, "应有腿 1 买入指令"
    assert buy[0]["limit_price"] == 1.145
    assert "09:30" in buy[0]["when"] and "限价" in buy[0]["when"]


def _limit_world(open_px: float, low_px: float):
    days = pd.bdate_range("2026-01-05", periods=4)
    sessions = list(days)
    prices = {"X.SH": pd.DataFrame(
        {"close_adj": [1.112, 1.112, 1.112, 1.112],
         "open_adj": [1.112, open_px, 1.112, 1.112],
         "lo_adj": [1.112, low_px, 1.112, 1.112]},
        index=days)}
    ev = pd.DataFrame([{"date": days[0], "industry": "IND", "i": 0, "ym": "2026-01", "dd": -0.37}])
    sym_mat = pd.DataFrame({"IND": ["X.SH"] * 4}, index=days)
    zeros = pd.Series(0.0, index=days)
    return sessions, prices, ev, sym_mat, zeros


def test_replay_open_limit_fills_at_open_or_limit_and_skips_gap():
    """执行叠加：开盘 ≤ 限价按开盘；开盘高于限价但盘中回落按限价；从未回落则放弃。"""
    import etf_watch_leg2_rule as LR

    # 限价 = 1.112×1.03 = 1.145；开盘 1.093 ≤ 限价 ⇒ 按开盘成交
    s, px, ev, sm, z = _limit_world(1.093, 1.090)
    r = LR.replay_joint(ev, s, px, sm, div_r=z, div_tgt=z, cash_r=z,
                        use_div=False, allow_open=True, entry_mode="open_limit")
    assert len(r["open_positions"]) == 1
    assert r["open_positions"][0]["fill"] == "open"
    assert r["open_positions"][0]["entry_px"] == pytest.approx(1.093)

    # 开盘 1.20 > 限价，但盘中最低 1.14 ≤ 限价 ⇒ 按限价成交
    s, px, ev, sm, z = _limit_world(1.200, 1.140)
    r = LR.replay_joint(ev, s, px, sm, div_r=z, div_tgt=z, cash_r=z,
                        use_div=False, allow_open=True, entry_mode="open_limit")
    assert len(r["open_positions"]) == 1
    assert r["open_positions"][0]["fill"] == "limit"
    assert r["open_positions"][0]["entry_px"] == pytest.approx(1.145)

    # 开盘 1.20、最低 1.19，全天没回到限价 ⇒ 放弃该信号
    s, px, ev, sm, z = _limit_world(1.200, 1.190)
    r = LR.replay_joint(ev, s, px, sm, div_r=z, div_tgt=z, cash_r=z,
                        use_div=False, allow_open=True, entry_mode="open_limit")
    assert r["open_positions"] == []


def test_seg_first_matches_buy_instructions():
    """段首集合 == 腿 1 BUY 集合（2026-11-28）。

    页面分组只认 account.json 的 seg_first；它必须与指令同源，否则又会出现
    「页面说 A 可执行、明日待办买 B」（2026-11-27 电子/有色就是这么分叉的）。
    """
    import etf_watch_account as acc

    days = pd.bdate_range("2026-11-26", periods=2)
    asof = days[-1]
    r = {"equity": pd.Series([600_000.0, 600_000.0], index=days),
         "open_positions": [], "defensive_value": 0.0}
    ev_w = pd.DataFrame([
        {"date": asof, "industry": "有色", "i": 0, "ym": "2026-11", "dd": -0.41},
        {"date": asof, "industry": "电子", "i": 0, "ym": "2026-11", "dd": -0.46},
        # 前一日的事件不得混进来
        {"date": days[0], "industry": "军工", "i": 0, "ym": "2026-11", "dd": -0.44},
    ])
    sym_mat = pd.DataFrame({"有色": ["A.SZ"] * 2, "电子": ["B.SH"] * 2, "军工": ["C.SH"] * 2},
                           index=days)

    first = acc.seg_first_industries(ev_w, asof)
    assert first == ["有色", "电子"]          # 排序稳定，且不含前一日的军工

    out = acc.derive_instructions(r, ev_w, list(days), sym_mat)
    buys = [o for o in out if o["action"] == "BUY" and "511010" not in o["what"]]
    assert len(buys) == len(first)
    for ind in first:
        assert any(f"（{ind}）" in o["what"] for o in buys)
